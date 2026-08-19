"""PDF 压缩 — 页面与 API。"""

from __future__ import annotations

import os
from contextlib import suppress

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from core.concurrency import run_conversion
from core.errors import PDFParseError, ValidationError
from tools.common import (
    PDF_MEDIA,
    check_upload_size_header,
    safe_stem,
    save_upload,
    templates,
    with_nav,
)
from tools.pipeline import TempWorkspace, archive_input, map_conversion_error

from .compress import MAX_PAGES, compress_pdf, quality_presets

router = APIRouter(prefix="/tools/pdf-compress", tags=["pdf-compress"])


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/pdf-compress.html",
        with_nav(
            {
                "tool": {
                    "name": "PDF 压缩",
                    "slug": "pdf-compress",
                    "category": "pdf",
                },
                "qualities": quality_presets(),
                "max_pages": MAX_PAGES,
            }
        ),
    )


@router.get("/options")
async def api_options():
    return JSONResponse(
        {
            "qualities": [
                {
                    "id": "light",
                    "label": "轻量",
                    "hint": "清理冗余对象，不改图片",
                },
                {
                    "id": "balanced",
                    "label": "均衡（推荐）",
                    "hint": "适度重采样图片，体积明显下降",
                },
                {
                    "id": "strong",
                    "label": "强压缩",
                    "hint": "更小体积，图片更糊",
                },
            ],
            "max_pages": MAX_PAGES,
            "defaults": {"quality": "balanced", "password": ""},
        }
    )


def _parse_quality(raw: str | None) -> str:
    q = (raw or "balanced").strip().lower()
    if q not in quality_presets():
        raise HTTPException(
            status_code=400,
            detail=f"quality must be one of: {', '.join(quality_presets())}",
        )
    return q


@router.post("/compress")
async def api_compress(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    quality: str = Form("balanced"),
    password: str | None = Form(None),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="file must be a .pdf file")
    q = _parse_quality(quality)
    pw = (password or "").strip() or None
    check_upload_size_header(file)

    ws = TempWorkspace(prefix="pdfc_")
    try:
        work = ws.create()
        in_path = os.path.join(work, "input.pdf")
        await save_upload(file, in_path)
        out_name = f"{safe_stem(file.filename)}_compressed.pdf"
        out_path = ws.join(out_name)
        stats = await run_conversion(
            compress_pdf, in_path, out_path, quality=q, password=pw
        )
    except HTTPException:
        ws.cleanup_now()
        raise
    except (PDFParseError, ValidationError):
        ws.cleanup_now()
        raise
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc, label="PDF compression failed") from exc

    with suppress(Exception):
        await archive_input(
            tool="pdf-compress",
            original_name=file.filename or "input.pdf",
            input_path=in_path,
            extra={
                "quality": q,
                "original_bytes": stats.get("original_bytes"),
                "compressed_bytes": stats.get("compressed_bytes"),
                "percent_saved": stats.get("percent_saved"),
            },
        )

    ws.schedule_cleanup(background_tasks)
    headers = {
        "X-Original-Bytes": str(stats.get("original_bytes", 0)),
        "X-Compressed-Bytes": str(stats.get("compressed_bytes", 0)),
        "X-Saved-Bytes": str(stats.get("saved_bytes", 0)),
        "X-Percent-Saved": str(stats.get("percent_saved", 0)),
        "X-Input-Pages": str(stats.get("input_pages", 0)),
        "X-Compress-Mode": str(stats.get("mode") or ""),
        "Cache-Control": "no-store",
    }
    return FileResponse(
        out_path,
        media_type=PDF_MEDIA,
        filename=out_name,
        headers=headers,
        background=None,
    )
