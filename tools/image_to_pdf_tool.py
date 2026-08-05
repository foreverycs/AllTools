"""图片转 PDF — 页面与 API（多图合成单 PDF）。"""

from __future__ import annotations

import os
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from core.concurrency import run_heavy
from media.image_to_pdf import (
    ImageToPdfError,
    images_to_pdf,
    input_formats,
    max_images,
    orientations,
    page_modes,
)
from tools.common import (
    check_upload_size_header,
    safe_stem,
    save_upload,
    templates,
    with_nav,
)
from tools.pipeline import TempWorkspace, archive_input, map_conversion_error

router = APIRouter(prefix="/tools/image-to-pdf", tags=["image-to-pdf"])


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/image-to-pdf.html",
        with_nav(
            {
                "tool": {
                    "name": "图片转 PDF",
                    "slug": "image-to-pdf",
                    "category": "office",
                },
                "input_formats": input_formats(),
                "page_modes": page_modes(),
                "orientations": orientations(),
                "max_images": max_images(),
            }
        ),
    )


def _parse_page_mode(raw: Optional[str]) -> str:
    m = (raw or "fit").strip().lower()
    if m not in page_modes():
        raise HTTPException(
            status_code=400,
            detail=f"page_mode must be one of: {', '.join(page_modes())}",
        )
    return m


def _parse_orientation(raw: Optional[str]) -> str:
    o = (raw or "auto").strip().lower()
    aliases = {
        "v": "portrait",
        "vertical": "portrait",
        "port": "portrait",
        "h": "landscape",
        "horizontal": "landscape",
        "land": "landscape",
    }
    o = aliases.get(o, o)
    if o not in orientations():
        raise HTTPException(
            status_code=400,
            detail=f"orientation must be one of: {', '.join(orientations())}",
        )
    return o


def _build_pdf(
    paths: List[str],
    *,
    filenames: List[str],
    page_mode: str,
    orientation: str,
    background: str,
) -> dict:
    blobs: List[bytes] = []
    for p in paths:
        with open(p, "rb") as f:
            blobs.append(f.read())
    return images_to_pdf(
        blobs,
        filenames=filenames,
        page_mode=page_mode,
        orientation=orientation,
        background=background,
    )


def _stats_headers(result: dict) -> dict:
    headers = {
        "X-Original-Bytes": str(result.get("original_bytes", 0)),
        "X-Output-Bytes": str(result.get("output_bytes", 0)),
        "X-Page-Count": str(result.get("page_count", 0)),
        "X-Page-Mode": str(result.get("page_mode") or ""),
    }
    if result.get("orientation"):
        headers["X-Orientation"] = str(result["orientation"])
    notes = result.get("notes") or []
    if notes:
        headers["X-Convert-Notes"] = quote(",".join(str(n) for n in notes)[:500])
    return headers


@router.get("/options")
async def api_options():
    return JSONResponse(
        {
            "input": input_formats(),
            "page_modes": page_modes(),
            "orientations": orientations(),
            "max_images": max_images(),
            "defaults": {
                "page_mode": "fit",
                "orientation": "auto",
                "background": "#ffffff",
            },
            "modes": [
                {
                    "id": "fit",
                    "label": "原图尺寸",
                    "hint": "每页与图片像素一致，适合截图 / 扫码",
                },
                {
                    "id": "a4",
                    "label": "A4 适配",
                    "hint": "等比缩放到 A4 可打印区并居中留白",
                },
            ],
            "orientation_options": [
                {
                    "id": "auto",
                    "label": "自动",
                    "hint": "按图片宽高比选横版或竖版",
                },
                {
                    "id": "portrait",
                    "label": "竖版",
                    "hint": "固定 A4 纵向 210×297 mm",
                },
                {
                    "id": "landscape",
                    "label": "横版",
                    "hint": "固定 A4 横向 297×210 mm",
                },
            ],
        }
    )


@router.post("/convert")
async def api_convert(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    page_mode: str = Form("fit"),
    orientation: str = Form("auto"),
    background: str = Form("#ffffff"),
):
    """Merge one or more images into a single PDF download."""
    if not files:
        raise HTTPException(status_code=400, detail="At least one image is required")

    # Filter empty UploadFile placeholders some clients send.
    uploads = [f for f in files if f is not None and (f.filename or "").strip()]
    if not uploads:
        raise HTTPException(status_code=400, detail="At least one image is required")

    limit = max_images()
    if len(uploads) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"Too many images (max {limit}, got {len(uploads)})",
        )

    mode = _parse_page_mode(page_mode)
    orient = _parse_orientation(orientation)
    bg = (background or "#ffffff").strip() or "#ffffff"

    for f in uploads:
        check_upload_size_header(f)

    ws = TempWorkspace(prefix="i2p_")
    try:
        work = ws.create()
        paths: List[str] = []
        names: List[str] = []
        for i, f in enumerate(uploads):
            name = f.filename or f"image_{i + 1}.bin"
            dest = os.path.join(work, f"in_{i:03d}.bin")
            await save_upload(f, dest)
            paths.append(dest)
            names.append(name)

        result = await run_heavy(
            _build_pdf,
            paths,
            filenames=names,
            page_mode=mode,
            orientation=orient,
            background=bg,
        )
    except HTTPException:
        ws.cleanup_now()
        raise
    except ImageToPdfError as exc:
        ws.cleanup_now()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc, label="Image to PDF failed") from exc

    first_name = names[0] if names else "images"
    if len(names) == 1:
        out_name = f"{safe_stem(first_name)}.pdf"
    else:
        out_name = f"{safe_stem(first_name)}_images.pdf"

    out_path = ws.join(out_name)
    try:
        with open(out_path, "wb") as out:
            out.write(result["data"])
    except Exception as exc:
        ws.cleanup_now()
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}") from exc

    try:
        await archive_input(
            tool="image-to-pdf",
            original_name=first_name,
            input_path=paths[0],
            extra={
                "page_count": result.get("page_count"),
                "page_mode": mode,
                "orientation": orient if mode == "a4" else None,
                "image_count": len(names),
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
