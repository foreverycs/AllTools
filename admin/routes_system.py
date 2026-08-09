"""Admin console: system diagnostics and the JSON stats API."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from admin._common import _build_health, _tpl, get_cached_health
from admin.auth import is_admin, require_admin
from core.settings import dotenv_status, get_settings
from storage import (
    file_dir,
    retention_days,
    storage_stats,
)
from tools import TOOL_REGISTRY, tools_by_category

router = APIRouter(tags=["admin"])


@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    redir = require_admin(request)
    if redir:
        return redir
    from core.tool_flags import flags_status
    from core.plugins import get_plugin_statuses

    # Prefer warm cache; only probe synchronously if still cold after startup warm.
    health = get_cached_health()
    if health is None:
        health = _build_health()

    return _tpl(
        request,
        "admin/system.html",
        active="system",
        health=health,
        stats=storage_stats(),
        tools=TOOL_REGISTRY,
        categories=tools_by_category(include_disabled=True),
        tool_flags=flags_status(),
        plugins=get_plugin_statuses(),
        env_hints={
            **get_settings().admin_security_summary(),
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
            "health": _build_health(),
            "tool_flags": flags_status(),
        }
    )
