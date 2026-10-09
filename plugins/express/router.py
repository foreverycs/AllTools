"""文件快递 — 取件码上传 / 取件下载（支持多文件打包与阅后即焚）。"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import tempfile
import threading
import time
import zipfile
from contextlib import suppress

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.requests import Request

from core.rate_limit_base import SlidingWindow, client_key_from_request
from storage.express import (
    claim_download,
    claim_text,
    create_package,
    create_text_package,
    ensure_express_dir,
    express_default_ttl_hours,
    express_max_bytes,
    express_max_ttl_hours,
    express_stats,
    get_package_by_code,
    is_valid_code_format,
    max_text_chars,
)
from tools.common import (
    check_upload_size_header,
    content_disposition,
    templates,
    to_bool,
    upload_chunk_size,
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

# Multi-file send: hard caps (also bound by express_max_bytes total).
_MAX_SEND_FILES = 20

_QR_SIZE = 320

# Brute-force guard: the 6-digit code space (10^6) is enumerable, so wrong-code
# attempts are counted per client IP. Past _CODE_FAIL_MAX misses inside
# _CODE_FAIL_WINDOW_SEC the client is locked out for _CODE_LOCKOUT_SEC and all
# code endpoints (lookup / read / pickup) refuse it before touching the DB.
_CODE_FAIL_MAX = 10
_CODE_FAIL_WINDOW_SEC = 600.0
_CODE_LOCKOUT_SEC = 900.0

_guard_lock = threading.Lock()
_code_failures = SlidingWindow()
_code_lockouts: dict[str, float] = {}
_lockout_sweep_ts: float = 0.0
_LOCKOUT_SWEEP_INTERVAL_SEC = 60.0


def _sweep_lockouts(t: float) -> None:
    """Drop expired lockout entries so the dict cannot grow unbounded.

    Callers hold ``_guard_lock``. Throttled like the admin login limiter so the
    common (not-locked) request is one dict lookup, not a full scan.
    """
    global _lockout_sweep_ts
    if t - _lockout_sweep_ts < _LOCKOUT_SWEEP_INTERVAL_SEC:
        return
    _lockout_sweep_ts = t
    for k, until in list(_code_lockouts.items()):
        if until <= t:
            _code_lockouts.pop(k, None)


def reset_code_guard() -> None:
    """Drop brute-force guard state (tests)."""
    global _lockout_sweep_ts
    with _guard_lock:
        _code_lockouts.clear()
        _lockout_sweep_ts = 0.0
    _code_failures.clear()


def _guard_key(request: Request) -> str:
    return f"code:{client_key_from_request(request)}"


def _guard_reject_if_locked(key: str) -> None:
    t = time.monotonic()
    with _guard_lock:
        _sweep_lockouts(t)
        until = _code_lockouts.get(key)
        if until is None:
            return
        if until <= t:
            _code_lockouts.pop(key, None)
            return
        retry = max(1, int(until - t))
    raise HTTPException(
        status_code=429,
        detail=f"尝试次数过多，请 {retry} 秒后再试",
        headers={"Retry-After": str(retry)},
    )


def _guard_register_failure(key: str) -> None:
    """Record one wrong-code attempt; lock the client out past the limit."""
    t = time.monotonic()
    exceeded, _ = _code_failures.record(
        key, limit=_CODE_FAIL_MAX, window_sec=_CODE_FAIL_WINDOW_SEC, now=t
    )
    if exceeded:
        with _guard_lock:
            _sweep_lockouts(t)
            _code_lockouts[key] = t + _CODE_LOCKOUT_SEC
        raise HTTPException(
            status_code=429,
            detail=f"尝试次数过多，请 {int(_CODE_LOCKOUT_SEC)} 秒后再试",
            headers={"Retry-After": str(int(_CODE_LOCKOUT_SEC))},
        )


def _guard_clear_failures(key: str) -> None:
    """Clear failure history after a correct code (like admin login)."""
    with _guard_lock:
        _code_lockouts.pop(key, None)
    _code_failures.discard(key)


def _guard_fail_or_clear(key: str, err: str | None) -> None:
    """Count unknown-code misses; clear the counter once the code is correct.

    Any non-``invalid`` outcome (success, expired, exhausted, missing payload)
    proves the caller knew a real code, so the miss counter is reset.
    """
    if err == "invalid":
        _guard_register_failure(key)
    else:
        _guard_clear_failures(key)


def _render_qr_data_uri(payload: str) -> str:
    """Render ``payload`` to a PNG data URI for the pickup link QR."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _absolute_pickup_url(pickup_path: str, request: Request) -> str:
    """Public absolute pickup URL honoring ``SITE_ORIGIN`` + ROOT_PATH.

    The QR must point at the configured public domain, never at the
    reverse-proxy's internal host / container IP. ``pickup_path`` is already
    fully-prefixed by ``url_path`` (applies ROOT_PATH); we just prepend the
    public origin. When ``SITE_ORIGIN`` is unset there is no public host, so we
    return the relative path (frontend keeps using ``pickup_url``).
    """
    from core.seo import site_origin

    origin = site_origin()
    if not origin:
        return pickup_path
    return origin.rstrip("/") + "/" + pickup_path.lstrip("/")


