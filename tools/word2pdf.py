from __future__ import annotations

import asyncio
import os
import zipfile
from typing import List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from core.concurrency import run_conversion
from core.jobs import create_job, job_public_dict, schedule_job, update_job
from tools.common import (
    PDF_MEDIA,
    ZIP_MEDIA,
    check_upload_size_header,
    max_batch_files,
    safe_stem,
    save_upload,
    templates,
    with_nav,
)
from tools.pipeline import TempWorkspace, archive_input, map_conversion_error
from word2pdf import convert_to_pdf, engine_info

router = APIRouter(prefix="/tools/word2pdf", tags=["word2pdf"])

_WORD_EXTS = (".docx", ".doc")


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    info = engine_info()
    return templates.TemplateResponse(
        request,
        "tools/word2pdf.html",
        with_nav(
            {
                "engine": info,
                "tool": {
                    "slug": "word2pdf",
                    "name": "Word 转 PDF",
                    "category": "document",
                },
            }
        ),
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


def _job_urls(job_id: str) -> dict:
    return {
        "poll_url": f"/api/jobs/{job_id}",
        "download_url": f"/api/jobs/{job_id}/download",
    }


async def _run_single_async_job(
    *,
    job_id: str,
    doc_path: str,
    pdf_path: str,
    original_name: str,
) -> dict:
    await update_job(job_id, progress=0.15, message="converting")
    stats = await run_conversion(_convert_one, doc_path, pdf_path)
    await archive_input(
        tool="word2pdf",
        original_name=original_name,
        input_path=doc_path,
        extra={
            "engine": stats.get("engine"),
            "bytes": stats.get("bytes"),
            "async": True,
        },
    )
    try:
        os.remove(doc_path)
    except OSError:
        pass
    return {
        "result": {
            "engine": stats.get("engine"),
            "bytes": stats.get("bytes"),
            "files": 1,
        },
        "response_headers": _engine_headers(stats),
        "progress": 1.0,
        "message": "done",
    }


async def _run_batch_async_job(
    *,
    job_id: str,
    items: List[Tuple[int, str, str, str]],
    zip_path: str,
) -> dict:
    total_files = max(len(items), 1)
    done_count = 0
    done_lock = asyncio.Lock()

    async def _one(
        idx: int, name: str, doc_path: str, pdf_path: str
    ) -> Tuple[int, str, str, str, dict]:
        nonlocal done_count
        stats = await run_conversion(_convert_one, doc_path, pdf_path)
        async with done_lock:
            done_count += 1
            await update_job(
                job_id,
                progress=min(0.95, 0.1 + 0.85 * (done_count / total_files)),
                message=f"file {done_count}/{total_files}",
            )
        return idx, name, doc_path, pdf_path, stats

    results = await asyncio.gather(
        *[_one(idx, name, doc, pdf) for idx, name, doc, pdf in items]
    )
    results = sorted(results, key=lambda r: r[0])

    used_names: set = set()
    converted = 0
    engines_used: set = set()
    total_bytes = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, name, doc_path, pdf_path, stats in results:
            stem = safe_stem(name)
            out_name = f"{stem}.pdf"
            if out_name in used_names:
                out_name = f"{stem}_{idx + 1}.pdf"
            used_names.add(out_name)
            zf.write(pdf_path, out_name)

            await archive_input(
                tool="word2pdf",
                original_name=name,
                input_path=doc_path,
                extra={
                    "engine": stats.get("engine"),
                    "bytes": stats.get("bytes"),
                    "batch": True,
                    "async": True,
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

    headers = {
        "X-Files": str(converted),
        "X-Bytes": str(total_bytes),
        "X-Engine": ",".join(sorted(e for e in engines_used if e)),
    }
    return {
        "result": {
            "files": converted,
            "bytes": total_bytes,
            "engine": headers["X-Engine"],
            "batch": True,
        },
        "response_headers": headers,
        "progress": 1.0,
        "message": "done",
    }


@router.post("/convert")
async def convert(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """Convert a single Word document to PDF (synchronous file response)."""
    if not _is_word_filename(file.filename):
        raise HTTPException(
            status_code=400,
            detail="Only .docx / .doc files are supported",
        )
    check_upload_size_header(file)
    _require_engine()

    ws = TempWorkspace("word2pdf_")
    ws.create()
    ext = os.path.splitext(file.filename or "input.docx")[1].lower() or ".docx"
    doc_path = ws.join(f"input{ext}")
    pdf_path = ws.join("output.pdf")

    try:
        await save_upload(file, doc_path)
        stats = await run_conversion(_convert_one, doc_path, pdf_path)
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc) from exc

    out_name = safe_stem(file.filename) + ".pdf"
    await archive_input(
        tool="word2pdf",
        original_name=file.filename or "input.docx",
        input_path=doc_path,
        extra={"engine": stats.get("engine"), "bytes": stats.get("bytes")},
    )
    ws.schedule_cleanup(background_tasks)
    return FileResponse(
        pdf_path,
        media_type=PDF_MEDIA,
        filename=out_name,
        headers=_engine_headers(stats),
    )


@router.post("/convert-async")
async def convert_async(file: UploadFile = File(...)):
    """Queue a single Word→PDF job; poll ``/api/jobs/{id}`` then download."""
    if not _is_word_filename(file.filename):
        raise HTTPException(
            status_code=400,
            detail="Only .docx / .doc files are supported",
        )
    check_upload_size_header(file)
    _require_engine()

    ws = TempWorkspace("word2pdf_async_")
    work_dir = ws.create()
    ext = os.path.splitext(file.filename or "input.docx")[1].lower() or ".docx"
    doc_path = ws.join(f"input{ext}")
    pdf_path = ws.join("output.pdf")
    original = file.filename or "input.docx"
    out_name = safe_stem(original) + ".pdf"

    try:
        await save_upload(file, doc_path)
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc) from exc

    job = await create_job(
        "word2pdf",
        work_dir=work_dir,
        output_path=pdf_path,
        download_name=out_name,
        media_type=PDF_MEDIA,
        message="queued",
        progress=0.0,
    )

    async def _factory():
        return await _run_single_async_job(
            job_id=job.id,
            doc_path=doc_path,
            pdf_path=pdf_path,
            original_name=original,
        )

    schedule_job(job.id, _factory)
    body = job_public_dict(job)
    body.update(_job_urls(job.id))
    body["mode"] = "async"
    return JSONResponse(body, status_code=202)


@router.post("/convert-batch")
async def convert_batch(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
):
    """Convert multiple Word documents; returns a ZIP of PDFs (synchronous).

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

    _require_engine()

    ws = TempWorkspace("word2pdf_batch_")
    ws.create()
    zip_path = ws.join("output.zip")
    jobs: List[Tuple[int, str, str, str]] = []

    try:
        for idx, file in enumerate(files):
            if not _is_word_filename(file.filename):
                raise HTTPException(
                    status_code=400,
                    detail=f"File {idx + 1}: only .docx / .doc are supported",
                )
            check_upload_size_header(file)

            ext = os.path.splitext(file.filename or "input.docx")[1].lower() or ".docx"
            doc_path = ws.join(f"in_{idx}{ext}")
            pdf_path = ws.join(f"out_{idx}.pdf")
            await save_upload(file, doc_path)
            jobs.append(
                (idx, file.filename or f"input_{idx}.docx", doc_path, pdf_path)
            )

        async def _run_job(
            idx: int, name: str, doc_path: str, pdf_path: str
        ) -> Tuple[int, str, str, str, dict]:
            try:
                stats = await run_conversion(_convert_one, doc_path, pdf_path)
            except Exception as exc:
                raise map_conversion_error(exc, name_prefix=name) from exc
            return idx, name, doc_path, pdf_path, stats

        results = await asyncio.gather(
            *[_run_job(idx, name, doc, pdf) for idx, name, doc, pdf in jobs]
        )
        results = sorted(results, key=lambda r: r[0])

        used_names: set = set()
        converted = 0
        engines_used: set = set()
        total_bytes = 0

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, name, doc_path, pdf_path, stats in results:
                stem = safe_stem(name)
                out_name = f"{stem}.pdf"
                if out_name in used_names:
                    out_name = f"{stem}_{idx + 1}.pdf"
                used_names.add(out_name)
                zf.write(pdf_path, out_name)

                await archive_input(
                    tool="word2pdf",
                    original_name=name,
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
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(
            exc, label="Batch conversion failed"
        ) from exc

    ws.schedule_cleanup(background_tasks)
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


@router.post("/convert-batch-async")
async def convert_batch_async(files: List[UploadFile] = File(...)):
    """Queue a batch Word→PDF job; poll then download a ZIP."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    batch_limit = max_batch_files()
    if len(files) > batch_limit:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files (max {batch_limit})",
        )

    _require_engine()

    ws = TempWorkspace("word2pdf_batch_async_")
    work_dir = ws.create()
    zip_path = ws.join("output.zip")
    items: List[Tuple[int, str, str, str]] = []

    try:
        for idx, file in enumerate(files):
            if not _is_word_filename(file.filename):
                raise HTTPException(
                    status_code=400,
                    detail=f"File {idx + 1}: only .docx / .doc are supported",
                )
            check_upload_size_header(file)
            ext = os.path.splitext(file.filename or "input.docx")[1].lower() or ".docx"
            doc_path = ws.join(f"in_{idx}{ext}")
            pdf_path = ws.join(f"out_{idx}.pdf")
            await save_upload(file, doc_path)
            items.append(
                (idx, file.filename or f"input_{idx}.docx", doc_path, pdf_path)
            )
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc, label="Batch upload failed") from exc

    job = await create_job(
        "word2pdf",
        work_dir=work_dir,
        output_path=zip_path,
        download_name="word2pdf_batch.zip",
        media_type=ZIP_MEDIA,
        message="queued",
        progress=0.0,
    )

    async def _factory():
        return await _run_batch_async_job(
            job_id=job.id,
            items=items,
            zip_path=zip_path,
        )

    schedule_job(job.id, _factory)
    body = job_public_dict(job)
    body.update(_job_urls(job.id))
    body["mode"] = "async"
    body["files"] = len(items)
    return JSONResponse(body, status_code=202)
