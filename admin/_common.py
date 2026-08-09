"""Shared helpers for the admin console routers.

Holds the template/redirect helpers, the engine-health cache with its background
warm thread, the upload-preview conversion helpers, and small utilities used by
the per-domain route modules under ``admin/routes_*.py``.
"""

from __future__ import annotations

import time
from functools import wraps
from pathlib import Path
from typing import Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from admin.auth import COOKIE_NAME as SESSION_COOKIE
from admin.auth import is_admin, require_admin, verify_session_token
from admin.csrf import (
    FIELD_NAME as CSRF_FIELD,
    bound_csrf_token,
    get_or_create_csrf_token,
    set_csrf_cookie,
    verify_csrf,
)
from core.errors import ToolkitError
from core.version import __version__
from tools import TOOL_REGISTRY
from tools.common import templates

# NOTE: tags list closes with ], then APIRouter call closes with )

# Cached health info — engines don't change at runtime
_health_cache: dict = {}
_health_cache_ts: float = 0.0
_HEALTH_TTL: float = 300.0
_health_warming: bool = False


def _tpl(request: Request, name: str, **ctx):
    csrf = get_or_create_csrf_token(request)
    # While an admin session is active, derive the CSRF value from the session
    # token so forms are tied to that specific login.
    session_token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if verify_session_token(session_token):
        csrf = bound_csrf_token(session_token)
    data = {
        "request": request,
        "is_admin": is_admin(request),
        "app_version": __version__,
        "csrf_token": csrf,
        "csrf_field": CSRF_FIELD,
        **ctx,
    }
    resp = templates.TemplateResponse(request, name, data)
    set_csrf_cookie(resp, csrf)
    return resp


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def admin_post(fn):
    """Require an admin session and a valid CSRF token for a POST handler.

    The decorated function must declare ``request`` (first) and
    ``csrf_token: Optional[str] = Form(None)`` parameters; FastAPI resolves
    them via the wrapped signature. Login/logout stay custom (they use the
    pre-session double-submit path).
    """

    @wraps(fn)
    async def _guarded(request: Request, *args, **kwargs):
        redir = require_admin(request)
        if redir:
            return redir
        if not verify_csrf(request, kwargs.get("csrf_token")):
            raise HTTPException(status_code=403, detail="CSRF validation failed")
        return await fn(request, *args, **kwargs)

    return _guarded


def _safe_next(next_url: Optional[str], request: Optional[Request] = None) -> str:
    from tools.common import effective_root_path, url_path

    root = effective_root_path(request)
    admin_home = url_path("/admin", request)
    if not next_url:
        return admin_home
    # Allow both app-absolute and root-prefixed paths.
    allowed_prefixes = ("/admin",)
    if root:
        allowed_prefixes = (f"{root}/admin", "/admin")
    if (
        any(next_url.startswith(p) for p in allowed_prefixes)
        and "://" not in next_url
        and "\\" not in next_url
    ):
        return next_url
    return admin_home


def _admin_url(path: str, request: Optional[Request] = None) -> str:
    from tools.common import url_path

    return url_path(path, request)


def get_cached_health() -> Optional[dict]:
    """Return warm health snapshot without probing engines (may be None)."""
    if not _health_cache:
        return None
    if time.monotonic() - _health_cache_ts >= _HEALTH_TTL:
        return None
    return _health_cache


def schedule_health_warm() -> None:
    """Fire-and-forget engine probe so the first admin click is not blocked."""
    global _health_warming
    if _health_warming:
        return
    if get_cached_health() is not None:
        return
    _health_warming = True

    def _run() -> None:
        global _health_warming
        try:
            _build_health(force=True)
        except Exception:
            pass
        finally:
            _health_warming = False

    import threading

    threading.Thread(target=_run, name="admin-health-warm", daemon=True).start()


