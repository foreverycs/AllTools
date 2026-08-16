"""Admin console: file-express (取件码) package management."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from admin._common import _admin_url, _redirect, _tpl, admin_post
from admin.auth import require_admin
from tools.common import content_disposition
from storage.express import (
    cleanup_express,
    delete_package,
    delete_packages,
    express_stats,
    get_package_by_id,
    list_packages,
    resolve_package_file,
)

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
