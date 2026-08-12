"""图片九宫格分割 — 页面与 API（单图切块 → ZIP）。"""

from __future__ import annotations

import base64
import os
import zipfile
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from core.concurrency import run_heavy
from media import check_image_dimensions
from media.image_grid import (
    ImageGridError,
    build_grid_preview,
    max_dim,
    split_image,
    supported_formats,
)
from tools.common import (
    ZIP_MEDIA,
    check_upload_size_header,
    safe_stem,
    save_upload,
    templates,
    with_nav,
)
from tools.pipeline import TempWorkspace, archive_input, map_conversion_error

router = APIRouter(prefix="/tools/image-grid", tags=["image-grid"])

DEFAULT_ROWS = 3
DEFAULT_COLS = 3
DEFAULT_FORMAT = "png"


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/image-grid.html",
        with_nav(
            {
                "tool": {
                    "name": "图片九宫格",
                    "slug": "image-grid",
                    "category": "image",
                },
                "formats": supported_formats(),
                "max_dim": max_dim(),
            }
        ),
    )


def _parse_axis(raw: Optional[str], name: str, default: int) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be an integer") from exc
    limit = max_dim()
    if value < 1 or value > limit:
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be between 1 and {limit}",
        )
    return value


def _parse_format(raw: Optional[str]) -> str:
    f = (raw or DEFAULT_FORMAT).strip().lower()
    if f not in supported_formats():
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of: {', '.join(supported_formats())}",
        )
    return f


def _split_file(
    path: str,
    *,
    filename: str,
    rows: int,
    cols: int,
    fmt: str,
    background: str,
) -> dict:
    with open(path, "rb") as f:
        raw = f.read()
    return split_image(
        raw,
        filename=filename,
        rows=rows,
        cols=cols,
        fmt=fmt,
        background=background,
    )


def _stats_headers(result: dict) -> dict:
    return {
        "X-Rows": str(result["grid"]["rows"]),
        "X-Cols": str(result["grid"]["cols"]),
        "X-Tiles": str(result["grid"]["total"]),
        "X-Image-Width": str(result["input"]["width"]),
        "X-Image-Height": str(result["input"]["height"]),
        "X-Image-Format": str(result["format"]),
        "X-Original-Bytes": str(result.get("original_bytes", 0)),
        "X-Output-Bytes": str(result.get("output_bytes", 0)),
    }


@router.get("/options")
async def api_options():
    return JSONResponse(
        {
            "formats": supported_formats(),
            "max_dim": max_dim(),
            "defaults": {
                "rows": DEFAULT_ROWS,
                "cols": DEFAULT_COLS,
                "format": DEFAULT_FORMAT,
                "background": "#ffffff",
            },
            "presets": [
                {"rows": 2, "cols": 2, "label": "2×2 四宫格"},
                {"rows": 3, "cols": 3, "label": "3×3 九宫格"},
                {"rows": 4, "cols": 4, "label": "4×4 十六宫格"},
                {"rows": 3, "cols": 4, "label": "3×4 十二宫格"},
            ],
        }
    )


@router.post("/split")
async def api_split(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    rows: Optional[str] = Form(None),
    cols: Optional[str] = Form(None),
    fmt: str = Form(DEFAULT_FORMAT),
    background: str = Form("#ffffff"),
):
    """Split one image into a grid; returns a ZIP of the tiles."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    r = _parse_axis(rows, "rows", DEFAULT_ROWS)
    c = _parse_axis(cols, "cols", DEFAULT_COLS)
    f = _parse_format(fmt)
    bg = (background or "#ffffff").strip() or "#ffffff"
    check_upload_size_header(file)

    ws = TempWorkspace(prefix="grid_")
    try:
        work = ws.create()
        in_path = os.path.join(work, "input.bin")
        await save_upload(file, in_path)
        try:
            check_image_dimensions(in_path)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc

        result = await run_heavy(
            _split_file,
            in_path,
            filename=file.filename,
            rows=r,
            cols=c,
            fmt=f,
            background=bg,
        )
    except HTTPException:
        ws.cleanup_now()
        raise
    except ImageGridError as exc:
        ws.cleanup_now()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc, label="Image grid split failed") from exc

    stem = safe_stem(file.filename)
    zip_name = f"{stem}_{result['grid']['rows']}x{result['grid']['cols']}.zip"
    zip_path = ws.join(zip_name)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for tile in result["tiles"]:
                zf.writestr(tile["name"], tile["data"])
    except Exception as exc:
        ws.cleanup_now()
        raise HTTPException(status_code=500, detail=f"Zip write failed: {exc}") from exc

    try:
        await archive_input(
            tool="image-grid",
            original_name=file.filename or zip_name,
            input_path=in_path,
            extra={
                "rows": r,
                "cols": c,
                "tiles": result["grid"]["total"],
                "format": f,
                "original_bytes": result.get("original_bytes"),
                "output_bytes": result.get("output_bytes"),
            },
        )
    except Exception:
        pass

    ws.schedule_cleanup(background_tasks)
    return FileResponse(
        zip_path,
        media_type=ZIP_MEDIA,
        filename=zip_name,
        headers=_stats_headers(result),
        background=None,
    )


@router.post("/preview")
async def api_preview(
    file: UploadFile = File(...),
    rows: Optional[str] = Form(None),
    cols: Optional[str] = Form(None),
):
    """Return JSON metadata + a base64 PNG preview with grid lines drawn."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    r = _parse_axis(rows, "rows", DEFAULT_ROWS)
    c = _parse_axis(cols, "cols", DEFAULT_COLS)
    check_upload_size_header(file)

    raw = await file.read()
    limit = None
    try:
        from tools.common import max_upload_bytes

        limit = max_upload_bytes()
        if len(raw) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"File too large (max {limit // (1024 * 1024)} MB)",
            )
    except HTTPException:
        raise
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        preview = await run_heavy(
            build_grid_preview,
            raw,
            filename=file.filename,
            rows=r,
            cols=c,
        )
    except ImageGridError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise map_conversion_error(exc, label="Image grid preview failed") from exc

    b64 = base64.b64encode(preview).decode("ascii")
    return JSONResponse(
        {
            "rows": r,
            "cols": c,
            "tiles": r * c,
            "preview": f"data:image/png;base64,{b64}",
        }
    )
