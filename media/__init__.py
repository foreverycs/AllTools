"""媒体处理工具核心逻辑。"""

from __future__ import annotations

import io
from typing import Optional, Tuple

from PIL import Image

# App-level raster dimension cap (pixels). Pillow's built-in decompression
# bomb threshold is much higher (~179M) — decoding an image up to that limit
# can still allocate hundreds of MB per request, so bound it earlier.
# ~ 8000x8000, safely below common printable/browser sizes.
MAX_IMAGE_PIXELS = 64_000_000


def check_image_dimensions(
    path: str, *, head_bytes: int = 16 * 1024
) -> Optional[Tuple[int, int]]:
    """Header-only raster dimension check before any heavy decoding.

    Reads only the first ``head_bytes`` (dimensions live in the file header for
    PNG/GIF/BMP/ICO/WebP and the JPEG SOF marker) and rejects oversized images.
    Non-raster or unreadable inputs are skipped — the worker produces the real
    error. Returns ``(width, height)`` when the header parsed, else None.

    Raises ``ValueError`` when ``width * height`` exceeds ``MAX_IMAGE_PIXELS``.
    """
    with open(path, "rb") as f:
        head = f.read(head_bytes)
    if not head:
        raise ValueError("empty image file")
    try:
        with Image.open(io.BytesIO(head)) as im:
            w, h = im.size
    except Exception:
        return None
    if (w or 0) * (h or 0) > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"image too large ({w}x{h}); max {MAX_IMAGE_PIXELS} pixels"
        )
    return int(w or 0), int(h or 0)


from .image_compress import (
    CompressError,
    compress_image,
    detect_format,
    supported_formats,
)
from .image_convert import (
    ConvertError,
    convert_image,
    detect_format as convert_detect_format,
    input_formats,
    output_formats,
)
from .image_to_pdf import (
    ImageToPdfError,
    images_to_pdf,
    input_formats as image_to_pdf_input_formats,
    max_images as image_to_pdf_max_images,
    orientations as image_to_pdf_orientations,
    page_modes as image_to_pdf_page_modes,
)
from .image_grid import (
    ImageGridError,
    build_grid_preview,
    max_dim,
    split_image,
    supported_formats as image_grid_supported_formats,
)

__all__ = [
    "MAX_IMAGE_PIXELS",
    "check_image_dimensions",
    "compress_image",
    "detect_format",
    "supported_formats",
    "CompressError",
    "convert_image",
    "convert_detect_format",
    "input_formats",
    "output_formats",
    "ConvertError",
    "images_to_pdf",
    "ImageToPdfError",
    "image_to_pdf_input_formats",
    "image_to_pdf_max_images",
    "image_to_pdf_orientations",
    "image_to_pdf_page_modes",
    "ImageGridError",
    "build_grid_preview",
    "max_dim",
    "split_image",
    "image_grid_supported_formats",
]
