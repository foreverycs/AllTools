"""Admin console: login, dashboard, uploads, system."""

from __future__ import annotations

import os
import time
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from admin.auth import (
    check_password,
    clear_session_cookie,
    create_session_token,
    is_admin,
    require_admin,
    set_session_cookie,
)
from storage import (
    RETENTION_DAYS,
    cleanup_expired,
    delete_record,
    get_record,
    list_records,
    resolve_stored,
    storage_stats,
)
from tools import TOOL_REGISTRY, tools_by_category

# NOTE: tags list closes with ], then APIRouter call closes with )
router = APIRouter(prefix="/admin", tags=["admin"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Pre-compute static data
_categories_cache = tools_by_category()

# Cached health info — engines don't change at runtime
_health_cache: dict = {}
_health_cache_ts: float = 0.0
_HEALTH_TTL: float = 60.0


def _tpl(request: Request, name: str, **ctx):
    data = {
        "request": request,
        "is_admin": is_admin(request),
        "app_version": "0.7.0",
        **ctx,
    }
    return templates.TemplateResponse(request, name, data)


def _safe_next(next_url: Optional[str]) -> str:
    if not next_url:
        return "/admin"
    if next_url.startswith("/admin") and "://" not in next_url and "\\" not in next_url:
        return next_url
    return "/admin"


def _build_health() -> dict:
    global _health_cache, _health_cache_ts
    now = time.monotonic()
    if _health_cache and now - _health_cache_ts < _HEALTH_TTL:
        return _health_cache
    from word2pdf import engine_info
    from converter import ocr_info

    w2p = engine_info()
    ocr = ocr_info()
    _health_cache = {
        "word2pdf": w2p,
        "ocr": ocr,
        "tools": len(TOOL_REGISTRY),
        "categories": len(_categories_cache),
    }
    _health_cache_ts = now
    return _health_cache


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    if is_admin(request):
        return _redirect(_safe_next(next))
    return _tpl(
        request,
        "admin/login.html",
        next_url=_safe_next(next),
        error=error,
    )


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    next: Optional[str] = Form(None),
):
    if not check_password(password):
        dest = "/admin/login?error=" + quote("password error") + "&next=" + quote(
            _safe_next(next)
        )
        return _redirect(dest)
    resp = _redirect(_safe_next(next))
    set_session_cookie(resp, create_session_token())
    return resp


@router.post("/logout")
async def logout():
    resp = _redirect("/admin/login")
    clear_session_cookie(resp)
    return resp


@router.get("/logout")
async def logout_get():
    resp = _redirect("/admin/login")
    clear_session_cookie(resp)
    return resp


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    redir = require_admin(request)
    if redir:
        return redir
    return _tpl(
        request,
        "admin/dashboard.html",
        active="dashboard",
        stats=storage_stats(),
        health=_build_health(),
        recent=list_records(limit=8),
        tools=TOOL_REGISTRY,
        categories=_categories_cache,
    )


@router.get("/uploads", response_class=HTMLResponse)
async def uploads_page(
    request: Request,
    tool: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=200),
):
    redir = require_admin(request)
    if redir:
        return redir

    all_items = list_records(limit=max(limit, 200))
    tool_f = (tool or "").strip()
    q_f = (q or "").strip().lower()

    tools_used = sorted(
        {str(r.get("tool") or "") for r in all_items if r.get("tool")}
    )

    items = all_items[:limit]
    if tool_f:
        items = [r for r in items if r.get("tool") == tool_f]
    if q_f:
        items = [
            r
            for r in items
            if q_f in str(r.get("original_name") or "").lower()
            or q_f in str(r.get("id") or "").lower()
        ]

    return _tpl(
        request,
        "admin/uploads.html",
        active="uploads",
        items=items,
        tool_filter=tool_f,
        q=q or "",
        tools_used=tools_used,
        retention_days=RETENTION_DAYS,
        flash=request.query_params.get("msg"),
    )


@router.post("/uploads/{record_id}/delete")
async def uploads_delete(request: Request, record_id: str):
    redir = require_admin(request)
    if redir:
        return redir
    ok = delete_record(record_id)
    msg = "deleted" if ok else "not found"
    return _redirect("/admin/uploads?msg=" + quote(msg))


@router.get("/uploads/{record_id}/download")
async def uploads_download(request: Request, record_id: str):
    redir = require_admin(request)
    if redir:
        return redir
    rec = get_record(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Record not found")
    rel = rec.get("input_rel")
    if not rel:
        raise HTTPException(status_code=404, detail="No file")
    path = resolve_stored(str(rel))
    if path is None:
        raise HTTPException(status_code=404, detail="File missing")
    name = rec.get("original_name") or path.name
    return FileResponse(path, filename=str(name))


_PREVIEW_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
}


@router.get("/uploads/{record_id}/preview")
async def uploads_preview(request: Request, record_id: str):
    redir = require_admin(request)
    if redir:
        return redir
    rec = get_record(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Record not found")
    rel = rec.get("input_rel")
    if not rel:
        raise HTTPException(status_code=404, detail="No file")
    path = resolve_stored(str(rel))
    if path is None:
        raise HTTPException(status_code=404, detail="File missing")
    ext = path.suffix.lower()
    media_type = _PREVIEW_MIME.get(ext, "application/octet-stream")
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


@router.post("/cleanup")
async def run_cleanup(request: Request):
    redir = require_admin(request)
    if redir:
        return redir
    removed = cleanup_expired()
    return _redirect("/admin/uploads?msg=" + quote("cleaned %d" % removed))


@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    redir = require_admin(request)
    if redir:
        return redir
    return _tpl(
        request,
        "admin/system.html",
        active="system",
        health=_build_health(),
        stats=storage_stats(),
        tools=TOOL_REGISTRY,
        categories=_categories_cache,
        env_hints={
            "ADMIN_PASSWORD": (
                "set" if os.environ.get("ADMIN_PASSWORD") else "default admin123"
            ),
            "UPLOAD_RETENTION_DAYS": str(RETENTION_DAYS),
            "UPLOAD_FILE_DIR": os.environ.get("UPLOAD_FILE_DIR") or "(default ./file)",
            "LIBREOFFICE_PATH": os.environ.get("LIBREOFFICE_PATH") or "(auto)",
            "PDF2WORD_OCR": os.environ.get("PDF2WORD_OCR") or "0",
        },
    )


@router.get("/api/stats")
async def api_stats(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse(
        {
            "storage": storage_stats(),
            "health": _build_health(),
        }
    )