def _build_health(*, force: bool = False) -> dict:
    global _health_cache, _health_cache_ts
    now = time.monotonic()
    if (
        not force
        and _health_cache
        and now - _health_cache_ts < _HEALTH_TTL
    ):
        return _health_cache
    from word2pdf import engine_info
    from converter import ocr_info

    from core.tool_flags import flags_status
    from tools import tools_by_category

    w2p = engine_info(force=force)
    ocr = ocr_info()
    flags = flags_status()
    _health_cache = {
        "word2pdf": w2p,
        "ocr": ocr,
        "tools": flags["enabled_count"],
        "tools_registered": len(TOOL_REGISTRY),
        "tools_disabled": len(flags["disabled"]),
        "categories": len(tools_by_category()),
    }
    _health_cache_ts = now
    return _health_cache


def bust_health_cache() -> None:
    """Invalidate the cached engine-health snapshot (after flag/category edits)."""
    global _health_cache, _health_cache_ts
    _health_cache = {}
    _health_cache_ts = 0.0


def _bust_public_and_health() -> None:
    """Invalidate the public catalog snapshot and admin health caches."""
    from tools import clear_public_snapshot

    clear_public_snapshot()
    bust_health_cache()


def _sanitize_category_id(value: str) -> str:
    """Category id must be a safe, short identifier."""
    import re

    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "", (value or "").strip())[:40]
    if not cleaned or cleaned in ("_other",):
        return ""
    return cleaned


# ---------------------------------------------------------------------------
# Upload preview helpers (Word → PDF cache, inline content-disposition)
# ---------------------------------------------------------------------------

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

# Extensions whose preview would execute active content in the browser
# (stored-XSS vector). These are always served as downloads, never inline.
_PREVIEW_FORCE_DOWNLOAD_EXTS = {".html", ".htm", ".svg"}

# Word docs are rendered to PDF (via word2pdf) for browser inline preview.
_WORD_PREVIEW_EXTS = {".docx", ".doc"}
_PREVIEW_CACHE_SUFFIX = ".preview.pdf"


def _content_disposition_inline(filename: str) -> str:
    """Inline ``Content-Disposition`` (delegates to the shared safe builder)."""
    from tools.common import content_disposition

    return content_disposition(filename, "inline", fallback="preview")


def _word_preview_cache_path(src: Path) -> Path:
    """Disk cache path for a Word→PDF preview (next to the archived input)."""
    return Path(str(src) + _PREVIEW_CACHE_SUFFIX)


def _word_preview_cache_fresh(src: Path, cache: Path) -> bool:
    """True if cache exists and is newer than (or same age as) the source."""
    try:
        if not cache.is_file() or cache.stat().st_size <= 0:
            return False
        return cache.stat().st_mtime >= src.stat().st_mtime
    except OSError:
        return False


def _convert_word_preview(src: Path, cache: Path) -> Tuple[str, str]:
    """Sync helper: convert Word → PDF into ``cache`` (atomic replace)."""
    import os

    from word2pdf import convert_to_pdf

    # Write to a sibling temp file then rename so partial converts never
    # pollute a previously-good cache.
    tmp = cache.with_suffix(cache.suffix + ".tmp")
    try:
        if tmp.is_file():
            tmp.unlink()
    except OSError:
        pass
    pdf_path, engine = convert_to_pdf(str(src), str(tmp))
    out = Path(pdf_path)
    if not out.is_file() or out.stat().st_size <= 0:
        raise ToolkitError("Word preview conversion produced an empty PDF")
    os.replace(str(out), str(cache))
    return str(cache), engine


async def _word_preview_pdf_response(
    path: Path, *, original_name: str
) -> FileResponse:
    """Convert .doc/.docx to PDF (cached) and return an inline FileResponse."""
    cache = _word_preview_cache_path(path)
    if not _word_preview_cache_fresh(path, cache):
        from core.concurrency import run_conversion

        try:
            await run_conversion(_convert_word_preview, path, cache)
        except ToolkitError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Word preview failed: {exc}",
            ) from exc
    if not cache.is_file():
        raise HTTPException(status_code=500, detail="Word preview cache missing")

    stem = Path(original_name or path.name).stem or "preview"
    preview_name = f"{stem}.pdf"
    return FileResponse(
        cache,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _content_disposition_inline(preview_name),
            "X-Preview-Source": "word",
            "Cache-Control": "private, max-age=300",
        },
    )
