"""Image watermarking — text / logo overlay onto raster images.

Draws a text or logo watermark on JPEG / PNG / WebP / BMP / TIFF / GIF
(animation input uses its first frame). Output format is preserved by
default or can be forced to JPEG / PNG / WebP.

Self-contained plugin module: the page/API shell lives alongside in
``plugins/image_watermark/`` (see ``router.py``).
"""

from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

from tools.common import (
    ImageFormatError as ConvertError,
    detect_image_format as detect_format,
)

# ---------------------------------------------------------------------------
# Public constants / errors
# ---------------------------------------------------------------------------

SUPPORTED_INPUTS = ("jpeg", "png", "webp", "gif", "bmp", "tiff")

# Output formats we can emit: fmt id -> (PIL name, media type, extension)
_OUTPUT_FORMATS: Dict[str, Tuple[str, str, str]] = {
    "auto": ("", "image/png", ""),
    "jpeg": ("JPEG", "image/jpeg", ".jpg"),
    "png": ("PNG", "image/png", ".png"),
    "webp": ("WEBP", "image/webp", ".webp"),
}

POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")

WATERMARK_TYPES = ("text", "image")

DEFAULT_TEXT = "样例水印"
DEFAULT_POSITION = "bottom-right"
DEFAULT_OPACITY = 40
DEFAULT_ANGLE = 0.0
DEFAULT_FONT_SIZE_PCT = 5.0
DEFAULT_LOGO_SIZE_PCT = 15.0

# --- JPEG/WebP encode quality for watermarked output ----------------------
_JPEG_QUALITY = 92
_WEBP_QUALITY = 92

# --- CJK font discovery (Windows / macOS / Linux common paths) ------------
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\arialuni.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_font_cache: Optional[str] = None


class WatermarkError(ValueError):
    """Raised when the input cannot be watermarked (bad format / corrupt data)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def watermark_types() -> List[str]:
    return list(WATERMARK_TYPES)


def positions() -> List[str]:
    return list(POSITIONS)


def output_formats() -> List[str]:
    return list(_OUTPUT_FORMATS)


def _find_font_path() -> Optional[str]:
    """Return the first readable CJK-capable font path, else None."""
    global _font_cache
    if _font_cache is not None:
        return _font_cache or None
    for path in _FONT_CANDIDATES:
        try:
            if os.path.isfile(path):
                _font_cache = path
                return path
        except OSError:
            continue
    _font_cache = ""
    return None


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Build a TrueType font sized ``size``; fall back to default bitmap."""
    path = _find_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _normalize_fmt(raw: Optional[str], input_fmt: str) -> str:
    """Return an output format id from user input (``auto`` → input fmt)."""
    f = (raw or "auto").strip().lower()
    if f not in _OUTPUT_FORMATS:
        raise WatermarkError(
            f"format must be one of: {', '.join(_OUTPUT_FORMATS)}"
        )
    if f == "auto":
        # GIF/BMP/TIFF map to PNG; everything else keeps its native format.
        if input_fmt in ("gif", "bmp", "tiff"):
            return "png"
        return input_fmt if input_fmt in ("jpeg", "png", "webp") else "png"
    return f


def _parse_color(raw: Optional[str]) -> Tuple[int, int, int]:
    """Parse ``#rrggbb`` / ``#rgb`` (or short names) into an RGB triple."""
    value = (raw or "#ffffff").strip()
    low = value.lower()
    if low in ("white", "#fff", "#ffffff"):
        return (255, 255, 255)
    if low in ("black", "#000", "#000000"):
        return (0, 0, 0)
    if low in ("red", "#f00", "#ff0000"):
        return (255, 0, 0)
    if value.startswith("#"):
        h = value[1:]
        if len(h) == 3 and all(c in "0123456789abcdefABCDEF" for c in h):
            return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16))
        if len(h) == 6 and all(c in "0123456789abcdefABCDEF" for c in h):
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    raise WatermarkError(
        f"Invalid color: {value!r}. Use #rrggbb or white/black/red."
    )