def _sync_save_upload(src, dest: str, limit: int, chunk_size: int) -> int:
    """Copy an already-parsed upload spool to ``dest`` in a worker thread."""
    total = 0
    with open(dest, "wb") as out:
        while True:
            buf = src.read(chunk_size)
            if not buf:
                break
            total += len(buf)
            if total > limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件过大（上限 {limit // (1024 * 1024)} MB）",
                )
            out.write(buf)
    if total == 0:
        raise HTTPException(status_code=400, detail="文件为空，请重新选择")
    return total


def _spooled_disk_path(file: UploadFile) -> str | None:
    try:
        p = file.file.name
    except Exception:
        return None
    return p if p and os.path.isfile(p) else None


def _tool_ctx(request: Request) -> dict:
    max_b = express_max_bytes()
    return {
        "tool": {
            "name": "文件快递",
            "slug": "express",
            "category": "text",
        },
        "limits": {
            "max_bytes": max_b,
            "max_mb": max(1, max_b // (1024 * 1024)),
            "default_ttl_hours": express_default_ttl_hours(),
            "max_ttl_hours": express_max_ttl_hours(),
            "max_files": _MAX_SEND_FILES,
            "max_text_chars": max_text_chars(),
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
            "max_files": _MAX_SEND_FILES,
            "max_text_chars": max_text_chars(),
            **{k: v for k, v in express_stats().items() if k in ("package_count",)},
        }
    )


def _parse_ttl(raw: str | None) -> int:
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


def _parse_max_downloads(raw: str | None) -> int:
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


def _safe_zip_name(name: str, used: set) -> str:
    from core.filename import sanitize_filename

    base = sanitize_filename(name or "file", "file", stem_limit=80, ext_limit=20)
    candidate = base
    n = 1
    while candidate.lower() in used:
        stem, ext = os.path.splitext(base)
        candidate = f"{stem}_{n}{ext}"
        n += 1
    used.add(candidate.lower())
    return candidate


async def _materialize_upload(
    file: UploadFile, dest_dir: str, limit: int, *, remaining: int
) -> tuple[str, int]:
    """Save one upload under dest_dir; return (path, size). Enforces remaining budget."""
    if remaining <= 0:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（上限 {limit // (1024 * 1024)} MB）",
        )
    if file.size is not None and file.size > remaining:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（上限 {limit // (1024 * 1024)} MB）",
        )
    check_upload_size_header(file, max_bytes=min(limit, remaining))

    src_path = _spooled_disk_path(file)
    if src_path:
        size = os.path.getsize(src_path)
        if size <= 0:
            raise HTTPException(status_code=400, detail="文件为空，请重新选择")
        if size > remaining:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大（上限 {limit // (1024 * 1024)} MB）",
            )
        # Copy spool into dest_dir (do not consume Starlette temp across files).
        dest = os.path.join(dest_dir, f"part_{os.urandom(4).hex()}")
        await asyncio.to_thread(_copy_file, src_path, dest, remaining)
        return dest, os.path.getsize(dest)

    fd, tmp_path = tempfile.mkstemp(prefix="express_part_", dir=dest_dir)
    os.close(fd)
    try:
        size = await asyncio.to_thread(
            _sync_save_upload,
            file.file,
            tmp_path,
            remaining,
            upload_chunk_size(),
        )
    except HTTPException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise
    return tmp_path, size


def _copy_file(src: str, dest: str, limit: int) -> None:
    total = 0
    with open(src, "rb") as inf, open(dest, "wb") as out:
        while True:
            buf = inf.read(1024 * 1024)
            if not buf:
                break
            total += len(buf)
            if total > limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件过大（上限 {limit // (1024 * 1024)} MB）",
                )
            out.write(buf)
    if total == 0:
        raise HTTPException(status_code=400, detail="文件为空，请重新选择")


def _zip_parts(parts: list[tuple[str, str]], zip_path: str) -> None:
    """Write (disk_path, arcname) into zip_path."""
    used: set = set()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for disk, arc in parts:
            name = _safe_zip_name(arc, used)
            zf.write(disk, name)


@router.post("/send")
async def api_send(
    request: Request,
    file: UploadFile | None = File(None),
    files: list[UploadFile] | None = File(None),
    ttl_hours: str | None = Form(None),
    max_downloads: str | None = Form(None),
    note: str | None = Form(None),
    burn_after: str | None = Form(None),
):
    """Upload one or more files and receive a 6-digit pickup code.

    Multiple files are packed into a single ZIP payload. ``burn_after`` forces
    one download then deletes the stored payload (阅后即焚).
    """
    ensure_express_dir()
    limit = express_max_bytes()
    hours = _parse_ttl(ttl_hours)
    burn = to_bool(burn_after, False)
    max_dl = 1 if burn else _parse_max_downloads(max_downloads)
    note_s = (note or "").strip()[:200]

    # Normalize file list: accept legacy single ``file`` or multi ``files``.
    uploads: list[UploadFile] = []
    if files:
        uploads.extend([f for f in files if f is not None and f.filename])
    if file is not None and file.filename and (not uploads or file not in uploads):
        # Avoid double-counting if client sent both
        uploads.insert(0, file)
    # Some clients only use files= repeated
    if not uploads:
        # Try form multi without filename filter — empty list
        form = await request.form()
        for key in ("files", "file"):
            for item in form.getlist(key):
                if hasattr(item, "filename") and item.filename:
                    uploads.append(item)  # type: ignore[arg-type]
        # dedupe by identity
        seen = set()
        uniq = []
        for u in uploads:
            i = id(u)
            if i not in seen:
                seen.add(i)
                uniq.append(u)
        uploads = uniq

    if not uploads:
        raise HTTPException(status_code=400, detail="请先选择要寄送的文件")
    if len(uploads) > _MAX_SEND_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"一次最多寄送 {_MAX_SEND_FILES} 个文件",
        )

    work_dir = tempfile.mkdtemp(prefix="express_up_", dir=str(ensure_express_dir()))
    payload_path: str | None = None
    owns_payload = False
    file_count = len(uploads)
    try:
        remaining = limit
        parts: list[tuple[str, str]] = []
        for idx, up in enumerate(uploads):
            path, size = await _materialize_upload(
                up, work_dir, limit, remaining=remaining
            )
            remaining -= size
            parts.append((path, up.filename or f"file-{idx + 1}"))

        if file_count == 1:
            # Single file: keep original name/type (move into package).
            src_path = parts[0][0]
            original = (uploads[0].filename or "").strip() or "file"
            content_type = uploads[0].content_type or ""
            payload_path = src_path
            owns_payload = True
        else:
            zip_path = os.path.join(work_dir, "bundle.zip")
            await asyncio.to_thread(_zip_parts, parts, zip_path)
            zsize = os.path.getsize(zip_path)
            if zsize > limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"打包后过大（上限 {limit // (1024 * 1024)} MB）",
                )
            original = "files.zip"
            content_type = "application/zip"
            payload_path = zip_path
            owns_payload = True

        try:
            pkg = create_package(
                payload_path,
                original,
                content_type=content_type,
                ttl_hours=hours,
                max_downloads=max_dl,
                note=note_s,
                move_src=True,
                burn_after=burn,
                file_count=file_count,
            )
            owns_payload = False  # consumed by create_package
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
        # Best-effort cleanup of work dir leftovers.
        try:
            import shutil

            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass
        if owns_payload and payload_path:
            with suppress(OSError):
                os.unlink(payload_path)

    pickup_path = url_path(f"/tools/express?code={pkg['code']}", request)
    abs_url = _absolute_pickup_url(pickup_path, request)
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
            "burn_after": bool(pkg.get("burn_after")),
            "file_count": int(pkg.get("file_count") or file_count),
            "is_text": False,
            "pickup_url": pickup_path,
            "pickup_url_absolute": abs_url,
            "qr_image": _render_qr_data_uri(abs_url),
            "message": (
                f"寄送成功，取件码 {pkg['code']}"
                + ("（阅后即焚）" if pkg.get("burn_after") else "")
            ),
        }
    )


