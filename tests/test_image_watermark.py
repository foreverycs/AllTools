"""Tests for image watermarking (core + HTTP)."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import app
from plugins.image_watermark.watermark import (
    WatermarkError,
    apply_watermark,
    output_formats,
    positions,
    watermark_types,
)


def _png_bytes(size=(300, 200), *, alpha: bool = False, color=(40, 120, 200)) -> bytes:
    if alpha:
        img = Image.new("RGBA", size, (255, 0, 0, 128))
    else:
        img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size=(300, 200)) -> bytes:
    img = Image.new("RGB", size, (240, 240, 240))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_public_lists():
    assert "text" in watermark_types()
    assert "image" in watermark_types()
    assert "bottom-right" in positions()
    assert "center" in positions()
    assert "auto" in output_formats()
    assert "png" in output_formats()


def test_text_watermark_defaults():
    raw = _png_bytes((300, 200))
    out = apply_watermark(raw, filename="a.png")
    assert out["format"] == "png"
    assert out["extension"] == ".png"
    assert out["width"] == 300
    assert out["height"] == 200
    assert out["watermark_type"] == "text"
    assert "text_watermark" in out["notes"]
    with Image.open(io.BytesIO(out["data"])) as im:
        assert im.format == "PNG"
        assert im.size == (300, 200)


def test_jpeg_input_stays_jpeg():
    raw = _jpeg_bytes((200, 200))
    out = apply_watermark(raw, filename="p.jpg", text="TEST")
    assert out["format"] == "jpeg"
    assert out["extension"] == ".jpg"
    with Image.open(io.BytesIO(out["data"])) as im:
        assert im.format == "JPEG"


def test_force_png_output():
    raw = _jpeg_bytes((200, 200))
    out = apply_watermark(raw, filename="p.jpg", fmt="png")
    assert out["format"] == "png"
    assert "converted_to_png" in out["notes"]


def test_tiled_text():
    raw = _png_bytes((200, 200))
    out = apply_watermark(
        raw,
        filename="t.png",
        text="CONFIDENTIAL",
        repeat=True,
        angle="45",
        opacity="20",
    )
    assert out["repeat"] is True
    assert "tiled" in out["notes"]
    assert out["angle"] == 45


def test_logo_watermark():
    base = _png_bytes((300, 200))
    logo = _png_bytes((50, 50), alpha=True, color=(0, 0, 0))
    out = apply_watermark(
        base,
        filename="b.png",
        watermark_type="image",
        logo_data=logo,
        logo_filename="logo.png",
        position="top-left",
    )
    assert out["watermark_type"] == "image"
    assert "logo_watermark" in out["notes"]
    assert out["position"] == "top-left"
    assert out["format"] == "png"


def test_gif_input_uses_first_frame():
    raw = _png_bytes((100, 100))
    out = apply_watermark(raw, filename="g.gif", text="X")
    assert "animation_first_frame_only" not in out["notes"]
    # A multi-frame GIF is produced below.
    frames = []
    for i in range(2):
        im = Image.new("RGB", (60, 40), (i * 100, 0, 0))
        frames.append(im)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100)
    gif = buf.getvalue()
    out = apply_watermark(gif, filename="g.gif", text="X")
    assert "animation_first_frame_only" in out["notes"]
    assert out["format"] == "png"


def test_empty_text_rejected():
    raw = _png_bytes((100, 100))
    with pytest.raises(WatermarkError):
        apply_watermark(raw, filename="a.png", text="   ")


def test_logo_required_rejected():
    raw = _png_bytes((100, 100))
    with pytest.raises(WatermarkError):
        apply_watermark(raw, filename="a.png", watermark_type="image")


def test_invalid_color_rejected():
    raw = _png_bytes((100, 100))
    with pytest.raises(WatermarkError):
        apply_watermark(raw, filename="a.png", color="#zzz")


def test_invalid_opacity_rejected():
    raw = _png_bytes((100, 100))
    with pytest.raises(WatermarkError):
        apply_watermark(raw, filename="a.png", opacity="101")


def test_invalid_position_rejected():
    raw = _png_bytes((100, 100))
    with pytest.raises(WatermarkError):
        apply_watermark(raw, filename="a.png", position="nowhere")


def test_invalid_format_rejected():
    raw = _png_bytes((100, 100))
    with pytest.raises(WatermarkError):
        apply_watermark(raw, filename="a.png", fmt="gif")


def test_http_options():
    client = TestClient(app)
    r = client.get("/tools/image-watermark/options")
    assert r.status_code == 200
    body = r.json()
    assert "text" in body["types"]
    assert "image" in body["types"]
    assert body["defaults"]["position"] == "bottom-right"


def test_http_page_renders():
    client = TestClient(app)
    r = client.get("/tools/image-watermark")
    assert r.status_code == 200
    assert "图片加水印" in r.text
    assert "wmText" in r.text


def test_http_watermark_text():
    client = TestClient(app)
    raw = _png_bytes((120, 120))
    r = client.post(
        "/tools/image-watermark/watermark",
        files={"file": ("pic.png", raw, "image/png")},
        data={"type": "text", "text": "DEMO", "opacity": "30", "fmt": "png"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("image/png")
    assert r.headers.get("X-Watermark-Type") == "text"
    assert r.headers.get("X-Image-Width") == "120"
    with Image.open(io.BytesIO(r.content)) as im:
        assert im.format == "PNG"
        assert im.size == (120, 120)


def test_http_watermark_logo():
    client = TestClient(app)
    raw = _png_bytes((120, 120))
    logo = _png_bytes((30, 30), alpha=True, color=(0, 0, 0))
    r = client.post(
        "/tools/image-watermark/watermark",
        files={
            "file": ("pic.png", raw, "image/png"),
            "logo": ("logo.png", logo, "image/png"),
        },
        data={"type": "image", "position": "center", "fmt": "auto"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Watermark-Type") == "image"
    assert r.headers.get("X-Watermark-Position") == "center"


def test_http_watermark_rejects_non_image():
    client = TestClient(app)
    r = client.post(
        "/tools/image-watermark/watermark",
        files={"file": ("x.txt", b"not an image", "text/plain")},
        data={"type": "text", "text": "X"},
    )
    assert r.status_code == 400
