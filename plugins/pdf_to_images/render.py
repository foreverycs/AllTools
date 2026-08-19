"""Render PDF pages to image files (PyMuPDF)."""

from __future__ import annotations

import os
from typing import Any

from core.errors import PDFParseError, ValidationError

MAX_PAGES = 200
DEFAULT_DPI = 144
MIN_DPI = 72
MAX_DPI = 300
FORMATS = ("png", "jpeg")


def output_formats() -> list[str]:
    return list(FORMATS)


def _import_fitz():
    try:
        import fitz
    except ImportError as exc:
        raise ValidationError(
            "服务器未安装 pymupdf，无法渲染 PDF。请执行：pip install pymupdf"
        ) from exc
    return fitz


def _parse_page_ranges(spec: str | None, total: int) -> list[int]:
    if spec is None or str(spec).strip() == "":
        return list(range(1, total + 1))
    selected: list[int] = []
    seen: set = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo = int(a.strip())
                hi = int(b.strip())
            except ValueError as exc:
                raise ValidationError(f"无法解析页码范围：{part!r}") from exc
            if lo > hi:
                raise ValidationError(f"页码范围起始大于结束：{part!r}")
            for page in range(lo, hi + 1):
                if 1 <= page <= total and page not in seen:
                    seen.add(page)
                    selected.append(page)
        else:
            try:
                num = int(part)
            except ValueError as exc:
                raise ValidationError(f"无法解析页码：{part!r}") from exc
            if num < 1:
                raise ValidationError(f"页码必须 ≥ 1：{part!r}")
            if 1 <= num <= total and num not in seen:
                seen.add(num)
                selected.append(num)
    if not selected:
        raise ValidationError(f"指定页码超出文档范围（共 {total} 页）。")
    if len(selected) > MAX_PAGES:
        raise ValidationError(
            f"一次最多导出 {MAX_PAGES} 页（当前 {len(selected)}）。"
        )
    return selected


def _open_doc(fitz, path: str, password: str | None = None):
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise PDFParseError(f"无法打开 PDF：{exc}") from exc
    if doc.is_encrypted:
        pw = (password or "").strip()
        if not pw:
            doc.close()
            raise ValidationError("PDF 已加密，请提供打开密码。")
        try:
            ok = doc.authenticate(pw)
        except Exception as exc:
            doc.close()
            raise PDFParseError(f"解密失败：{exc}") from exc
        if not ok:
            doc.close()
            raise ValidationError("PDF 密码错误，无法打开。")
    if doc.page_count <= 0:
        doc.close()
        raise PDFParseError("PDF 没有可用的页面。")
    if doc.page_count > MAX_PAGES and not True:
        pass
    return doc


def render_pdf_to_images(
    input_path: str,
    out_dir: str,
    *,
    fmt: str = "png",
    dpi: int = DEFAULT_DPI,
    page_spec: str | None = None,
    password: str | None = None,
    jpeg_quality: int = 85,
    prefix: str = "page",
) -> dict[str, Any]:
    """Render selected pages into ``out_dir``; returns file list + stats."""
    f = (fmt or "png").strip().lower()
    if f in ("jpg", "jpeg"):
        f = "jpeg"
    if f not in FORMATS:
        raise ValidationError(f"format must be one of: {', '.join(FORMATS)}")
    try:
        d = int(dpi)
    except (TypeError, ValueError) as exc:
        raise ValidationError("dpi must be an integer") from exc
    if d < MIN_DPI or d > MAX_DPI:
        raise ValidationError(f"dpi must be between {MIN_DPI} and {MAX_DPI}")
    jq = max(40, min(95, int(jpeg_quality or 85)))

    fitz = _import_fitz()
    doc = _open_doc(fitz, input_path, password)
    total = doc.page_count
    try:
        if total > MAX_PAGES and (not page_spec or not str(page_spec).strip()):
            raise ValidationError(
                f"PDF 页数过多（{total} > {MAX_PAGES}），请用页码范围分批导出。"
            )
        pages = _parse_page_ranges(page_spec, total)
        os.makedirs(out_dir, exist_ok=True)
        zoom = float(d) / 72.0
        mat = fitz.Matrix(zoom, zoom)
        written: list[str] = []
        ext = ".png" if f == "png" else ".jpg"
        for page_no in pages:
            page = doc[page_no - 1]
            pix = page.get_pixmap(matrix=mat, alpha=False)
            name = f"{prefix}-{page_no:03d}{ext}"
            path = os.path.join(out_dir, name)
            try:
                if f == "png":
                    pix.save(path)
                else:
                    pix.save(path, jpg_quality=jq)
            finally:
                pix = None
            written.append(name)
    finally:
        doc.close()

    return {
        "input_pages": total,
        "output_files": len(written),
        "files": written,
        "format": f,
        "dpi": d,
        "pages": pages,
    }


__all__ = [
    "MAX_PAGES",
    "DEFAULT_DPI",
    "MIN_DPI",
    "MAX_DPI",
    "FORMATS",
    "output_formats",
    "render_pdf_to_images",
]
