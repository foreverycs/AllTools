"""图片加水印 — 页面与 API（文字 / Logo 水印）。"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from core.concurrency import run_heavy
from tools.common import (
    check_image_dimensions,
    check_upload_size_header,
    safe_stem,
    save_upload,
    templates,
    to_bool,
    with_nav,
)
from tools.pipeline import TempWorkspace, archive_input, map_conversion_error

from .watermark import (
    WatermarkError,
    apply_watermark,
    output_formats,
    positions,
    watermark_types,
)

router = APIRouter(prefix="/tools/image-watermark", tags=["image-watermark"])


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/image-watermark.html",
        with_nav(
            {
                "tool": {
                    "name": "图片加水印",
                    "slug": "image-watermark",
                    "category": "image",
                },
                "types": watermark_types(),
                "positions": positions(),
                "formats": output_formats(),
            }
        ),
    )


@router.get("/options")
async def api_options():
    return JSONResponse(
        {
            "types": watermark_types(),
            "positions": positions(),
            "formats": output_formats(),
            "defaults": {
                "type": "text",
                "text": "样例水印",
                "position": "bottom-right",
                "opacity": 40,
                "angle": 0,
                "repeat": False,
                "color": "#ffffff",
                "font_size_pct": 5,
                "logo_size_pct": 15,
                "format": "auto",
            },
        }
    )


def _parse_type(raw: Optional[str]) -> str:
    t = (raw or "text").strip().lower()
    if t not in watermark_types():
        raise HTTPException(
            status_code=400,
            detail=f"type must be one of: {', '.join(watermark_types())}",
        )
    return t


def _parse_format(raw: Optional[str]) -> str:
    f = (raw or "auto").strip().lower()
    if f not in output_formats():
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of: {', '.join(output_formats())}",
        )
    return f


def _parse_position(raw: Optional[str]) -> str:
    p = (raw or "bottom-right").strip().lower()
    if p not in positions():
        raise HTTPException(
            status_code=400,
            detail=f"position must be one of: {', '.join(positions())}",
        )
    return p


def _reject_oversize(path: str) -> None:
    try:
        check_image_dimensions(path)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc


def _watermark_file(
    path: str,
    *,
    filename: str,
    wm_type: str,
    text: str,
    font_size_pct: float,
    color: str,
    opacity: Optional[str],
    angle: Optional[str],
    repeat: bool,
    position: str,
    logo_path: Optional[str],
    logo_filename: Optional[str],
    logo_size_pct: float,
    fmt: str,
) -> dict:
    with open(path, "rb") as f:
        raw = f.read()
    logo_data = None
    if logo_path:
        with open(logo_path, "rb") as f:
            logo_data = f.read()
    return apply_watermark(
        raw,
        filename=filename,
        watermark_type=wm_type,
        text=text,
        font_size_pct=font_size_pct,
        color=color,
        opacity=opacity,
        angle=angle,
        repeat=repeat,
        position=position,
        logo_data=logo_data,
        logo_filename=logo_filename,
        logo_size_pct=logo_size_pct,
        fmt=fmt,
    )


def _stats_headers(result: dict) -> dict:
    headers = {
        "X-Image-Width": str(result.get("width") or 0),
        "X-Image-Height": str(result.get("height") or 0),
        "X-Image-Format": str(result.get("format") or ""),
        "X-Watermark-Type": str(result.get("watermark_type") or ""),
        "X-Watermark-Opacity": str(result.get("opacity") or 0),
        "X-Watermark-Position": str(result.get("position") or ""),
        "X-Watermark-Repeat": "1" if result.get("repeat") else "0",
        "X-Original-Bytes": str(result.get("original_bytes", 0)),
        "X-Output-Bytes": str(result.get("output_bytes", 0)),
    }
    notes = result.get("notes") or []
    if notes:
        headers["X-Watermark-Notes"] = ",".join(str(n) for n in notes)[:500]
    return headers


@router.post("/watermark")
async def api_watermark(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    type: str = Form("text"),
    text: str = Form("样例水印"),
    font_size_pct: Optional[str] = Form(None),
    color: str = Form("#ffffff"),
    opacity: Optional[str] = Form(None),
    angle: Optional[str] = Form(None),
    repeat: str = Form("0"),
    position: str = Form("bottom-right"),
    logo: Optional[UploadFile] = File(None),
    logo_size_pct: Optional[str] = Form(None),
    fmt: str = Form("auto"),
):
    """Watermark one image; returns the result as a download."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    wm_type = _parse_type(type)
    f = _parse_format(fmt)
    pos = _parse_position(position)
    rep = to_bool(repeat, False)
    check_upload_size_header(file)
    if logo is not None:
        check_upload_size_header(logo, label="logo")

    ws = TempWorkspace(prefix="wmark_")
    logo_path = None
    logo_filename = None
    try:
        work = ws.create()
        in_path = os.path.join(work, "input.bin")
        await save_upload(file, in_path)
        _reject_oversize(in_path)

        if logo is not None:
            if not logo.filename:
                raise HTTPException(status_code=400, detail="Missing logo filename")
            logo_path = os.path.join(work, "logo.bin")
            await save_upload(logo, logo_path)
            logo_filename = logo.filename

        result = await run_heavy(
            _watermark_file,
            in_path,
            filename=file.filename,
            wm_type=wm_type,
            text=text,
            font_size_pct=font_size_pct,
            color=color,
            opacity=opacity,
            angle=angle,
            repeat=rep,
            position=pos,
            logo_path=logo_path,
            logo_filename=logo_filename,
            logo_size_pct=logo_size_pct,
            fmt=f,
            file_size=os.path.getsize(in_path),
        )
    except HTTPException:
        ws.cleanup_now()
        raise
    except WatermarkError as exc:
        ws.cleanup_now()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc, label="Image watermark failed") from exc

    out_name = f"{safe_stem(file.filename)}_watermarked{result['extension']}"
    out_path = ws.join(out_name)
    try:
        with open(out_path, "wb") as out:
            out.write(result["data"])
    except Exception as exc:
        ws.cleanup_now()
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}") from exc

    try:
        await archive_input(
            tool="image-watermark",
            original_name=file.filename or out_name,
            input_path=in_path,
            extra={
                "watermark_type": wm_type,
                "position": pos,
                "repeat": rep,
                "format": result.get("format"),
                "original_bytes": result.get("original_bytes"),
                "output_bytes": result.get("output_bytes"),
            },
        )
    except Exception:
        pass

    ws.schedule_cleanup(background_tasks)
    return FileResponse(
        out_path,
        media_type=result["media_type"],
        filename=out_name,
        headers=_stats_headers(result),
        background=None,
    )
