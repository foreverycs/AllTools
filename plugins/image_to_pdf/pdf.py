"""Images → single multi-page PDF.

Each image becomes one page. Supports common raster formats (same set as
``image_convert``). No extra dependencies beyond Pillow.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageOps

from tools.common import (
    ImageFormatError as ConvertError,
    detect_image_format as detect_format,
)

# ---------------------------------------------------------------------------
# Public constants / errors
# ---------------------------------------------------------------------------

MAX_IMAGES = 50

# A4 in PDF points (1/72 inch)
A4_W_PT = 595.28
A4_H_PT = 841.89
A4_MARGIN_PT = 36.0  # 0.5 inch

PAGE_MODES = ("fit", "a4")

# A4 orientation when page_mode is a4.
# auto — per image aspect; portrait — always 210×297; landscape — always 297×210.
ORIENTATIONS = ("auto", "portrait", "landscape")

INPUT_FORMATS = ("jpeg", "png", "gif", "webp", "bmp", "tiff", "ico")


class ImageToPdfError(ValueError):
    """Raised when images cannot be turned into a PDF."""


def input_formats() -> List[str]:
    return list(INPUT_FORMATS)


def page_modes() -> List[str]:
    return list(PAGE_MODES)


def orientations() -> List[str]:
    return list(ORIENTATIONS)


def max_images() -> int:
    return MAX_IMAGES


def _parse_bg(color: Optional[str]) -> Tuple[int, int, int]:
    """Parse ``#rgb`` / ``#rrggbb`` / white/black."""
    raw = (color or "#ffffff").strip()
    low = raw.lower()
    if low in ("white", "#fff", "#ffffff"):
        return (255, 255, 255)
    if low in ("black", "#000", "#000000"):
        return (0, 0, 0)
    if low.startswith("rgb(") and low.endswith(")"):
        parts = low[4:-1].split(",")
        if len(parts) == 3:
            try:
                r, g, b = (int(p.strip()) for p in parts)
                return (
                    max(0, min(255, r)),
                    max(0, min(255, g)),
                    max(0, min(255, b)),
                )
            except ValueError:
                pass
    if raw.startswith("#"):
        h = raw[1:]
        if len(h) == 3 and all(c in "0123456789abcdefABCDEF" for c in h):
            return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16))
        if len(h) == 6 and all(c in "0123456789abcdefABCDEF" for c in h):
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    raise ImageToPdfError(
        f"Invalid background color: {color!r}. Use #rrggbb or white/black."
    )


def _normalize_page_mode(mode: Optional[str]) -> str:
    m = (mode or "fit").strip().lower()
    if m not in PAGE_MODES:
        raise ImageToPdfError(
            f"page_mode must be one of: {', '.join(PAGE_MODES)}"
        )
    return m


def _normalize_orientation(orientation: Optional[str]) -> str:
    o = (orientation or "auto").strip().lower()
    aliases = {
        "v": "portrait",
        "vertical": "portrait",
        "port": "portrait",
        "h": "landscape",
        "horizontal": "landscape",
        "land": "landscape",
    }
    o = aliases.get(o, o)
    if o not in ORIENTATIONS:
        raise ImageToPdfError(
            f"orientation must be one of: {', '.join(ORIENTATIONS)}"
        )
    return o


def _has_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA"):
        return True
    if img.mode == "P" and "transparency" in img.info:
        return True
    return False


def _flatten_to_rgb(
    img: Image.Image, bg: Tuple[int, int, int]
) -> Image.Image:
    """Return an RGB image; composite alpha onto ``bg`` when needed."""
    if img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, bg)
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if img.mode == "RGB":
        return img
    if img.mode == "L":
        return img.convert("RGB")
    return img.convert("RGB")


def _load_rgb_page(
    data: bytes,
    *,
    filename: Optional[str],
    bg: Tuple[int, int, int],
    notes: List[str],
) -> Image.Image:
    """Decode one image to RGB (first frame only for animations)."""
    if not data:
        raise ImageToPdfError(
            f"Empty file{f': {filename}' if filename else ''}"
        )
    try:
        detect_format(data, filename)
    except ConvertError as exc:
        raise ImageToPdfError(str(exc)) from exc

    try:
        with Image.open(io.BytesIO(data)) as im:
            n = getattr(im, "n_frames", 1) or 1
            if n > 1:
                notes.append("animation_first_frame_only")
            fr = ImageOps.exif_transpose(im.copy())
            if _has_alpha(fr):
                notes.append("alpha_flattened")
            return _flatten_to_rgb(fr, bg)
    except ImageToPdfError:
        raise
    except OSError as exc:
        name = filename or "image"
        raise ImageToPdfError(f"Cannot read image ({name}): {exc}") from exc
    except Exception as exc:
        name = filename or "image"
        raise ImageToPdfError(f"Cannot read image ({name}): {exc}") from exc


