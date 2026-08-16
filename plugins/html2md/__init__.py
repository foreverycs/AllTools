"""HTML 转 Markdown — 插件。

Converts HTML fragments / web-page source to clean CommonMark/GFM Markdown:
headings, paragraphs, nested lists, GFM tables, fenced code blocks, quotes,
links and images. The conversion core lives in ``converter.py`` (stdlib only).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import Request

from tools.common import templates, with_nav

from .converter import convert_html
from .fetcher import FetchError, fetch_page

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "html2md",
    "name": "HTML 转 Markdown",
    "name_en": "HTML to Markdown",
    "category": "text",
    "description": "将 HTML 片段或网页源码转换为干净的 Markdown：标题、列表、表格、代码块、图片与链接。",
    "icon": "📃",
    "route": "/tools/html2md",
    "badge": "HTML → MD",
    "features": ["标题/段落", "嵌套列表", "GFM 表格", "代码块/图片/链接"],
    "cta": "开始转换",
    "accent": "cyan",
}

router = APIRouter(prefix="/tools/html2md", tags=["html2md"])

# Input cap for the public endpoint (see plugin rate-limit conventions).
MAX_CHARS = 2 * 1024 * 1024


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/html2md.html",
        with_nav({
            "tool": {
                "name": "HTML 转 Markdown",
                "slug": "html2md",
                "category": "text",
            }
        }),
    )


@router.post("/convert")
async def api_convert(
    html: Optional[str] = Form(None),
    tables: str = Form("1"),
    base_url: Optional[str] = Form(None),
):
    """Convert an HTML snippet to Markdown (path contains /convert ⇒ rate-limited).

    The response includes ``rendered`` — the Markdown rendered to sanitized
    HTML (same bleach pipeline as the built-in Markdown tool) so the page can
    show a preview by default and switch to the raw source. ``rendered`` is
    ``None`` when the output is too large for the render cap (source-only).
    """
    if html is None:
        raise HTTPException(status_code=400, detail="Missing html")
    if len(html) > MAX_CHARS:
        raise HTTPException(
            status_code=413, detail="html too large (max 2M characters)"
        )
    use_tables = str(tables).strip().lower() in ("1", "true", "on", "yes")
    base = (base_url or "").strip()
    _validate_base_url(base)
    from core.concurrency import run_heavy

    data = await run_heavy(
        convert_html, html, tables=use_tables, base_url=base,
        file_size=len(html),
    )

    data["rendered"] = _render_preview(str(data["result"] or ""))
    return JSONResponse(data)


def _validate_base_url(base_url: str) -> None:
    """Reject non-http(s) base_url so urljoin cannot produce a dangerous link."""
    if not base_url:
        return
    scheme = urlsplit(base_url).scheme.lower()
    if scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400, detail="base_url 仅支持 http/https 协议"
        )


def _render_preview(md_source: str) -> Optional[str]:
    """Best-effort Markdown → sanitized HTML preview (None when too large).

    Uses the same bleach pipeline as the built-in Markdown tool, so the
    preview is XSS-safe. ``None`` means "source-only" (empty or over the cap).
    """
    if not md_source:
        return None
    try:
        from coding.markdown_render import MAX_INPUT_CHARS, render_markdown

        if len(md_source) <= MAX_INPUT_CHARS:
            return render_markdown(md_source)["html"]
    except Exception:
        return None  # preview is best-effort; source stays available
    return None


@router.post("/convert-url")
async def api_convert_url(url: str = Form(None)):
    """Fetch a public web page, extract its main content and convert it.

    Path contains ``/convert`` ⇒ rate-limited. The page is fetched with SSRF
    guardrails (see ``fetcher``); the extracted content HTML is returned so the
    page can show/edit what was captured.
    """
    if not url or not str(url).strip():
        raise HTTPException(status_code=400, detail="Missing url")
    try:
        page = await fetch_page(str(url).strip())
    except FetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    html = page["html"]
    if len(html) > MAX_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"抓取内容过大（>{MAX_CHARS // 1024 // 1024}M 字符），请改用粘贴方式",
        )
    from core.concurrency import run_heavy

    data = await run_heavy(
        convert_html, html, tables=True, base_url=page["url"],
        file_size=len(html),
    )
    data["rendered"] = _render_preview(str(data["result"] or ""))
    data["page_url"] = page["url"]
    data["page_title"] = page["title"]
    data["page_html"] = html
    data["main_extracted"] = page["main"]
    return JSONResponse(data)
