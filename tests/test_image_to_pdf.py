"""Tests for image → PDF (core + HTTP)."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader

from media.image_to_pdf import (
    ImageToPdfError,
    images_to_pdf,
    input_formats,
    max_images,
    orientations,
    page_modes,
)


def _png_bytes(size=(80, 60), *, alpha: bool = False, color=(40, 120, 200)) -> bytes:
    if alpha:
        img = Image.new("RGBA", size, (255, 0, 0, 128))
        for i in range(size[0]):
            img.putpixel((i, size[1] // 2), (0, 255, 0, 255))
    else:
        img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size=(100, 50), color=(10, 20, 30)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_format_and_mode_lists():
    assert "jpeg" in input_formats()
    assert "png" in input_formats()
    assert "fit" in page_modes()
    assert "a4" in page_modes()
    assert "auto" in orientations()
    assert "portrait" in orientations()
    assert "landscape" in orientations()
    assert max_images() >= 1


def test_single_png_to_pdf():
    raw = _png_bytes()
    out = images_to_pdf([raw], filenames=["a.png"], page_mode="fit")
    assert out["data"][:4] == b"%PDF"
    assert out["page_count"] == 1
    assert out["extension"] == ".pdf"
    assert out["media_type"] == "application/pdf"
    assert out["original_bytes"] == len(raw)
    assert out["output_bytes"] == len(out["data"])
    reader = PdfReader(io.BytesIO(out["data"]))
    assert len(reader.pages) == 1


def test_multi_images_page_count():
    items = [
        _png_bytes(color=(255, 0, 0)),
        _jpeg_bytes(color=(0, 255, 0)),
        _png_bytes(size=(40, 40), color=(0, 0, 255)),
    ]
    out = images_to_pdf(
        items,
        filenames=["a.png", "b.jpg", "c.png"],
        page_mode="fit",
    )
    assert out["page_count"] == 3
    reader = PdfReader(io.BytesIO(out["data"]))
    assert len(reader.pages) == 3


def test_a4_mode_auto_landscape_for_wide():
    raw = _png_bytes(size=(200, 100))
    out = images_to_pdf(
        [raw], filenames=["wide.png"], page_mode="a4", orientation="auto"
    )
    assert out["page_mode"] == "a4"
    assert out["orientation"] == "auto"
    assert "page_mode_a4" in out["notes"]
    assert "orientation_auto" in out["notes"]
    assert out["data"][:4] == b"%PDF"
    reader = PdfReader(io.BytesIO(out["data"]))
    page = reader.pages[0]
    # Landscape A4: width > height in points
    w = float(page.mediabox.width)
    h = float(page.mediabox.height)
    assert w > h


def test_a4_force_portrait_on_wide_image():
    raw = _png_bytes(size=(200, 100))
    out = images_to_pdf(
        [raw], filenames=["wide.png"], page_mode="a4", orientation="portrait"
    )
    assert out["orientation"] == "portrait"
    assert "orientation_portrait" in out["notes"]
    reader = PdfReader(io.BytesIO(out["data"]))
    page = reader.pages[0]
    w = float(page.mediabox.width)
    h = float(page.mediabox.height)
    assert h > w


def test_a4_force_landscape_on_tall_image():
    raw = _png_bytes(size=(80, 160))
    out = images_to_pdf(
        [raw], filenames=["tall.png"], page_mode="a4", orientation="landscape"
    )
    assert out["orientation"] == "landscape"
    reader = PdfReader(io.BytesIO(out["data"]))
    page = reader.pages[0]
    w = float(page.mediabox.width)
    h = float(page.mediabox.height)
    assert w > h


def test_bad_orientation():
    with pytest.raises(ImageToPdfError, match="orientation"):
        images_to_pdf([_png_bytes()], page_mode="a4", orientation="diagonal")


def test_alpha_flattened():
    raw = _png_bytes(alpha=True)
    out = images_to_pdf(
        [raw],
        filenames=["a.png"],
        page_mode="fit",
        background="#ffffff",
    )
    assert "alpha_flattened" in out["notes"]
    assert out["data"][:4] == b"%PDF"


def test_empty_list_raises():
    with pytest.raises(ImageToPdfError, match="At least one"):
        images_to_pdf([])


def test_bad_bytes_raises():
    with pytest.raises(ImageToPdfError):
        images_to_pdf([b"not-an-image"], filenames=["x.txt"])


def test_too_many_images():
    one = _png_bytes(size=(10, 10))
    many = [one] * (max_images() + 1)
    with pytest.raises(ImageToPdfError, match="Too many"):
        images_to_pdf(many)


def test_bad_page_mode():
    with pytest.raises(ImageToPdfError, match="page_mode"):
        images_to_pdf([_png_bytes()], page_mode="letter")


def test_http_options():
    from app import app

    client = TestClient(app)
    r = client.get("/tools/image-to-pdf/options")
    assert r.status_code == 200
    body = r.json()
    assert "jpeg" in body["input"]
    assert body["defaults"]["page_mode"] == "fit"
    assert body["defaults"]["orientation"] == "auto"
    assert "portrait" in body["orientations"]
    assert body["max_images"] == max_images()


def test_http_convert_single():
    from app import app

    client = TestClient(app)
    raw = _png_bytes(alpha=True)
    r = client.post(
        "/tools/image-to-pdf/convert",
        files=[("files", ("logo.png", raw, "image/png"))],
        data={"page_mode": "fit", "background": "#ffffff"},
    )
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert r.content[:4] == b"%PDF"
    assert int(r.headers.get("X-Page-Count", "0")) == 1
    assert int(r.headers.get("X-Original-Bytes", "0")) == len(raw)
    assert int(r.headers.get("X-Output-Bytes", "0")) > 0


def test_http_convert_multi():
    from app import app

    client = TestClient(app)
    files = [
        ("files", ("a.png", _png_bytes(color=(1, 2, 3)), "image/png")),
        ("files", ("b.jpg", _jpeg_bytes(), "image/jpeg")),
    ]
    r = client.post(
        "/tools/image-to-pdf/convert",
        files=files,
        data={"page_mode": "a4", "orientation": "portrait"},
    )
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"
    assert int(r.headers.get("X-Page-Count", "0")) == 2
    assert r.headers.get("X-Page-Mode") == "a4"
    assert r.headers.get("X-Orientation") == "portrait"
    reader = PdfReader(io.BytesIO(r.content))
    assert len(reader.pages) == 2
    page = reader.pages[0]
    assert float(page.mediabox.height) > float(page.mediabox.width)


def test_http_reject_bad_file():
    from app import app

    client = TestClient(app)
    r = client.post(
        "/tools/image-to-pdf/convert",
        files=[("files", ("x.txt", b"hello world", "text/plain"))],
        data={"page_mode": "fit"},
    )
    assert r.status_code == 400


def test_http_tool_page():
    from app import app

    client = TestClient(app)
    r = client.get("/tools/image-to-pdf")
    assert r.status_code == 200
    assert "图片转 PDF" in r.text


def test_registry_lists_image_to_pdf():
    from tools import TOOL_REGISTRY

    slugs = {t["slug"] for t in TOOL_REGISTRY}
    assert "image-to-pdf" in slugs
    tool = next(t for t in TOOL_REGISTRY if t["slug"] == "image-to-pdf")
    assert tool["category"] == "image"
    assert tool["route"] == "/tools/image-to-pdf"


def test_office_tool_on_home():
    from app import app

    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "图片转 PDF" in r.text
    assert "/tools/image-to-pdf" in r.text