def _resolve_a4_size(
    img_w: int, img_h: int, orientation: str
) -> Tuple[float, float, str]:
    """Return (page_w_pt, page_h_pt, resolved_label) for A4."""
    if orientation == "portrait":
        return A4_W_PT, A4_H_PT, "portrait"
    if orientation == "landscape":
        return A4_H_PT, A4_W_PT, "landscape"
    # auto
    if img_w > img_h:
        return A4_H_PT, A4_W_PT, "landscape"
    return A4_W_PT, A4_H_PT, "portrait"


def _place_on_a4(
    img: Image.Image,
    bg: Tuple[int, int, int],
    *,
    orientation: str = "auto",
) -> Tuple[Image.Image, str]:
    """Scale ``img`` into A4 with margins. Returns (canvas, resolved orientation)."""
    w, h = img.size
    if w <= 0 or h <= 0:
        raise ImageToPdfError("Invalid image dimensions")

    page_w, page_h, resolved = _resolve_a4_size(w, h, orientation)
    # Pillow PDF uses pixels ≈ points at 72 dpi; build page at point resolution.
    pw = max(1, int(round(page_w)))
    ph = max(1, int(round(page_h)))
    margin = int(round(A4_MARGIN_PT))
    usable_w = max(1, pw - 2 * margin)
    usable_h = max(1, ph - 2 * margin)

    scale = min(usable_w / float(w), usable_h / float(h))
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    if (nw, nh) != (w, h):
        resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    else:
        resized = img

    canvas = Image.new("RGB", (pw, ph), bg)
    ox = (pw - nw) // 2
    oy = (ph - nh) // 2
    canvas.paste(resized, (ox, oy))
    return canvas, resolved


def images_to_pdf(
    items: Sequence[bytes],
    *,
    filenames: Optional[Sequence[Optional[str]]] = None,
    page_mode: str = "fit",
    orientation: str = "auto",
    background: str = "#ffffff",
) -> Dict[str, Any]:
    """Convert one or more image blobs into a single PDF.

    Parameters
    ----------
    items:
        Raw image file bytes (order = page order).
    filenames:
        Optional names (format detection / error messages).
    page_mode:
        ``fit`` — page size matches each image; ``a4`` — fit into A4 with margin.
    orientation:
        When ``page_mode`` is ``a4``: ``auto`` / ``portrait`` / ``landscape``.
        Ignored for ``fit``.
    background:
        Solid color when flattening alpha (and A4 page fill).

    Returns
    -------
    dict with ``data``, ``media_type``, ``extension``, ``page_count``,
    ``page_mode``, ``orientation``, ``notes``, ``original_bytes``,
    ``output_bytes``, etc.
    """
    if not items:
        raise ImageToPdfError("At least one image is required")
    if len(items) > MAX_IMAGES:
        raise ImageToPdfError(
            f"Too many images (max {MAX_IMAGES}, got {len(items)})"
        )

    mode = _normalize_page_mode(page_mode)
    orient = _normalize_orientation(orientation)
    bg = _parse_bg(background)
    names: List[Optional[str]] = list(filenames or [])
    while len(names) < len(items):
        names.append(None)

    notes: List[str] = []
    pages: List[Image.Image] = []
    total_in = 0
    resolved_orients: List[str] = []

    for data, name in zip(items, names):
        total_in += len(data) if data else 0
        page_notes: List[str] = []
        rgb = _load_rgb_page(
            data, filename=name, bg=bg, notes=page_notes
        )
        for n in page_notes:
            if n not in notes:
                notes.append(n)
        if mode == "a4":
            rgb, resolved = _place_on_a4(rgb, bg, orientation=orient)
            resolved_orients.append(resolved)
        pages.append(rgb)

    if mode == "a4":
        notes.append("page_mode_a4")
        notes.append(f"orientation_{orient}")
        if orient == "auto" and resolved_orients:
            # Summarize mixed auto resolutions for debugging / headers.
            uniq = sorted(set(resolved_orients))
            if len(uniq) == 1:
                notes.append(f"resolved_{uniq[0]}")
            else:
                notes.append("resolved_mixed")
    else:
        notes.append("page_mode_fit")

    try:
        buf = io.BytesIO()
        first = pages[0]
        rest = pages[1:]
        save_kwargs: Dict[str, Any] = {
            "format": "PDF",
            "resolution": 72.0,
        }
        if rest:
            save_kwargs["save_all"] = True
            save_kwargs["append_images"] = rest
        first.save(buf, **save_kwargs)
        out = buf.getvalue()
    except OSError as exc:
        raise ImageToPdfError(f"PDF write failed: {exc}") from exc
    except Exception as exc:
        raise ImageToPdfError(f"PDF write failed: {exc}") from exc

    if not out.startswith(b"%PDF"):
        raise ImageToPdfError("PDF encoder produced invalid output")

    return {
        "data": out,
        "media_type": "application/pdf",
        "extension": ".pdf",
        "page_count": len(pages),
        "page_mode": mode,
        "orientation": orient if mode == "a4" else None,
        "background": background if isinstance(background, str) else "#ffffff",
        "notes": list(notes),
        "original_bytes": total_in,
        "output_bytes": len(out),
        "image_count": len(pages),
    }