def _parse_opacity(raw: Optional[str], default: int = DEFAULT_OPACITY) -> int:
    try:
        value = int(str(raw).strip()) if raw is not None and str(raw).strip() != "" else default
    except (TypeError, ValueError) as exc:
        raise WatermarkError("opacity must be an integer") from exc
    if value < 0 or value > 100:
        raise WatermarkError("opacity must be between 0 and 100")
    return value


def _parse_float(raw: Optional[str], name: str, default: float, *, lo: float, hi: float) -> float:
    try:
        value = float(str(raw).strip()) if raw is not None and str(raw).strip() != "" else default
    except (TypeError, ValueError) as exc:
        raise WatermarkError(f"{name} must be a number") from exc
    if value < lo or value > hi:
        raise WatermarkError(f"{name} must be between {lo} and {hi}")
    return value


def _parse_position(raw: Optional[str]) -> str:
    pos = (raw or DEFAULT_POSITION).strip().lower()
    if pos not in POSITIONS:
        raise WatermarkError(f"position must be one of: {', '.join(POSITIONS)}")
    return pos


def _open_first_frame(data: bytes, filename: Optional[str]) -> Tuple[Image.Image, str, int]:
    """Decode the image (EXIF-corrected) and return (image, fmt, frame count)."""
    if not data:
        raise WatermarkError("Empty file")
    try:
        fmt = detect_format(data, filename)
    except ConvertError as exc:
        raise WatermarkError(str(exc)) from exc
    if fmt not in SUPPORTED_INPUTS:
        raise WatermarkError(
            f"Unsupported image format: {fmt}. "
            f"Use one of: {', '.join(SUPPORTED_INPUTS)}"
        )
    try:
        with Image.open(io.BytesIO(data)) as im:
            frames = getattr(im, "n_frames", 1) or 1
            frame = ImageOps.exif_transpose(im.copy())
            frame.load()
            return frame, fmt, frames
    except OSError as exc:
        name = filename or "image"
        raise WatermarkError(f"Cannot read image ({name}): {exc}") from exc
    except Exception as exc:
        name = filename or "image"
        raise WatermarkError(f"Cannot read image ({name}): {exc}") from exc


def _make_overlay(size: Tuple[int, int]) -> Image.Image:
    """Transparent layer to draw the watermark on (kept separate for clean alpha)."""
    return Image.new("RGBA", size, (0, 0, 0, 0))


def _draw_text_tile(
    overlay: Image.Image,
    font: ImageFont.FreeTypeFont,
    fill: Tuple[int, int, int, int],
    text: str,
    angle: float,
    spacing: float,
) -> None:
    """Tile rotated ``text`` across the overlay using rotated grid axes.

    Grid points follow the rotated u/v axes so the pattern stays seamless for
    any angle (e.g. 45° diagonals), with a gap of ``spacing`` pixels between
    neighboring tiles.
    """
    w, h = overlay.size

    probe = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    probe_draw = ImageDraw.Draw(probe)
    bbox = probe_draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    tile = Image.new("RGBA", (max(tw, 2), max(th, 2)), (0, 0, 0, 0))
    tile_draw = ImageDraw.Draw(tile)
    tile_draw.text((-bbox[0], -bbox[1]), text, font=font, fill=fill)
    rot = tile.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    rw, rh = rot.size

    rad = angle * math.pi / 180.0
    ux = math.cos(rad)
    uy = math.sin(rad)
    vx = -math.sin(rad)
    vy = math.cos(rad)

    step = max(spacing, 8)
    su = rw + step
    sv = rh + step

    # Number of grid cells needed to cover the image plus a margin.
    diag = int(math.hypot(w, h))
    nu = (diag * 2) // max(su, 1) + 2
    nv = (diag * 2) // max(sv, 1) + 2
    for i in range(-nu, nu + 1):
        for j in range(-nv, nv + 1):
            cx = i * su * ux + j * sv * vx
            cy = i * su * uy + j * sv * vy
            x = int(round(cx - rw / 2))
            y = int(round(cy - rh / 2))
            if x < -rw or x > w or y < -rh or y > h:
                continue
            overlay.alpha_composite(rot, (x, y))


