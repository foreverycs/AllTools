"""Runtime wiring for plugins: route/static installation + hot reload.

Plugin *discovery* lives in ``core.plugins`` (scanning ``PLUGINS_DIR`` and
loading modules). This module owns the FastAPI-facing half: installing the
discovered routers/statics onto an app and swapping them on hot reload, so
``app.py`` stays thin and the runtime is testable against arbitrary apps.

Routers / statics are installed through a mutable container so hot reload
(admin「插件重载」, or ``PLUGIN_AUTO_RELOAD`` watcher) can swap plugins without
restarting the app.
"""

from __future__ import annotations

import threading
from typing import Any, List, Set


class PluginRuntime:
    """Owns plugin route/static wiring for one FastAPI app."""

    def __init__(self, app) -> None:
        self._app = app
        self._installed_plugin_routes: List[Any] = []
        self._mounted_plugin_static: Set[str] = set()
        # Serialize plugin route swaps: hot reload runs in a background thread
        # while requests match routes concurrently.
        self._lock = threading.RLock()

    @property
    def installed_plugin_routes(self) -> List[Any]:
        """The plugin routes currently installed on the app (identity list)."""
        return self._installed_plugin_routes

    def install_routes(self, app=None) -> None:
        """(Re)install the current plugin routers onto ``app``.

        Previously installed plugin routes are removed by identity, then the
        fresh set is included under a new container router. Safe to call
        repeatedly; ``app`` defaults to the app passed at construction (used
        by tests to install onto a throwaway app).

        The route list is REBUILT as a new list and swapped atomically:
        Starlette iterates ``router.routes`` while dispatching requests, so an
        in-place mutation could race with an in-flight request (stale index /
        mixed routes). Assigning a fresh list object leaves any current reader
        iterating the old list undisturbed.
        """
        target = app if app is not None else self._app
        with self._lock:
            from fastapi import APIRouter

            from core.plugins import get_plugin_discovery

            base = [
                r
                for r in target.router.routes
                if r not in self._installed_plugin_routes
            ]
            routers = get_plugin_discovery().routers
            if routers:
                container = APIRouter()
                for r in routers:
                    container.include_router(r)
                base = base + list(container.routes)
                self._installed_plugin_routes = list(container.routes)
            else:
                self._installed_plugin_routes = []
            target.router.routes = base  # single atomic swap
            target.openapi_schema = None  # force OpenAPI schema regeneration

    def mount_statics(self) -> None:
        """Mount static dirs for plugins not yet mounted (idempotent)."""
        from fastapi.staticfiles import StaticFiles

        from core.plugins import get_plugin_static_mounts

        with self._lock:
            for slug, static_path in get_plugin_static_mounts():
                key = f"/plugins/{slug}/static"
                if key in self._mounted_plugin_static:
                    continue
                self._app.mount(
                    key,
                    StaticFiles(directory=str(static_path)),
                    name=f"plugin-{slug}",
                )
                self._mounted_plugin_static.add(key)

    def reload(self):
        """Re-discover plugins and swap registry, routes, templates and static.

        Called from the admin「插件重载」button and (optionally) the file
        watcher. Returns the new PluginDiscovery. Never raises for
        plugin-level failures.

        The whole reload holds ``self._lock`` so concurrent reloads (admin
        click + file watcher) cannot interleave registry purges / route swaps.
        """
        with self._lock:
            from tools import refresh_plugins_registry

            disc = refresh_plugins_registry()
            self.install_routes(self._app)
            self.mount_statics()
            try:
                from core.health import bust_health_cache

                bust_health_cache()
            except Exception:
                pass
            return disc

    def fingerprint(self):
        """Snapshot of plugin files for the optional auto-reload watcher."""
        from core.plugins import plugins_dir

        root = plugins_dir()
        if not root.is_dir():
            return None
        parts = []
        try:
            for p in root.rglob("*"):
                if "__pycache__" in p.parts:
                    continue
                try:
                    st = p.stat()
                    parts.append(
                        (p.relative_to(root).as_posix(), st.st_mtime_ns, st.st_size)
                    )
                except OSError:
                    continue
        except OSError:
            return None
        return tuple(sorted(parts))


__all__ = ["PluginRuntime"]
