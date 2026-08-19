"""Tests for PDF → images plugin."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from plugins.pdf_to_images.render import render_pdf_to_images


def _pdf_bytes(n_pages: int = 3) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i in range(n_pages):
        c.setFont("Helvetica", 18)
        c.drawString(72, 720, f"Page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_render_png(tmp_path):
    pytest.importorskip("fitz")
    src = tmp_path / "in.pdf"
    src.write_bytes(_pdf_bytes(3))
    out = tmp_path / "pages"
    out.mkdir()
    stats = render_pdf_to_images(str(src), str(out), fmt="png", dpi=72)
    assert stats["output_files"] == 3
    assert len(list(out.glob("*.png"))) == 3


def test_render_range(tmp_path):
    pytest.importorskip("fitz")
    src = tmp_path / "in.pdf"
    src.write_bytes(_pdf_bytes(5))
    out = tmp_path / "pages"
    out.mkdir()
    stats = render_pdf_to_images(
        str(src), str(out), fmt="jpeg", dpi=96, page_spec="1,3-4"
    )
    assert stats["output_files"] == 3
    names = sorted(stats["files"])
    assert names == ["page-001.jpg", "page-003.jpg", "page-004.jpg"]


def test_api_convert(client_env):
    pytest.importorskip("fitz")
    client = client_env
    r = client.get("/tools/pdf-to-images")
    assert r.status_code == 200
    r = client.post(
        "/tools/pdf-to-images/convert",
        files={"file": ("t.pdf", _pdf_bytes(2), "application/pdf")},
        data={"format": "png", "dpi": "72"},
    )
    assert r.status_code == 200, r.text
    assert "zip" in (r.headers.get("content-type") or "")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert len(zf.namelist()) == 2


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    d = tmp_path / "file"
    d.mkdir()
    monkeypatch.setenv("UPLOAD_FILE_DIR", str(d))
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-for-unit-tests-only")
    monkeypatch.setenv("DOTENV_OVERRIDE", "0")
    import importlib

    import app as app_mod
    import core.api_rate_limit as rl
    import core.concurrency as concurrency_mod
    import core.settings as settings_mod
    import core.tool_flags as flags_mod

    settings_mod.clear_settings_cache()
    concurrency_mod.reset_semaphore()
    rl.reset_all()
    flags_mod.clear_tool_flags_cache()
    importlib.reload(app_mod)
    return TestClient(app_mod.app)
