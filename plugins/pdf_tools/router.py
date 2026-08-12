"""PDF 工具集 — 页面与 API（拆分 / 合并 / 解密 / 抽页）。"""

from __future__ import annotations

import os
import zipfile
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from core.concurrency import run_conversion
from core.errors import PDFParseError, ValidationError
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

from .pdf_ops import MAX_PAGES, decrypt_pdf, extract_pages, merge_pdfs, split_pdf

router = APIRouter(prefix="/tools/pdf-tools", tags=["pdf-tools"])

_ACTIONS = ("split", "merge", "decrypt", "extract")


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/pdf-tools.html",
        with_nav(
            {
                "tool": {
                    "name": "PDF 工具集",
                    "slug": "pdf-tools",
                    "category": "pdf",
                },
                "actions": list(_ACTIONS),
                "max_pages": MAX_PAGES,
                "max_batch": max_batch_files(),
            }
        ),
    )


@router.get("/options")
async def api_options():
    return JSONResponse(
        {
            "actions": list(_ACTIONS),
            "max_pages": MAX_PAGES,
            "max_batch": max_batch_files(),
            "defaults": {
                "action": "split",
                "password": "",
                "page_spec": "",
            },
        }
    )


def _parse_action(raw: Optional[str]) -> str:
    a = (raw or "split").strip().lower()
    if a not in _ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of: {', '.join(_ACTIONS)}",
        )
    return a


def _reject_non_pdf(filename: Optional[str], label: str = "file") -> None:
    if not filename or not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400, detail=f"{label} must be a .pdf file"
        )


@router.post("/convert")
async def api_process(
    background_tasks: BackgroundTasks,
    action: str = Form("split"),
    files: List[UploadFile] = File(...),
    password: Optional[str] = Form(None),
    page_spec: Optional[str] = Form(None),
):
    """Run one PDF tool action; returns a PDF, or a ZIP for split."""
    act = _parse_action(action)
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    max_files = max_batch_files()
    if len(files) > max_files:
        raise HTTPException(
            status_code=413,
            detail=f"Too many files (max {max_files})",
        )
    if act == "merge" and len(files) < 2:
        raise HTTPException(status_code=400, detail="Merge requires at least 2 PDF files")
    for idx, f in enumerate(files):
        _reject_non_pdf(f.filename, label=f"file {idx + 1}")
        check_upload_size_header(f, label=f.filename)

    ws = TempWorkspace(prefix="pdftools_")
    try:
        work = ws.create()
        if act == "merge":
            result = await _run_merge(ws, work, files)
            out_path = result["out_path"]
        elif act == "split":
            result = await _run_split(ws, work, files[0], password, page_spec)
            out_path = result["out_path"]
        else:
            result = await _run_single(ws, work, files[0], act, password, page_spec)
            out_path = result["out_path"]
    except HTTPException:
        ws.cleanup_now()
        raise
    except (PDFParseError, ValidationError):
        ws.cleanup_now()
        raise
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc, label="PDF tools failed") from exc

    ws.schedule_cleanup(background_tasks)
    headers = result["headers"]
    return FileResponse(
        out_path,
        media_type=result["media_type"],
        filename=result["filename"],
        headers=headers,
        background=None,
    )


async def _run_merge(ws: TempWorkspace, work: str, files: List[UploadFile]) -> dict:
    in_paths: List[str] = []
    passwords: List[Optional[str]] = []
    for idx, f in enumerate(files):
        path = os.path.join(work, f"input-{idx + 1}.pdf")
        await save_upload(f, path)
        in_paths.append(path)
        passwords.append(None)

    stats = await run_conversion(
        merge_pdfs, in_paths, ws.join("merged.pdf"), passwords
    )
    out_name = "merged.pdf"
    return {
        "out_path": ws.join(out_name),
        "media_type": PDF_MEDIA,
        "filename": out_name,
        "headers": {
            "X-Input-Files": str(stats["input_files"]),
            "X-Output-Pages": str(stats["output_pages"]),
            "Cache-Control": "no-store",
        },
    }


async def _run_split(
    ws: TempWorkspace,
    work: str,
    file: UploadFile,
    password: Optional[str],
    page_spec: Optional[str],
) -> dict:
    in_path = os.path.join(work, "input.pdf")
    await save_upload(file, in_path)
    out_dir = os.path.join(work, "pages")
    os.makedirs(out_dir, exist_ok=True)
    pw = (password or "").strip() or None

    stats = await run_conversion(
        split_pdf,
        in_path,
        out_dir,
        "page",
        pw,
        (page_spec or "").strip() or None,
    )
    zip_path = ws.join("split_pages.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in stats["files"]:
            zf.write(os.path.join(out_dir, name), name)

    return {
        "out_path": zip_path,
        "media_type": ZIP_MEDIA,
        "filename": f"{safe_stem(file.filename)}_split.zip",
        "headers": {
            "X-Input-Pages": str(stats["input_pages"]),
            "X-Output-Files": str(stats["output_files"]),
            "Cache-Control": "no-store",
        },
    }


async def _run_single(
    ws: TempWorkspace,
    work: str,
    file: UploadFile,
    act: str,
    password: Optional[str],
    page_spec: Optional[str],
) -> dict:
    in_path = os.path.join(work, "input.pdf")
    await save_upload(file, in_path)
    pw = (password or "").strip() or None
    stem = safe_stem(file.filename)

    if act == "decrypt":
        out_name = f"{stem}_decrypted.pdf"
        out_path = ws.join(out_name)
        stats = await run_conversion(decrypt_pdf, in_path, out_path, pw)
        media = PDF_MEDIA
        headers = {
            "X-Input-Pages": str(stats["input_pages"]),
            "X-Output-Pages": str(stats["output_pages"]),
            "Cache-Control": "no-store",
        }
    else:  # extract
        spec = (page_spec or "").strip()
        if not spec:
            raise HTTPException(status_code=400, detail="page_spec is required for extract")
        out_name = f"{stem}_extracted.pdf"
        out_path = ws.join(out_name)
        stats = await run_conversion(
            extract_pages, in_path, out_path, spec, pw
        )
        media = PDF_MEDIA
        headers = {
            "X-Input-Pages": str(stats["input_pages"]),
            "X-Output-Pages": str(stats["output_pages"]),
            "Cache-Control": "no-store",
        }

    try:
        await archive_input(
            tool="pdf-tools",
            original_name=file.filename or "input.pdf",
            input_path=in_path,
            extra={"action": act, "password": bool(pw)},
        )
    except Exception:
        pass

    return {
        "out_path": out_path,
        "media_type": media,
        "filename": out_name,
        "headers": headers,
    }