def _draw_text_once(
    overlay: Image.Image,
    font: ImageFont.FreeTypeFont,
    fill: Tuple[int, int, int, int],
    text: str,
    angle: float,
    position: str,
) -> None:
    """Draw a single rotated text watermark anchored at ``position``."""
    w, h = overlay.size
    margin = max(16, int(min(w, h) * 0.03))

    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((-bbox[0], -bbox[1]), text, font=font, fill=fill)

    rot = tile.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    rw, rh = rot.size

    if position == "center":
        x = (w - rw) // 2
        y = (h - rh) // 2
    elif position == "top-left":
        x, y = margin, margin
    elif position == "top-right":
        x, y = w - rw - margin, margin
    elif position == "bottom-left":
        x, y = margin, h - rh - margin
    else:  # bottom-right
        x, y = w - rw - margin, h - rh - margin

    overlay.alpha_composite(rot, (x, y))


def _draw_logo_once(
    overlay: Image.Image,
    logo: Image.Image,
    opacity: int,
    position: str,
) -> None:
    """Place a resized logo watermark at the given position."""
    w, h = overlay.size
    margin = max(16, int(min(w, h) * 0.03))

    lw, lh = logo.size
    x = 0
    if position in ("top-right", "bottom-right"):
        x = w - lw - margin
    elif position == "center":
        x = (w - lw) // 2
    else:
        x = margin

    y = 0
    if position in ("bottom-left", "bottom-right"):
        y = h - lh - margin
    elif position == "center":
        y = (h - lh) // 2
    else:
        y = margin

    if opacity < 100:
        alpha = logo.getchannel("A").point(lambda v: int(v * opacity / 100))
        logo = logo.copy()
        logo.putalpha(alpha)
    overlay.alpha_composite(logo, (x, y))


