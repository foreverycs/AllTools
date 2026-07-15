"""PDF→Word extraction tuning constants (env-overridable where noted)."""

from __future__ import annotations

import os

# ----- tuning constants -----------------------------------------------------
SNAP_TOLERANCE = 3.0        # grid line snapping tolerance (pt)
LINE_GAP = 3.0              # max vertical gap (pt) to group words into one text line
# max horizontal gap (pt) to keep words in the same text segment; larger gaps
# split one visual line into multiple blocks (e.g. "检验：" … "审核：").
TEXT_COL_GAP = 40.0
MIN_IMAGE_AREA = 40.0 * 40.0  # skip decorative icons smaller than this (pt²)
MAX_IMAGES_PER_PAGE = 15
# Fallback rasterisation DPI when the PDF stream cannot be extracted natively.
# 144 made screenshots/photos look soft in Word; 220 is a better default for
# print-like sharpness without huge DOCX payloads. Override with PDF2WORD_IMAGE_DPI.
IMAGE_RENDER_DPI = int(os.environ.get("PDF2WORD_IMAGE_DPI", "220"))
# Cap the long edge of rendered bitmaps (px) to keep memory / file size bounded.
IMAGE_RENDER_MAX_PX = int(os.environ.get("PDF2WORD_IMAGE_MAX_PX", "3500"))
# Prefer native embedded streams when their pixel density is at least this
# many pixels per PDF point on either axis (≈96 DPI ≈ 1.33 px/pt).
IMAGE_NATIVE_MIN_PX_PER_PT = 1.2
# Thin filled rectangles / strokes treated as horizontal rules (pt).
HLINE_MAX_THICKNESS = 2.5
HLINE_MIN_WIDTH = 40.0
# Table detection: text-strategy tables must not heavily overlap line tables.
TABLE_OVERLAP_REJECT = 0.45
# Word bbox must cover this fraction of a grid cell to claim a merge span.
SPAN_COVER_RATIO = 0.55
# OCR render resolution (higher than display images for better recognition).
OCR_RENDER_DPI = int(os.environ.get("PDF2WORD_OCR_DPI", "250"))
# Borderless (text-strategy) tables: reject grids that look like prose / multi-col
# layout rather than real forms. Line-based tables bypass these limits.
TEXT_TABLE_MAX_COLS = 6
TEXT_TABLE_MAX_ROWS = 25
TEXT_TABLE_MAX_CELLS = 40
TEXT_TABLE_MIN_FILLED = 4
# Cluster word left edges within this gap (pt) when estimating real columns.
TEXT_COL_CLUSTER_TOL = 18.0
