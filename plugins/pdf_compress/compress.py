"""PDF compression via PyMuPDF.

- ``light``: garbage-collect + deflate streams (keeps text selectable).
- ``balanced`` / ``strong``: rasterize pages to JPEG and rebuild (smaller,
  text becomes image — typical office “强压扫描件” trade-off).
"""

from __future__ import annotations

import os
from typing import Any

from core.errors import PDFParseError, ValidationError

MAX_PAGES = 500

# quality → (mode, jpeg_q, dpi)
# mode: "clean" | "raster"
_PRESETS = {
    "light": ("clean", 0, 0),
    "balanced": ("raster", 75, 120),
    "strong": ("raster", 55, 96),
}


def quality_presets() -> list[str]:
    return list(_PRESETS.keys())


def _import_fitz():
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise ValidationError(
            "服务器未安装 pymupdf，无法压缩 PDF。请执行：pip install pymupdf"
        ) from exc
    return fitz


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
    if doc.page_count > MAX_PAGES:
        n = doc.page_count
        doc.close()
        raise ValidationError(
            f"PDF 页数过多（{n} > {MAX_PAGES}），已超出单次处理上限。"
        )
    return doc


def compress_pdf(
    input_path: str,
    out_path: str,
    *,
    quality: str = "balanced",
    password: str | None = None,
) -> dict[str, Any]:
    """Compress PDF to ``out_path``; returns size / page stats."""
    q = (quality or "balanced").strip().lower()
    if q not in _PRESETS:
        raise ValidationError(f"quality must be one of: {', '.join(_PRESETS)}")
    mode, jpeg_q, dpi = _PRESETS[q]
    fitz = _import_fitz()
    original_bytes = int(os.path.getsize(input_path))
    doc = _open_doc(fitz, input_path, password)
    pages = doc.page_count

    try:
        if mode == "clean":
            doc.save(
                out_path,
                garbage=4,
                deflate=True,
                clean=True,
                ascii=False,
            )
        else:
            out = fitz.open()
            try:
                zoom = float(dpi) / 72.0
                mat = fitz.Matrix(zoom, zoom)
                for page in doc:
                    rect = page.rect
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    try:
                        img_bytes = pix.tobytes("jpeg", jpg_quality=int(jpeg_q))
                    finally:
                        pix = None
                    npage = out.new_page(width=rect.width, height=rect.height)
                    npage.insert_image(npage.rect, stream=img_bytes)
                out.save(
                    out_path,
                    garbage=4,
                    deflate=True,
                    clean=True,
                )
            finally:
                out.close()
    except (PDFParseError, ValidationError):
        raise
    except Exception as exc:
        raise PDFParseError(f"压缩失败：{exc}") from exc
    finally:
        doc.close()

    if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
        raise PDFParseError("压缩结果为空。")

    compressed_bytes = int(os.path.getsize(out_path))
    used_original = False
    if compressed_bytes >= original_bytes:
        import shutil

        shutil.copy2(input_path, out_path)
        compressed_bytes = original_bytes
        used_original = True

    saved = max(0, original_bytes - compressed_bytes)
    percent = round(100.0 * saved / original_bytes, 1) if original_bytes else 0.0
    return {
        "input_pages": pages,
        "original_bytes": original_bytes,
        "compressed_bytes": compressed_bytes,
        "saved_bytes": saved,
        "percent_saved": percent,
        "quality": q,
        "mode": mode,
        "used_original": used_original,
    }


__all__ = ["MAX_PAGES", "compress_pdf", "quality_presets"]
