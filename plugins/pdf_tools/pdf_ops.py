"""PDF 工具集 — 拆分 / 合并 / 解密 / 抽页 核心逻辑。

基于 pypdf 实现，纯 Python 无额外依赖，风格与 ``pdf_merge`` / ``pdf2word``
保持一致。所有函数接收磁盘路径，便于在 ``run_conversion`` 工作线程中执行。
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence, Tuple

from pypdf import PdfReader, PdfWriter

from core.errors import PDFParseError, ValidationError

# 拆分/抽页时单次最多处理的页数（防止超大 PDF 拖垮服务）。
MAX_PAGES = 500


def _open_reader(path: str, password: Optional[str] = None) -> PdfReader:
    """Open a PDF with optional password; maps errors to PDFParseError."""
    try:
        reader = PdfReader(path)
    except Exception as exc:
        raise PDFParseError(f"无法解析 PDF：{exc}") from exc
    if reader.is_encrypted:
        if not password:
            raise ValidationError(
                "PDF 已加密，请输入密码（或勾选「移除密码」前先提供正确密码）。"
            )
        try:
            decrypt_result = reader.decrypt(password)
        except Exception as exc:
            raise PDFParseError(f"解密失败：{exc}") from exc
        # pypdf: 返回 0 表示密码错误，1/2 表示成功；旧版返回 True/False。
        if decrypt_result == 0 or decrypt_result is False:
            raise ValidationError("PDF 密码错误，无法打开。")
    return reader


def _check_pages(count: int) -> None:
    if count <= 0:
        raise PDFParseError("PDF 没有可用的页面。")
    if count > MAX_PAGES:
        raise ValidationError(f"PDF 页数过多（{count} > {MAX_PAGES}），已超出单次处理上限。")


def _parse_page_ranges(
    spec: Optional[str], total: int
) -> List[Tuple[int, int]]:
    """解析页码范围串，如 ``1,3,5-8`` → 归一化为闭区间列表。

    页码从 1 开始；越界页码会被忽略，空结果抛 ValidationError。
    """
    if spec is None or str(spec).strip() == "":
        return [(1, total)]
    ranges: List[Tuple[int, int]] = []
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
            ranges.append((lo, hi))
        else:
            try:
                num = int(part)
            except ValueError as exc:
                raise ValidationError(f"无法解析页码：{part!r}") from exc
            if num < 1:
                raise ValidationError(f"页码必须 ≥ 1：{part!r}")
            ranges.append((num, num))
    if not ranges:
        raise ValidationError("页码范围为空。")
    # 裁剪到文档页数并展平为页号集合（保持顺序、去重）。
    selected: List[int] = []
    seen: set = set()
    for lo, hi in ranges:
        for page in range(lo, hi + 1):
            if 1 <= page <= total and page not in seen:
                seen.add(page)
                selected.append(page)
    if not selected:
        raise ValidationError(f"指定页码超出文档范围（共 {total} 页）。")
    # 转回紧凑区间，供抽页/拆分使用。
    compact: List[Tuple[int, int]] = []
    start = prev = selected[0]
    for page in selected[1:]:
        if page == prev + 1:
            prev = page
        else:
            compact.append((start, prev))
            start = prev = page
    compact.append((start, prev))
    return compact


def _writer_from_reader(
    reader: PdfReader, ranges: List[Tuple[int, int]]
) -> PdfWriter:
    """按区间（1-based 闭区间）从 reader 拷贝页面到新 writer。

    使用 ``add_page`` 逐个引用原页对象，兼容 pypdf 6.x（其
    ``append_pages_from_reader`` 不支持按页区间选择）。
    """
    writer = PdfWriter()
    for lo, hi in ranges:
        for page in range(lo, hi + 1):
            writer.add_page(reader.pages[page - 1])
    return writer


def decrypt_pdf(
    input_path: str,
    out_path: str,
    password: Optional[str] = None,
) -> dict:
    """移除 PDF 加密并写为明文；无密码且未加密时原样拷贝。"""
    reader = _open_reader(input_path, password)
    _check_pages(len(reader.pages))
    writer = _writer_from_reader(reader, [(1, len(reader.pages))])
    with open(out_path, "wb") as f:
        writer.write(f)
    return {"input_pages": len(reader.pages), "output_pages": len(reader.pages)}


def split_pdf(
    input_path: str,
    out_dir: str,
    prefix: str = "page",
    password: Optional[str] = None,
    ranges: Optional[str] = None,
) -> dict:
    """把 PDF 按页拆分为独立文件（默认逐页），写入 ``out_dir``。

    ``ranges`` 指定后仅拆分所选页（文件名保留原页码）。
    """
    reader = _open_reader(input_path, password)
    total = len(reader.pages)
    _check_pages(total)

    selected = _parse_page_ranges(ranges, total) if ranges else None
    parts = _parse_page_ranges(ranges, total) if ranges else [(1, total)]

    written: List[str] = []
    for lo, hi in parts:
        for page in range(lo, hi + 1):
            writer = PdfWriter()
            writer.add_page(reader.pages[page - 1])
            name = f"{prefix}-{page:03d}.pdf"
            path = os.path.join(out_dir, name)
            with open(path, "wb") as f:
                writer.write(f)
            written.append(name)
    return {
        "input_pages": total,
        "output_files": len(written),
        "files": written,
        "selected": selected or [(1, total)],
    }


def merge_pdfs(
    input_paths: Sequence[str],
    out_path: str,
    passwords: Optional[Sequence[Optional[str]]] = None,
) -> dict:
    """按给定顺序合并多个 PDF 为一个。"""
    if not input_paths:
        raise ValidationError("没有可合并的 PDF 文件。")

    writer = PdfWriter()
    total = 0
    for idx, path in enumerate(input_paths):
        pw = None
        if passwords and idx < len(passwords):
            pw = passwords[idx] or None
        reader = _open_reader(path, pw)
        _check_pages(len(reader.pages))
        total += len(reader.pages)
        if total > MAX_PAGES:
            raise ValidationError(
                f"合并后页数过多（{total} > {MAX_PAGES}），已超出单次处理上限。"
            )
        for page in reader.pages:
            writer.add_page(page)

    with open(out_path, "wb") as f:
        writer.write(f)
    return {"input_files": len(input_paths), "output_pages": total}


def extract_pages(
    input_path: str,
    out_path: str,
    page_spec: str,
    password: Optional[str] = None,
) -> dict:
    """按页码范围从 PDF 抽取指定页为新 PDF。"""
    reader = _open_reader(input_path, password)
    total = len(reader.pages)
    _check_pages(total)
    ranges = _parse_page_ranges(page_spec, total)

    writer = _writer_from_reader(reader, ranges)
    with open(out_path, "wb") as f:
        writer.write(f)
    return {
        "input_pages": total,
        "output_pages": sum(hi - lo + 1 for lo, hi in ranges),
        "ranges": ranges,
    }


__all__ = [
    "MAX_PAGES",
    "decrypt_pdf",
    "split_pdf",
    "merge_pdfs",
    "extract_pages",
]
