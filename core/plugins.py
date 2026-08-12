"""Discover and load optional tool plugins from ``PLUGINS_DIR``.

A plugin is a directory (``plugins/<name>/``) containing an ``__init__.py``
that exposes:

- ``TOOL`` — a dict with the same shape as ``tools.TOOL_REGISTRY`` entries
  (``slug`` is required; the rest default sensibly)
- ``router`` — a FastAPI ``APIRouter`` mounted under ``/tools/<slug>``
- optional ``PLUGIN_VERSION`` — a display version string

Optional sibling folders:
- ``templates/`` — extra Jinja2 templates (resolved after the builtin dir)
- ``static/`` — plugin assets, mounted at ``/plugins/<slug>/static``

Plugins are loaded once at startup when ``tools`` builds its registry. A
plugin that fails to import (missing dependency, syntax error, invalid
manifest, slug conflict) is marked unavailable and logged — it never prevents
the app from starting.

Trust model: a plugin is arbitrary Python running in the same process with the
same privileges as the app (SQLite, filesystem, LibreOffice). Only place
trusted code in ``PLUGINS_DIR``.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("toolkit.plugins")

BASE_DIR = Path(__file__).resolve().parent.parent
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Manifest defaults applied to every plugin (all overridable by the plugin).
# ``name`` / ``route`` are intentionally absent so their fallbacks (package
# directory name / ``/tools/<slug>``) apply when a plugin omits them.
# ``order`` is a display-order hint: plugins sort by it (default 999) so the
# homepage / category grids keep a curated order even though discovery scans
# directories alphabetically.
_TOOL_DEFAULTS: Dict[str, Any] = {
    "name_en": "",
    "category": "text",
    "description": "",
    "icon": "🧩",
    "badge": "",
    "features": [],
    "cta": "打开工具",
    "accent": "indigo",
    "featured": False,
    "order": 999,
}


@dataclass
class PluginStatus:
    """Load result for one plugin (shown on the admin system page)."""

    name: str
    slug: str = ""
    version: str = ""
    loaded: bool = False
    error: str = ""


@dataclass
class PluginDiscovery:
    """Aggregated result of scanning ``PLUGINS_DIR``."""

    entries: List[Dict[str, Any]] = field(default_factory=list)
    routers: List[Any] = field(default_factory=list)
    statuses: List[PluginStatus] = field(default_factory=list)
    template_dirs: List[Path] = field(default_factory=list)
    static_mounts: List[Tuple[str, Path]] = field(default_factory=list)


def plugins_dir() -> Path:
    """Absolute plugin root (env ``PLUGINS_DIR`` or project ``plugins/``)."""
    configured = (os.environ.get("PLUGINS_DIR") or "").strip()
    return Path(configured) if configured else (BASE_DIR / "plugins")


def _candidate_dirs(base: Path) -> List[Path]:
    if not base.is_dir():
        return []
    try:
        return sorted(
            p for p in base.iterdir() if p.is_dir() and (p / "__init__.py").is_file()
        )
    except OSError:
        logger.error("plugin directory unreadable: %s", base, exc_info=True)
        return []


_discovery: Optional[PluginDiscovery] = None


def get_plugin_discovery() -> PluginDiscovery:
    """Return the discovery result from startup (or an empty one)."""
    return _discovery if _discovery is not None else PluginDiscovery()


def get_plugin_statuses() -> List[PluginStatus]:
    """Plugin load statuses for the admin system page."""
    return get_plugin_discovery().statuses


def get_plugin_static_mounts() -> List[Tuple[str, Path]]:
    """``(slug, static_dir)`` pairs for mounting plugin assets."""
    return get_plugin_discovery().static_mounts


def _load_plugin(name: str, base: Path) -> Any:
    """Import a plugin package by name from ``base``.

    The parent ``plugins`` package's search path is extended with ``base`` so
    a custom ``PLUGINS_DIR`` (or a test temp dir) resolves even though the
    package lives at the project root. ``importlib`` tolerates hyphenated
    directory names that the ``import`` statement would reject.
    """
    parent = sys.modules.get("plugins")
    if parent is None:
        import plugins  # noqa: F401  (ensure the parent package is importable)

        parent = sys.modules.get("plugins")
    base_str = str(base)
    if base_str not in parent.__path__:
        parent.__path__ = [base_str] + list(parent.__path__)
    return importlib.import_module(f"plugins.{name}")


def _purge_plugin_modules(names: List[str]) -> None:
    """Drop previously imported plugin modules so a hot reload re-executes them."""
    for name in names:
        prefix = f"plugins.{name}"
        for modname in [
            m for m in list(sys.modules) if m == prefix or m.startswith(prefix + ".")
        ]:
            del sys.modules[modname]
    if names:
        importlib.invalidate_caches()


def _purge_bytecode(pkg_dirs) -> None:
    """Delete ``__pycache__`` for plugin dirs.

    Required for reliable hot reload: an in-place edit that keeps the file
    size identical (e.g. only a digit changes) can leave a stale ``.pyc`` that
    Python reuses when the filesystem timestamp tick does not change, so a
    re-import would serve the old bytecode.
    """
    for d in pkg_dirs:
        pycache = d / "__pycache__"
        if pycache.is_dir():
            shutil.rmtree(pycache, ignore_errors=True)


def discover_plugins(
    reserved_slugs: Optional[set] = None,
    base: Optional[Path] = None,
    force: bool = False,
) -> PluginDiscovery:
    """Scan ``PLUGINS_DIR`` and load every valid plugin.

    ``reserved_slugs`` are builtin (or previously loaded) slugs that plugins
    must not shadow; colliding plugins are skipped with a logged error. When
    ``base`` is omitted the real ``PLUGINS_DIR`` is used and the result is
    cached for ``get_plugin_*`` consumers.

    ``force=True`` purges previously imported plugin modules (including their
    submodules) from ``sys.modules`` first, so edited plugin code is re-executed
    on the next request — the hot-reload path.
    """
    out = PluginDiscovery()
    global _discovery  # read in the force-purge path, written when base is None
    reserved = set(reserved_slugs or ())
    used: set = set()
    scan_base = base if base is not None else plugins_dir()

    if force:
        pkg_dirs = _candidate_dirs(scan_base)
        prev_names = (
            [s.name for s in _discovery.statuses] if _discovery is not None else []
        )
        purge_names = sorted(set(prev_names) | {p.name for p in pkg_dirs})
        _purge_plugin_modules(purge_names)
        _purge_bytecode(pkg_dirs)

    for pkg_dir in _candidate_dirs(scan_base):
        name = pkg_dir.name
        st = PluginStatus(name=name)
        try:
            module = _load_plugin(name, scan_base)
        except Exception as exc:
            st.error = f"import failed: {type(exc).__name__}: {exc}"
            logger.error("plugin import failed name=%s error=%s", name, st.error)
            out.statuses.append(st)
            continue

        tool = getattr(module, "TOOL", None)
        if not isinstance(tool, dict):
            st.error = "missing TOOL manifest (dict required)"
            logger.error("plugin %s: %s", name, st.error)
            out.statuses.append(st)
            continue
        slug = str(tool.get("slug") or "").strip()
        if not _SLUG_RE.match(slug):
            st.error = f"invalid slug {slug!r}"
            logger.error("plugin %s: %s", name, st.error)
            out.statuses.append(st)
            continue
        if slug in reserved or slug in used:
            st.error = f"slug {slug!r} already registered"
            logger.error("plugin %s: %s", name, st.error)
            out.statuses.append(st)
            continue

        router = getattr(module, "router", None)
        if router is None:
            st.error = "missing router (FastAPI APIRouter required)"
            logger.error("plugin %s: %s", name, st.error)
            out.statuses.append(st)
            continue
        prefix = getattr(router, "prefix", "") or ""
        if prefix and prefix != f"/tools/{slug}":
            st.error = f"router prefix {prefix!r} != /tools/{slug}"
            logger.error("plugin %s: %s", name, st.error)
            out.statuses.append(st)
            continue

        entry = dict(_TOOL_DEFAULTS)
        # The ``features`` default is a shared list; copy it so plugins cannot
        # leak mutations into each other through the defaults.
        entry["features"] = list(entry["features"])
        entry.update({k: v for k, v in tool.items() if v is not None})
        entry["slug"] = slug
        entry.setdefault("name", name)
        entry.setdefault("route", f"/tools/{slug}")

        used.add(slug)
        out.entries.append(entry)
        out.routers.append(router)
        st.slug = slug
        st.version = str(getattr(module, "PLUGIN_VERSION", "") or "")
        st.loaded = True
        out.statuses.append(st)

        tpl_dir = pkg_dir / "templates"
        if tpl_dir.is_dir():
            out.template_dirs.append(tpl_dir)
        static_dir = pkg_dir / "static"
        if static_dir.is_dir():
            out.static_mounts.append((slug, static_dir))

    # Preserve curated display order (manifest ``order``), not directory
    # alphabetical scan order. ``entries`` and ``routers`` are kept aligned.
    if len(out.entries) > 1:
        ordered = sorted(
            zip(out.entries, out.routers),
            key=lambda pair: int(pair[0].get("order", 999)),
        )
        out.entries = [e for e, _ in ordered]
        out.routers = [r for _, r in ordered]

    if base is None:
        _discovery = out
    return out
