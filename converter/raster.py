"""Raster geometry helpers for scanned pages (pure Pillow).

OCR turns a scan into text, which drops everything that is *drawn* rather than
typed: the ruled grid of a form and the stamps/seals on a certificate.  Both
are recoverable from the same raster the OCR engine already reads, so this
module works on a rendered page image and reports geometry back in PDF points.

Nothing here needs numpy/OpenCV: row/column profiles come from Pillow's
``Image.resize`` box filter, which averages a whole axis in one call.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageChops

# Pixels darker than this count as printed ink.
INK_LUMA = int(os.environ.get("PDF2WORD_RASTER_INK_LUMA") or "180")
# A row/column must be inked over this share of its extent to read as a rule.
RULE_FRAC = float(os.environ.get("PDF2WORD_RASTER_RULE_FRAC") or "0.45")
# Inside a table band vertical rules are near-solid; plain text columns are not.
VRULE_FRAC = 0.70
# Two rules belong to one grid when their x-extents overlap this much
# (relative to the shorter rule).
RULE_X_OVERLAP = 0.40
# Saturation (max-min channel) at or above this is a coloured stamp, not paper.
COLOR_SAT = 60
# Coloured ink darker than this is black text showing through the stamp.
COLOR_MIN_LUMA = 70
# Ignore colour specks smaller than this (dust, moiré) and stitch bands that
# are closer together than this (one seal split by a text row).
MIN_COLOR_PT = 20.0
COLOR_BAND_GAP_PT = 40.0
# Sanity caps so a barcode / hatching does not become a 500-cell table.
MAX_GRID_CELLS = 400


@dataclass
class RasterGrid:
    """Ruled grid found on a scan, in PDF points."""

    x_lines: List[float]   # vertical rules, sorted
    y_lines: List[float]   # horizontal rules, sorted
    thickness: float       # measured stroke width (pt)

    @property
    def rows(self) -> int:
        return max(len(self.y_lines) - 1, 0)

    @property
    def cols(self) -> int:
        return max(len(self.x_lines) - 1, 0)

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return (self.x_lines[0], self.y_lines[0], self.x_lines[-1], self.y_lines[-1])

    def cell_rects(self) -> List[List[Tuple[float, float, float, float]]]:
        """``cell_rects[r][c] == (x0, y0, x1, y1)`` for every grid cell."""
        out = []
        for r in range(self.rows):
            row = []
            for c in range(self.cols):
                row.append((
                    self.x_lines[c], self.y_lines[r],
                    self.x_lines[c + 1], self.y_lines[r + 1],
                ))
            out.append(row)
        return out


def _row_profile(img: Image.Image) -> List[float]:
    """Mean pixel value per row, 0.0–1.0 (one box-filter resize per axis)."""
    w, h = img.size
    if w < 1 or h < 1:
        return []
    strip = img.resize((1, h), Image.BOX)
    return [strip.getpixel((0, y)) / 255.0 for y in range(h)]


def _col_profile(img: Image.Image) -> List[float]:
    w, h = img.size
    if w < 1 or h < 1:
        return []
    strip = img.resize((w, 1), Image.BOX)
    return [strip.getpixel((x, 0)) / 255.0 for x in range(w)]


def _runs(flags: Sequence[bool]) -> List[Tuple[int, int]]:
    """Contiguous ``True`` spans as inclusive (start, end) pairs."""
    out: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for i, flag in enumerate(flags):
        if flag:
            if start is None:
                start = i
        elif start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _ink_image(img: Image.Image) -> Image.Image:
    """Binary L image: 255 where printed, 0 for paper."""
    return img.convert("L").point(lambda v: 255 if v < INK_LUMA else 0)


def _x_overlap_ratio(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    """Overlap of two inclusive x-ranges relative to the shorter one."""
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    if hi < lo:
        return 0.0
    span = min(a[1] - a[0] + 1, b[1] - b[0] + 1)
    return (hi - lo + 1) / span if span > 0 else 0.0


def _dedupe(values: Sequence[float], tol: float) -> List[float]:
    """Collapse near-equal positions (anti-aliased rules split across px)."""
    out: List[float] = []
    for v in sorted(values):
        if not out or v - out[-1] > tol:
            out.append(v)
        else:
            out[-1] = (out[-1] + v) / 2.0
    return out


def detect_ruled_grid(
    img: Image.Image,
    page_width: float,
    page_height: float,
) -> Optional[RasterGrid]:
    """Find a ruled table grid on a scan; None when the page has no grid.

    Horizontal rules are rows that are mostly ink (text rows are not — a body
    line inks ~15% of its row, a rule ~65%).  Rules are grouped by shared
    x-extent, and a group only becomes a table once at least two vertical
    rules run through its band, which is what rejects a lone header underline.
    """
    if page_width <= 0 or page_height <= 0:
        return None
    ink = _ink_image(img)
    iw, ih = ink.size
    if iw < 8 or ih < 8:
        return None

    sy = page_height / float(ih)
    max_h_px = max(6, ih // 100)
    rowfrac = _row_profile(ink)
    h_runs = [
        (a, b) for a, b in _runs([f > RULE_FRAC for f in rowfrac])
        if (b - a + 1) <= max_h_px
    ]
    if len(h_runs) < 2:
        return None

    # Each rule's own x-extent; a rule that stops mid-page is still a rule.
    rules: List[Tuple[int, int, int, int]] = []  # y0, y1, x0, x1
    for y0, y1 in h_runs:
        strip = ink.crop((0, y0, iw, y1 + 1))
        xs = [x for x, v in enumerate(_col_profile(strip)) if v > 0.5]
        if len(xs) < iw * 0.05:
            continue
        rules.append((y0, y1, min(xs), max(xs)))
    if len(rules) < 2:
        return None

    groups: List[List[Tuple[int, int, int, int]]] = []
    for rule in rules:
        span = (rule[2], rule[3])
        for group in groups:
            gx = (min(r[2] for r in group), max(r[3] for r in group))
            if _x_overlap_ratio(span, gx) >= RULE_X_OVERLAP:
                group.append(rule)
                break
        else:
            groups.append([rule])

    for group in sorted(groups, key=lambda g: -(max(r[1] for r in g) - min(r[0] for r in g))):
        if len(group) < 2:
            continue
        by0, by1 = min(r[0] for r in group), max(r[1] for r in group)
        bx0, bx1 = min(r[2] for r in group), max(r[3] for r in group)
        if (by1 - by0 + 1) < 4 or (bx1 - bx0 + 1) < 4:
            continue

        band = ink.crop((bx0, by0, bx1 + 1, by1 + 1))
        bw = band.size[0]
        v_runs = [
            (a, b) for a, b in _runs([f > VRULE_FRAC for f in _col_profile(band)])
            if (b - a + 1) <= max(6, bw // 100)
        ]
        if len(v_runs) < 3:
            continue  # need ≥2 columns to be a grid, not a divider line

        sx = page_width / float(iw)
        x_lines = _dedupe([bx0 + (a + b + 1) / 2.0 for a, b in v_runs], 4.0 * sx)
        y_lines = _dedupe([(a + b + 1) / 2.0 for a, b in h_runs
                           if by0 <= a and b <= by1], 4.0 * sy)
        if len(x_lines) < 3 or len(y_lines) < 3:
            continue
        x_lines = [x * sx for x in x_lines]
        y_lines = [y * sy for y in y_lines]
        if (len(x_lines) - 1) * (len(y_lines) - 1) > MAX_GRID_CELLS:
            continue
        thickness = max(0.5, min(2.5, sum(b - a + 1 for a, b in h_runs) / len(h_runs) * sy))
        return RasterGrid(x_lines=x_lines, y_lines=y_lines, thickness=thickness)
    return None


def color_mask(img: Image.Image) -> Image.Image:
    """Binary L mask of saturated (stamped / highlighted) pixels."""
    r, g, b = img.convert("RGB").split()
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    sat = ImageChops.subtract(mx, mn)
    return sat.point(lambda v: 255 if v >= COLOR_SAT else 0)


def color_regions(
    img: Image.Image,
    page_width: float,
    page_height: float,
    *,
    exclude: Optional[Tuple[float, float, float, float]] = None,
) -> List[Tuple[float, float, float, float]]:
    """Bounding boxes (PDF points) of coloured graphics such as seals.

    Rows with only a handful of coloured pixels are noise; real stamps span a
    band.  Bands closer together than ``COLOR_BAND_GAP_PT`` are stitched, since
    one seal can be split by a line of text passing through it.
    """
    if page_width <= 0 or page_height <= 0:
        return []
    mask = color_mask(img)
    iw, ih = mask.size
    sx = page_width / float(iw)
    sy = page_height / float(ih)
    min_px = max(1, int(MIN_COLOR_PT / sx))
    min_py = max(1, int(MIN_COLOR_PT / sy))

    rows = _row_profile(mask)
    bands = [(a, b) for a, b in _runs([v > 0 for v in rows]) if (b - a + 1) >= min_py]
    if not bands:
        return []
    merged: List[Tuple[int, int]] = []
    gap_px = max(1, int(COLOR_BAND_GAP_PT / sy))
    for a, b in bands:
        if merged and a - merged[-1][1] <= gap_px:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))

    out: List[Tuple[float, float, float, float]] = []
    for a, b in merged:
        bbox = mask.crop((0, a, iw, b + 1)).getbbox()
        if not bbox:
            continue
        left, top, right, bottom = bbox
        if (right - left) < min_px or (bottom - top) < min_py:
            continue
        rect = (left * sx, (a + top) * sy, right * sx, (a + bottom) * sy)
        if exclude and not (rect[2] < exclude[0] or rect[0] > exclude[2]
                            or rect[3] < exclude[1] or rect[1] > exclude[3]):
            continue
        out.append(rect)
    return out


def cut_color_png(
    img: Image.Image,
    rect_pt: Tuple[float, float, float, float],
    page_width: float,
    page_height: float,
) -> Optional[bytes]:
    """Crop ``rect_pt`` and drop everything that is not coloured ink.

    Black text printed across a stamp is excluded (it is emitted as OCR text
    too), leaving a transparent PNG of just the stamped mark.
    """
    if page_width <= 0 or page_height <= 0:
        return None
    iw, ih = img.size
    sx = iw / float(page_width)
    sy = ih / float(page_height)
    x0 = max(0, int(rect_pt[0] * sx))
    y0 = max(0, int(rect_pt[1] * sy))
    x1 = min(iw, int(round(rect_pt[2] * sx)) + 1)
    y1 = min(ih, int(round(rect_pt[3] * sy)) + 1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None

    crop = img.crop((x0, y0, x1, y1)).convert("RGB")
    mask = color_mask(crop)
    gray = crop.convert("L")
    alpha = ImageChops.multiply(
        mask,
        gray.point(lambda v: 255 if v >= COLOR_MIN_LUMA else 0),
    )
    rgba = crop.convert("RGBA")
    rgba.putalpha(alpha)
    if alpha.getbbox() is None:
        return None
    buf = io.BytesIO()
    try:
        rgba.save(buf, format="PNG", optimize=True)
    except Exception:
        return None
    return buf.getvalue()


__all__ = [
    "RasterGrid",
    "detect_ruled_grid",
    "color_mask",
    "color_regions",
    "cut_color_png",
    "_row_profile",
    "_col_profile",
    "_runs",
    "_ink_image",
    "_dedupe",
]
