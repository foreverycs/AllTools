"""Shared content model for PDF extraction and DOCX writing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


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
