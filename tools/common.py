"""Shared helpers for tool HTTP routes: templates, uploads, naming."""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from fastapi import HTTPException, UploadFile
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.requests import Request
from urllib.parse import quote

from core.settings import get_settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def add_template_dir(path) -> None:
    """Add a directory to the shared Jinja2 template loader (for plugins).

    Plugin template folders are appended after the builtin ``templates/``, so
    builtin names win on collision and plugin pages are found by their slug.
    Called once per plugin at registry build time.
    """
    if not path or not os.path.isdir(str(path)):
        return
    from jinja2 import ChoiceLoader, FileSystemLoader

    loader = FileSystemLoader(str(path))
    current = templates.env.loader
    templates.env.loader = ChoiceLoader([current, loader]) if current else loader

_SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff.\-]+", re.UNICODE)

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


def _static_file_version(rel_path: str) -> str:
    """Return a short cache-buster for a static file under /static/."""
    rel = rel_path.lstrip("/").removeprefix("static/").lstrip("/")
    full = os.path.join(STATIC_DIR, rel.replace("/", os.sep))
    try:
        mtime = int(os.path.getmtime(full))
    except OSError:
        mtime = 0
    return f"{_ASSET_BUILD}.{mtime}" if mtime else _ASSET_BUILD


def static_url(path: str, request: Optional[Request] = None) -> str:
    """URL for a static asset with ``?v=`` cache buster.

    ``path`` may be ``/static/css/layout.css`` or ``css/layout.css``.
    """
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    if not p.startswith("/static/"):
        p = "/static" + p
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

    Ensures ``nav_items`` / ``active_nav`` so ``partials/top_nav.html`` works
    on tool pages the same way as home / category.
    """
    # Local import: tools package imports common at load time.
    from tools import get_tool_by_slug, nav_categories

    from tools import enabled_tools, featured_tools

    ctx: dict = dict(context or {})
    ctx.setdefault("nav_items", nav_categories())

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
        ctx["tools_catalog"] = build_tools_catalog()

    # Sibling tools in the same category (for quick chips under tool_nav).
    if "sibling_tools" not in ctx and tool.get("category") and slug:
        from core.tool_catalog import get_tool_category

        cat_id = str(tool["category"])
        siblings: list = []
        for t in enabled_tools(include_featured=True):
            if (get_tool_category(t.get("slug") or "") or t.get("category")) != cat_id:
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


def upload_chunk_size() -> int:
    return get_settings().upload_chunk_size


def safe_stem(filename: Optional[str], default: str = "output") -> str:
    stem = os.path.splitext(os.path.basename(filename or default))[0]
    stem = _SAFE_NAME_RE.sub("_", stem).strip("._") or default
    return stem[:80]


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
]
