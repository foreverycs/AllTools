"""Tests for image nine-grid split (core + HTTP)."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import app
from media.image_grid import (
    ImageGridError,
    build_grid_preview,
    max_dim,
    split_image,
    supported_formats,
)


def _png_bytes(size=(300, 300), *, alpha: bool = False, color=(40, 120, 200)) -> bytes:
    if alpha:
        img = Image.new("RGBA", size, (255, 0, 0, 128))
    else:
        img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_formats_and_max():
    assert "png" in supported_formats()
    assert "jpeg" in supported_formats()
    assert "webp" in supported_formats()
    assert max_dim() >= 10


def test_default_three_by_three():
    raw = _png_bytes((300, 300))
    out = split_image(raw, filename="a.png")
    assert out["grid"] == {"rows": 3, "cols": 3, "total": 9}
    assert len(out["tiles"]) == 9
    assert out["format"] == "png"
    assert out["input"] == {"width": 300, "height": 300, "filename": "a.png"}
    # Tiles tile the source exactly (each ~100px, last to 300).
    for tile in out["tiles"]:
        assert tile["width"] >= 99
        assert tile["height"] >= 99
        assert tile["data"]
    assert out["original_bytes"] == len(raw)
    assert out["output_bytes"] == sum(len(t["data"]) for t in out["tiles"])


def test_custom_grid_and_names():
    raw = _png_bytes((200, 100))
    out = split_image(raw, filename="photo.jpeg", rows=2, cols=4, fmt="png")
    assert out["grid"]["total"] == 8
    names = [t["name"] for t in out["tiles"]]
    assert "photo_r1c1.png" in names
    assert "photo_r2c4.png" in names


def test_jpeg_flattens_alpha():
    raw = _png_bytes((90, 90), alpha=True)
    out = split_image(raw, filename="t.png", fmt="jpeg")
    assert out["format"] == "jpeg"
    assert out["extension"] == ".jpg"
    assert "alpha_flattened" in out["notes"]
    with Image.open(io.BytesIO(out["tiles"][0]["data"])) as im:
        assert im.mode == "RGB"


def test_small_image_rejected():
    raw = _png_bytes((2, 2))
    with pytest.raises(ImageGridError):
        split_image(raw, rows=3, cols=3)


def test_invalid_axis_rejected():
    raw = _png_bytes((100, 100))
    with pytest.raises(ImageGridError):
        split_image(raw, rows=11, cols=3)
    with pytest.raises(ImageGridError):
        split_image(raw, rows=0, cols=3)


def test_invalid_format_rejected():
    raw = _png_bytes((100, 100))
    with pytest.raises(ImageGridError):
        split_image(raw, fmt="bmp")


def test_grid_preview_png():
    raw = _png_bytes((80, 80))
    preview = build_grid_preview(raw, filename="p.png", rows=3, cols=3)
    assert preview.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(preview)) as im:
        assert im.format == "PNG"
        assert im.size == (80, 80)


def test_http_options():
    client = TestClient(app)
    r = client.get("/tools/image-grid/options")
    assert r.status_code == 200
    body = r.json()
    assert "png" in body["formats"]
    assert body["defaults"]["rows"] == 3
    assert body["defaults"]["cols"] == 3


def test_http_page_renders():
    client = TestClient(app)
    r = client.get("/tools/image-grid")
    assert r.status_code == 200
    assert "图片九宫格" in r.text
    assert "previewGrid" in r.text


def test_http_split_zip():
    client = TestClient(app)
    raw = _png_bytes((120, 120))
    r = client.post(
        "/tools/image-grid/split",
        files={"file": ("pic.png", raw, "image/png")},
        data={"rows": "3", "cols": "3", "fmt": "png"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("application/zip")
    assert r.headers.get("X-Tiles") == "9"
    assert r.headers.get("X-Rows") == "3"
    assert r.headers.get("X-Image-Width") == "120"
    assert r.headers.get("content-disposition", "").lower().find("zip") != -1
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        assert len(names) == 9
        assert any(n.endswith(".png") for n in names)


def test_http_split_rejects_non_image():
    client = TestClient(app)
    r = client.post(
        "/tools/image-grid/split",
        files={"file": ("x.txt", b"not an image", "text/plain")},
    )
    assert r.status_code == 400


def test_http_preview():
    client = TestClient(app)
    raw = _png_bytes((60, 60))
    r = client.post(
        "/tools/image-grid/preview",
        files={"file": ("p.png", raw, "image/png")},
        data={"rows": "2", "cols": "2"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == 2
    assert body["cols"] == 2
    assert body["tiles"] == 4
    assert body["preview"].startswith("data:image/png;base64,")
