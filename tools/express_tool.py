"""文件快递 — 取件码上传 / 取件下载。"""

from __future__ import annotations

import os
import tempfile
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from storage.express import (
    claim_download,
    create_package,
    ensure_express_dir,
    express_default_ttl_hours,
    express_max_bytes,
    express_max_ttl_hours,
    express_stats,
    get_package_by_code,
    is_valid_code_format,
)
from tools.common import (
    check_upload_size_header,
    save_upload,
    templates,
    url_path,
    with_nav,
)

router = APIRouter(prefix="/tools/express", tags=["express"])

_ERROR_MESSAGES = {
    "invalid": "取件码无效或不存在",
    "expired": "文件已过期",
    "exhausted": "下载次数已用完",
    "missing": "文件已丢失，请重新寄送",
}


def _tool_ctx(request: Request) -> dict:
    max_b = express_max_bytes()
    return {
        "tool": {
            "name": "文件快递",
            "slug": "express",
            "category": "office",
        },
        "limits": {
            "max_bytes": max_b,
            "max_mb": max(1, max_b // (1024 * 1024)),
            "default_ttl_hours": express_default_ttl_hours(),
            "max_ttl_hours": express_max_ttl_hours(),
        },
        "prefill_code": (request.query_params.get("code") or "").strip(),
    }


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/express.html",
        with_nav(_tool_ctx(request)),
    )


@router.get("/limits")
async def api_limits():
    return JSONResponse(
        {
            "max_bytes": express_max_bytes(),
            "default_ttl_hours": express_default_ttl_hours(),
            "max_ttl_hours": express_max_ttl_hours(),
            **{k: v for k, v in express_stats().items() if k in ("package_count",)},
        }
    )


def _parse_ttl(raw: Optional[str]) -> int:
    default = express_default_ttl_hours()
    max_h = express_max_ttl_hours()
    if raw is None or str(raw).strip() == "":
        return default
    try:
        hours = int(str(raw).strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="有效期必须是整数小时") from exc
    if hours < 1:
        raise HTTPException(status_code=400, detail="有效期至少 1 小时")
    if hours > max_h:
        raise HTTPException(
            status_code=400, detail=f"有效期最长 {max_h} 小时"
        )
    return hours


def _parse_max_downloads(raw: Optional[str]) -> int:
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        n = int(str(raw).strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="下载次数必须是整数") from exc
    if n < 0:
        raise HTTPException(status_code=400, detail="下载次数不能为负")
    if n > 1000:
        raise HTTPException(status_code=400, detail="下载次数上限 1000")
    return n


@router.post("/send")
async def api_send(
    request: Request,
    file: UploadFile = File(...),
    ttl_hours: Optional[str] = Form(None),
    max_downloads: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
):
    """Upload a file and receive a 6-digit pickup code."""
    ensure_express_dir()
    limit = express_max_bytes()
    # Express-specific limit (may differ from global MAX_UPLOAD_BYTES).
    if file.size is not None and file.size > limit:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（上限 {limit // (1024 * 1024)} MB）",
        )
    check_upload_size_header(file, max_bytes=limit)

    hours = _parse_ttl(ttl_hours)
    max_dl = _parse_max_downloads(max_downloads)
    note_s = (note or "").strip()[:200]

    # Prefer original name; empty upload filename still allowed.
    original = (file.filename or "").strip() or "file"

    fd, tmp_path = tempfile.mkstemp(prefix="express_")
    os.close(fd)
    try:
        try:
            await save_upload(file, tmp_path, max_bytes=limit)
        except HTTPException as exc:
            # Map common English messages to Chinese for this tool surface.
            detail = str(exc.detail or "")
            if "Empty file" in detail or detail == "Empty file":
                raise HTTPException(status_code=400, detail="文件为空，请重新选择") from exc
            if "too large" in detail.lower() or "文件过大" in detail:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件过大（上限 {limit // (1024 * 1024)} MB）",
                ) from exc
            raise
        try:
            pkg = create_package(
                tmp_path,
                original,
                content_type=file.content_type or "",
                ttl_hours=hours,
                max_downloads=max_dl,
                note=note_s,
            )
        except ValueError as exc:
            msg = str(exc)
            if "empty or missing" in msg.lower():
                msg = "文件为空或无效，请重新选择"
            elif "too large" in msg.lower():
                msg = f"文件过大（上限 {limit // (1024 * 1024)} MB）"
            raise HTTPException(status_code=400, detail=msg) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"服务器无法保存文件：{exc}",
            ) from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    pickup_path = url_path(f"/tools/express?code={pkg['code']}", request)
    return JSONResponse(
        {
            "ok": True,
            "code": pkg["code"],
            "id": pkg["id"],
            "original_name": pkg["original_name"],
            "size_bytes": pkg["size_bytes"],
            "created_at": pkg["created_at"],
            "expires_at": pkg["expires_at"],
            "seconds_remaining": pkg["seconds_remaining"],
            "max_downloads": pkg["max_downloads"],
            "downloads_left": pkg["downloads_left"],
            "note": pkg["note"],
            "pickup_url": pickup_path,
            "message": f"寄送成功，取件码 {pkg['code']}",
        }
    )


@router.post("/lookup")
async def api_lookup(code: str = Form(...)):
    """Query package metadata by code (does not consume a download)."""
    if not is_valid_code_format(code):
        raise HTTPException(status_code=400, detail="请输入 6 位数字取件码")
    info = get_package_by_code(code)
    if info is None:
        raise HTTPException(status_code=404, detail=_ERROR_MESSAGES["invalid"])
    if info.get("expired"):
        raise HTTPException(status_code=410, detail=_ERROR_MESSAGES["expired"])
    if info.get("exhausted"):
        raise HTTPException(status_code=410, detail=_ERROR_MESSAGES["exhausted"])
    # Never expose stored_rel to clients
    safe = {
        k: info[k]
        for k in (
            "code",
            "original_name",
            "size_bytes",
            "content_type",
            "created_at",
            "expires_at",
            "max_downloads",
            "download_count",
            "downloads_left",
            "note",
            "available",
            "seconds_remaining",
        )
        if k in info
    }
    return JSONResponse({"ok": True, **safe})


def _pickup_response(raw_code: str) -> FileResponse:
    if not is_valid_code_format(raw_code):
        raise HTTPException(status_code=400, detail="请输入 6 位数字取件码")

    info, err = claim_download(raw_code)
    if err:
        status = 404 if err in ("invalid", "missing") else 410
        raise HTTPException(
            status_code=status,
            detail=_ERROR_MESSAGES.get(err, "取件失败"),
        )
    assert info is not None
    path = info.get("_abs_path")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=_ERROR_MESSAGES["missing"])

    name = info.get("original_name") or "download"
    media = info.get("content_type") or "application/octet-stream"
    ascii_name = "".join(
        ch if 32 <= ord(ch) < 127 and ch not in '\\";' else "_" for ch in name
    ).strip("._") or "download"
    while "__" in ascii_name:
        ascii_name = ascii_name.replace("__", "_")
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(name, safe='')}"
        ),
        "X-Express-Code": str(info.get("code") or ""),
        "X-Express-Downloads": str(info.get("download_count") or 0),
        "Cache-Control": "no-store",
    }
    return FileResponse(path, media_type=media, headers=headers)


@router.post("/pickup")
async def api_pickup_post(code: str = Form(...)):
    """Download by form code (consumes one download if limited)."""
    return _pickup_response(code)


@router.get("/pickup/{code}")
async def api_pickup_get(code: str):
    """Download by path code (bookmarkable)."""
    return _pickup_response(code)
