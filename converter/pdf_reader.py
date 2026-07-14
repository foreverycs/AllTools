from __future__ import annotations

import io
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pdfplumber

# ----- tuning constants -----------------------------------------------------
SNAP_TOLERANCE = 3.0        # grid line snapping tolerance (pt)
LINE_GAP = 3.0              # max vertical gap (pt) to group words into one text line
# max horizontal gap (pt) to keep words in the same text segment; larger gaps
# split one visual line into multiple blocks (e.g. "检验：" … "审核：").
TEXT_COL_GAP = 40.0
MIN_IMAGE_AREA = 40.0 * 40.0  # skip decorative icons smaller than this (pt²)
MAX_IMAGES_PER_PAGE = 15
IMAGE_RENDER_DPI = 144
# Thin filled rectangles / strokes treated as horizontal rules (pt).
HLINE_MAX_THICKNESS = 2.5
HLINE_MIN_WIDTH = 40.0
# Table detection: text-strategy tables must not heavily overlap line tables.
TABLE_OVERLAP_REJECT = 0.45
# Word bbox must cover this fraction of a grid cell to claim a merge span.
SPAN_COVER_RATIO = 0.55
# OCR render resolution (higher than display images for better recognition).
OCR_RENDER_DPI = 200
# Borderless (text-strategy) tables: reject grids that look like prose / multi-col
# layout rather than real forms. Line-based tables bypass these limits.
TEXT_TABLE_MAX_COLS = 6
TEXT_TABLE_MAX_ROWS = 25
TEXT_TABLE_MAX_CELLS = 40
TEXT_TABLE_MIN_FILLED = 4
# Cluster word left edges within this gap (pt) when estimating real columns.
TEXT_COL_CLUSTER_TOL = 18.0


@dataclass
class TextRun:
    """A contiguous styled span inside a cell or paragraph."""
    text: str
    font_size: Optional[float] = None
    font_name: Optional[str] = None


@dataclass
class Cell:
    text: str
    rowspan: int = 1
    colspan: int = 1
    font_size: Optional[float] = None   # dominant font size (pt) in the cell
    font_name: Optional[str] = None     # dominant PDF font name in the cell
    align: str = "left"                 # horizontal alignment: left/center/right
    valign: str = "top"                 # vertical alignment: top/center/bottom
    bg_color: Optional[str] = None        # cell fill colour as RRGGBB hex
    # per-edge borders: dict with keys top/left/bottom/right, each
    # (width_pt, color_hex, dashed) or omitted when no line exists on that edge.
    borders: Optional[dict] = None
    # Nested styles: list of paragraphs, each a list of TextRun spans.
    # When set, the writer prefers this over the flat ``text`` + single font.
    paragraphs: Optional[List[List[TextRun]]] = None


@dataclass
class TableBlock:
    rows: int
    cols: int
    # cells[r][c] holds a Cell only at the top-left (anchor) of a (possibly merged)
    # region. Covered cells are None. owner[r][c] points to the anchor (r, c).
    cells: List[List[Optional[Cell]]]
    owner: List[List[Tuple[int, int]]]
    col_widths: List[float] = field(default_factory=list)   # column widths (pt)
    row_heights: List[float] = field(default_factory=list)  # row heights (pt)
    border_outer: float = 0.5           # outer border width (pt)
    border_inner: float = 0.5           # inner grid line width (pt)
    border_color: str = "000000"        # border colour as RRGGBB hex
    border_dashed: bool = False         # whether borders are dashed
    top: float = 0.0                    # vertical position on page (pt)
    bottom: float = 0.0
    x0: float = 0.0                     # left edge of table bbox (pt)


@dataclass
class TextBlock:
    text: str
    top: float = 0.0
    bottom: float = 0.0
    x0: float = 0.0                     # left edge on the PDF page (pt)
    x1: float = 0.0                     # right edge on the PDF page (pt)
    font_size: Optional[float] = None
    font_name: Optional[str] = None
    align: str = "left"                 # horizontal alignment: left/center/right
    from_ocr: bool = False              # produced by optional OCR on a scan



@dataclass
class ImageBlock:
    """Raster image extracted (or rendered) from the PDF page."""
    image_bytes: bytes                  # PNG bytes
    top: float = 0.0
    bottom: float = 0.0
    x0: float = 0.0                     # left edge on the PDF page (pt)
    width_pt: float = 0.0               # display width in PDF points
    height_pt: float = 0.0
    page_width: float = 0.0             # source page width (pt), for placement
    align: str = "left"                 # left/center/right relative to page


@dataclass
class LineBlock:
    """Standalone horizontal rule (header underline, separator, …)."""
    top: float = 0.0
    bottom: float = 0.0
    x0: float = 0.0
    x1: float = 0.0
    thickness: float = 0.5              # stroke / fill height (pt)
    color: str = "000000"


@dataclass
class PageContent:
    blocks: List  # ordered TextBlock | TableBlock | ImageBlock | LineBlock
    width: float = 0.0                  # page width (pt)
    height: float = 0.0                 # page height (pt)


# ----- low level helpers ----------------------------------------------------
_CJK_RE = re.compile(
    r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s or ""))


def _word_line_sort_key(w: dict) -> Tuple[float, float]:
    """Reading order within one visual line: left-to-right, then top.

    Primary key must be ``x0``. Sorting by ``top`` first breaks Chinese list
    lines such as ``10、进入安装过程`` when the numeric marker sits a fraction
    of a point lower/higher than the CJK run — the marker was appended after
    the body (``、进入安装过程 10``).
    """
    return (float(w["x0"]), float(w.get("top") or 0.0))


def _join_words(words: list) -> str:
    """Join pdfplumber words into a line, keeping CJK characters tight (no
    space between adjacent CJK glyphs) while preserving spaces between Latin
    words and at CJK/Latin boundaries.

    Words are ordered left-to-right (not by vertical baseline) so slightly
    misaligned list markers stay before their text.
    """
    out = []
    prev = None
    for w in sorted(words, key=_word_line_sort_key):
        txt = w["text"]
        if prev is None:
            out.append(txt)
        else:
            prev_cjk = _has_cjk(prev["text"])
            cur_cjk = _has_cjk(txt)
            gap = w["x0"] - prev["x1"]
            if prev_cjk and cur_cjk:
                out.append(txt)
            elif not prev_cjk and not cur_cjk:
                out.append((" " + txt) if gap > 1.0 else txt)
            elif prev_cjk and not cur_cjk:
                # Number / Latin after CJK on the same line (e.g. "项 10") —
                # keep a space. Pure list markers should already be first by x0.
                out.append((" " + txt) if gap > 0.5 else txt)
            else:  # Latin followed by CJK: keep them tight ("10、进入…")
                out.append(txt)
        prev = w
    return "".join(out)


_SP_RE = re.compile(
    r"(?<=[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef])"
    r" +"
    r"(?=[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef])"
)


def _normalize_spacing(text: str) -> str:
    """Remove spaces that sit between two CJK characters (some extractors
    insert one between every glyph)."""
    return _SP_RE.sub("", text)


# Matches a \n that is a soft word-wrap (NOT a real paragraph break).
# A real break is one preceded by sentence-ending punctuation or followed by a
# numbered list marker; everything else is a soft wrap from PDF auto-layout.
_SOFT_NL_RE = re.compile(r"(?<![。！？；：])\n(?!\d[、.])")


def _normalize_newlines(text: str) -> str:
    """Replace soft word-wrap \\n from PDF auto-layout with spaces, while
    preserving real paragraph/list breaks (e.g. after 。 or before 1、)."""
    return _SOFT_NL_RE.sub(" ", text)


