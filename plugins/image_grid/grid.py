"""图片九宫格分割 — 把一张图等分为 rows×cols 小块。

用于朋友圈/微博等九宫格配图：上传一张长图，切成多张依次发，拼回完整大图。
以 PNG 输出为主（无损、保留透明）；也可选 JPEG / WebP。分割边界按像素等分
（round(i*size/n)），因此相邻块严格无缝，回拼即可复原。

除 Pillow 外无额外依赖，风格与 ``image_convert`` / ``image_to_pdf`` 一致。
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageOps

from tools.common import (
    ImageFormatError as ConvertError,
    detect_image_format as detect_format,
)

# ---------------------------------------------------------------------------
# Public constants / errors
# ---------------------------------------------------------------------------

MIN_DIM = 1
MAX_DIM = 10  # 单方向最多切 10 份（total ≤ 100）

# fmt -> (PIL save name, media type, file extension)
_FORMATS: Dict[str, Tuple[str, str, str]] = {
    "png": ("PNG", "image/png", ".png"),
    "jpeg": ("JPEG", "image/jpeg", ".jpg"),
    "webp": ("WEBP", "image/webp", ".webp"),
}

DEFAULT_FORMAT = "png"
DEFAULT_ROWS = 3
DEFAULT_COLS = 3


class ImageGridError(ValueError):
    """Raised when an image cannot be split into a grid."""


def formats() -> List[str]:
    return list(_FORMATS)


def supported_formats() -> List[str]:
    return list(_FORMATS)


def max_dim() -> int:
    return MAX_DIM


def _normalize_axis(value: Optional[int], name: str, default: int) -> int:
    try:
        v = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ImageGridError(f"{name} must be an integer") from exc
    if v < MIN_DIM or v > MAX_DIM:
        raise ImageGridError(
            f"{name} must be between {MIN_DIM} and {MAX_DIM}"
        )
    return v


def _normalize_format(fmt: Optional[str]) -> str:
    f = (fmt or DEFAULT_FORMAT).strip().lower()
    if f not in _FORMATS:
        raise ImageGridError(
            f"format must be one of: {', '.join(_FORMATS)}"
        )
    return f


def _parse_bg(color: Optional[str]) -> Tuple[int, int, int]:
    raw = (color or "#ffffff").strip()
    low = raw.lower()
    if low in ("white", "#fff", "#ffffff"):
        return (255, 255, 255)
    if low in ("black", "#000", "#000000"):
        return (0, 0, 0)
    if raw.startswith("#"):
        h = raw[1:]
        if len(h) == 3 and all(c in "0123456789abcdefABCDEF" for c in h):
            return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16))
        if len(h) == 6 and all(c in "0123456789abcdefABCDEF" for c in h):
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    raise ImageGridError(
        f"Invalid background color: {color!r}. Use #rrggbb or white/black."
    )


def _open_image(data: bytes, filename: Optional[str]) -> Image.Image:
    """Decode one image (EXIF-corrected, first frame for animations)."""
    if not data:
        raise ImageGridError(
            f"Empty file{f': {filename}' if filename else ''}"
        )
    try:
        detect_format(data, filename)
    except ConvertError as exc:
        raise ImageGridError(str(exc)) from exc
    try:
        with Image.open(io.BytesIO(data)) as im:
            fr = ImageOps.exif_transpose(im.copy())
            fr.load()
            return fr
    except OSError as exc:
        name = filename or "image"
        raise ImageGridError(f"Cannot read image ({name}): {exc}") from exc
    except Exception as exc:
        name = filename or "image"
        raise ImageGridError(f"Cannot read image ({name}): {exc}") from exc


def _bounds(size: int, n: int) -> List[int]:
    """Pixel boundaries that tile ``size`` into ``n`` equal slices."""
    return [round(i * size / n) for i in range(n + 1)]


def _flatten_alpha(img: Image.Image, bg: Tuple[int, int, int]) -> Image.Image:
    """Composite alpha onto ``bg`` for lossy formats (jpeg/webp)."""
    if img.mode in ("RGBA", "LA"):
        rgba = img.convert("RGBA")
        bg_img = Image.new("RGB", rgba.size, bg)
        bg_img.paste(rgba, mask=rgba.split()[-1])
        return bg_img
    if img.mode == "P" and "transparency" in img.info:
        rgba = img.convert("RGBA")
        bg_img = Image.new("RGB", rgba.size, bg)
        bg_img.paste(rgba, mask=rgba.split()[-1])
        return bg_img
    if img.mode == "RGB":
        return img
    return img.convert("RGB")


def _save_tile(img: Image.Image, fmt: str) -> bytes:
    save_name, _media, _ext = _FORMATS[fmt]
    buf = io.BytesIO()
    try:
        img.save(buf, format=save_name)
    except OSError as exc:
        raise ImageGridError(f"Tile write failed: {exc}") from exc
    except Exception as exc:
        raise ImageGridError(f"Tile write failed: {exc}") from exc
    return buf.getvalue()


def build_grid_preview(
    data: bytes,
    *,
    filename: Optional[str] = None,
    rows: int = DEFAULT_ROWS,
    cols: int = DEFAULT_COLS,
) -> bytes:
    """Return a PNG of the image with grid lines drawn (for preview)."""
    img = _open_image(data, filename)
    W, H = img.size
    canvas = _flatten_alpha(img, (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    line = (255, 0, 0)
    for i in range(1, cols):
        x = round(i * W / cols)
        draw.line([(x, 0), (x, H)], fill=line, width=3)
    for i in range(1, rows):
        y = round(i * H / rows)
        draw.line([(0, y), (W, y)], fill=line, width=3)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def split_image(
    data: bytes,
    *,
    filename: Optional[str] = None,
    rows: int = DEFAULT_ROWS,
    cols: int = DEFAULT_COLS,
    fmt: str = DEFAULT_FORMAT,
    background: str = "#ffffff",
) -> Dict[str, Any]:
    """Split one image into a ``rows``×``cols`` grid of tiles.

    Parameters
    ----------
    data:
        Raw image file bytes.
    filename:
        Optional original name (stem is used for tile names).
    rows / cols:
        Grid dimensions, each 1..10 (default 3×3 = 九宫格).
    fmt:
        Tile output format: ``png`` / ``jpeg`` / ``webp`` (default png).
    background:
        Solid color used to flatten alpha for lossy formats.

    Returns
    -------
    dict with ``tiles`` (each ``{name, data, row, col, width, height}``),
    ``grid``, ``input`` dims, ``format``, ``media_type``, ``extension``,
    ``original_bytes``, ``output_bytes``, ``notes``.
    """
    r = _normalize_axis(rows, "rows", DEFAULT_ROWS)
    c = _normalize_axis(cols, "cols", DEFAULT_COLS)
    f = _normalize_format(fmt)
    bg = _parse_bg(background)
    name = filename or "image"
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = (stem.replace("\\", "_").replace("/", "_") or "image")

    img = _open_image(data, name)
    W, H = img.size
    if W < c or H < r:
        raise ImageGridError(
            f"Image is too small ({W}×{H}) for a {r}×{c} grid"
        )

    save_name, media, ext = _FORMATS[f]
    needs_flatten = f in ("jpeg", "webp") and (
        img.mode in ("RGBA", "LA")
        or (img.mode == "P" and "transparency" in img.info)
    )

    xs = _bounds(W, c)
    ys = _bounds(H, r)

    notes: List[str] = []
    if needs_flatten:
        notes.append("alpha_flattened")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            n = getattr(probe, "n_frames", 1) or 1
            if n > 1:
                notes.append("animation_first_frame_only")
    except OSError:
        pass

    tiles: List[Dict[str, Any]] = []
    total_out = 0
    for row in range(r):
        y0, y1 = ys[row], ys[row + 1]
        for col in range(c):
            x0, x1 = xs[col], xs[col + 1]
            tile = img.crop((x0, y0, x1, y1))
            if needs_flatten:
                tile = _flatten_alpha(tile, bg)
            tdata = _save_tile(tile, f)
            total_out += len(tdata)
            tiles.append(
                {
                    "name": f"{stem}_r{row + 1}c{col + 1}{ext}",
                    "data": tdata,
                    "row": row,
                    "col": col,
                    "width": x1 - x0,
                    "height": y1 - y0,
                }
            )

    return {
        "tiles": tiles,
        "grid": {"rows": r, "cols": c, "total": r * c},
        "input": {"width": W, "height": H, "filename": name},
        "format": f,
        "media_type": media,
        "extension": ext,
        "background": background,
        "notes": list(notes),
        "original_bytes": len(data),
        "output_bytes": total_out,
    }
