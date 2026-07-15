from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import zipfile
from typing import List, Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from converter import content_warnings, count_blocks, extract_document, ocr_info, write_document
from core.concurrency import run_conversion, run_conversion_process, should_use_process_pool
from core.errors import ConversionError, PDFParseError, ToolkitError
from storage import archive_conversion
from tools.common import (
    DOCX_MEDIA,
    ZIP_MEDIA,
    check_upload_size_header,
    max_batch_files,
    safe_stem,
    save_upload,
    templates,
)

router = APIRouter(prefix="/tools/pdf2word", tags=["pdf2word"])


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/pdf2word.html",
        {"ocr": ocr_info()},
    )


@router.get("/ocr-status")
async def ocr_status():
    """Return whether optional OCR (Tesseract) is available."""
    return JSONResponse(ocr_info())


def _convert_one(
    pdf_path: str,
    docx_path: str,
    page_range: Optional[str],
    page_breaks: bool,
    ocr: bool = False,
) -> dict:
    pages = extract_document(pdf_path, page_range=page_range, ocr=ocr)
    if not pages:
        raise PDFParseError("No content extracted from PDF")
    write_document(pages, docx_path, page_breaks=page_breaks)
    stats = count_blocks(pages)
    stats["warnings"] = content_warnings(pages)
    if "empty" in stats["warnings"]:
        raise ConversionError(
            "No extractable content (empty or unsupported PDF). "
            "Scanned PDFs without a renderable image cannot be converted. "
            "Enable OCR if Tesseract is installed."
        )
    return stats


def _stats_headers(stats: dict) -> dict:
    headers = {
        "X-Pages": str(stats.get("pages", 0)),
        "X-Tables": str(stats.get("tables", 0)),
        "X-Text-Blocks": str(stats.get("text_blocks", 0)),
        "X-Images": str(stats.get("images", 0)),
        "X-Lines": str(stats.get("lines", 0)),
    }
    warns = stats.get("warnings") or []
    if warns:
        headers["X-Warnings"] = ",".join(warns)
        if "ocr_applied" in warns:
            headers["X-Warning-Message"] = quote(
                "OCR applied on scanned page(s); text is editable but may contain errors"
            )
        elif "image_only" in warns:
            headers["X-Warning-Message"] = quote(
                "Detected image-only/scanned pages; embedded as images "
                "(enable OCR for editable text)"
            )
    return headers


def _parse_ocr_flag(ocr: Optional[str]) -> bool:
    if ocr is None:
        return False
    return str(ocr).strip().lower() in ("1", "true", "yes", "on")


