"""Page text extraction outside table regions."""

from __future__ import annotations

from collections import Counter
from typing import List, Optional, Tuple

from .constants import LINE_GAP, TEXT_COL_GAP
from .models import TextBlock
from .text_utils import (
    _join_words,
    _merge_soft_wrap_text_blocks,
    _normalize_spacing,
    _word_line_sort_key,
)
from .word_index import WordIndex

def _page_h_align(x0: float, x1: float, page_w: float) -> str:
    """Legacy page-based alignment guess, used when the page gives no context.

    A line whose side pads are balanced on the *page* reads as centred — which
    is also what a full-width line looks like in a document with symmetric
    margins, so callers should prefer :func:`_text_h_align` with content bounds.
    """
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


def _text_h_align(
    x0: float,
    x1: float,
    page_w: float,
    *,
    content_left: Optional[float] = None,
    content_right: Optional[float] = None,
    content_lines: int = 0,
) -> str:
    """Infer paragraph alignment from the text bbox relative to the content area.

    ``content_left`` / ``content_right`` are the page's real text extent; with
    fewer than two lines there is nothing to judge the extent against, so the
    legacy page-based heuristic is used instead.

    "left" is the position-preserving choice (the writer indents to ``x0``),
    so "center" is only returned when the line is clearly inset on *both*
    sides of the content box and balanced between them.
    """
    if page_w <= 0:
        return "left"
    if (
        content_lines < 2
        or content_left is None
        or content_right is None
        or content_right - content_left <= 0
    ):
        return _page_h_align(x0, x1, page_w)

    box = content_right - content_left
    width = x1 - x0
    if width >= box * 0.90:
        return "left"
    left_gap = x0 - content_left
    right_gap = content_right - x1
    balanced = abs(left_gap - right_gap) <= max(box * 0.10, 8.0)
    if left_gap >= box * 0.10 and right_gap >= box * 0.10 and balanced:
        return "center"
    if right_gap <= box * 0.08 and left_gap >= box * 0.25:
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


def _content_extent(segs: List[dict]) -> Tuple[Optional[float], Optional[float]]:
    """(left, right) of the page's own text box, or (None, None) when empty."""
    if not segs:
        return None, None
    return min(s["x0"] for s in segs), max(s["x1"] for s in segs)


def _extract_text_blocks(page, table_bboxes, widx: WordIndex) -> List[TextBlock]:
    outside = widx.query_outside_rects(table_bboxes)
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
    segs: List[dict] = []
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
            segs.append({
                "text": text.strip(), "top": top, "bottom": bottom,
                "x0": x0, "x1": x1, "size": size, "fontname": fname,
            })

    # Content extent over the whole page: alignment must be judged against the
    # real text box, not the page (a full-width line in a symmetric-margin
    # document has balanced *page* pads and was being sent to "center").
    content_left, content_right = _content_extent(segs)
    blocks: List[TextBlock] = []
    for s in segs:
        align = _text_h_align(
            s["x0"], s["x1"], page_w,
            content_left=content_left,
            content_right=content_right,
            content_lines=len(segs),
        )
        blocks.append(TextBlock(
            text=s["text"],
            top=s["top"], bottom=s["bottom"], x0=s["x0"], x1=s["x1"],
            font_size=s["size"] or None, font_name=s["fontname"] or None,
            align=align,
        ))
    # PDF auto word-wrap produces one TextBlock per visual line; merge soft
    # wraps so a single sentence is one Word paragraph (not hard line breaks).
    return _merge_soft_wrap_text_blocks(blocks, page_right=page_w or None)


__all__ = [
    "_text_h_align",
    "_page_h_align",
    "_content_extent",
    "_word_mid_y",
    "_words_same_visual_line",
    "_extract_text_blocks",
]
