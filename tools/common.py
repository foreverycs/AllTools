"""Shared helpers for tool HTTP routes: templates, uploads, naming.

Also hosts cross-plugin image helpers (dimension guard + format detection)
so image plugins stay self-contained and the ``media`` package can be removed.
"""

from __future__ import annotations

import io
import os
import re
import threading
import time
from typing import Any, List, Optional, Tuple

from fastapi import HTTPException, UploadFile
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from PIL import Image
from starlette.requests import Request
from urllib.parse import quote

from core.settings import get_settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Guards loader rebuilds (plugin hot reload runs in a background thread while
# requests render templates concurrently). The swap itself is a single atomic
# attribute assignment, but rebuilding the loader chain must not interleave
# with another reload.
_plugin_loader_lock = threading.RLock()


def set_plugin_template_dirs(dirs) -> None:
    """Rebuild the Jinja2 loader with the current plugin template folders.

    Called at startup and on plugin hot reload, so added/removed plugin
    template dirs take effect without a restart. Builtin ``templates/`` always
    comes first and wins on name collisions.
    """
    from jinja2 import ChoiceLoader, FileSystemLoader

    with _plugin_loader_lock:
        loaders = [FileSystemLoader(TEMPLATES_DIR)]
        for d in dirs or ():
            if os.path.isdir(str(d)):
                loaders.append(FileSystemLoader(str(d)))
        templates.env.loader = ChoiceLoader(loaders)

# Bump when shipping CSS/JS that must invalidate CDN/browser caches.
# Also mixed with file mtime so local edits bust cache without code changes.
_ASSET_BUILD = os.environ.get("STATIC_ASSET_VERSION") or "20260715c"


def effective_root_path(request: Optional[Request] = None) -> str:
    """App mount prefix for reverse proxies (ROOT_PATH or ASGI root_path)."""
    if request is not None:
        scoped = (request.scope.get("root_path") or "").rstrip("/")
        if scoped:
            return scoped if scoped.startswith("/") else f"/{scoped}"
    return get_settings().root_path


def join_url(root: str, path: str) -> str:
    """Join root prefix with an absolute app path (``/static/...``)."""
    if not path:
        return root or "/"
    if path.startswith(("http://", "https://", "//")):
        return path
    if not path.startswith("/"):
        path = "/" + path
    root = (root or "").rstrip("/")
    return f"{root}{path}" if root else path


def url_path(path: str, request: Optional[Request] = None) -> str:
    """Build a browser path that respects reverse-proxy subpath mounts."""
    return join_url(effective_root_path(request), path)


# Short TTL so frequent template renders (which call static_url for many
# assets) dedupe the per-file stat() without hiding local edits for long.
_VER_CACHE_TTL = 5.0  # seconds
_ver_cache: dict = {}
_ver_cache_lock = threading.Lock()

# When True, ``static_url`` rewrites ``foo.css`` → ``foo.min.css`` if the
# minified sibling exists on disk. Disabled by default in dev (no min files);
# production images run ``scripts/minify_static.py`` so the rewrite kicks in
# automatically without touching templates.
_USE_MIN_ASSETS = (os.environ.get("USE_MIN_ASSETS") or "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _min_rel(rel: str) -> str:
    """Return the ``.min.<ext>`` sibling path for ``rel`` (no existence check)."""
    stem, dot, ext = rel.rpartition(".")
    if not dot or not ext or rel.endswith(f".min.{ext}"):
        return rel
    return f"{stem}.min.{ext}"


def _resolve_static(rel: str) -> str:
    """Return the rel path to serve: ``.min`` sibling if it exists, else ``rel``.

    Cached alongside the mtime lookup so repeated renders do not re-stat the
    min sibling on every request. Returns ``rel`` unchanged when minification
    is disabled or the sibling is absent.
    """
    if not _USE_MIN_ASSETS:
        return rel
    now = time.monotonic()
    with _ver_cache_lock:
        hit = _ver_cache.get(("rel", rel))
        if hit is not None and now - hit[0] < _VER_CACHE_TTL:
            return hit[1]
    candidate = _min_rel(rel)
    if candidate != rel:
        full = os.path.join(STATIC_DIR, candidate.replace("/", os.sep))
        resolved = candidate if os.path.isfile(full) else rel
    else:
        resolved = rel
    with _ver_cache_lock:
        _ver_cache[("rel", rel)] = (now, resolved)
    return resolved


def _static_file_version(rel_path: str) -> str:
    """Return a short cache-buster for a static file under /static/.

    The mtime lookup is cached for ``_VER_CACHE_TTL`` seconds; dev edits are
    still picked up (and uvicorn --reload restarts the process anyway).
    """
    rel = rel_path.lstrip("/").removeprefix("static/").lstrip("/")
    now = time.monotonic()
    with _ver_cache_lock:
        hit = _ver_cache.get(rel)
        if hit is not None and now - hit[0] < _VER_CACHE_TTL:
            return hit[1]
    full = os.path.join(STATIC_DIR, rel.replace("/", os.sep))
    try:
        mtime = int(os.path.getmtime(full))
    except OSError:
        mtime = 0
    ver = f"{_ASSET_BUILD}.{mtime}" if mtime else _ASSET_BUILD
    with _ver_cache_lock:
        _ver_cache[rel] = (now, ver)
    return ver


def static_url(path: str, request: Optional[Request] = None) -> str:
    """URL for a static asset with ``?v=`` cache buster.

    ``path`` may be ``/static/css/layout.css`` or ``css/layout.css``.

    When ``USE_MIN_ASSETS`` is on and a ``.min.<ext>`` sibling exists next to
    the requested file, the URL is rewritten to the minified variant — so
    templates keep referencing the source filename and production automatically
    serves the compressed version produced by ``scripts/minify_static.py``.
    """
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    if not p.startswith("/static/"):
        p = "/static" + p
    rel = p.lstrip("/").removeprefix("static/").lstrip("/")
    rel = _resolve_static(rel)
    if rel:
        p = "/static/" + rel
    base = url_path(p, request)
    ver = _static_file_version(p)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}v={ver}"


@pass_context
def _jinja_url_path(ctx: Any, path: str) -> str:
    request = ctx.get("request")
    if request is not None and not isinstance(request, Request):
        request = None
    return url_path(path, request)


@pass_context
def _jinja_static_url(ctx: Any, path: str) -> str:
    request = ctx.get("request")
    if request is not None and not isinstance(request, Request):
        request = None
    return static_url(path, request)


@pass_context
def _jinja_root_path(ctx: Any) -> str:
    request = ctx.get("request")
    if request is not None and not isinstance(request, Request):
        request = None
    return effective_root_path(request)


# Available in all templates:
#   {{ url_path('/tools/pdf2word') }}
#   {{ static_url('/static/css/layout.css') }}
templates.env.globals["url_path"] = _jinja_url_path
templates.env.globals["static_url"] = _jinja_static_url
templates.env.globals["root_path"] = _jinja_root_path


@pass_context
def _jinja_seo_absolute_url(ctx: Any, path: str) -> str:
    """Absolute (or site-relative) URL honoring SITE_ORIGIN + ROOT_PATH."""
    from core.seo import absolute_url

    request = ctx.get("request")
    if request is not None and not isinstance(request, Request):
        request = None
    return absolute_url(path, request)


@pass_context
def _jinja_og_image_url(ctx: Any) -> str:
    from core.seo import og_image_url

    request = ctx.get("request")
    if request is not None and not isinstance(request, Request):
        request = None
    return og_image_url(request)


@pass_context
def _jinja_robots_meta(ctx: Any) -> str:
    """Per-page robots meta (respect global indexability)."""
    from core.seo import is_indexable

    return "index,follow,max-image-preview:large" if is_indexable() else "noindex,follow"


templates.env.globals["seo_url"] = _jinja_seo_absolute_url
templates.env.globals["og_image_url"] = _jinja_og_image_url
templates.env.globals["robots_meta"] = _jinja_robots_meta


def build_tools_catalog(
    *, include_featured: bool = True
) -> list:
    """Return a flat catalog of enabled tools for homepage / palette.

    Merges regular and featured tools, deduped by slug, with a stable
    set of display fields.
    """
    from tools import enabled_tools, featured_tools
    from core.tool_catalog import get_tool_category

    seen: set = set()
    catalog: list = []
    for t in list(enabled_tools(include_featured=include_featured)) + list(
        featured_tools()
    ):
        slug = str(t.get("slug") or "")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        catalog.append(
            {
                "slug": slug,
                "name": t.get("name"),
                "route": t.get("route"),
                "icon": t.get("icon"),
                "description": t.get("description"),
                "accent": t.get("accent") or "indigo",
                "category": get_tool_category(slug) or t.get("category"),
            }
        )
    return catalog


def with_nav(
    context: Optional[dict] = None,
    *,
    active_nav: Optional[str] = None,
) -> dict:
    """Merge top-nav context into a tool (or other) page template dict.

    Ensures ``nav_items`` / ``active_nav`` so ``partials/pill_nav.html`` works
    on tool pages the same way as home / category.

    Nav items and the command-palette catalog come from the cached public
    snapshot (``public_snapshot``), which is rebuilt only when admin enable/
    disable flags or category overrides change — tool page renders no longer
    rebuild the whole category/nav lists per request.
    """
    # Local import: tools package imports common at load time.
    from tools import get_tool_by_slug, public_snapshot

    ctx: dict = dict(context or {})
    if "nav_items" not in ctx:
        ctx["nav_items"] = public_snapshot()["nav"]

    tool = ctx.get("tool")
    if not isinstance(tool, dict):
        tool = {}
    slug = str(tool.get("slug") or "").strip()
    if slug:
        reg = get_tool_by_slug(slug) or {}
        from core.tool_catalog import get_tool_category

        eff_cat = get_tool_category(slug) or tool.get("category") or reg.get("category")
        # Fill missing fields from registry (route/icon needed for recent tools).
        merged = {
            "name": tool.get("name") or reg.get("name") or slug,
            "slug": slug,
            "category": eff_cat,
            "icon": tool.get("icon") or reg.get("icon") or "🔧",
            "route": tool.get("route") or reg.get("route") or f"/tools/{slug}",
            "description": tool.get("description") or reg.get("description") or "",
            "accent": tool.get("accent") or reg.get("accent") or "indigo",
            "featured": bool(tool.get("featured") or reg.get("featured")),
        }
        tool = merged
        ctx["tool"] = tool

    if active_nav:
        ctx["active_nav"] = active_nav
    elif tool.get("featured") or slug == "express":
        ctx["active_nav"] = "home"
    elif tool.get("category"):
        ctx["active_nav"] = str(tool["category"])
    else:
        ctx.setdefault("active_nav", "home")

    # Flat catalog for command palette (same shape as homepage tools_catalog).
    if "tools_catalog" not in ctx:
        ctx["tools_catalog"] = public_snapshot()["catalog"]

    # Sibling tools in the same category (for quick chips under tool_nav).
    if "sibling_tools" not in ctx and tool.get("category") and slug:
        cat_id = str(tool["category"])
        # Snapshot catalog entries carry the *effective* category (admin
        # override or registry default) and include featured tools, matching
        # the previous enabled_tools(include_featured=True) scan.
        siblings: list = []
        for t in public_snapshot()["catalog"]:
            if t.get("category") != cat_id:
                continue
            if str(t.get("slug") or "") == slug:
                continue
            siblings.append(
                {
                    "slug": t.get("slug"),
                    "name": t.get("name"),
                    "route": t.get("route"),
                    "icon": t.get("icon"),
                    "accent": t.get("accent") or "indigo",
                }
            )
        # Featured express lives under office but is not a sibling of office modules.
        if tool.get("featured") or slug == "express":
            siblings = []
        ctx["sibling_tools"] = siblings[:6]
    elif "sibling_tools" not in ctx:
        ctx["sibling_tools"] = []
    return ctx

# Media types
DOCX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
PDF_MEDIA = "application/pdf"
ZIP_MEDIA = "application/zip"


def max_upload_bytes() -> int:
    return get_settings().max_upload_bytes


def max_batch_files() -> int:
    return get_settings().max_batch_files


def max_batch_bytes() -> int:
    """Cumulative byte cap for batch upload endpoints."""
    return get_settings().max_batch_bytes


def upload_chunk_size() -> int:
    return get_settings().upload_chunk_size


def safe_stem(filename: Optional[str], default: str = "output") -> str:
    from core.filename import safe_stem as _core_safe_stem

    return _core_safe_stem(filename, default)


def content_disposition(
    name: Optional[str],
    disposition: str = "attachment",
    *,
    fallback: str = "download",
) -> str:
    """Latin-1-safe ``Content-Disposition`` with an RFC 5987 UTF-8 fallback.

    Starlette encodes header values as latin-1; raw CJK (etc.) inside
    ``filename="..."`` raises ``UnicodeEncodeError`` → HTTP 500. This single
    helper keeps the escaping consistent across downloads (app / express) and
    inline previews (admin).
    """
    raw = (name or fallback).replace("\\", "_").replace("/", "_").replace('"', "")
    ascii_name = "".join(
        ch if 32 <= ord(ch) < 127 and ch not in '\\";' else "_" for ch in raw
    ).strip("._") or fallback
    while "__" in ascii_name:
        ascii_name = ascii_name.replace("__", "_")
    return (
        f'{disposition}; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(raw, safe='')}"
    )


def to_bool(value: Any, default: bool = False) -> bool:
    """Parse a form/query value as a boolean (1/true/yes/on → True).

    ``value`` may be ``None`` (→ ``default``) or a raw form/query string.
    """
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def check_max_chars(text: Optional[str], max_chars: int, *, label: str = "input") -> None:
    """Raise 400/413 when ``text`` is missing or exceeds ``max_chars``."""
    if text is None:
        raise HTTPException(status_code=400, detail=f"Missing {label}")
    if len(text) > max_chars:
        raise HTTPException(
            status_code=413,
            detail=f"{label} too large (max {max_chars} characters)",
        )


async def save_upload(
    file: UploadFile,
    dest: str,
    *,
    max_bytes: Optional[int] = None,
) -> int:
    """Stream an upload to ``dest``, enforcing size limit. Returns byte count."""
    limit = max_bytes if max_bytes is not None else max_upload_bytes()
    chunk_size = upload_chunk_size()
    total = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (max {limit // (1024 * 1024)} MB)",
                    )
                out.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="Empty file")
    except HTTPException:
        # Do not leave a partially-written file behind on the limit path.
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    return total


def check_upload_size_header(
    file: UploadFile,
    *,
    label: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> None:
    """Reject early when Content-Length / starlette size exceeds the limit."""
    limit = max_bytes if max_bytes is not None else max_upload_bytes()
    if file.size is not None and file.size > limit:
        name = label or file.filename or "file"
        raise HTTPException(
            status_code=413,
            detail=f"{name}: 文件过大（上限 {limit // (1024 * 1024)} MB）",
        )


async def check_batch_total(
    files: List[UploadFile],
    *,
    max_bytes: Optional[int] = None,
    label: str = "批量上传",
) -> int:
    """Enforce a cumulative size cap across a batch of uploads.

    Uses each file's declared ``size`` (Content-Length); returns the declared
    total. Endpoints that later stream files to disk should also track the
    streamed total (see ``save_upload``) in case a spool reports a smaller size.
    """
    limit = max_bytes if max_bytes is not None else max_batch_bytes()
    total = 0
    for f in files or []:
        if f.size is not None:
            total += f.size
    if total > limit:
        raise HTTPException(
            status_code=413,
            detail=f"{label}总大小超出上限（{limit // (1024 * 1024)} MB）",
        )
    return total


# ---------------------------------------------------------------------------
# Cross-plugin image helpers (replaces the former ``media`` package)
# ---------------------------------------------------------------------------

# App-level raster dimension cap (pixels). Pillow's built-in decompression
# bomb threshold is much higher (~179M) — decoding an image up to that limit
# can still allocate hundreds of MB per request, so bound it earlier.
# ~ 8000x4000 / 5657x5657 — bounded well below Pillow's decompression-bomb
# threshold (~179M px) so decoding a single image stays under ~128MB RGBA.
MAX_IMAGE_PIXELS = 32_000_000

# Common raster formats accepted by the shared detector.
IMAGE_INPUT_FORMATS = ("jpeg", "png", "gif", "webp", "bmp", "tiff", "ico")

_SVG_RE = re.compile(
    rb"^\s*(?:<\?xml\b[^>]*>\s*)?(?:<!--.*?-->\s*)*<svg\b",
    re.IGNORECASE | re.DOTALL,
)


class ImageFormatError(ValueError):
    """Raised when input cannot be processed (bad format / corrupt data)."""


def check_image_dimensions(
    path: str, *, head_bytes: int = 16 * 1024
) -> Optional[Tuple[int, int]]:
    """Header-only raster dimension check before any heavy decoding.

    Reads only the first ``head_bytes`` (dimensions live in the file header for
    PNG/GIF/BMP/ICO/WebP and the JPEG SOF marker) and rejects oversized images.
    Non-raster or unreadable inputs are skipped — the worker produces the real
    error. Returns ``(width, height)`` when the header parsed, else None.

    Raises ``ValueError`` when ``width * height`` exceeds ``MAX_IMAGE_PIXELS``.
    """
    with open(path, "rb") as f:
        head = f.read(head_bytes)
    if not head:
        raise ValueError("empty image file")
    try:
        with Image.open(io.BytesIO(head)) as im:
            w, h = im.size
    except Exception:
        return None
    if (w or 0) * (h or 0) > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"image too large ({w}x{h}); max {MAX_IMAGE_PIXELS} pixels"
        )
    return int(w or 0), int(h or 0)


def image_input_formats() -> List[str]:
    """Common raster formats accepted by the shared detector."""
    return list(IMAGE_INPUT_FORMATS)


def detect_image_format(data: bytes, filename: Optional[str] = None) -> str:
    """Return one of ``IMAGE_INPUT_FORMATS`` or raise ``ImageFormatError``."""
    if not data:
        raise ImageFormatError("Empty file")

    name = (filename or "").lower()
    head = data[:32]

    # Magic numbers first.
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    # WebP: RIFF....WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM":
        return "bmp"
    # TIFF little/big endian
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    # ICO / CUR
    if head[:4] in (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"):
        return "ico"

    # Reject SVG early with a clear message (no native rasterizer here).
    if _SVG_RE.match(data[:4096] if len(data) > 4096 else data):
        raise ImageFormatError(
            "SVG is not supported for raster image processing. "
            "Export to PNG/JPEG first, or use a vector editor."
        )

    # Extension fallback.
    if name.endswith((".jpg", ".jpeg", ".jpe", ".jfif")):
        return "jpeg"
    if name.endswith(".png"):
        return "png"
    if name.endswith(".gif"):
        return "gif"
    if name.endswith(".webp"):
        return "webp"
    if name.endswith(".bmp"):
        return "bmp"
    if name.endswith((".tif", ".tiff")):
        return "tiff"
    if name.endswith((".ico", ".cur")):
        return "ico"
    if name.endswith((".svg", ".svgz")):
        raise ImageFormatError(
            "SVG is not supported for raster image processing. "
            "Export to PNG/JPEG first, or use a vector editor."
        )

    raise ImageFormatError(
        f"Unsupported image format. Use one of: {', '.join(IMAGE_INPUT_FORMATS)}."
    )


__all__ = [
    "BASE_DIR",
    "TEMPLATES_DIR",
    "templates",
    "DOCX_MEDIA",
    "PDF_MEDIA",
    "ZIP_MEDIA",
    "max_upload_bytes",
    "max_batch_files",
    "upload_chunk_size",
    "safe_stem",
    "content_disposition",
    "to_bool",
    "check_max_chars",
    "save_upload",
    "check_upload_size_header",
    "effective_root_path",
    "join_url",
    "url_path",
    "static_url",
    "build_tools_catalog",
    "MAX_IMAGE_PIXELS",
    "IMAGE_INPUT_FORMATS",
    "ImageFormatError",
    "check_image_dimensions",
    "image_input_formats",
    "detect_image_format",
]
