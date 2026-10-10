"""Optional OCR for scanned / image-only PDF pages.

Uses Tesseract via ``pytesseract`` when both the Python package and a system
Tesseract binary are available. Callers should treat OCR as best-effort: if
unavailable, the pipeline falls back to embedding full-page images.
"""

from __future__ import annotations

import io
import os
import shutil
import statistics
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional, Sequence

from .models import TextBlock
from .text_blocks import _text_h_align
from .text_utils import _is_cjk_char, _merge_soft_wrap_text_blocks

# Default languages: simplified Chinese + English (common for this toolbox).
DEFAULT_OCR_LANG = "chi_sim+eng"
# Page segmentation mode. 6 (uniform text block) suits flowing prose; forms and
# multi-column pages often read better with 4 or 11 — tune per deployment via
# PDF2WORD_OCR_PSM without a code change.
DEFAULT_OCR_PSM = "6"
# Drop OCR boxes with very low confidence (-1 means Tesseract withheld a score).
MIN_OCR_CONFIDENCE = 35.0

# Tesseract reports per-line *ink* boxes, not the em box: a line of CJK
# measures ~0.95em, a line of digits/punctuation less.  Calibrated against a
# 四号(14pt) body scan whose ink measured 12.4pt.
OCR_INK_TO_EM = float(os.environ.get("PDF2WORD_OCR_INK_EM") or "0.95")
# OCR estimates snap onto sizes Word and Chinese documents actually use.
FONT_SIZE_LADDER = (
    6.0, 6.5, 7.0, 7.5, 8.0, 9.0, 10.0, 10.5, 11.0, 12.0, 14.0, 15.0,
    16.0, 18.0, 20.0, 22.0, 24.0, 26.0, 28.0, 32.0, 36.0, 44.0, 48.0,
    54.0, 60.0, 72.0,
)
# Two lines whose ink heights differ by less than this belong to one size
# class; Tesseract jitter on identical text easily reaches ±5%.
SIZE_CLUSTER_RATIO = 1.12
# Ink shorter than this is a rule/artefact, not measurable text.
MIN_OCR_LINE_HEIGHT = 4.0
# Fallback when every ink box on the page is sub-threshold (degraded scan).
DEFAULT_OCR_SIZE = 10.5


def _ocr_config() -> str:
    psm = (os.environ.get("PDF2WORD_OCR_PSM") or DEFAULT_OCR_PSM).strip()
    if not psm.isdigit() or not 0 <= int(psm) <= 13:
        psm = DEFAULT_OCR_PSM
    return f"--psm {psm}"


def _snap_font_size(raw: float) -> float:
    """Nearest size Word / Chinese documents actually use."""
    return min(FONT_SIZE_LADDER, key=lambda s: abs(s - raw))


def _estimate_font_sizes(heights: Sequence[float]) -> List[Optional[float]]:
    """Convert per-line ink heights (pt) into stable page font sizes.

    Taking ``height * 0.75`` per line made identical body text jump between
    8.5 and 9.5pt depending on whether a line happened to include a comma.
    Heights are therefore clustered into size classes first, each class is
    converted once, and the class size is snapped onto a standard ladder.
    """
    n = len(heights)
    if n == 0:
        return []
    measured = [i for i, h in enumerate(heights) if h >= MIN_OCR_LINE_HEIGHT]
    if not measured:
        return [None] * n
    measured.sort(key=lambda i: heights[i])

    clusters: List[List[int]] = []
    for i in measured:
        if not clusters:
            clusters.append([i])
            continue
        ref = statistics.median(heights[j] for j in clusters[-1])
        if heights[i] / ref > SIZE_CLUSTER_RATIO:
            clusters.append([i])
        else:
            clusters[-1].append(i)

    out: List[Optional[float]] = [None] * n
    smallest: Optional[float] = None
    for cluster in clusters:
        ref = statistics.median(heights[j] for j in cluster)
        size = _snap_font_size(ref / OCR_INK_TO_EM)
        smallest = size if smallest is None else min(smallest, size)
        for i in cluster:
            out[i] = size
    # Sub-threshold rows inherit the smallest real class instead of vanishing.
    return [v if v is not None else smallest for v in out]


