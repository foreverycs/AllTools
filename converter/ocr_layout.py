"""Compose a scanned page into editable blocks (OCR text + raster geometry).

One Tesseract pass yields every recognised token on the page; the ruled grid
and the seal come from the same raster (see :mod:`converter.raster`).  This
module splits the page into three block kinds:

* tokens whose centres fall inside the grid -> one :class:`TableBlock`
* saturated colour regions                  -> RGBA :class:`ImageBlock` (seal)
* every other OCR line                      -> merged :class:`TextBlock` wraps

Splitting by token centre (rather than running OCR per cell) keeps a single
page-wide size cluster, so cells of the same text keep the same font size.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from .models import Cell, ImageBlock, TableBlock, TextBlock
from .ocr import OcrLine, OcrWord, _line_to_block, join_ocr_words
from .raster import RasterGrid, color_regions, cut_color_png, detect_ruled_grid
from .tables import _index_of
from .text_utils import _merge_soft_wrap_text_blocks


def _cell_of(w: OcrWord, x_lines: Sequence[float], y_lines: Sequence[float]) -> Optional[Tuple[int, int]]:
    """Cell containing the token's centre, or None when it is off-grid.

    ``_index_of`` clamps out-of-range values into the edge band (it exists for
    pdfplumber words already queried inside the table bbox), so the grid bbox
    must be rejected explicitly — otherwise the signature line below the table
    would be swallowed into its last row.
    """
    cy = (w.top + w.bottom) / 2.0
    cx = (w.x0 + w.x1) / 2.0
    if not (y_lines[0] - 2.0 <= cy <= y_lines[-1] + 2.0):
        return None
    if not (x_lines[0] - 2.0 <= cx <= x_lines[-1] + 2.0):
        return None
    ri = _index_of(cy, y_lines)
    ci = _index_of(cx, x_lines)
    if ri is None or ci is None:
        return None
    return ri, ci


def _split_lines_by_grid(
    lines: Sequence[OcrLine], grid: RasterGrid
) -> Tuple[List[Tuple[int, int, dict]], List[OcrLine]]:
    """Partition OCR lines into grid-cell segments and free (non-table) lines.

    A line belongs to the table only when most of its tokens land in cells;
    a title above the grid or a caption beside it stays free text.
    """
    x_lines, y_lines = grid.x_lines, grid.y_lines
    segs: Dict[Tuple[int, int], List[dict]] = {}
    free: List[OcrLine] = []
    for ln in lines:
        if not ln.words:
            free.append(ln)
            continue
        buckets: Dict[Tuple[int, int], List[OcrWord]] = {}
        for w in ln.words:
            cell = _cell_of(w, x_lines, y_lines)
            if cell is None:
                continue
            buckets.setdefault(cell, []).append(w)
        placed = sum(len(v) for v in buckets.values())
        if placed < max(1, int(0.6 * len(ln.words))):
            free.append(ln)
            continue
        for (ri, ci), ws in buckets.items():
            ws.sort(key=lambda w: w.x0)
            segs.setdefault((ri, ci), []).append({
                "top": min(w.top for w in ws),
                "bottom": max(w.bottom for w in ws),
                "x0": min(w.x0 for w in ws),
                "x1": max(w.x1 for w in ws),
                "size": ln.font_size,
                "words": ws,
            })
    return [(k[0], k[1], v) for k, v in segs.items()], free


def _cell_align_valign(
    items: Sequence[dict], rect: Tuple[float, float, float, float]
) -> Tuple[str, str]:
    """Per-line pad vote (matches the pdfplumber table path) + box-based valign."""
    cx0, cy0, cx1, cy1 = rect
    votes = []
    for s in items:
        lpad = s["x0"] - cx0
        rpad = cx1 - s["x1"]
        if rpad > lpad * 2.5:
            votes.append("left")
        elif lpad > rpad * 2.5:
            votes.append("right")
        else:
            votes.append("center")
    align = Counter(votes).most_common(1)[0][0] if votes else "left"

    bt = min(s["top"] for s in items)
    bb = max(s["bottom"] for s in items)
    tpad = bt - cy0
    bpad = cy1 - bb
    if bpad > tpad * 2.5:
        valign = "top"
    elif tpad > bpad * 2.5:
        valign = "bottom"
    else:
        valign = "center"
    return align, valign


def _build_ocr_table(
    grid: RasterGrid, lines: Sequence[OcrLine]
) -> Tuple[Optional[TableBlock], List[OcrLine]]:
    """TableBlock from grid + the free lines that fall outside it."""
    segs, free = _split_lines_by_grid(lines, grid)
    if not segs:
        return None, list(lines)

    rects = grid.cell_rects()
    rows, cols = grid.rows, grid.cols
    cells: List[List[Optional[Cell]]] = [[None] * cols for _ in range(rows)]
    owner: List[List[Tuple[int, int]]] = [
        [(r, c) for c in range(cols)] for r in range(rows)
    ]
    content: Dict[Tuple[int, int], List[dict]] = {}
    for r, c, items in segs:
        content.setdefault((r, c), []).extend(items)

    for r in range(rows):
        for c in range(cols):
            items = content.get((r, c))
            if not items:
                # Every grid position needs an anchor Cell — the writer skips
                # only cells covered by a merge (owner != self).
                cells[r][c] = Cell(text="")
                continue
            items.sort(key=lambda s: s["top"])
            text = "\n".join(
                join_ocr_words(s["words"], height=s["bottom"] - s["top"])
                for s in items
            )
            sizes = Counter(round(s["size"], 1) for s in items if s.get("size"))
            size = float(sizes.most_common(1)[0][0]) if sizes else None
            align, valign = _cell_align_valign(items, rects[r][c])
            cells[r][c] = Cell(
                text=text, font_size=size, font_name="宋体",
                align=align, valign=valign,
            )

    # No cell text means the grid was a false positive (or OCR missed it) —
    # an empty box is worse than plain text lines.
    if not any(cell is not None and cell.text.strip() for row in cells for cell in row):
        return None, list(lines)

    gx0, gy0, gx1, gy1 = grid.bbox
    thickness = max(0.5, float(grid.thickness or 0.5))
    table = TableBlock(
        rows=rows, cols=cols, cells=cells, owner=owner,
        col_widths=[round(grid.x_lines[i + 1] - grid.x_lines[i], 1) for i in range(cols)],
        row_heights=[round(grid.y_lines[i + 1] - grid.y_lines[i], 1) for i in range(rows)],
        border_outer=thickness, border_inner=thickness,
        border_color="000000", border_dashed=False,
        top=float(gy0), bottom=float(gy1), x0=float(gx0),
    )
    return table, free


def _seal_blocks(
    img, page_width: float, page_height: float
) -> List[ImageBlock]:
    """Coloured regions (seals / stamps) as transparent PNG ImageBlocks.

    Alignment is "left" + x0 indent so the writer places the stamp at its
    exact page position (centre/right align would collapse that to a guess).
    """
    out: List[ImageBlock] = []
    for rect in color_regions(img, page_width, page_height):
        png = cut_color_png(img, rect, page_width, page_height)
        if not png:
            continue
        x0, y0, x1, y1 = rect
        out.append(ImageBlock(
            image_bytes=png, top=y0, bottom=y1, x0=x0,
            width_pt=x1 - x0, height_pt=y1 - y0,
            page_width=page_width, align="left",
        ))
    return out


def compose_ocr_blocks(
    img,
    lines: Sequence[OcrLine],
    *,
    page_width: float,
    page_height: float,
) -> List:
    """Full block list for a scanned page, vertically ordered."""
    table: Optional[TableBlock] = None
    free = list(lines)
    if lines:
        grid = detect_ruled_grid(img, page_width, page_height)
        if grid is not None:
            table, free = _build_ocr_table(grid, lines)

    blocks: List = []
    if table is not None:
        blocks.append(table)

    text_blocks: List[TextBlock] = [_line_to_block(ln) for ln in free]
    if text_blocks:
        content_right = max(ln.x1 for ln in free)
        blocks.extend(
            _merge_soft_wrap_text_blocks(text_blocks, page_right=content_right)
        )

    blocks.extend(_seal_blocks(img, page_width, page_height))
    blocks.sort(key=lambda b: float(getattr(b, "top", 0.0) or 0.0))
    return blocks


__all__ = ["compose_ocr_blocks"]