def _package_pickup_info(pkg: dict) -> dict:
    """Common response fields shared by send / send-text."""
    return {
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
        "burn_after": bool(pkg.get("burn_after")),
        "file_count": int(pkg.get("file_count") or 1),
        "is_text": bool(pkg.get("is_text")),
    }


@router.post("/send-text")
async def api_send_text(
    request: Request,
    text: str = Form(...),
    ttl_hours: str | None = Form(None),
    max_downloads: str | None = Form(None),
    note: str | None = Form(None),
    burn_after: str | None = Form(None),
):
    """小纸条：发送一段文字，对方输入取件码即可查看。"""
    hours = _parse_ttl(ttl_hours)
    burn = to_bool(burn_after, False)
    max_dl = 1 if burn else _parse_max_downloads(max_downloads)
    note_s = (note or "").strip()[:200]

    body = (text or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="请输入要发送的内容")
    if len(body) > max_text_chars():
        raise HTTPException(
            status_code=413,
            detail=f"内容过长（上限 {max_text_chars()} 字）",
        )

    try:
        pkg = create_text_package(
            body,
            ttl_hours=hours,
            max_downloads=max_dl,
            note=note_s,
            burn_after=burn,
        )
    except ValueError as exc:
        msg = str(exc)
        if "empty or missing" in msg.lower():
            msg = "请输入要发送的内容"
        elif "too long" in msg.lower():
            msg = f"内容过长（上限 {max_text_chars()} 字）"
        raise HTTPException(status_code=400, detail=msg) from exc

    pickup_path = url_path(f"/tools/express?code={pkg['code']}", request)
    abs_url = _absolute_pickup_url(pickup_path, request)
    info = _package_pickup_info(pkg)
    info["pickup_url"] = pickup_path
    info["pickup_url_absolute"] = abs_url
    info["qr_image"] = _render_qr_data_uri(abs_url)
    info["message"] = (
        f"小纸条已生成，取件码 {pkg['code']}"
        + ("（阅后即焚）" if pkg.get("burn_after") else "")
    )
    return JSONResponse(info)