def _index_of(value: float, bounds: List[float]) -> Optional[int]:
    for i in range(len(bounds) - 1):
        if bounds[i] <= value < bounds[i + 1]:
            return i
    if bounds and value >= bounds[-1]:
        return len(bounds) - 2
    return None


def _build_table(table, page, words) -> Optional[TableBlock]:
    # In pdfplumber >=0.11 `table.cells` is a flat list of (x0, top, x1, bottom)
    # rects describing the detected grid. A merged region is rendered as a
    # single rect that spans multiple column/row bands, so the rect geometry is
    # the most reliable source for span (rowspan/colspan) information.
    rects = table.cells
    vx = sorted({round(r[0], 1) for r in rects} | {round(r[2], 1) for r in rects})
    hy = sorted({round(r[1], 1) for r in rects} | {round(r[3], 1) for r in rects})
    ncols = len(vx) - 1
    nrows = len(hy) - 1
    if ncols < 1 or nrows < 1:
        return None

    # `extract()` gives the text of every (non-covered) cell.
    logical = table.extract()

    # `words` is the page-level word list (extracted once by the caller) carrying
    # font name/size; we only keep the ones inside this table's bbox.
    x0, top, x1, bottom = table.bbox
    # Lines that lie within this table, used for per-edge border detection.
    table_lines = [
        ln for ln in page.lines
        if not (ln["x0"] < x0 - 1 or ln["x1"] > x1 + 1
                or ln["top"] < top - 1 or ln["bottom"] > bottom + 1)
    ]
    word_font: dict = {}
    word_box: dict = {}  # (r, c) -> (x0, x1, top, bottom) of its text
    # (r, c) -> {rounded_line_top: [min_x0, max_x1]} per text line, used to infer
    # alignment line-by-line (a wrapped paragraph reveals its alignment on every
    # line, not on the union bounding box).
    word_lines: dict = {}
    for w in words:
        if w["x1"] < x0 - 1 or w["x0"] > x1 + 1 or w["bottom"] < top - 1 or w["top"] > bottom + 1:
            continue
        cx = (w["x0"] + w["x1"]) / 2
        cy = (w["top"] + w["bottom"]) / 2
        ci = _index_of(cx, vx)
        ri = _index_of(cy, hy)
        if ci is None or ri is None:
            continue
        size = w.get("size") or 0.0
        fname = w.get("fontname") or ""
        word_font.setdefault((ri, ci), Counter())[(round(size, 1), fname)] += 1
        b = word_box.get((ri, ci))
        if b is None:
            word_box[(ri, ci)] = [w["x0"], w["x1"], w["top"], w["bottom"]]
        else:
            b[0] = min(b[0], w["x0"]); b[1] = max(b[1], w["x1"])
            b[2] = min(b[2], w["top"]); b[3] = max(b[3], w["bottom"])
        lm = word_lines.setdefault((ri, ci), {})
        key = round(w["top"])
        entry = lm.get(key)
        if entry is None:
            lm[key] = [w["x0"], w["x1"]]
        else:
            entry[0] = min(entry[0], w["x0"])
            entry[1] = max(entry[1], w["x1"])

    def _region_font(r0: int, c0: int, r1: int, c1: int):
        merged: Counter = Counter()
        for rr in range(r0, r1 + 1):
            for cc in range(c0, c1 + 1):
                merged.update(word_font.get((rr, cc), Counter()))
        if not merged:
            return None, None
        (size, fname), _ = merged.most_common(1)[0]
        return (size or None), (fname or None)

    def _region_align(r0: int, c0: int, r1: int, c1: int):
        box = None
        votes = []
        for rr in range(r0, r1 + 1):
            for cc in range(c0, c1 + 1):
                b = word_box.get((rr, cc))
                if b is None:
                    continue
                if box is None:
                    box = list(b)
                else:
                    box[0] = min(box[0], b[0]); box[1] = max(box[1], b[1])
                    box[2] = min(box[2], b[2]); box[3] = max(box[3], b[3])
                for line in word_lines.get((rr, cc), {}).values():
                    lx0, lx1 = line
                    lpad = lx0 - vx[c0]
                    rpad = vx[c1 + 1] - lx1
                    if rpad > lpad * 2.5:
                        votes.append("left")
                    elif lpad > rpad * 2.5:
                        votes.append("right")
                    else:
                        votes.append("center")

        if box is None:
            return "left", "top"
        cell_l, cell_r = vx[c0], vx[c1 + 1]
        cell_t, cell_b = hy[r0], hy[r1 + 1]
        cell_h = cell_b - cell_t
        bx0, bx1, bt, bb = box

        # horizontal: take the majority vote across the cell's text lines, so a
        # wrapped (left/centre/right) paragraph is classified by each line rather
        # than by its union bounding box (which would otherwise fill the width
        # and look "centred").
        align = "left"
        if votes:
            align = Counter(votes).most_common(1)[0][0]

        # vertical: use the union text box (single-line cells dominate).
        tpad = bt - cell_t
        bpad = cell_b - bb
        if bpad > tpad * 2.5:
            valign = "top"
        elif tpad > bpad * 2.5:
            valign = "bottom"
        else:
            valign = "center"
        return align, valign

    # Filled rectangles (cell background fills) that belong to this table.
    fill_rects = []
    for rct in page.rects:
        if not rct.get("fill"):
            continue
        if (rct["x0"] < x0 - 1 or rct["x1"] > x1 + 1
                or rct["top"] < top - 1 or rct["bottom"] > bottom + 1):
            continue
        fill_rects.append((rct["x0"], rct["top"], rct["x1"], rct["bottom"],
                           _rgb_to_hex(rct.get("non_stroking_color"))))

    def _region_bg(r0: int, c0: int, r1: int, c1: int):
        cell_l, cell_r = vx[c0], vx[c1 + 1]
        cell_t, cell_b = hy[r0], hy[r1 + 1]
        cell_area = max((cell_r - cell_l) * (cell_b - cell_t), 1e-6)
        best, best_area = None, 0.0
        for (fx0, ftop, fx1, fbottom, color) in fill_rects:
            ix0, ix1 = max(fx0, cell_l), min(fx1, cell_r)
            it0, it1 = max(ftop, cell_t), min(fbottom, cell_b)
            if ix1 <= ix0 or it1 <= it0:
                continue
            area = (ix1 - ix0) * (it1 - it0)
            if area / cell_area >= 0.5 and area > best_area:
                best, best_area = color, area
        return best

    cells: List[List[Optional[Cell]]] = [[None] * ncols for _ in range(nrows)]
    owner: List[List[Tuple[int, int]]] = [
        [(r, c) for c in range(ncols)] for r in range(nrows)
    ]

    for (rx0, rtop, rx1, rbottom) in rects:
        c_start = _index_of(rx0 + 0.5, vx)
        c_end = _index_of(rx1 - 0.5, vx)
        r_start = _index_of(rtop + 0.5, hy)
        r_end = _index_of(rbottom - 0.5, hy)
        if None in (c_start, c_end, r_start, r_end):
            continue
        colspan = c_end - c_start + 1
        # A rect whose bottom edge touches the table boundary actually spans
        # from r_start to the very last row; _index_of clamps to the last row
        # index so we must extend the span manually.
        if abs(rbottom - hy[-1]) < 1.0:
            rowspan = nrows - r_start
        else:
            rowspan = r_end - r_start + 1
        text = ""
        if r_start < len(logical) and c_start < len(logical[r_start]) \
                and logical[r_start][c_start] not in (None, ""):
            text = _normalize_spacing(
                _normalize_newlines(str(logical[r_start][c_start]))
            )
        font_size, font_name = _region_font(r_start, c_start, r_end, c_end)
        align, valign = _region_align(r_start, c_start, r_end, c_end)
        bg_color = _region_bg(r_start, c_start, r_end, c_end)
        borders = _cell_borders(table_lines, vx[c_start], vx[c_end + 1],
                                hy[r_start], hy[r_end + 1])
        paragraphs = _region_paragraphs(
            words, vx, hy, r_start, c_start, r_end, c_end
        )
        if paragraphs and not text:
            text = _paragraphs_to_text(paragraphs)

        if cells[r_start][c_start] is not None:
            continue
        for rr in range(r_start, r_end + 1):
            for cc in range(c_start, c_end + 1):
                owner[rr][cc] = (r_start, c_start)
        cells[r_start][c_start] = Cell(
            text=text,
            rowspan=rowspan,
            colspan=colspan,
            font_size=font_size,
            font_name=font_name,
            align=align,
            valign=valign,
            bg_color=bg_color,
            borders=borders or None,
            paragraphs=paragraphs or None,
        )

    # Text-strategy / partial grids: grow merges when a word bbox spans
    # multiple empty neighbour cells (common for borderless tables).
    _refine_merges_from_words(cells, owner, vx, hy, words, x0, top, x1, bottom)

    # Fill still-empty anchors with word text when extract() left them blank.
    for r in range(nrows):
        for c in range(ncols):
            if owner[r][c] != (r, c):
                continue
            cell = cells[r][c]
            if cell is None:
                continue
            if cell.text.strip() and cell.paragraphs:
                continue
            r1 = r + cell.rowspan - 1
            c1 = c + cell.colspan - 1
            paragraphs = _region_paragraphs(words, vx, hy, r, c, r1, c1)
            if not paragraphs:
                continue
            cell.paragraphs = paragraphs
            if not cell.text.strip():
                cell.text = _paragraphs_to_text(paragraphs)
            if cell.font_size is None or cell.font_name is None:
                fs, fn = _region_font(r, c, r1, c1)
                cell.font_size = cell.font_size or fs
                cell.font_name = cell.font_name or fn

    # Any grid cell still unclaimed (no rect covers it) is part of a merge; it
    # is already marked via `owner`, so leave cells[r][c] as None.
    border = _table_border(table, page)
    col_widths = [round(vx[i + 1] - vx[i], 1) for i in range(ncols)]
    row_heights = [round(hy[i + 1] - hy[i], 1) for i in range(nrows)]
    tx0, ttop, tx1, tbottom = table.bbox
    return TableBlock(
        rows=nrows, cols=ncols, cells=cells, owner=owner,
        col_widths=col_widths, row_heights=row_heights,
        top=float(ttop), bottom=float(tbottom), x0=float(tx0),
        **border,
    )


