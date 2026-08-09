"""Unix 时间戳转换 — 页面与 API。"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import Request

from coding import TimestampError, convert, now_snapshot
from tools.common import check_max_chars, templates, with_nav

router = APIRouter(prefix="/tools/timestamp", tags=["timestamp"])

MAX_TEXT_CHARS = 64


def _tz_offset(raw: str) -> int:
    """Minutes east of UTC (browser offset); clamp to a sane range."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 480
    return max(-720, min(840, value))


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/timestamp.html",
        with_nav({
            "tool": {
                "name": "时间戳转换",
                "slug": "timestamp",
                "category": "text",
            }
        }),
    )


@router.post("/convert")
async def api_convert(
    value: str = Form(...),
    tz_offset: str = Form("480"),
):
    """Convert a unix timestamp or a date string to human-readable info."""
    check_max_chars(value, MAX_TEXT_CHARS, label="value")
    try:
        result = convert(value, tz_offset_min=_tz_offset(tz_offset))
    except TimestampError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@router.post("/now")
async def api_now(tz_offset: str = Form("480")):
    """Return current-moment timestamp info."""
    return JSONResponse(now_snapshot(tz_offset_min=_tz_offset(tz_offset)))
