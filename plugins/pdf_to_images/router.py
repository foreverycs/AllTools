"""PDF 转图片 — 页面与 API。"""

from __future__ import annotations

import os
import zipfile
from contextlib import suppress

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from core.concurrency import run_conversion
from core.errors import PDFParseError, ValidationError
from tools.common import (
    ZIP_MEDIA,
    check_upload_size_header,
    safe_stem,
    save_upload,
    templates,
    with_nav,
)
from tools.pipeline import TempWorkspace, archive_input, map_conversion_error

from .render import (
    DEFAULT_DPI,
    MAX_DPI,
    MAX_PAGES,
    MIN_DPI,
    output_formats,
    render_pdf_to_images,
)

router = APIRouter(prefix="/tools/pdf-to-images", tags=["pdf-to-images"])


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/pdf-to-images.html",
        with_nav(
            {
                "tool": {
                    "name": "PDF 转图片",
                    "slug": "pdf-to-images",
                    "category": "pdf",
                },
                "formats": output_formats(),
                "max_pages": MAX_PAGES,
                "min_dpi": MIN_DPI,
                "max_dpi": MAX_DPI,
                "default_dpi": DEFAULT_DPI,
            }
        ),
    )


@router.get("/options")
async def api_options():
    return JSONResponse(
        {
            "formats": output_formats(),
            "max_pages": MAX_PAGES,
            "min_dpi": MIN_DPI,
            "max_dpi": MAX_DPI,
            "defaults": {
                "format": "png",
                "dpi": DEFAULT_DPI,
                "page_spec": "",
                "jpeg_quality": 85,
            },
        }
    )


def _parse_fmt(raw: str | None) -> str:
    f = (raw or "png").strip().lower()
    if f in ("jpg", "jpeg"):
        f = "jpeg"
    if f not in output_formats():
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of: {', '.join(output_formats())}",
        )
    return f


def _parse_dpi(raw: str | None) -> int:
    if raw is None or str(raw).strip() == "":
        return DEFAULT_DPI
    try:
        d = int(str(raw).strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="dpi must be an integer") from exc
    if d < MIN_DPI or d > MAX_DPI:
        raise HTTPException(
            status_code=400, detail=f"dpi must be between {MIN_DPI} and {MAX_DPI}"
        )
    return d


@router.post("/convert")
async def api_convert(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    format: str = Form("png"),
    dpi: str | None = Form(None),
    page_spec: str | None = Form(None),
    password: str | None = Form(None),
    jpeg_quality: str | None = Form("85"),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="file must be a .pdf file")
    fmt = _parse_fmt(format)
    d = _parse_dpi(dpi)
    pw = (password or "").strip() or None
    try:
        jq = int(str(jpeg_quality or "85").strip())
    except ValueError:
        jq = 85
    check_upload_size_header(file)

    ws = TempWorkspace(prefix="pdf2img_")
    try:
        work = ws.create()
        in_path = os.path.join(work, "input.pdf")
        await save_upload(file, in_path)
        out_dir = os.path.join(work, "pages")
        os.makedirs(out_dir, exist_ok=True)
        stats = await run_conversion(
            render_pdf_to_images,
            in_path,
            out_dir,
            fmt=fmt,
            dpi=d,
            page_spec=(page_spec or "").strip() or None,
            password=pw,
            jpeg_quality=jq,
            prefix="page",
        )
        zip_path = ws.join(f"{safe_stem(file.filename)}_pages.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in stats["files"]:
                zf.write(os.path.join(out_dir, name), name)
        out_name = os.path.basename(zip_path)
    except HTTPException:
        ws.cleanup_now()
        raise
    except (PDFParseError, ValidationError):
        ws.cleanup_now()
        raise
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc, label="PDF to images failed") from exc

    with suppress(Exception):
        await archive_input(
            tool="pdf-to-images",
            original_name=file.filename or "input.pdf",
            input_path=in_path,
            extra={
                "format": fmt,
                "dpi": d,
                "output_files": stats.get("output_files"),
            },
        )

    ws.schedule_cleanup(background_tasks)
    headers = {
        "X-Input-Pages": str(stats.get("input_pages", 0)),
        "X-Output-Files": str(stats.get("output_files", 0)),
        "X-Image-Format": str(stats.get("format") or fmt),
        "X-Render-Dpi": str(stats.get("dpi") or d),
        "Cache-Control": "no-store",
    }
    return FileResponse(
        zip_path,
        media_type=ZIP_MEDIA,
        filename=out_name,
        headers=headers,
        background=None,
    )