def _paragraphs_to_text(paragraphs: List[List[TextRun]]) -> str:
    lines = []
    for para in paragraphs:
        lines.append("".join(run.text for run in para))
    return "\n".join(lines)


def _region_paragraphs(
    words,
    vx: List[float],
    hy: List[float],
    r0: int,
    c0: int,
    r1: int,
    c1: int,
) -> List[List[TextRun]]:
    """Build nested paragraphs/runs for a cell region from page words."""
    cell_l, cell_r = vx[c0], vx[c1 + 1]
    cell_t, cell_b = hy[r0], hy[r1 + 1]
    region_words = []
    for w in words:
        cx = (w["x0"] + w["x1"]) / 2
        cy = (w["top"] + w["bottom"]) / 2
        if cell_l - 1 <= cx <= cell_r + 1 and cell_t - 1 <= cy <= cell_b + 1:
            region_words.append(w)
    if not region_words:
        return []

    region_words.sort(key=lambda w: (round((w["top"] + w["bottom"]) / 2.0, 1), w["x0"]))
    lines: List[List[dict]] = []
    for w in region_words:
        if lines and _words_same_visual_line(lines[-1], w):
            lines[-1].append(w)
        else:
            lines.append([w])

    paragraphs: List[List[TextRun]] = []
    for line in lines:
        ordered = sorted(line, key=_word_line_sort_key)
        runs: List[TextRun] = []
        cur_key: Optional[Tuple[float, str]] = None
        buf: List[dict] = []
        for w in ordered:
            size = round(float(w.get("size") or 0.0), 1)
            fname = w.get("fontname") or ""
            key = (size, fname)
            if cur_key is None:
                cur_key = key
                buf = [w]
            elif key == cur_key:
                buf.append(w)
            else:
                text = _normalize_spacing(_join_words(buf))
                if text:
                    runs.append(TextRun(
                        text=text,
                        font_size=cur_key[0] or None,
                        font_name=cur_key[1] or None,
                    ))
                cur_key = key
                buf = [w]
        if buf and cur_key is not None:
            text = _normalize_spacing(_join_words(buf))
            if text:
                runs.append(TextRun(
                    text=text,
                    font_size=cur_key[0] or None,
                    font_name=cur_key[1] or None,
                ))
        if runs:
            paragraphs.append(runs)
    return paragraphs


def _refine_merges_from_words(
    cells: List[List[Optional[Cell]]],
    owner: List[List[Tuple[int, int]]],
    vx: List[float],
    hy: List[float],
    words,
    table_x0: float,
    table_top: float,
    table_x1: float,
    table_bottom: float,
) -> None:
    """Grow merged regions when word boxes span multiple grid cells.

    Borderless (text-strategy) tables often report a full grid of 1×1 cells
    even when a heading visually spans columns. If a word's horizontal extent
    covers several columns of the same row (and those cells are empty or share
    the same anchor), merge them under the left-most anchor.
    """
    nrows = len(cells)
    ncols = len(cells[0]) if cells else 0
    if nrows < 1 or ncols < 2:
        return

    # Collect candidate horizontal spans per row from words.
    for w in words:
        if (w["x1"] < table_x0 - 1 or w["x0"] > table_x1 + 1
                or w["bottom"] < table_top - 1 or w["top"] > table_bottom + 1):
            continue
        cy = (w["top"] + w["bottom"]) / 2
        ri = _index_of(cy, hy)
        if ri is None:
            continue
        c_start = _index_of(w["x0"] + 0.5, vx)
        c_end = _index_of(w["x1"] - 0.5, vx)
        if c_start is None or c_end is None or c_end <= c_start:
            continue

        # Require the word to cover a meaningful portion of each intermediate
        # column so we do not merge on a single overflowing glyph.
        covers_all = True
        for cc in range(c_start, c_end + 1):
            col_l, col_r = vx[cc], vx[cc + 1]
            col_w = max(col_r - col_l, 1e-6)
            overlap = min(w["x1"], col_r) - max(w["x0"], col_l)
            if overlap / col_w < SPAN_COVER_RATIO * 0.5 and cc not in (c_start, c_end):
                covers_all = False
                break
            if cc in (c_start, c_end) and overlap / col_w < 0.15:
                covers_all = False
                break
        if not covers_all:
            continue

        # Anchor = left-most cell owner in this row span that already has content,
        # else the left-most grid cell.
        anchor = owner[ri][c_start]
        ar, ac = anchor
        # Only expand within the same row for horizontal word spans.
        if ar != ri:
            continue
        cell = cells[ar][ac]
        if cell is None:
            # Create a minimal anchor if the grid left a hole.
            cells[ar][ac] = Cell(text="")
            cell = cells[ar][ac]
            owner[ar][ac] = (ar, ac)

        new_c_end = max(ac + cell.colspan - 1, c_end)
        # Refuse merge if an intermediate cell already has different text.
        conflict = False
        for cc in range(ac, new_c_end + 1):
            or_, oc = owner[ri][cc]
            other = cells[or_][oc] if or_ == ri else None
            if other is None or (or_, oc) == (ar, ac):
                continue
            if other.text.strip() and other.text.strip() != (cell.text or "").strip():
                # Different content — not a merge.
                conflict = True
                break
            if other.rowspan > 1:
                conflict = True
                break
        if conflict:
            continue

        # Absorb intermediate 1×1 cells into the anchor.
        for cc in range(ac, new_c_end + 1):
            or_, oc = owner[ri][cc]
            if (or_, oc) == (ar, ac):
                continue
            # Clear absorbed anchor cells (same row only).
            if or_ == ri and cells[or_][oc] is not None and (or_, oc) != (ar, ac):
                cells[or_][oc] = None
            owner[ri][cc] = (ar, ac)
        cell.colspan = new_c_end - ac + 1


