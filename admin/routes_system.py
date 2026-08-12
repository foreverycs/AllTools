"""Admin console: system diagnostics and the JSON stats API."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from admin._common import _tpl, get_cached_health, schedule_health_warm
from admin.auth import is_admin, require_admin
from core.settings import dotenv_status, get_settings
from storage import (
    file_dir,
    retention_days,
    storage_stats,
)
from tools import get_registry, tools_by_category

router = APIRouter(tags=["admin"])

# Placeholder rendered while the engine probe warms in the background.
_EMPTY_HEALTH = {
    "word2pdf": {
        "ready": False,
        "engines": [],
        "preferred": "",
        "libreoffice_path": "",
    },
    "ocr": {"available": False, "lang": "", "tesseract_cmd": ""},
    "tools": 0,
    "tools_registered": 0,
    "tools_disabled": 0,
    "categories": 0,
}


@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    redir = require_admin(request)
    if redir:
        return redir
    from core.tool_flags import flags_status

    # Never block the page on LibreOffice/Tesseract probes: use the warm cache
    # and refresh engines in the browser via /admin/api/stats when cold.
    health = get_cached_health()
    health_pending = health is None
    if health_pending:
        schedule_health_warm()
        health = _EMPTY_HEALTH

    return _tpl(
        request,
        "admin/system.html",
        active="system",
        health=health,
        health_pending=health_pending,
        stats=storage_stats(),
        tools=get_registry(),
        categories=tools_by_category(include_disabled=True),
        tool_flags=flags_status(),
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

    # Never block the event loop on a cold engine probe: return the cached
    # snapshot (None while warming) and let the browser poll again. The
    # background warm (schedule_health_warm) fills the cache off the request
    # path, so a subsequent poll returns fresh data without stalling.
    health = get_cached_health()
    if health is None:
        schedule_health_warm()

    return JSONResponse(
        {
            "storage": storage_stats(),
            "health": health,
            "tool_flags": flags_status(),
        }
    )
