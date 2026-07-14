from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import tempfile
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from starlette.requests import Request

from storage import archive_conversion

router = APIRouter(prefix="/tools/pdf-merge", tags=["pdf-merge"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024
_PDF_MEDIA = "application/pdf"
_SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff.\-]+", re.UNICODE)

# A4 dimensions in points
A4_W = 595.28
A4_H = 841.89
HALF_H = A4_H / 2
MARGIN = 18


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(request, "tools/pdf-merge.html", {})


def _safe_stem(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename or "output"))[0]
    stem = _SAFE_NAME_RE.sub("_", stem).strip("._") or "output"
    return stem[:80]


async def _save_upload(file: UploadFile, dest: str) -> None:
    total = 0
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="File too large (max 50 MB)")
            out.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="Empty file")


def _make_divider() -> bytes:
    """Create a tiny PDF containing a single horizontal dashed line."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    y = HALF_H
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.setLineWidth(0.5)
    c.setDash(6, 3)
    c.line(MARGIN, y, A4_W - MARGIN, y)
    c.save()
    return buf.getvalue()


def _scale_to_fit(src_w: float, src_h: float, dst_w: float, dst_h: float) -> float:
    """Return uniform scale that fits src into dst preserving aspect ratio."""
    return min(dst_w / src_w, dst_h / src_h)


def _place_transform(page: PageObject, *, top: bool) -> Transformation:
    """Scale a source page into the top or bottom half of A4 and center it."""
    usable_w = A4_W - 2 * MARGIN
    usable_h = HALF_H - MARGIN
    src_w = float(page.mediabox.width)
    src_h = float(page.mediabox.height)
    if src_w <= 0 or src_h <= 0:
        raise ValueError("Invalid page dimensions")
    scale = _scale_to_fit(src_w, src_h, usable_w, usable_h)
    scaled_w = src_w * scale
    scaled_h = src_h * scale
    tx = MARGIN + (usable_w - scaled_w) / 2
    if top:
        ty = HALF_H + (usable_h - scaled_h) / 2
    else:
        ty = (usable_h - scaled_h) / 2
    # Apply scale then translate: x' = s*x + tx, y' = s*y + ty
    return Transformation().scale(scale, scale).translate(tx, ty)


def _merge_pair(
    top_page: Optional[PageObject],
    bottom_page: Optional[PageObject],
    divider_page: Optional[PageObject],
) -> PageObject:
    """Place up to two invoice pages onto one A4 sheet (top / bottom halves).

    Uses ``merge_transformed_page`` so content and resources are applied with the
    CTM in one step (avoids blank pages from clone + ``add_transformation``).
    A lone invoice is placed only on the upper half; the lower half stays empty.
    """
    if top_page is None and bottom_page is None:
        raise ValueError("At least one page is required")

    out = PageObject.create_blank_page(width=A4_W, height=A4_H)

    if top_page is not None:
        out.merge_transformed_page(
            top_page, _place_transform(top_page, top=True), over=True
        )
    if bottom_page is not None:
        out.merge_transformed_page(
            bottom_page, _place_transform(bottom_page, top=False), over=True
        )
    if divider_page is not None and top_page is not None and bottom_page is not None:
        out.merge_page(divider_page)

    return out


def _first_page(path: str) -> PageObject:
    reader = PdfReader(path)
    if not reader.pages:
        raise ValueError("PDF has no pages")
    return reader.pages[0]


def merge_invoices(
    pdf1_path: str,
    out_path: str,
    pdf2_path: Optional[str] = None,
    add_divider: bool = True,
) -> dict:
    """Merge one or two single-invoice PDFs onto one A4 page.

    - Two files: first → upper half, second → lower half.
    - One file: invoice only on the upper half.
    Multi-page inputs use the first page only (one invoice per file).
    """
    top = _first_page(pdf1_path)
    bottom = _first_page(pdf2_path) if pdf2_path else None

    divider_page = None
    if add_divider and bottom is not None:
        divider_page = PdfReader(io.BytesIO(_make_divider())).pages[0]

    merged = _merge_pair(top, bottom, divider_page)
    writer = PdfWriter()
    writer.add_page(merged)
    with open(out_path, "wb") as f:
        writer.write(f)

    input_pages = 1 + (1 if bottom is not None else 0)
    return {"input_pages": input_pages, "output_pages": 1}


# Backwards-compatible names used by older tests / imports
def _merge_two_files(
    pdf1_path: str, pdf2_path: str, out_path: str, add_divider: bool
) -> dict:
    return merge_invoices(pdf1_path, out_path, pdf2_path=pdf2_path, add_divider=add_divider)


def _merge_single(pdf_path: str, out_path: str, add_divider: bool) -> dict:
    """Place a single invoice on the upper half of one A4 page."""
    return merge_invoices(pdf_path, out_path, pdf2_path=None, add_divider=False)


@router.post("/convert")
async def convert(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    file2: Optional[UploadFile] = File(None),
    divider: Optional[str] = Form(None),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 50 MB)")

    use_divider = str(divider or "").strip().lower() in ("1", "true", "yes", "on")

    tmp_dir = tempfile.mkdtemp(prefix="pdf_merge_")
    pdf1_path = os.path.join(tmp_dir, "input1.pdf")
    out_path = os.path.join(tmp_dir, "merged.pdf")

    try:
        await _save_upload(file, pdf1_path)

        pdf2_path: Optional[str] = None
        if file2 and file2.filename:
            if not file2.filename.lower().endswith(".pdf"):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=400, detail="Second file must be a PDF"
                )
            if file2.size is not None and file2.size > MAX_UPLOAD_BYTES:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=413, detail="Second file too large (max 50 MB)"
                )
            pdf2_path = os.path.join(tmp_dir, "input2.pdf")
            await _save_upload(file2, pdf2_path)

        stats = await asyncio.to_thread(
            merge_invoices, pdf1_path, out_path, pdf2_path, use_divider
        )
        archive_name = file.filename
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except ValueError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"Merge failed: {exc}"
        ) from exc

    out_name = _safe_stem(file.filename) + "_merged.pdf"
    await asyncio.to_thread(
        archive_conversion,
        tool="pdf-merge",
        original_name=archive_name or "input.pdf",
        input_path=pdf1_path,
        extra={
            "pages": stats.get("input_pages"),
            "output_pages": stats.get("output_pages"),
        },
    )
    background_tasks.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)
    return FileResponse(
        out_path,
        media_type=_PDF_MEDIA,
        filename=out_name,
        headers={
            "X-Input-Pages": str(stats.get("input_pages", 0)),
            "X-Output-Pages": str(stats.get("output_pages", 0)),
        },
    )