def _rgb_to_hex(stroke) -> str:
    """Convert a pdfplumber stroke colour (tuple of 0-1 floats/ints) to RRGGBB."""
    if not isinstance(stroke, (tuple, list)) or len(stroke) < 3:
        return "000000"
    parts = []
    for ch in stroke[:3]:
        try:
            parts.append(f"{int(round(float(ch) * 255)):02X}")
        except (TypeError, ValueError):
            parts.append("00")
    return "".join(parts)


def _cell_borders(lines, rx0: float, rx1: float, rtop: float, rbottom: float,
                  tol: float = 1.0) -> dict:
    """Per-edge border info for one cell rectangle.

    Returns a dict with some of the keys top/left/bottom/right; each value is a
    (width_pt, color_hex, dashed) tuple for the line covering that edge.
    """
    best: dict = {}  # kind -> (value_tuple, overlap)

    def consider(kind: str, ln: dict, overlap: float) -> None:
        cur = best.get(kind)
        width = ln.get("linewidth") or 0.5
        color_src = ln.get("stroking_color")
        if not isinstance(color_src, (tuple, list)):
            color_src = ln.get("stroke")
        if cur is None or overlap > cur[1] or (overlap == cur[1] and width > cur[0][0]):
            val = (width, _rgb_to_hex(color_src), bool(ln.get("dash")))
            best[kind] = (val, overlap)

    for ln in lines:
        if ln.get("linewidth") is None:
            continue
        is_vertical = abs(ln["x0"] - ln["x1"]) < 0.5
        if is_vertical:
            x = ln["x0"]
            y0, y1 = min(ln["top"], ln["bottom"]), max(ln["top"], ln["bottom"])
            ov = min(y1, rbottom) - max(y0, rtop)
            if ov <= 0:
                continue
            if abs(x - rx0) <= tol:
                consider("left", ln, ov)
            if abs(x - rx1) <= tol:
                consider("right", ln, ov)
        else:
            y = ln["top"]
            x0, x1 = min(ln["x0"], ln["x1"]), max(ln["x0"], ln["x1"])
            ov = min(x1, rx1) - max(x0, rx0)
            if ov <= 0:
                continue
            if abs(y - rtop) <= tol:
                consider("top", ln, ov)
            if abs(y - rbottom) <= tol:
                consider("bottom", ln, ov)

    return {k: v[0] for k, v in best.items()}


def _table_border(table, page) -> dict:
    x0, top, x1, bottom = table.bbox
    tol = 1.0
    outer_w, inner_w = [], []
    color = "000000"
    dashed = False

    for line in page.lines:
        # keep only lines that lie within the table area
        if (line["x0"] < x0 - tol or line["x1"] > x1 + tol
                or line["top"] < top - tol or line["bottom"] > bottom + tol):
            continue
        lw = line.get("linewidth") or 0.5
        is_vertical = abs(line["x0"] - line["x1"]) < 0.5
        on_outer = False
        if is_vertical:
            if abs(line["x0"] - x0) <= tol or abs(line["x0"] - x1) <= tol:
                on_outer = True
        else:
            if abs(line["top"] - top) <= tol or abs(line["bottom"] - bottom) <= tol:
                on_outer = True
        (outer_w if on_outer else inner_w).append(lw)
        csrc = line.get("stroking_color")
        if not isinstance(csrc, (tuple, list)):
            csrc = line.get("stroke")
        if isinstance(csrc, (tuple, list)):
            color = _rgb_to_hex(csrc)
        if line.get("dash"):
            dashed = True

    if not outer_w and not inner_w:
        return {"border_outer": 0.5, "border_inner": 0.5,
                "border_color": color, "border_dashed": dashed}
    outer = max(outer_w) if outer_w else (max(inner_w) if inner_w else 0.5)
    inner = max(inner_w) if inner_w else outer
    return {"border_outer": outer, "border_inner": inner,
            "border_color": color, "border_dashed": dashed}


def _table_bbox_overlap_ratio(a, b) -> float:
    """Intersection over smaller-area ratio for two (x0, top, x1, bottom) boxes."""
    ax0, atop, ax1, abottom = a
    bx0, btop, bx1, bbottom = b
    ix0, ix1 = max(ax0, bx0), min(ax1, bx1)
    it0, it1 = max(atop, btop), min(abottom, bbottom)
    if ix1 <= ix0 or it1 <= it0:
        return 0.0
    inter = (ix1 - ix0) * (it1 - it0)
    area_a = max((ax1 - ax0) * (abottom - atop), 1e-6)
    area_b = max((bx1 - bx0) * (bbottom - btop), 1e-6)
    return inter / min(area_a, area_b)


