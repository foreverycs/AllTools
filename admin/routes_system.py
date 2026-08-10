"""Admin console: system diagnostics and the JSON stats API."""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from admin._common import (
    _admin_url,
    _redirect,
    _tpl,
    admin_post,
    get_cached_health,
)
from admin.auth import is_admin, require_admin
from core.health import get_health_snapshot
from core.plugins import get_plugin_statuses
from core.settings import dotenv_status, get_settings
from storage import (
    file_dir,
    retention_days,
    storage_stats,
)
from tools import get_registry, tools_by_category

router = APIRouter(tags=["admin"])


@router.post("/plugins/reload")
@admin_post
async def plugins_reload(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Hot reload plugins: re-scan plugins/, swap routes/registry/templates."""
    from app import hot_reload_plugins

    try:
        disc = hot_reload_plugins()
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"插件重载失败: {exc}"
        ) from exc
    loaded = sum(1 for s in disc.statuses if s.loaded)
    failed = len(disc.statuses) - loaded
    from core.plugins import plugins_dir

    msg = (
        f"插件已重载：{loaded} 个加载，{failed} 个失败（目录：{plugins_dir()}）"
    )
    return _redirect(_admin_url("/admin/system", request) + "?msg=" + quote(msg))


@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    redir = require_admin(request)
    if redir:
        return redir
    from core.tool_flags import flags_status

    # Prefer warm cache; only probe synchronously if still cold after startup warm.
    health = get_cached_health()
    if health is None:
        health = get_health_snapshot(force=True)

    return _tpl(
        request,
        "admin/system.html",
        active="system",
        health=health,
        stats=storage_stats(),
        tools=get_registry(),
        categories=tools_by_category(include_disabled=True),
        tool_flags=flags_status(),
        plugins=get_plugin_statuses(),
        env_hints={
            **get_settings().admin_security_summary(),
            "PLUGINS_DIR": os.environ.get("PLUGINS_DIR") or "(默认 项目根/plugins)",
            "UPLOAD_RETENTION_DAYS": str(retention_days()),
            "UPLOAD_FILE_DIR": str(file_dir()),
            "LIBREOFFICE_PATH": os.environ.get("LIBREOFFICE_PATH") or "(auto)",
            "PDF2WORD_OCR": os.environ.get("PDF2WORD_OCR") or "0",
            "MAX_UPLOAD_BYTES": str(get_settings().max_upload_bytes),
            **{f".env {k}": v for k, v in dotenv_status().items()},
        },
    )


@router.get("/api/stats")
async def api_stats(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    from core.tool_flags import flags_status

    return JSONResponse(
        {
            "storage": storage_stats(),
            "health": get_health_snapshot(),
            "tool_flags": flags_status(),
        }
    )
