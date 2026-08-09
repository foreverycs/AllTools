"""文本行处理 — 示例插件。

Demonstrates the plugin contract: a ``TOOL`` manifest dict plus a FastAPI
``router`` (see ``core.plugins``). The page template lives in this package's
``templates/`` directory.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import Request

from tools.common import templates, with_nav

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "text-lines",
    "name": "文本行处理",
    "name_en": "Text Lines",
    "category": "text",
    "description": "对多行文本按行去重、去除空行、反转行序或排序（示例插件）。",
    "icon": "📋",
    "route": "/tools/text-lines",
    "badge": "插件示例",
    "features": ["行去重", "去空行", "反转", "排序"],
    "cta": "开始处理",
    "accent": "cyan",
}

router = APIRouter(prefix="/tools/text-lines", tags=["text-lines"])

MAX_CHARS = 1024 * 1024


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/text-lines.html",
        with_nav({
            "tool": {
                "name": "文本行处理",
                "slug": "text-lines",
                "category": "text",
            }
        }),
    )


@router.post("/process")
async def api_process(
    text: Optional[str] = Form(None),
    dedupe: str = Form("0"),
    strip_empty: str = Form("0"),
    reverse: str = Form("0"),
    sort: str = Form("0"),
):
    if text is None:
        raise HTTPException(status_code=400, detail="Missing text")
    if len(text) > MAX_CHARS:
        raise HTTPException(status_code=413, detail="text too large")

    lines = text.splitlines()
    input_lines = len(lines)
    if strip_empty in ("1", "true", "on", "yes"):
        lines = [ln for ln in lines if ln.strip() != ""]
    if dedupe in ("1", "true", "on", "yes"):
        seen = set()
        unique = []
        for ln in lines:
            if ln not in seen:
                seen.add(ln)
                unique.append(ln)
        lines = unique
    if reverse in ("1", "true", "on", "yes"):
        lines = list(reversed(lines))
    if sort in ("1", "true", "on", "yes"):
        lines = sorted(lines)

    return JSONResponse({
        "result": "\n".join(lines),
        "lines": len(lines),
        "input_lines": input_lines,
    })