@lru_cache(maxsize=1)
def ocr_available() -> bool:
    """Return True when pytesseract + a Tesseract binary can be used."""
    try:
        import pytesseract  # type: ignore
    except ImportError:
        return False

    cmd = os.environ.get("TESSERACT_CMD") or os.environ.get("TESSERACT_PATH")
    if cmd and os.path.isfile(cmd):
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif shutil.which("tesseract"):
        pass
    else:
        # Common Windows install path.
        win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.isfile(win):
            pytesseract.pytesseract.tesseract_cmd = win
        else:
            return False

    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def ocr_info() -> dict:
    """Diagnostic info for health / UI (cached; binary probe is expensive)."""
    available = ocr_available()
    lang = os.environ.get("PDF2WORD_OCR_LANG") or DEFAULT_OCR_LANG
    cmd = None
    version = None
    if available:
        try:
            import pytesseract  # type: ignore

            cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", None) or shutil.which(
                "tesseract"
            )
            version = str(pytesseract.get_tesseract_version())
        except Exception:
            pass
    return {
        "available": available,
        "lang": lang,
        "psm": _ocr_config().split(" ", 1)[-1],
        "tesseract_cmd": cmd,
        "version": version,
    }


@dataclass
class OcrWord:
    """One recognised token in page coordinates (pt)."""

    text: str
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass
class OcrLine:
    """One recognised visual line in page coordinates (pt).

    ``font_size`` is a page-level size class (not this line's raw ink height)
    and ``words`` are kept so callers can split a line across table cells —
    a ruled row is reported by Tesseract as one line spanning every column.
    """

    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    font_size: float
    align: str
    words: List[OcrWord] = field(default_factory=list)


def _line_to_block(line: OcrLine) -> TextBlock:
    return TextBlock(
        text=line.text,
        top=line.top,
        bottom=line.bottom,
        x0=line.x0,
        x1=line.x1,
        font_size=line.font_size,
        font_name="宋体",
        align=line.align,
        from_ocr=True,
        first_x0=line.x0,
    )


def join_ocr_words(words: Sequence[OcrWord], *, height: float) -> str:
    """Join OCR tokens back into line text.

    A space is inserted only when the horizontal gap between two tokens is
    wider than a fraction of the line's ink height **and** the pair is not
    pure CJK. Tesseract reports each CJK character as its own token with
    almost no gap, while a real word space is visibly wider; character-class
    rules alone cannot tell ``2025优秀`` (adjacent) from ``2025 优秀``
    (a real space).
    """
    parts: List[str] = []
    prev: Optional[OcrWord] = None
    for w in words:
        if not w.text:
            continue
        gap = w.x0 - prev.x1 if prev is not None else 0.0
        spaced = (
            prev is not None
            and gap > max(1.5, height * 0.2)
            and not (_is_cjk_char(prev.text[-1:]) and _is_cjk_char(w.text[:1]))
        )
        parts.append((" " if spaced else "") + w.text)
        prev = w
    return "".join(parts).strip()


def ocr_image_lines(
    png_bytes: bytes,
    *,
    page_width: float,
    page_height: float,
    lang: Optional[str] = None,
) -> List[OcrLine]:
    """Run Tesseract on a page PNG and return positioned :class:`OcrLine`.

    Coordinates from Tesseract are in image pixels; they are scaled back to
    PDF points using ``page_width`` / ``page_height``.  Empty list when OCR is
    unavailable or the page yields nothing (callers fall back to an image).
    """
    if not ocr_available() or not png_bytes:
        return []
    if page_width <= 0 or page_height <= 0:
        return []

    try:
        import pytesseract  # type: ignore
        from PIL import Image
    except ImportError:
        return []

    try:
        img = Image.open(io.BytesIO(png_bytes))
        # Grayscale + slight contrast helps forms / scans.
        gray = img.convert("L")
        img_w, img_h = gray.size
        if img_w < 8 or img_h < 8:
            return []

        ocr_lang = (lang or os.environ.get("PDF2WORD_OCR_LANG") or DEFAULT_OCR_LANG).strip()
        data = pytesseract.image_to_data(
            gray,
            lang=ocr_lang,
            output_type=pytesseract.Output.DICT,
            config=_ocr_config(),
        )
    except Exception:
        return []

    sx = page_width / float(img_w)
    sy = page_height / float(img_h)
    return _tesseract_lines(data, scale_x=sx, scale_y=sy, page_width=page_width)


