"""Tests for PDF compress plugin."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from plugins.pdf_compress.compress import compress_pdf, quality_presets


def _pdf_bytes(n_pages: int = 2) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i in range(n_pages):
        c.setFont("Helvetica", 20)
        c.drawString(72, 720, f"Page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_quality_presets():
    assert "light" in quality_presets()
    assert "balanced" in quality_presets()
    assert "strong" in quality_presets()


def test_compress_light(tmp_path):
    pytest.importorskip("fitz")
    src = tmp_path / "in.pdf"
    src.write_bytes(_pdf_bytes(3))
    out = tmp_path / "out.pdf"
    stats = compress_pdf(str(src), str(out), quality="light")
    assert out.is_file() and out.stat().st_size > 0
    assert stats["input_pages"] == 3
    assert "original_bytes" in stats
    with PdfReader(str(out)) as r:
        assert len(r.pages) == 3


def test_compress_api(client_env):
    pytest.importorskip("fitz")
    client = client_env
    r = client.get("/tools/pdf-compress")
    assert r.status_code == 200
    r = client.post(
        "/tools/pdf-compress/compress",
        files={"file": ("t.pdf", _pdf_bytes(2), "application/pdf")},
        data={"quality": "light"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert int(r.headers.get("X-Input-Pages") or 0) == 2


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
