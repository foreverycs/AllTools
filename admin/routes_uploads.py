"""Admin console: upload history listing, delete, download, preview, cleanup."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from admin._common import (
    _admin_url,
    _PREVIEW_FORCE_DOWNLOAD_EXTS,
    _PREVIEW_MIME,
    _WORD_PREVIEW_EXTS,
    _content_disposition_inline,
    _redirect,
    _tpl,
    _word_preview_pdf_response,
    admin_post,
)
from admin.auth import require_admin
from storage import (
    cleanup_expired,
    delete_record,
    delete_records,
    get_record,
    list_records,
    resolve_stored,
    retention_days,
)

router = APIRouter(tags=["admin"])


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
        retention_days=retention_days(),
        flash=request.query_params.get("msg"),
    )


@router.post("/uploads/batch-delete")
@admin_post
async def uploads_batch_delete(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Delete multiple upload records selected in the admin table."""
    form = await request.form()
    raw_ids = form.getlist("ids")
    ids = [str(v).strip() for v in raw_ids if str(v).strip()]
    # Cap batch size to avoid accidental huge deletes / DoS via form spam.
    max_batch = 200
    if len(ids) > max_batch:
        ids = ids[:max_batch]

    if not ids:
        return _redirect(
            _admin_url("/admin/uploads", request) + "?msg=" + quote("no selection")
        )

    removed = delete_records(ids)
    msg = f"deleted {removed}" if removed else "not found"
    return _redirect(_admin_url("/admin/uploads", request) + "?msg=" + quote(msg))


@router.post("/uploads/{record_id}/delete")
@admin_post
async def uploads_delete(
    request: Request,
    record_id: str,
    csrf_token: Optional[str] = Form(None),
):
    ok = delete_record(record_id)
    msg = "deleted" if ok else "not found"
    return _redirect(_admin_url("/admin/uploads", request) + "?msg=" + quote(msg))


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

    # Word: convert to PDF via word2pdf so the browser can render inline.
    if ext in _WORD_PREVIEW_EXTS:
        return await _word_preview_pdf_response(
            path, original_name=str(rec.get("original_name") or path.name)
        )

    media_type = _PREVIEW_MIME.get(ext, "application/octet-stream")
    display_name = str(rec.get("original_name") or path.name)
    if ext in _PREVIEW_FORCE_DOWNLOAD_EXTS:
        # Active content (HTML/SVG): force download to avoid stored XSS.
        return FileResponse(
            path,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{quote(display_name)}"',
                "X-Content-Type-Options": "nosniff",
            },
        )
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": _content_disposition_inline(display_name)},
    )


@router.post("/cleanup")
@admin_post
async def run_cleanup(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    removed = cleanup_expired()
    return _redirect(
        _admin_url("/admin/uploads", request) + "?msg=" + quote("cleaned %d" % removed)
    )