def _iter_page_strokes(page):
    """Yield line-like strokes as (x0, top, x1, bottom) from lines / edges / thin rects."""
    for ln in page.lines or []:
        yield (
            float(ln["x0"]), float(ln["top"]),
            float(ln["x1"]), float(ln["bottom"]),
        )
    for edge in getattr(page, "edges", None) or []:
        try:
            yield (
                float(edge["x0"]), float(edge["top"]),
                float(edge["x1"]), float(edge["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    # Thin filled/stroked rectangles often act as grid lines in forms.
    for rct in page.rects or []:
        try:
            x0, top = float(rct["x0"]), float(rct["top"])
            x1, bottom = float(rct["x1"]), float(rct["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        w = abs(x1 - x0)
        h = abs(bottom - top)
        if w >= HLINE_MIN_WIDTH and h <= HLINE_MAX_THICKNESS * 1.5:
            yield (x0, top, x1, bottom)
        elif h >= HLINE_MIN_WIDTH and w <= HLINE_MAX_THICKNESS * 1.5:
            yield (x0, top, x1, bottom)


def _count_grid_lines_in_bbox(
    page, bbox: Tuple[float, float, float, float], tol: float = 2.0
) -> Tuple[int, int]:
    """Count distinct vertical / horizontal strokes that intersect *bbox*."""
    x0, top, x1, bottom = bbox
    v_xs: List[float] = []
    h_ys: List[float] = []
    for sx0, stop, sx1, sbottom in _iter_page_strokes(page):
        is_v = abs(sx0 - sx1) < 0.75
        is_h = abs(stop - sbottom) < 0.75
        if is_v:
            x = (sx0 + sx1) / 2.0
            y0, y1 = min(stop, sbottom), max(stop, sbottom)
            # Line must run inside the table band and cross it meaningfully.
            if x < x0 - tol or x > x1 + tol:
                continue
            ov = min(y1, bottom) - max(y0, top)
            if ov < max(8.0, (bottom - top) * 0.15):
                continue
            if not any(abs(x - ex) <= tol for ex in v_xs):
                v_xs.append(x)
        elif is_h:
            y = (stop + sbottom) / 2.0
            lx0, lx1 = min(sx0, sx1), max(sx0, sx1)
            if y < top - tol or y > bottom + tol:
                continue
            ov = min(lx1, x1) - max(lx0, x0)
            if ov < max(12.0, (x1 - x0) * 0.15):
                continue
            if not any(abs(y - ey) <= tol for ey in h_ys):
                h_ys.append(y)
    return len(v_xs), len(h_ys)


def _has_drawn_grid(page, bbox: Tuple[float, float, float, float]) -> bool:
    """True when strokes form a real table grid (not just a header underline)."""
    v, h = _count_grid_lines_in_bbox(page, bbox)
    # Minimal grid: 2 vertical + 2 horizontal (one cell), or richer on one axis.
    return (v >= 2 and h >= 2) or (v >= 3 and h >= 1) or (h >= 3 and v >= 1)


def _table_bbox_from_block(tb: TableBlock) -> Tuple[float, float, float, float]:
    x1 = tb.x0 + (sum(tb.col_widths) if tb.col_widths else 0.0)
    if x1 <= tb.x0:
        x1 = tb.x0 + 1.0
    return (tb.x0, tb.top, x1, tb.bottom)


def _words_in_bbox(words, bbox: Tuple[float, float, float, float], pad: float = 1.0):
    x0, top, x1, bottom = bbox
    out = []
    for w in words:
        cx = (w["x0"] + w["x1"]) / 2.0
        cy = (w["top"] + w["bottom"]) / 2.0
        if x0 - pad <= cx <= x1 + pad and top - pad <= cy <= bottom + pad:
            out.append(w)
    return out


def _estimate_aligned_columns(words, x_tol: float = TEXT_COL_CLUSTER_TOL) -> int:
    """How many distinct left-edge columns the words form (alignment clusters)."""
    if not words:
        return 0
    xs = sorted(float(w["x0"]) for w in words)
    clusters = 0
    prev = None
    for x in xs:
        if prev is None or x - prev > x_tol:
            clusters += 1
            prev = x
        else:
            prev = prev  # keep cluster anchor (first x)
    return clusters


def _table_anchor_stats(tb: TableBlock) -> Tuple[List[Cell], List[str], List[int], List[int]]:
    """Return (anchor cells, filled texts, per-col fill counts, per-row fill counts)."""
    anchors: List[Cell] = []
    col_fill = [0] * tb.cols
    row_fill = [0] * tb.rows
    for r in range(tb.rows):
        for c in range(tb.cols):
            if tb.owner[r][c] != (r, c):
                continue
            cell = tb.cells[r][c]
            if cell is None:
                continue
            anchors.append(cell)
            text = (cell.text or "").strip()
            if text:
                col_fill[c] += 1
                row_fill[r] += 1
    filled_texts = [(c.text or "").strip() for c in anchors if (c.text or "").strip()]
    return anchors, filled_texts, col_fill, row_fill


def _inter_column_text_gaps(tb: TableBlock, words) -> List[float]:
    """Horizontal gaps between consecutive non-empty cell texts on the same row.

    Large gaps (label …… value) are typical of forms; small gaps mean the
    detector merely split a prose line into adjacent word chips.
    """
    if not tb.col_widths or tb.cols < 2:
        return []
    vx = [tb.x0]
    for w in tb.col_widths:
        vx.append(vx[-1] + w)
    hy = [tb.top]
    for h in tb.row_heights or []:
        hy.append(hy[-1] + h)
    if len(hy) != tb.rows + 1:
        # Heights missing / inconsistent — fall back to word clustering only.
        return []

    gaps: List[float] = []
    for r in range(tb.rows):
        # Collect (col_index, text_x0, text_x1) for non-empty anchors on this row.
        pieces = []
        for c in range(tb.cols):
            if tb.owner[r][c] != (r, c):
                continue
            cell = tb.cells[r][c]
            if cell is None or not (cell.text or "").strip():
                continue
            # Words whose centre falls in this cell's band.
            cx0, cx1 = vx[c], vx[c + cell.colspan]
            cy0, cy1 = hy[r], hy[min(r + cell.rowspan, tb.rows)]
            xs0, xs1 = [], []
            for w in words:
                wcx = (w["x0"] + w["x1"]) / 2.0
                wcy = (w["top"] + w["bottom"]) / 2.0
                if cx0 - 1 <= wcx <= cx1 + 1 and cy0 - 1 <= wcy <= cy1 + 1:
                    xs0.append(w["x0"])
                    xs1.append(w["x1"])
            if not xs0:
                continue
            pieces.append((c, min(xs0), max(xs1)))
        pieces.sort(key=lambda p: p[0])
        for i in range(len(pieces) - 1):
            # Gap from end of left text to start of right text.
            gap = pieces[i + 1][1] - pieces[i][2]
            gaps.append(gap)
    return gaps


def _is_plausible_borderless_table(tb: TableBlock, words) -> bool:
    """Heuristic gate for tables found without a drawn grid (text strategy).

    pdfplumber's text/text strategy eagerly treats multi-column prose and even
    single-column paragraphs as tables, splitting words across micro-columns.
    Real borderless forms look like compact label/value grids instead.
    """
    if tb.rows < 2 or tb.cols < 2:
        return False
    if tb.cols > TEXT_TABLE_MAX_COLS or tb.rows > TEXT_TABLE_MAX_ROWS:
        return False
    if tb.rows * tb.cols > TEXT_TABLE_MAX_CELLS:
        return False

    anchors, filled_texts, col_fill, row_fill = _table_anchor_stats(tb)
    n_filled = len(filled_texts)
    if n_filled < TEXT_TABLE_MIN_FILLED:
        return False

    # Need stable columns *and* rows (forms), not a single row of word chips.
    strong_cols = sum(1 for n in col_fill if n >= 2)
    strong_rows = sum(1 for n in row_fill if n >= 2)
    if strong_cols < 2 or strong_rows < 2:
        return False

    # Sparse grids from prose alignment (many empty slots) are unreliable.
    n_anchors = max(len(anchors), 1)
    empty_ratio = (n_anchors - n_filled) / n_anchors
    if empty_ratio > 0.55 and tb.cols >= 3:
        return False
    if empty_ratio > 0.65:
        return False

    # Tiny fragments mean the detector split glyphs/words into fake cells.
    tiny = sum(1 for t in filled_texts if len(t) <= 1)
    if tiny / max(n_filled, 1) > 0.2:
        return False
    short = sum(1 for t in filled_texts if len(t) <= 2)
    if short / max(n_filled, 1) > 0.45 and tb.cols >= 3:
        return False

    bbox = _table_bbox_from_block(tb)
    in_words = _words_in_bbox(words, bbox)
    n_words = len(in_words)
    if n_words < TEXT_TABLE_MIN_FILLED:
        return False

    # Over-segmentation: more non-empty cells than source words.
    if n_filled > n_words + 1:
        return False

    # Detected column count should match how text is actually aligned.
    est_cols = _estimate_aligned_columns(in_words)
    if est_cols <= 1:
        return False
    if tb.cols > max(est_cols + 1, int(est_cols * 1.5) + 1):
        return False

    # Adjacent cells with only word-spacing gaps → prose line, not form columns.
    # Real borderless forms leave a clear gutter (often 30–80+ pt) between fields.
    gaps = _inter_column_text_gaps(tb, words)
    gap_mid = None
    if gaps:
        gaps_sorted = sorted(gaps)
        gap_mid = gaps_sorted[len(gaps_sorted) // 2]
        # Most inter-cell gaps look like spaces between words on one line.
        if gap_mid < 18.0:
            return False
        tight = sum(1 for g in gaps if g < 12.0)
        if tight / len(gaps) >= 0.4:
            return False

    # Alternating empty grid rows + tight columns ⇒ line-spacing over-segmentation
    # of prose. Real forms may also insert spacer bands, but keep large gutters.
    empty_rows = sum(1 for n in row_fill if n == 0)
    if empty_rows / max(tb.rows, 1) > 0.35 and (gap_mid is None or gap_mid < 40.0):
        return False

    # Very wide "cells" that are really full prose lines: few columns but long text.
    avg_len = sum(len(t) for t in filled_texts) / max(n_filled, 1)
    if tb.cols == 2 and avg_len > 48 and strong_rows < 3:
        # Two long prose columns (article layout) — keep as text, not a table.
        # Short label/value pairs stay (avg_len small).
        longish = sum(1 for t in filled_texts if len(t) > 36)
        if longish / max(n_filled, 1) >= 0.5:
            return False

    return True


def _accept_table(tb: TableBlock, page, words) -> bool:
    """Keep line-grid tables; only accept borderless ones that look like forms."""
    bbox = _table_bbox_from_block(tb)
    _, filled_texts, _, _ = _table_anchor_stats(tb)
    if not filled_texts:
        return False
    if _has_drawn_grid(page, bbox):
        # Real ruled table: still require a minimal grid shape.
        return tb.rows >= 1 and tb.cols >= 1
    return _is_plausible_borderless_table(tb, words)


def _find_tables(page):
    """Detect tables with a hybrid strategy.

    1. Line-based (best for ruled forms).
    2. Mixed lines/text (vertical rules + horizontal text alignment).
    3. Pure text strategy for borderless grids, only when the region is not
       already covered by a line-based table.

    Candidates are de-duplicated here; plausibility filtering (to drop prose
    mis-detected as tables) happens after :func:`_build_table` via
    :func:`_accept_table`.
    """
    settings_list = [
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "snap_tolerance": SNAP_TOLERANCE,
            "intersection_tolerance": SNAP_TOLERANCE,
        },
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "text",
            "snap_tolerance": SNAP_TOLERANCE,
            "intersection_tolerance": SNAP_TOLERANCE,
            "text_tolerance": 3,
            "text_x_tolerance": 3,
            "text_y_tolerance": 3,
        },
        {
            "vertical_strategy": "text",
            "horizontal_strategy": "lines",
            "snap_tolerance": SNAP_TOLERANCE,
            "intersection_tolerance": SNAP_TOLERANCE,
            "text_tolerance": 3,
            "text_x_tolerance": 3,
            "text_y_tolerance": 3,
        },
        {
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "snap_tolerance": SNAP_TOLERANCE,
            "intersection_tolerance": SNAP_TOLERANCE,
            "text_tolerance": 3,
            "text_x_tolerance": 3,
            "text_y_tolerance": 3,
            # Slightly stricter than pdfplumber defaults so single-column prose
            # is less likely to form a micro-grid of word chips.
            "min_words_vertical": 3,
            "min_words_horizontal": 2,
        },
    ]
    kept = []
    for settings in settings_list:
        try:
            found = page.find_tables(settings) or []
        except Exception:
            found = []
        for t in found:
            bbox = t.bbox
            # Need at least a 2×1 or 1×2 structure after build; skip tiny noise.
            if (bbox[2] - bbox[0]) < 20 or (bbox[3] - bbox[1]) < 10:
                continue
            if any(
                _table_bbox_overlap_ratio(bbox, existing.bbox) >= TABLE_OVERLAP_REJECT
                for existing in kept
            ):
                continue
            kept.append(t)
    return kept


def _in_any_bbox(word: dict, bboxes: List[Tuple[float, float, float, float]]) -> bool:
    cx = (word["x0"] + word["x1"]) / 2
    cy = (word["top"] + word["bottom"]) / 2
    for (bx0, btop, bx1, bbottom) in bboxes:
        if bx0 - 1 <= cx <= bx1 + 1 and btop - 1 <= cy <= bbottom + 1:
            return True
    return False


def _text_h_align(x0: float, x1: float, page_w: float) -> str:
    """Infer paragraph alignment from the text bbox relative to the page."""
    if page_w <= 0:
        return "left"
    width = max(x1 - x0, 1.0)
    left_pad = max(x0, 0.0)
    right_pad = max(page_w - x1, 0.0)
    mid = (x0 + x1) / 2.0 / page_w
    # Full-ish width lines stay left.
    if width / page_w >= 0.7:
        return "left"
    # Balanced side margins or midpoint near page centre → centre.
    if abs(left_pad - right_pad) <= max(page_w * 0.12, 18.0) or 0.38 < mid < 0.62:
        # Prefer centre only when not clearly flush-left (logo-adjacent labels
        # often start past 0.25 of the page but are not titles).
        if left_pad > page_w * 0.18 or abs(left_pad - right_pad) <= page_w * 0.12:
            return "center"
    if left_pad > right_pad * 2.0 and left_pad / page_w > 0.45:
        return "right"
    return "left"


def _word_mid_y(w: dict) -> float:
    return (float(w["top"]) + float(w["bottom"])) / 2.0


def _words_same_visual_line(line: List[dict], w: dict) -> bool:
    """True when ``w`` belongs on the same visual text line as ``line``.

    Uses vertical centres / band overlap so a list marker that is a couple of
    points higher or lower than the body text still joins the same line
    (``10、进入安装过程``), instead of becoming a separate block that may
    later reorder as ``、进入安装过程 10``.
    """
    if not line:
        return True
    tops = [float(x["top"]) for x in line]
    bottoms = [float(x["bottom"]) for x in line]
    line_top, line_bot = min(tops), max(bottoms)
    line_mid = (line_top + line_bot) / 2.0
    w_top, w_bot = float(w["top"]), float(w["bottom"])
    w_mid = (w_top + w_bot) / 2.0
    # Centres close, or vertical ranges overlap with modest offset.
    if abs(w_mid - line_mid) <= LINE_GAP * 1.5:
        return True
    if not (w_bot < line_top - 0.5 or w_top > line_bot + 0.5):
        if abs(w_mid - line_mid) <= max(LINE_GAP * 2.5, 8.0):
            return True
    # Legacy sequential check (word just below previous word on the line).
    last = line[-1]
    if w_top - float(last["bottom"]) <= LINE_GAP and abs(w_mid - line_mid) <= 12.0:
        return True
    return False


def _extract_text_blocks(page, table_bboxes, words) -> List[TextBlock]:
    outside = [w for w in words if not _in_any_bbox(w, table_bboxes)]
    if not outside:
        return []

    # Group by vertical band using mid-Y first so slightly misaligned markers
    # (list numbers) cluster with their body text before left-to-right join.
    outside.sort(key=lambda w: (round(_word_mid_y(w), 1), w["x0"]))
    lines: List[List[dict]] = []
    for w in outside:
        if lines and _words_same_visual_line(lines[-1], w):
            lines[-1].append(w)
        else:
            lines.append([w])
    page_w = float(getattr(page, "width", 0) or 0)
    blocks: List[TextBlock] = []
    for line in lines:
        ordered = sorted(line, key=_word_line_sort_key)
        # Split a visual line into horizontal segments when words sit far apart
        # (form labels on opposite sides, header title next to logo, etc.).
        segments: List[List[dict]] = []
        current: List[dict] = []
        prev_x1 = None
        for w in ordered:
            if current and prev_x1 is not None and (w["x0"] - prev_x1) > TEXT_COL_GAP:
                segments.append(current)
                current = []
            current.append(w)
            prev_x1 = w["x1"]
        if current:
            segments.append(current)

        for seg in segments:
            text = _normalize_spacing(_join_words(seg))
            if not text.strip():
                continue
            top = min(w["top"] for w in seg)
            bottom = max(w["bottom"] for w in seg)
            x0 = min(w["x0"] for w in seg)
            x1 = max(w["x1"] for w in seg)
            counter: Counter = Counter()
            for w in seg:
                counter[(round(w.get("size") or 0.0, 1), w.get("fontname") or "")] += 1
            (size, fname), _ = counter.most_common(1)[0]
            align = _text_h_align(x0, x1, page_w)
            blocks.append(TextBlock(
                text=text.strip(),
                top=top, bottom=bottom, x0=x0, x1=x1,
                font_size=size or None, font_name=fname or None,
                align=align,
            ))
    return blocks



def _bbox_overlap_ratio(a, b) -> float:
    """Intersection area of ``a`` over area of ``a`` (both x0,top,x1,bottom)."""
    ax0, atop, ax1, abottom = a
    bx0, btop, bx1, bbottom = b
    ix0, ix1 = max(ax0, bx0), min(ax1, bx1)
    it0, it1 = max(atop, btop), min(abottom, bbottom)
    if ix1 <= ix0 or it1 <= it0:
        return 0.0
    inter = (ix1 - ix0) * (it1 - it0)
    area = max((ax1 - ax0) * (abottom - atop), 1e-6)
    return inter / area


def _render_region_png(page, bbox, resolution: int = IMAGE_RENDER_DPI) -> Optional[bytes]:
    """Rasterise a page region to PNG bytes. Returns None on failure."""
    try:
        cropped = page.crop(bbox, strict=False)
        pil = cropped.to_image(resolution=resolution).original
        if pil is None:
            return None
        # Drop nearly-blank crops (e.g. failed extract of vector-only art).
        extrema = pil.convert("L").getextrema()
        if extrema is not None and extrema[0] == extrema[1]:
            return None
        buf = io.BytesIO()
        pil.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def _image_h_align(x0: float, width: float, page_w: float) -> str:
    """Infer horizontal placement of an image relative to the page content box."""
    if page_w <= 0 or width <= 0:
        return "left"
    # Near full width → treat as centered full-bleed content.
    if width / page_w >= 0.85:
        return "center"
    left_pad = max(x0, 0.0)
    right_pad = max(page_w - (x0 + width), 0.0)
    # Balanced side margins → centre; otherwise keep flush to the denser side.
    if abs(left_pad - right_pad) <= max(page_w * 0.08, 12.0):
        return "center"
    if left_pad > right_pad * 2.0 and left_pad / page_w > 0.2:
        return "right"
    return "left"


def _extract_images(page, table_bboxes) -> List[ImageBlock]:
    """Pull embedded image regions that sit outside tables."""
    raw = getattr(page, "images", None) or []
    if not raw:
        return []

    page_w = float(getattr(page, "width", 0) or 0)
    page_h = float(getattr(page, "height", 0) or 0)
    page_area = max(page_w * page_h, 1.0)
    blocks: List[ImageBlock] = []

    # Sort top-to-bottom, left-to-right for stable ordering.
    ordered_imgs = sorted(
        raw,
        key=lambda im: (round(im.get("top", 0), 1), round(im.get("x0", 0), 1)),
    )
    for img in ordered_imgs:
        if len(blocks) >= MAX_IMAGES_PER_PAGE:
            break
        try:
            x0 = float(img["x0"])
            top = float(img["top"])
            x1 = float(img["x1"])
            bottom = float(img["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        w, h = x1 - x0, bottom - top
        if w <= 1 or h <= 1 or w * h < MIN_IMAGE_AREA:
            continue
        # Skip near-full-page images here; empty-page fallback handles scans.
        if page_area > 0 and (w * h) / page_area > 0.85:
            continue
        bbox = (x0, top, x1, bottom)
        if any(_bbox_overlap_ratio(bbox, tb) > 0.5 for tb in table_bboxes):
            continue
        png = _render_region_png(page, bbox)
        if not png:
            continue
        blocks.append(ImageBlock(
            image_bytes=png,
            top=top,
            bottom=bottom,
            x0=x0,
            width_pt=w,
            height_pt=h,
            page_width=page_w,
            align=_image_h_align(x0, w, page_w),
        ))
    return blocks


def _render_full_page_image(page) -> Optional[ImageBlock]:
    """Fallback for scanned / image-only pages: embed a full-page raster."""
    try:
        w = float(getattr(page, "width", 0) or 0)
        h = float(getattr(page, "height", 0) or 0)
        if w <= 0 or h <= 0:
            return None
        png = _render_region_png(page, (0, 0, w, h), resolution=IMAGE_RENDER_DPI)
        if not png:
            return None
        return ImageBlock(
            image_bytes=png,
            top=0.0,
            bottom=h,
            x0=0.0,
            width_pt=w,
            height_pt=h,
            page_width=w,
            align="center",
        )
    except Exception:
        return None


def _extract_hlines(page, table_bboxes) -> List[LineBlock]:
    """Standalone horizontal rules outside tables (header underlines, etc.).

    Many forms draw the header bar as a very thin filled rectangle rather than
    a stroked line; both sources are considered.
    """
    page_w = float(getattr(page, "width", 0) or 0)
    candidates: List[tuple] = []  # (top, x0, x1, thickness, color)

    for ln in page.lines or []:
        try:
            x0, x1 = float(ln["x0"]), float(ln["x1"])
            top, bottom = float(ln["top"]), float(ln["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        width = abs(x1 - x0)
        height = abs(bottom - top)
        # horizontal stroke: wide and nearly zero height
        if width < HLINE_MIN_WIDTH or height > HLINE_MAX_THICKNESS:
            continue
        if height < 1e-3 and width >= HLINE_MIN_WIDTH:
            height = float(ln.get("linewidth") or 0.5)
        color = _rgb_to_hex(ln.get("stroking_color") or ln.get("stroke"))
        candidates.append((min(top, bottom), min(x0, x1), max(x0, x1),
                           max(height, 0.3), color))

    for rct in page.rects or []:
        try:
            x0, x1 = float(rct["x0"]), float(rct["x1"])
            top, bottom = float(rct["top"]), float(rct["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        width = abs(x1 - x0)
        height = abs(bottom - top)
        if width < HLINE_MIN_WIDTH or height <= 0 or height > HLINE_MAX_THICKNESS:
            continue
        # Prefer filled thin rects (common for header bars).
        if not rct.get("fill") and not rct.get("stroke"):
            continue
        color_src = rct.get("non_stroking_color") if rct.get("fill") else (
            rct.get("stroking_color") or rct.get("stroke")
        )
        color = _rgb_to_hex(color_src)
        candidates.append((min(top, bottom), min(x0, x1), max(x0, x1),
                           height, color))

    blocks: List[LineBlock] = []
    for top, x0, x1, thick, color in sorted(candidates, key=lambda c: c[0]):
        # Skip lines that sit on / inside a table (grid lines).
        mid_y = top + thick / 2.0
        mid_x = (x0 + x1) / 2.0
        if any(
            bx0 - 1 <= mid_x <= bx1 + 1 and btop - 1 <= mid_y <= bbottom + 1
            for (bx0, btop, bx1, bbottom) in table_bboxes
        ):
            continue
        # Deduplicate near-identical rules.
        if any(
            abs(b.top - top) < 1.5 and abs(b.x0 - x0) < 2 and abs(b.x1 - x1) < 2
            for b in blocks
        ):
            continue
        blocks.append(LineBlock(
            top=top,
            bottom=top + thick,
            x0=x0,
            x1=x1,
            thickness=thick,
            color=color or "000000",
        ))
    return blocks


def _extract_page(page, *, ocr: bool = False, ocr_lang: Optional[str] = None) -> PageContent:
    # Extract words (with font info) and lines once for the whole page and reuse
    # them for every table and the text blocks, instead of re-parsing per table.
    words = page.extract_words(
        use_text_flow=False, keep_blank_chars=False, extra_attrs=["fontname", "size"]
    )
    page_w = float(getattr(page, "width", 0) or 0)
    page_h = float(getattr(page, "height", 0) or 0)
    raw_tables = _find_tables(page)
    tables = []          # list of (top, TableBlock)
    bboxes = []
    for t in raw_tables:
        tb = _build_table(t, page, words)
        if tb is None:
            continue
        # Drop text-strategy false positives (plain prose / multi-col layout
        # misread as a grid) so content stays as TextBlock / ImageBlock.
        if not _accept_table(tb, page, words):
            continue
        tables.append((tb.top, tb))
        bboxes.append(t.bbox)

    text_blocks = _extract_text_blocks(page, bboxes, words)
    image_blocks = _extract_images(page, bboxes)
    line_blocks = _extract_hlines(page, bboxes)

    # interleave text, tables, images and rules by vertical position
    ordered = (
        [(top, tb) for top, tb in tables]
        + [(b.top, b) for b in text_blocks]
        + [(b.top, b) for b in image_blocks]
        + [(b.top, b) for b in line_blocks]
    )
    ordered.sort(key=lambda item: item[0])
    blocks: List = [tb for _, tb in ordered]

    # Scanned / image-only page: no extractable text or tables.
    has_text_or_table = any(
        isinstance(b, (TextBlock, TableBlock)) for b in blocks
    )
    if not has_text_or_table:
        ocr_blocks: List[TextBlock] = []
        if ocr:
            ocr_blocks = _ocr_page_to_text_blocks(page, lang=ocr_lang)
        if ocr_blocks:
            # Prefer editable OCR text; keep a light full-page image behind? No —
            # OCR text alone is the editable output; caller can re-run without OCR
            # for image-only. Still attach page image only when OCR found nothing.
            blocks = sorted(ocr_blocks, key=lambda b: b.top)
        else:
            full = _render_full_page_image(page)
            if full is not None:
                blocks = [full]

    return PageContent(blocks=blocks, width=page_w, height=page_h)


def _ocr_page_to_text_blocks(page, *, lang: Optional[str] = None) -> List[TextBlock]:
    """Rasterise the page and OCR into TextBlocks (empty list if OCR unavailable)."""
    from .ocr import ocr_available, ocr_image_to_blocks

    if not ocr_available():
        return []
    try:
        w = float(getattr(page, "width", 0) or 0)
        h = float(getattr(page, "height", 0) or 0)
        if w <= 0 or h <= 0:
            return []
        png = _render_region_png(page, (0, 0, w, h), resolution=OCR_RENDER_DPI)
        if not png:
            return []
        return ocr_image_to_blocks(png, page_width=w, page_height=h, lang=lang)
    except Exception:
        return []


def parse_page_range(spec: Optional[str], total_pages: int) -> List[int]:
    """Parse a 1-based page range like ``1-3,5,7-9`` into 0-based indices.

    Empty / whitespace ``spec`` means all pages. Raises ``ValueError`` on
    malformed input or out-of-range numbers.
    """
    if total_pages < 1:
        raise ValueError("PDF has no pages")
    if not spec or not str(spec).strip():
        return list(range(total_pages))

    indices: List[int] = []
    seen = set()
    for part in str(spec).split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            ends = token.split("-", 1)
            if len(ends) != 2 or not ends[0].strip() or not ends[1].strip():
                raise ValueError(f"Invalid page range: {token!r}")
            try:
                start = int(ends[0].strip())
                end = int(ends[1].strip())
            except ValueError as exc:
                raise ValueError(f"Invalid page range: {token!r}") from exc
            if start < 1 or end < 1 or start > end:
                raise ValueError(f"Invalid page range: {token!r}")
            for n in range(start, end + 1):
                if n > total_pages:
                    raise ValueError(
                        f"Page {n} out of range (PDF has {total_pages} pages)"
                    )
                idx = n - 1
                if idx not in seen:
                    seen.add(idx)
                    indices.append(idx)
        else:
            try:
                n = int(token)
            except ValueError as exc:
                raise ValueError(f"Invalid page number: {token!r}") from exc
            if n < 1 or n > total_pages:
                raise ValueError(
                    f"Page {n} out of range (PDF has {total_pages} pages)"
                )
            idx = n - 1
            if idx not in seen:
                seen.add(idx)
                indices.append(idx)

    if not indices:
        raise ValueError("No pages selected")
    return indices


def count_blocks(pages: List[PageContent]) -> dict:
    """Return simple conversion stats for response headers / UI."""
    tables = sum(
        1 for p in pages for b in p.blocks if isinstance(b, TableBlock)
    )
    texts = sum(
        1 for p in pages for b in p.blocks if isinstance(b, TextBlock)
    )
    images = sum(
        1 for p in pages for b in p.blocks if isinstance(b, ImageBlock)
    )
    lines = sum(
        1 for p in pages for b in p.blocks if isinstance(b, LineBlock)
    )
    return {
        "pages": len(pages),
        "tables": tables,
        "text_blocks": texts,
        "images": images,
        "lines": lines,
    }


def content_warnings(pages: List[PageContent]) -> List[str]:
    """Heuristic warnings for the UI (scanned PDF, empty extract, …)."""
    stats = count_blocks(pages)
    warnings: List[str] = []
    if stats["pages"] == 0:
        warnings.append("empty")
        return warnings
    if (
        stats["tables"] == 0
        and stats["text_blocks"] == 0
        and stats["images"] == 0
    ):
        warnings.append("empty")
    elif stats["tables"] == 0 and stats["text_blocks"] == 0 and stats["images"] > 0:
        warnings.append("image_only")
    # OCR produced text from a scan (no native PDF text layer was present for
    # those pages) — still useful for UI messaging when only OCR text exists
    # without tables and the source was image-heavy. Detected via flag on blocks.
    if any(getattr(b, "from_ocr", False) for p in pages for b in p.blocks):
        warnings.append("ocr_applied")
    return warnings


def _friendly_open_error(exc: BaseException) -> str:
    msg = str(exc).lower()
    if any(k in msg for k in ("password", "encrypt", "crypt")):
        return "PDF is password-protected; please decrypt it first"
    return f"Cannot open PDF: {exc}"


def extract_document(
    pdf_path: str,
    page_range: Optional[str] = None,
    *,
    ocr: bool = False,
    ocr_lang: Optional[str] = None,
) -> List[PageContent]:
    """Extract structured content from a PDF.

    ``page_range`` is an optional 1-based spec (e.g. ``"1-3,5"``). When
    omitted, every page is processed.

    Image-only / scanned pages are embedded as full-page rasters by default.
    Pass ``ocr=True`` to run optional Tesseract OCR (requires ``pytesseract``
    and a system Tesseract install) so scanned text becomes editable.
    """
    # Env override: PDF2WORD_OCR=1 enables OCR even if the caller omitted it.
    if not ocr:
        env = (os.environ.get("PDF2WORD_OCR") or "").strip().lower()
        ocr = env in ("1", "true", "yes", "on")
    if ocr_lang is None:
        ocr_lang = os.environ.get("PDF2WORD_OCR_LANG") or None

    pages: List[PageContent] = []
    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as exc:
        raise ValueError(_friendly_open_error(exc)) from exc

    try:
        total = len(pdf.pages)
        if total == 0:
            raise ValueError("PDF has no pages")
        indices = parse_page_range(page_range, total)
        for i in indices:
            try:
                pages.append(
                    _extract_page(pdf.pages[i], ocr=ocr, ocr_lang=ocr_lang)
                )
            except Exception as exc:
                raise ValueError(
                    _friendly_open_error(exc)
                    if any(k in str(exc).lower() for k in ("password", "encrypt", "crypt"))
                    else f"Failed to read page {i + 1}: {exc}"
                ) from exc
    finally:
        pdf.close()
    return pages
