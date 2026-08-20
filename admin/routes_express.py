"""Admin console: file-express (取件码) package management."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from admin._common import (
    _admin_url,
    _PREVIEW_FORCE_DOWNLOAD_EXTS,
    _PREVIEW_MIME,
    _content_disposition_inline,
    _redirect,
    _tpl,
    admin_post,
)
from admin.auth import require_admin
from tools.common import content_disposition
from storage.express import (
    _TEXT_FILE_EXTS,
    cleanup_express,
    delete_package,
    delete_packages,
    express_stats,
    get_package_by_id,
    list_packages,
    resolve_package_file,
)

# Raster images safe to render inline in the browser preview. Derived from the
# shared image MIME allowlist, minus active-content formats (SVG/HTML) that the
# upload console force-downloads — so the two consoles stay in sync.
_PREVIEW_IMAGE_EXTS = {
    ext
    for ext, mime in _PREVIEW_MIME.items()
    if mime.startswith("image/") and ext not in _PREVIEW_FORCE_DOWNLOAD_EXTS
}
_PREVIEW_IMAGE_MIME = {ext: _PREVIEW_MIME[ext] for ext in _PREVIEW_IMAGE_EXTS}

# Cap the number of bytes read into memory for a text-file preview.
_PREVIEW_TEXT_MAX_BYTES = 200 * 1024


def _read_text_file_sync(path: "Path") -> str:
    """Read up to the preview cap as UTF-8 text (lossy; run in a worker thread)."""
    with open(path, "rb") as f:
        raw = f.read(_PREVIEW_TEXT_MAX_BYTES)
    truncated = len(raw) >= _PREVIEW_TEXT_MAX_BYTES
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += "\n\n…（文件过大，仅显示前 %d KB）" % (_PREVIEW_TEXT_MAX_BYTES // 1024)
    return text


async def _read_text_file(path: "Path") -> str:
    import asyncio

    return await asyncio.to_thread(_read_text_file_sync, path)

router = APIRouter(tags=["admin"])


@router.get("/express", response_class=HTMLResponse)
async def express_page(
    request: Request,
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=200),
):
    """Admin list of file-express packages (pickup codes)."""
    redir = require_admin(request)
    if redir:
        return redir

    q_f = (q or "").strip()
    status_f = (status or "").strip().lower()
    if status_f and status_f not in (
        "available",
        "expired",
        "exhausted",
        "missing",
    ):
        status_f = ""

    items = list_packages(limit=limit, q=q_f or None, status=status_f or None)
    stats = express_stats()

    return _tpl(
        request,
        "admin/express.html",
        active="express",
        items=items,
        q=q_f,
        status_filter=status_f,
        stats=stats,
        flash=request.query_params.get("msg"),
    )


@router.post("/express/batch-delete")
@admin_post
async def express_batch_delete(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    form = await request.form()
    raw_ids = form.getlist("ids")
    ids = [str(v).strip() for v in raw_ids if str(v).strip()]
    max_batch = 200
    if len(ids) > max_batch:
        ids = ids[:max_batch]

    if not ids:
        return _redirect(
            _admin_url("/admin/express", request) + "?msg=" + quote("no selection")
        )

    removed = delete_packages(ids)
    msg = f"deleted {removed}" if removed else "not found"
    return _redirect(_admin_url("/admin/express", request) + "?msg=" + quote(msg))


@router.post("/express/cleanup")
@admin_post
async def express_cleanup(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Explicit admin purge of expired packages (not automatic)."""
    # force=True required: expiry never auto-deletes; only this admin action does.
    removed = cleanup_express(force=True)
    return _redirect(
        _admin_url("/admin/express", request)
        + "?msg="
        + quote("cleaned %d" % removed)
    )


@router.get("/express/{package_id}/download")
async def express_download(request: Request, package_id: str):
    redir = require_admin(request)
    if redir:
        return redir

    pkg = get_package_by_id(package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")
    path = resolve_package_file(pkg)
    if path is None:
        raise HTTPException(status_code=404, detail="File missing")
    name = pkg.get("original_name") or path.name
    return FileResponse(
        path,
        headers={"Content-Disposition": content_disposition(str(name))},
    )


@router.get("/express/{package_id}/preview")
async def express_preview(request: Request, package_id: str):
    """Admin preview payload for 小纸条 (text), text/code files, images, and PDFs.

    Returns JSON ``{"kind": "text", "text": "..."}`` for 小纸条 and text/code
    files; the raw image bytes inline for image packages; and the raw PDF bytes
    inline for PDF packages (rendered by the modal). Text/code/HTML content is
    served as plain text — never executed — so there is no stored-XSS from
    previewing an uploaded HTML file.
    """
    redir = require_admin(request)
    if redir:
        return redir

    pkg = get_package_by_id(package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")

    if pkg.get("is_text"):
        full_text = pkg.get("_full_text") or ""
        if not full_text:
            raise HTTPException(status_code=404, detail="内容已清空（阅后即焚）")
        return JSONResponse({"kind": "text", "text": full_text})

    if not pkg.get("has_file"):
        raise HTTPException(status_code=404, detail="File missing")
    path = resolve_package_file(pkg)
    if path is None:
        raise HTTPException(status_code=404, detail="File missing")

    ext = path.suffix.lower()
    name = pkg.get("original_name") or path.name

    if ext in _PREVIEW_IMAGE_EXTS:
        return FileResponse(
            path,
            media_type=_PREVIEW_IMAGE_MIME[ext],
            headers={"Content-Disposition": _content_disposition_inline(str(name))},
        )

    if ext == ".pdf" or pkg.get("is_pdf"):
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={"Content-Disposition": _content_disposition_inline(str(name))},
        )

    if ext in _TEXT_FILE_EXTS or pkg.get("is_text_file"):
        text = await _read_text_file(path)
        return JSONResponse({"kind": "text", "text": text})

    raise HTTPException(status_code=415, detail="该文件类型不支持预览")


@router.post("/express/{package_id}/delete")
@admin_post
async def express_delete(
    request: Request,
    package_id: str,
    csrf_token: Optional[str] = Form(None),
):
    ok = delete_package(package_id)
    msg = "deleted" if ok else "not found"
    return _redirect(_admin_url("/admin/express", request) + "?msg=" + quote(msg))