def _tesseract_lines(
    data: dict,
    *,
    scale_x: float,
    scale_y: float,
    page_width: float,
) -> List[OcrLine]:
    """Post-process Tesseract ``image_to_data`` output into :class:`OcrLine`.

    Kept separate from the engine call so tests and harnesses can feed a
    fabricated ``data`` dict through the real grouping / sizing / alignment
    pipeline without a Tesseract binary.
    """
    n = len(data.get("text") or [])
    if n == 0:
        return []

    # Group words into lines by (block_num, par_num, line_num).
    lines: dict = {}
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf >= 0 and conf < MIN_OCR_CONFIDENCE:
            continue
        try:
            left = float(data["left"][i])
            top = float(data["top"][i])
            width = float(data["width"][i])
            height = float(data["height"][i])
        except (TypeError, ValueError, KeyError):
            continue
        if width <= 0 or height <= 0:
            continue
        key = (
            int(data.get("block_num", [0])[i] or 0),
            int(data.get("par_num", [0])[i] or 0),
            int(data.get("line_num", [0])[i] or 0),
        )
        lines.setdefault(key, []).append(
            {
                "text": text,
                "left": left,
                "top": top,
                "right": left + width,
                "bottom": top + height,
            }
        )

    raw: List[dict] = []
    for key in sorted(lines.keys()):
        words = sorted(lines[key], key=lambda w: w["left"])
        ocr_words = [
            OcrWord(
                text=w["text"],
                x0=w["left"] * scale_x,
                x1=w["right"] * scale_x,
                top=w["top"] * scale_y,
                bottom=w["bottom"] * scale_y,
            )
            for w in words
        ]
        height = max(w.bottom - w.top for w in ocr_words)
        text = join_ocr_words(ocr_words, height=height)
        if not text:
            continue
        raw.append({
            "text": text,
            "x0": min(w.x0 for w in ocr_words),
            "x1": max(w.x1 for w in ocr_words),
            "top": min(w.top for w in ocr_words),
            "bottom": max(w.bottom for w in ocr_words),
            "words": ocr_words,
        })
    if not raw:
        return []

    # Alignment is judged against the page's own text extent: a full-width body
    # line has balanced pads on the *page* too, and was being sent to "center".
    content_left = min(r["x0"] for r in raw)
    content_right = max(r["x1"] for r in raw)
    content_lines = len(raw)
    sizes = _estimate_font_sizes([r["bottom"] - r["top"] for r in raw])

    return [
        OcrLine(
            text=r["text"],
            x0=r["x0"],
            x1=r["x1"],
            top=r["top"],
            bottom=r["bottom"],
            font_size=float(size or DEFAULT_OCR_SIZE),
            align=_text_h_align(
                r["x0"], r["x1"], page_width,
                content_left=content_left,
                content_right=content_right,
                content_lines=content_lines,
            ),
            words=r["words"],
        )
        for r, size in zip(raw, sizes, strict=True)
    ]


def ocr_image_to_blocks(
    png_bytes: bytes,
    *,
    page_width: float,
    page_height: float,
    lang: Optional[str] = None,
) -> List[TextBlock]:
    """Run Tesseract on a page PNG and return merged TextBlocks."""
    ocr_lines = ocr_image_lines(
        png_bytes, page_width=page_width, page_height=page_height, lang=lang
    )
    if not ocr_lines:
        return []
    # Right edge of the page's own text: lets the soft-wrap test tell a line
    # that really reaches the margin from a short one that does not.
    content_right = max(ln.x1 for ln in ocr_lines)
    blocks: List[TextBlock] = [_line_to_block(ln) for ln in ocr_lines]
    return _merge_soft_wrap_text_blocks(blocks, page_right=content_right)
