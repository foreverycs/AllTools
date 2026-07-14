from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import zipfile
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from core.concurrency import run_conversion
from storage import archive_conversion
from tools.common import (
    PDF_MEDIA,
    ZIP_MEDIA,
    check_upload_size_header,
    max_batch_files,
    safe_stem,
    save_upload,
    templates,
)
from word2pdf import ConversionError, convert_to_pdf, engine_info

router = APIRouter(prefix="/tools/word2pdf", tags=["word2pdf"])

_WORD_EXTS = (".docx", ".doc")


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    info = engine_info()
    return templates.TemplateResponse(
        request,
        "tools/word2pdf.html",
        {"engine": info},
    )


@router.get("/status")
async def status():
    """Return whether a conversion engine is available."""
    return JSONResponse(engine_info())


def _is_word_filename(name: Optional[str]) -> bool:
    if not name:
        return False
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _WORD_EXTS)


def _convert_one(docx_path: str, pdf_path: str) -> dict:
    path, engine = convert_to_pdf(docx_path, pdf_path)
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    return {"engine": engine, "bytes": size}


def _engine_headers(stats: dict) -> dict:
    return {
        "X-Engine": str(stats.get("engine") or ""),
        "X-Bytes": str(stats.get("bytes") or 0),
    }


def _require_engine() -> None:
    info = engine_info()
    if not info["ready"]:
        raise HTTPException(
            status_code=503,
            detail=(
                "No conversion engine available. Install LibreOffice "
                "(set LIBREOFFICE_PATH if needed) or Microsoft Word on Windows."
            ),
        )


@router.post("/convert")
async def convert(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """Convert a single Word document to PDF."""
    if not _is_word_filename(file.filename):
        raise HTTPException(
            status_code=400,
            detail="Only .docx / .doc files are supported",
        )
    check_upload_size_header(file)
    _require_engine()

    tmp_dir = tempfile.mkdtemp(prefix="word2pdf_")
    ext = os.path.splitext(file.filename or "input.docx")[1].lower() or ".docx"
    doc_path = os.path.join(tmp_dir, f"input{ext}")
    pdf_path = os.path.join(tmp_dir, "output.pdf")

    try:
        await save_upload(file, doc_path)
        stats = await run_conversion(_convert_one, doc_path, pdf_path)
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except ConversionError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"Conversion failed: {exc}"
        ) from exc

    out_name = safe_stem(file.filename) + ".pdf"
    await asyncio.to_thread(
        archive_conversion,
        tool="word2pdf",
        original_name=file.filename or "input.docx",
        input_path=doc_path,
        extra={"engine": stats.get("engine"), "bytes": stats.get("bytes")},
    )
    background_tasks.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)
    return FileResponse(
        pdf_path,
        media_type=PDF_MEDIA,
        filename=out_name,
        headers=_engine_headers(stats),
    )


@router.post("/convert-batch")
async def convert_batch(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
):
    """Convert multiple Word documents; returns a ZIP of PDFs."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    batch_limit = max_batch_files()
    if len(files) > batch_limit:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files (max {batch_limit})",
        )

    _require_engine()

    tmp_dir = tempfile.mkdtemp(prefix="word2pdf_batch_")
    zip_path = os.path.join(tmp_dir, "output.zip")
    used_names: set = set()
    converted = 0
    engines_used: set = set()
    total_bytes = 0

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, file in enumerate(files):
                if not _is_word_filename(file.filename):
                    raise HTTPException(
                        status_code=400,
                        detail=f"File {idx + 1}: only .docx / .doc are supported",
                    )
                check_upload_size_header(file)

                ext = os.path.splitext(file.filename or "input.docx")[1].lower() or ".docx"
                doc_path = os.path.join(tmp_dir, f"in_{idx}{ext}")
                pdf_path = os.path.join(tmp_dir, f"out_{idx}.pdf")
                await save_upload(file, doc_path)

                try:
                    stats = await run_conversion(_convert_one, doc_path, pdf_path)
                except ConversionError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{file.filename}: {exc}",
                    ) from exc

                stem = safe_stem(file.filename)
                out_name = f"{stem}.pdf"
                if out_name in used_names:
                    out_name = f"{stem}_{idx + 1}.pdf"
                used_names.add(out_name)
                zf.write(pdf_path, out_name)

                await asyncio.to_thread(
                    archive_conversion,
                    tool="word2pdf",
                    original_name=file.filename or f"input_{idx}.docx",
                    input_path=doc_path,
                    extra={
                        "engine": stats.get("engine"),
                        "bytes": stats.get("bytes"),
                        "batch": True,
                    },
                )

                engines_used.add(stats.get("engine") or "")
                total_bytes += stats.get("bytes") or 0
                converted += 1
                for p in (doc_path, pdf_path):
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
    headers = {
        "X-Files": str(converted),
        "X-Bytes": str(total_bytes),
        "X-Engine": ",".join(sorted(e for e in engines_used if e)),
    }
    return FileResponse(
        zip_path,
        media_type=ZIP_MEDIA,
        filename="word2pdf_batch.zip",
        headers=headers,
    )
