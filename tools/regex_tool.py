"""正则表达式测试工具 — 页面与 API。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import Request

from coding import RegexError, generate_regex, replace_regex, test_regex
from tools.common import check_max_chars, templates, with_nav

router = APIRouter(prefix="/tools/regex", tags=["regex"])

MAX_TEXT_CHARS = 2 * 1024 * 1024  # 2M chars
MAX_PATTERN_CHARS = 64 * 1024


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/regex.html",
        with_nav({
            "tool": {
                "name": "正则测试",
                "slug": "regex",
                "category": "coding",
            }
        }),
    )


@router.post("/test")
async def api_test(
    pattern: str = Form(...),
    text: str = Form(...),
    flags: Optional[str] = Form(""),
    count: int = Form(0),
):
    """Test a regex against text and report matches / capture groups."""
    check_max_chars(pattern, MAX_PATTERN_CHARS, label="pattern")
    check_max_chars(text, MAX_TEXT_CHARS, label="text")
    try:
        result = test_regex(pattern, text, flags_raw=flags, count=int(count))
    except RegexError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@router.post("/generate")
async def api_generate(
    text: str = Form(...),
    target: str = Form(...),
    flags: Optional[str] = Form(""),
    anchor: Optional[str] = Form("auto"),
):
    """Synthesize a regex to extract ``target`` from ``text``."""
    check_max_chars(text, MAX_TEXT_CHARS, label="text")
    check_max_chars(target, MAX_PATTERN_CHARS, label="target")
    try:
        result = generate_regex(text, target, flags_raw=flags, anchor=anchor)
    except RegexError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@router.post("/replace")
async def api_replace(
    pattern: str = Form(...),
    text: str = Form(...),
    replacement: str = Form(""),
    flags: Optional[str] = Form(""),
    count: int = Form(0),
):
    """Replace regex matches in text."""
    check_max_chars(pattern, MAX_PATTERN_CHARS, label="pattern")
    check_max_chars(text, MAX_TEXT_CHARS, label="text")
    check_max_chars(replacement, MAX_PATTERN_CHARS, label="replacement")
    try:
        result = replace_regex(
            pattern, text, replacement, flags_raw=flags, count=int(count)
        )
    except RegexError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)
