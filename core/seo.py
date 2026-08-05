"""SEO helpers: absolute URLs, robots.txt and sitemap.xml.

All absolute-URL construction funnels through :func:`absolute_url`, which
combines ``SITE_ORIGIN`` (scheme+host) with the app-relative path returned by
:func:`tools.common.url_path` (which already honors ROOT_PATH reverse-proxy
mounts). When ``SITE_ORIGIN`` is unset the site is treated as non-indexable:
sitemap/robots stay self-consistent but pages drop to relative canonical URLs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response

from core.settings import get_settings
from tools.common import effective_root_path, url_path


def site_origin() -> str:
    """Public scheme+host (no trailing slash); ``""`` when not configured."""
    return get_settings().site_origin


def is_indexable() -> bool:
    """True when search engines are allowed to index the site."""
    s = get_settings()
    return bool(s.seo_indexable and s.site_origin)


def absolute_url(path: str, request: Optional[Request] = None) -> str:
    """Build a canonical absolute URL for ``path``.

    ``path`` is app-relative (e.g. ``/tools/pdf2word``). The ROOT_PATH prefix
    is applied via :func:`url_path` so reverse-proxy mounts are respected.

    Returns a site-relative URL (``/path``) when ``SITE_ORIGIN`` is empty;
    callers that need an absolute URL should treat ``""`` origin as
    "no absolute URL available".
    """
    relative = url_path(path, request)
    origin = site_origin()
    if not origin:
        return relative
    return urljoin(origin + "/", relative.lstrip("/"))


#/ Default OpenGraph cover image (site-relative).
DEFAULT_OG_IMAGE = "/static/icons/og-cover.png"


def og_image_url(request: Optional[Request] = None) -> str:
    """Absolute or site-relative URL for the OpenGraph cover image."""
    s = get_settings()
    rel = s.og_image_path or DEFAULT_OG_IMAGE
    return absolute_url(rel, request)


# Paths that must never appear in sitemaps and should be disallowed in robots.
# Admin console, API surface, health dashboard (ops-only), PWA internals and the
# legacy /tools/json 308 redirect are excluded from indexing.
NOINDEX_PATHS: tuple[str, ...] = (
    "/admin",
    "/api/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/manifest.webmanifest",
    "/sw.js",
)


def _robots_disallow_lines(root: str) -> List[str]:
    """``Disallow`` lines (ROOT_PATH-aware) for paths that must stay private."""
    out: List[str] = []
    for p in NOINDEX_PATHS:
        # Keep trailing slash semantics intact; join root with the literal path.
        joined = f"{root}{p}" if root else p
        out.append(joined)
    return out


def build_robots_txt(request: Optional[Request] = None) -> str:
    """Render robots.txt honoring SEO_INDEXABLE and ROOT_PATH."""
    root = effective_root_path(request)
    lines: List[str] = ["User-agent: *"]
    if not is_indexable():
        # Global noindex for staging / private deployments.
        lines.append("Disallow: /")
        lines.append("")
        return "\n".join(lines)

    for d in _robots_disallow_lines(root):
        lines.append(f"Disallow: {d}")
    lines.append("")
    # Point crawlers at the dynamic sitemap (absolute when possible).
    sitemap = absolute_url("/sitemap.xml", request)
    lines.append(f"Sitemap: {sitemap}")
    lines.append("")
    return "\n".join(lines)


def robots_response(request: Optional[Request] = None) -> PlainTextResponse:
    return PlainTextResponse(
        build_robots_txt(request),
        media_type="text/plain; charset=utf-8",
        # no-cache: stale (empty) copies must never linger for an hour.
        headers={"Cache-Control": "public, no-cache, max-age=600"},
    )


def _sitemap_entry(
    *,
    url: str,
    lastmod: Optional[str] = None,
    changefreq: str = "weekly",
    priority: float = 0.8,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"loc": url, "changefreq": changefreq, "priority": f"{priority:.1f}"}
    if lastmod:
        entry["lastmod"] = lastmod
    return entry


def build_sitemap(request: Optional[Request] = None) -> str:
    """Render sitemap.xml (homepage + enabled public tool pages).

    Only pages that are (a) indexable and (b) currently enabled in the admin
    tool-flag gate are included, so disabling a tool immediately removes its
    URL from the sitemap without a restart.
    """
    from tools import public_snapshot

    if not is_indexable():
        # An empty sitemap is valid and signals "nothing to index".
        return '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>\n'

    snap = public_snapshot()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entries: List[Dict[str, Any]] = []

    homepage = absolute_url("/", request)
    entries.append(
        _sitemap_entry(url=homepage, lastmod=today, changefreq="daily", priority=1.0)
    )

    # Regular module tools + featured tools (e.g. 文件快递) are all public pages.
    for tool in [*snap["public"], *snap["featured"]]:
        slug = str(tool.get("slug") or "").strip()
        route = str(tool.get("route") or "").strip()
        if not slug or not route:
            continue
        loc = absolute_url(route, request)
        entries.append(
            _sitemap_entry(url=loc, lastmod=today, changefreq="weekly", priority=0.8)
        )

    parts: List[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for e in entries:
        parts.append("  <url>")
        parts.append(f"    <loc>{_xml_escape(e['loc'])}</loc>")
        if e.get("lastmod"):
            parts.append(f"    <lastmod>{e['lastmod']}</lastmod>")
        parts.append(f"    <changefreq>{e['changefreq']}</changefreq>")
        parts.append(f"    <priority>{e['priority']}</priority>")
        parts.append("  </url>")
    parts.append("</urlset>")
    parts.append("")
    return "\n".join(parts)


def sitemap_response(request: Optional[Request] = None) -> Response:
    return Response(
        build_sitemap(request),
        media_type="application/xml; charset=utf-8",
        # no-cache: stale (empty) copies must never linger for an hour.
        headers={"Cache-Control": "public, no-cache, max-age=600"},
    )


def _xml_escape(s: str) -> str:
    from xml.sax.saxutils import escape

    # escaping of quotes is unnecessary for element/attribute text.
    return escape(s)