@router.post("/convert")
async def convert(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    page_range: Optional[str] = Form(None),
    page_breaks: bool = Form(True),
    ocr: Optional[str] = Form(None),
):
    """Convert a single PDF to Word."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    check_upload_size_header(file)

    use_ocr = _parse_ocr_flag(ocr)
    if use_ocr:
        from converter import ocr_available

        if not ocr_available():
            raise HTTPException(
                status_code=503,
                detail=(
                    "OCR requested but Tesseract is not available. "
                    "Install Tesseract and pytesseract, or set TESSERACT_CMD."
                ),
            )

    tmp_dir = tempfile.mkdtemp(prefix="pdf2word_")
    pdf_path = os.path.join(tmp_dir, "input.pdf")
    docx_path = os.path.join(tmp_dir, "output.docx")
    range_spec = (page_range or "").strip() or None

    try:
        await save_upload(file, pdf_path)
        file_size = os.path.getsize(pdf_path)
        runner = run_conversion_process if should_use_process_pool(file_size) else run_conversion
        stats = await runner(
            _convert_one, pdf_path, docx_path, range_spec, page_breaks, use_ocr
        )
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except ToolkitError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"Conversion failed: {exc}"
        ) from exc

    out_name = safe_stem(file.filename) + ".docx"
    await asyncio.to_thread(
        archive_conversion,
        tool="pdf2word",
        original_name=file.filename or "input.pdf",
        input_path=pdf_path,
        extra={
            "pages": stats.get("pages"),
            "tables": stats.get("tables"),
            "images": stats.get("images"),
        },
    )
    background_tasks.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)
    return FileResponse(
        docx_path,
        media_type=DOCX_MEDIA,
        filename=out_name,
        headers=_stats_headers(stats),
    )


@router.post("/convert-batch")
async def convert_batch(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    page_range: Optional[str] = Form(None),
    page_breaks: bool = Form(True),
    ocr: Optional[str] = Form(None),
):
    """Convert multiple PDFs; returns a ZIP of .docx files.

    Uploads are sequential; conversions run concurrently up to
    ``CONVERT_CONCURRENCY`` (via ``run_conversion``).
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    batch_limit = max_batch_files()
    if len(files) > batch_limit:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files (max {batch_limit})",
        )

    use_ocr = _parse_ocr_flag(ocr)
    if use_ocr:
        from converter import ocr_available

        if not ocr_available():
            raise HTTPException(
                status_code=503,
                detail=(
                    "OCR requested but Tesseract is not available. "
                    "Install Tesseract and pytesseract, or set TESSERACT_CMD."
                ),
            )

    range_spec = (page_range or "").strip() or None
    tmp_dir = tempfile.mkdtemp(prefix="pdf2word_batch_")
    zip_path = os.path.join(tmp_dir, "output.zip")

    # (idx, original_name, pdf_path, docx_path)
    jobs: List[Tuple[int, str, str, str]] = []

    try:
        for idx, file in enumerate(files):
            if not file.filename or not file.filename.lower().endswith(".pdf"):
                raise HTTPException(
                    status_code=400,
                    detail=f"File {idx + 1}: only PDF files are supported",
                )
            check_upload_size_header(file)

            pdf_path = os.path.join(tmp_dir, f"in_{idx}.pdf")
            docx_path = os.path.join(tmp_dir, f"out_{idx}.docx")
            await save_upload(file, pdf_path)
            jobs.append((idx, file.filename or f"input_{idx}.pdf", pdf_path, docx_path))

        async def _run_job(
            idx: int, name: str, pdf_path: str, docx_path: str
        ) -> Tuple[int, str, str, str, dict]:
            try:
                file_size = os.path.getsize(pdf_path)
                runner = run_conversion_process if should_use_process_pool(file_size) else run_conversion
                stats = await runner(
                    _convert_one,
                    pdf_path,
                    docx_path,
                    range_spec,
                    page_breaks,
                    use_ocr,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"{name}: {exc}",
                ) from exc
            return idx, name, pdf_path, docx_path, stats

        results = await asyncio.gather(
            *[_run_job(idx, name, pdf, docx) for idx, name, pdf, docx in jobs]
        )
        # Preserve upload order in the ZIP
        results = sorted(results, key=lambda r: r[0])

        used_names: set = set()
        total_pages = total_tables = total_texts = total_images = 0
        converted = 0
        all_warns: set = set()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, name, pdf_path, docx_path, stats in results:
                stem = safe_stem(name)
                out_name = f"{stem}.docx"
                if out_name in used_names:
                    out_name = f"{stem}_{idx + 1}.docx"
                used_names.add(out_name)
                zf.write(docx_path, out_name)

                await asyncio.to_thread(
                    archive_conversion,
                    tool="pdf2word",
                    original_name=name,
                    input_path=pdf_path,
                    extra={
                        "pages": stats.get("pages"),
                        "tables": stats.get("tables"),
                        "images": stats.get("images"),
                        "batch": True,
                    },
                )

                total_pages += stats["pages"]
                total_tables += stats["tables"]
                total_texts += stats["text_blocks"]
                total_images += stats.get("images", 0)
                all_warns.update(stats.get("warnings") or [])
                converted += 1
                for p in (pdf_path, docx_path):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"Batch conversion failed: {exc}"
        ) from exc

    background_tasks.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)
    headers = _stats_headers({
        "pages": total_pages,
        "tables": total_tables,
        "text_blocks": total_texts,
        "images": total_images,
        "warnings": sorted(all_warns),
    })
    headers["X-Files"] = str(converted)
    return FileResponse(
        zip_path,
        media_type=ZIP_MEDIA,
        filename="pdf2word_batch.zip",
        headers=headers,
    )
