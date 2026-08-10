"""Admin console: plugin hot reload.

The plugin table now lives inside the unified tool-management page
(``admin/routes_tools.py``); this module only provides the reload endpoint
(also invoked from the tool page header).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from admin._common import _admin_url, _redirect, admin_post
from admin.auth import require_admin
from core.plugins import plugins_dir

router = APIRouter(tags=["admin"])


@router.get("/plugins", include_in_schema=False)
async def plugins_page_redirect(request: Request):
    """The standalone plugin page was merged into /admin/tools."""
    return RedirectResponse(
        _admin_url("/admin/tools", request),
        status_code=307,
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
    return _redirect(_admin_url("/admin/tools", request) + "?msg=" + quote(msg))