def _render_result(
    base: Image.Image,
    overlay: Image.Image,
    *,
    out_fmt: str,
) -> bytes:
    """Composite the overlay onto the base and encode to ``out_fmt`` bytes."""
    if base.mode == "RGBA":
        merged = base.convert("RGBA")
    elif base.mode == "P" and "transparency" in base.info:
        merged = base.convert("RGBA")
    else:
        merged = base.convert("RGB").convert("RGBA")
    merged = Image.alpha_composite(merged, overlay)

    buf = io.BytesIO()
    if out_fmt == "jpeg":
        rgb = Image.new("RGB", merged.size, (255, 255, 255))
        rgb.paste(merged, mask=merged.split()[-1])
        rgb.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    elif out_fmt == "webp":
        merged.save(
            buf,
            format="WEBP",
            quality=_WEBP_QUALITY,
            method=6,
            lossless=False,
        )
    else:  # png
        merged.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_watermark(
    data: bytes,
    *,
    filename: Optional[str] = None,
    watermark_type: str = "text",
    text: str = DEFAULT_TEXT,
    font_size_pct: float = DEFAULT_FONT_SIZE_PCT,
    color: str = "#ffffff",
    opacity: Optional[str] = None,
    angle: Optional[str] = None,
    repeat: bool = False,
    position: str = DEFAULT_POSITION,
    logo_data: Optional[bytes] = None,
    logo_filename: Optional[str] = None,
    logo_size_pct: float = DEFAULT_LOGO_SIZE_PCT,
    fmt: str = "auto",
) -> Dict[str, Any]:
    """Watermark image bytes and return the encoded result.

    Parameters
    ----------
    data:
        Raw image file bytes.
    filename:
        Optional name (helps format detection / output naming).
    watermark_type:
        ``text`` or ``image``.
    text:
        Watermark text (used when ``watermark_type`` is ``text``).
    font_size_pct:
        Font size as a percentage of the shorter image side (default 5.0).
    color:
        ``#rrggbb`` or a short color name (default white).
    opacity:
        0..100; ``None`` uses the type default (text 40, logo 40).
    angle:
        Rotation in degrees (``None`` = 0; ``45`` is common for tiles).
    repeat:
        When true, tile the watermark across the whole image.
    position:
        Anchor when not repeating: one of ``positions()``.
    logo_data:
        Logo image bytes (used when ``watermark_type`` is ``image``).
    logo_filename:
        Optional logo name (format detection).
    logo_size_pct:
        Logo longest edge as a percentage of the base's shorter side.
    fmt:
        Output format: ``auto`` (keep input) / ``jpeg`` / ``png`` / ``webp``.

    Returns
    -------
    dict with keys: data, format, media_type, extension, width, height,
    frames, watermark_type, opacity, position, repeat, angle, notes.
    """
    wt = (watermark_type or "text").strip().lower()
    if wt not in WATERMARK_TYPES:
        raise WatermarkError(
            f"watermark_type must be one of: {', '.join(WATERMARK_TYPES)}"
        )

    base, in_fmt, frames = _open_first_frame(data, filename)
    W, H = base.size
    out_fmt = _normalize_fmt(fmt, in_fmt)
    pos = _parse_position(position)
    notes: List[str] = []

    if frames > 1:
        notes.append("animation_first_frame_only")

    overlay = _make_overlay((W, H))

    if wt == "text":
        label = (text or "").strip()
        if not label:
            raise WatermarkError("watermark text must not be empty")
        if len(label) > 200:
            raise WatermarkError("watermark text too long (max 200 chars)")

        size_pct = _parse_float(
            str(font_size_pct) if font_size_pct is not None else str(DEFAULT_FONT_SIZE_PCT),
            "font_size_pct",
            DEFAULT_FONT_SIZE_PCT,
            lo=1.0,
            hi=60.0,
        )
        fsize = max(10, int(min(W, H) * size_pct / 100.0))
        font = _load_font(fsize)
        fill = (*_parse_color(color), _parse_opacity(opacity, DEFAULT_OPACITY))
        ang = _parse_float(
            str(angle) if angle is not None else "0",
            "angle",
            0.0,
            lo=-180.0,
            hi=180.0,
        )

        if repeat:
            _draw_text_tile(overlay, font, fill, label, ang, spacing=fsize * 2)
            notes.append("tiled")
        else:
            _draw_text_once(overlay, font, fill, label, ang, pos)
            notes.append("single")
        notes.append("text_watermark")
    else:
        if not logo_data:
            raise WatermarkError("logo image is required for image watermark")
        try:
            logo, _l_fmt, _l_frames = _open_first_frame(logo_data, logo_filename)
        except WatermarkError as exc:
            raise WatermarkError(f"Cannot read watermark logo: {exc}") from exc

        size_pct = _parse_float(
            str(logo_size_pct) if logo_size_pct is not None else str(DEFAULT_LOGO_SIZE_PCT),
            "logo_size_pct",
            DEFAULT_LOGO_SIZE_PCT,
            lo=1.0,
            hi=90.0,
        )
        logo_side = min(logo.size)
        target = max(10, int(min(W, H) * size_pct / 100.0))
        scale = target / float(logo_side)
        nw = max(1, int(round(logo.size[0] * scale)))
        nh = max(1, int(round(logo.size[1] * scale)))
        logo = logo.resize((nw, nh), Image.Resampling.LANCZOS)
        if logo.mode != "RGBA":
            logo = logo.convert("RGBA")

        op = _parse_opacity(opacity, DEFAULT_OPACITY)
        _draw_logo_once(overlay, logo, op, pos)
        notes.append("logo_watermark")

    if out_fmt != "auto":
        notes.append(f"converted_to_{out_fmt}")

    out = _render_result(base, overlay, out_fmt=out_fmt)
    save_name, media, ext = _OUTPUT_FORMATS[out_fmt]
    return {
        "data": out,
        "format": out_fmt,
        "media_type": media,
        "extension": ext,
        "width": W,
        "height": H,
        "frames": frames,
        "watermark_type": wt,
        "opacity": _parse_opacity(opacity, DEFAULT_OPACITY),
        "position": pos,
        "repeat": bool(repeat),
        "angle": _parse_float(
            str(angle) if angle is not None else "0",
            "angle",
            0.0,
            lo=-180.0,
            hi=180.0,
        ),
        "notes": notes,
        "original_bytes": len(data),
        "output_bytes": len(out),
    }


__all__ = [
    "WatermarkError",
    "watermark_types",
    "positions",
    "output_formats",
    "apply_watermark",
    "DEFAULT_TEXT",
    "DEFAULT_POSITION",
    "DEFAULT_OPACITY",
    "DEFAULT_ANGLE",
    "DEFAULT_FONT_SIZE_PCT",
    "DEFAULT_LOGO_SIZE_PCT",
]
