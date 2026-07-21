"""Markdown 编辑与渲染预览 — 页面与 API。"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.requests import Request

from coding import MarkdownError, render_markdown, sample_markdown
from coding.markdown_render import MAX_INPUT_CHARS
from tools.common import templates, with_nav

router = APIRouter(prefix="/tools/markdown", tags=["markdown"])


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/markdown.html",
        with_nav({
            "tool": {
                "name": "Markdown 编辑",
                "slug": "markdown",
                "category": "coding",
            },
            "sample": sample_markdown(),
            "max_chars": MAX_INPUT_CHARS,
        }),
    )


@router.post("/render")
async def api_render(
    text: str = Form(""),
    sanitize: str = Form("true"),
):
    """Render Markdown to sanitized HTML (JSON)."""
    if text is not None and len(text) > MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"输入过长（最多 {MAX_INPUT_CHARS} 字符）",
        )
    do_sanitize = str(sanitize).strip().lower() in ("1", "true", "yes", "on", "")
    try:
        result = render_markdown(text or "", sanitize=do_sanitize)
    except MarkdownError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@router.post("/export-html")
async def api_export_html(
    text: str = Form(""),
    sanitize: str = Form("true"),
    title: str = Form("Markdown"),
):
    """Return a standalone HTML document for download."""
    if text is not None and len(text) > MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"输入过长（最多 {MAX_INPUT_CHARS} 字符）",
        )
    do_sanitize = str(sanitize).strip().lower() in ("1", "true", "yes", "on", "")
    safe_title = (title or "Markdown").strip()[:120] or "Markdown"
    # Escape title for HTML context (minimal).
    safe_title = (
        safe_title.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    try:
        result = render_markdown(text or "", sanitize=do_sanitize)
    except MarkdownError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    body = result["html"]
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{safe_title}</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC",
        "Microsoft YaHei", sans-serif;
      line-height: 1.65;
      color: #0f172a;
      max-width: 48rem;
      margin: 2rem auto;
      padding: 0 1.25rem 3rem;
    }}
    h1, h2, h3, h4 {{ line-height: 1.25; margin: 1.4em 0 0.55em; }}
    h1 {{ font-size: 1.75rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.35em; }}
    h2 {{ font-size: 1.35rem; border-bottom: 1px solid #f1f5f9; padding-bottom: 0.3em; }}
    p, ul, ol, blockquote, table, pre {{ margin: 0.75em 0; }}
    a {{ color: #4f46e5; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.9em;
      background: #f1f5f9;
      padding: 0.12em 0.35em;
      border-radius: 4px;
    }}
    pre {{
      background: #0f172a;
      color: #e2e8f0;
      padding: 1rem 1.1rem;
      border-radius: 10px;
      overflow: auto;
    }}
    pre code {{ background: transparent; padding: 0; color: inherit; }}
    blockquote {{
      margin-left: 0;
      padding: 0.35em 0 0.35em 1em;
      border-left: 4px solid #c7d2fe;
      color: #475569;
    }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #e2e8f0; padding: 0.45em 0.7em; text-align: left; }}
    th {{ background: #f8fafc; }}
    img {{ max-width: 100%; height: auto; }}
    hr {{ border: 0; border-top: 1px solid #e2e8f0; margin: 1.5em 0; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""
    return PlainTextResponse(
        doc,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="markdown-export.html"',
        },
    )


@router.get("/sample")
async def api_sample():
    return JSONResponse({"text": sample_markdown()})
