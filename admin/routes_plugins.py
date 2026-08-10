"""Admin console: plugin management (list / hot reload / docs).

The plugin table and the 「插件重载」 action used to live inside the system
status page; they are now a dedicated page so plugin health gets its own
focus. Reload re-discovers ``PLUGINS_DIR`` and swaps routers / registry /
templates / static mounts without restarting the app.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from admin._common import _admin_url, _redirect, _tpl, admin_post
from admin.auth import require_admin
from core.plugins import get_plugin_discovery, get_plugin_statuses, plugins_dir

router = APIRouter(tags=["admin"])


@router.get("/plugins", response_class=HTMLResponse)
async def plugins_page(request: Request):
    redir = require_admin(request)
    if redir:
        return redir
    disc = get_plugin_discovery()
    statuses = disc.statuses
    loaded = [s for s in statuses if s.loaded]
    failed = [s for s in statuses if not s.loaded]
    entries_by_slug = {str(e.get("slug") or ""): e for e in disc.entries}
    rows = []
    for s in statuses:
        entry = entries_by_slug.get(str(s.slug or "")) or {}
        rows.append(
            {
                "name": s.name,
                "slug": s.slug,
                "version": s.version,
                "loaded": s.loaded,
                "error": s.error,
                "route": entry.get("route"),
            }
        )
    return _tpl(
        request,
        "admin/plugins.html",
        active="plugins",
        plugins=rows,
        loaded_count=len(loaded),
        failed_count=len(failed),
        plugin_dir=str(plugins_dir()),
        static_mounts=disc.static_mounts,
        flash=request.query_params.get("msg"),
    )


@router.post("/plugins/reload")
@admin_post
async def plugins_reload(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Hot reload plugins: re-scan PLUGINS_DIR, swap routes/registry/templates."""
    from app import hot_reload_plugins

    try:
        disc = hot_reload_plugins()
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"插件重载失败: {exc}"
        ) from exc
    loaded = sum(1 for s in disc.statuses if s.loaded)
    failed = len(disc.statuses) - loaded
    msg = f"插件已重载：{loaded} 个加载，{failed} 个失败（目录：{plugins_dir()}）"
    return _redirect(_admin_url("/admin/plugins", request) + "?msg=" + quote(msg))