@router.post("/lookup")
async def api_lookup(request: Request, code: str = Form(...)):
    """Query package metadata by code (does not consume a download)."""
    key = _guard_key(request)
    _guard_reject_if_locked(key)
    if not is_valid_code_format(code):
        raise HTTPException(status_code=400, detail="请输入 6 位数字取件码")
    info = get_package_by_code(code)
    if info is None:
        _guard_register_failure(key)
        raise HTTPException(status_code=404, detail=_ERROR_MESSAGES["invalid"])
    _guard_clear_failures(key)
    if info.get("expired"):
        raise HTTPException(status_code=410, detail=_ERROR_MESSAGES["expired"])
    if info.get("exhausted"):
        raise HTTPException(status_code=410, detail=_ERROR_MESSAGES["exhausted"])
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
            "burn_after",
            "file_count",
            "is_text",
            "text_preview",
        )
        if k in info
    }
    return JSONResponse({"ok": True, **safe})


def _read_response(request: Request, raw_code: str) -> JSONResponse:
    key = _guard_key(request)
    _guard_reject_if_locked(key)
    if not is_valid_code_format(raw_code):
        raise HTTPException(status_code=400, detail="请输入 6 位数字取件码")
    info, err = claim_text(raw_code)
    _guard_fail_or_clear(key, err)
    if err:
        status = 404 if err in ("invalid", "missing") else 410
        raise HTTPException(
            status_code=status,
            detail=_ERROR_MESSAGES.get(err, "读取失败"),
        )
    assert info is not None
    if not info.get("is_text"):
        raise HTTPException(status_code=400, detail="该取件码对应的是文件，请使用「下载文件」")
    safe = {
        k: info[k]
        for k in (
            "code",
            "original_name",
            "note",
            "burn_after",
            "is_text",
            "download_count",
            "downloads_left",
            "max_downloads",
            "text_preview",
        )
        if k in info
    }
    safe["text"] = info.get("_text") or ""
    return JSONResponse({"ok": True, **safe})


@router.post("/read")
async def api_read_post(request: Request, code: str = Form(...)):
    """Read a 小纸条 by code (consumes one download if limited)."""
    return _read_response(request, code)


@router.get("/read/{code}")
async def api_read_get(request: Request, code: str):
    """Read a 小纸条 by path code."""
    return _read_response(request, code)


def _burn_unlink(rel: str) -> None:
    from storage.express import _unlink_package_file

    if rel:
        _unlink_package_file(rel)


def _pickup_response(request: Request, raw_code: str) -> FileResponse:
    key = _guard_key(request)
    _guard_reject_if_locked(key)
    if not is_valid_code_format(raw_code):
        raise HTTPException(status_code=400, detail="请输入 6 位数字取件码")

    info, err = claim_download(raw_code)
    _guard_fail_or_clear(key, err)
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
    headers = {
        "Content-Disposition": content_disposition(name),
        "X-Express-Code": str(info.get("code") or ""),
        "X-Express-Downloads": str(info.get("download_count") or 0),
        "X-Express-Burn": "1" if info.get("burn_after") else "0",
        "Cache-Control": "no-store",
    }
    bg = None
    burn_rel = info.get("_burn_rel")
    if burn_rel:
        bg = BackgroundTask(_burn_unlink, str(burn_rel))
    return FileResponse(path, media_type=media, headers=headers, background=bg)


@router.post("/pickup")
async def api_pickup_post(request: Request, code: str = Form(...)):
    """Download by form code (consumes one download if limited)."""
    return _pickup_response(request, code)


@router.get("/pickup/{code}")
async def api_pickup_get(request: Request, code: str):
    """Download by path code (bookmarkable)."""
    return _pickup_response(request, code)
