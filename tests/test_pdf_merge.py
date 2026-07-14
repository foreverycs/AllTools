"""Tests for the PDF merge (invoice merge) tool."""
from __future__ import annotations

import importlib
import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader


def _make_pdf(path: str, pages: int = 2) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas

    c = rl_canvas.Canvas(path, pagesize=A4)
    for i in range(1, pages + 1):
        c.drawString(100, 700, f"Page {i}")
        c.showPage()
    c.save()


def _make_single_page_pdf(path: str, text: str = "invoice") -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas

    c = rl_canvas.Canvas(path, pagesize=A4)
    c.drawString(100, 700, text)
    c.save()


@pytest.fixture()
def merge_client(tmp_path, monkeypatch):
    d = tmp_path / "file"
    d.mkdir()
    monkeypatch.setenv("UPLOAD_FILE_DIR", str(d))
    monkeypatch.setenv("UPLOAD_RETENTION_DAYS", "5")

    import storage.history as h
    import storage as s
    import admin.auth as auth
    import admin.routes as routes
    import app as app_mod

    importlib.reload(h)
    importlib.reload(s)
    importlib.reload(auth)
    importlib.reload(routes)
    importlib.reload(app_mod)

    client = TestClient(app_mod.app)
    yield client, tmp_path


def test_merge_single_pair(tmp_path):
    from tools.pdf_merge import _merge_single

    src = tmp_path / "2pages.pdf"
    _make_pdf(str(src), pages=2)
    out = tmp_path / "merged.pdf"

    stats = _merge_single(str(src), str(out), add_divider=True)
    assert stats["input_pages"] == 2
    assert stats["output_pages"] == 1
    assert out.exists()

    reader = PdfReader(str(out))
    assert len(reader.pages) == 1
    page = reader.pages[0]
    assert float(page.mediabox.width) == pytest.approx(595.28, abs=1)
    assert float(page.mediabox.height) == pytest.approx(841.89, abs=1)


def test_merge_single_four_pages(tmp_path):
    from tools.pdf_merge import _merge_single

    src = tmp_path / "4pages.pdf"
    _make_pdf(str(src), pages=4)
    out = tmp_path / "merged.pdf"

    stats = _merge_single(str(src), str(out), add_divider=False)
    assert stats["input_pages"] == 4
    assert stats["output_pages"] == 2

    reader = PdfReader(str(out))
    assert len(reader.pages) == 2


def test_merge_single_odd_pages(tmp_path):
    from tools.pdf_merge import _merge_single

    src = tmp_path / "3pages.pdf"
    _make_pdf(str(src), pages=3)
    out = tmp_path / "merged.pdf"

    stats = _merge_single(str(src), str(out), add_divider=True)
    assert stats["input_pages"] == 3
    assert stats["output_pages"] == 2

    reader = PdfReader(str(out))
    assert len(reader.pages) == 2


def test_merge_two_files(tmp_path):
    from tools.pdf_merge import _merge_two_files

    src1 = tmp_path / "a.pdf"
    src2 = tmp_path / "b.pdf"
    _make_single_page_pdf(str(src1), "Invoice A")
    _make_single_page_pdf(str(src2), "Invoice B")
    out = tmp_path / "merged.pdf"

    stats = _merge_two_files(str(src1), str(src2), str(out), add_divider=True)
    assert stats["input_pages"] == 2
    assert stats["output_pages"] == 1

    reader = PdfReader(str(out))
    assert len(reader.pages) == 1


def test_merge_rejects_single_page(tmp_path):
    from tools.pdf_merge import _merge_single

    src = tmp_path / "1page.pdf"
    _make_single_page_pdf(str(src))
    out = tmp_path / "merged.pdf"

    with pytest.raises(ValueError, match="at least 2 pages"):
        _merge_single(str(src), str(out), add_divider=True)


def test_api_merge_single(merge_client):
    client, tmp_path = merge_client
    src = tmp_path / "test.pdf"
    _make_pdf(str(src), pages=2)

    with open(str(src), "rb") as f:
        resp = client.post(
            "/tools/pdf-merge/convert",
            files={"file": ("test.pdf", f, "application/pdf")},
            data={"divider": "true"},
        )

    assert resp.status_code == 200
    assert resp.headers.get("content-type") == "application/pdf"
    assert resp.headers.get("X-Input-Pages") == "2"
    assert resp.headers.get("X-Output-Pages") == "1"

    reader = PdfReader(io.BytesIO(resp.content))
    assert len(reader.pages) == 1


def test_api_merge_two_files(merge_client):
    client, tmp_path = merge_client
    src1 = tmp_path / "a.pdf"
    src2 = tmp_path / "b.pdf"
    _make_single_page_pdf(str(src1), "A")
    _make_single_page_pdf(str(src2), "B")

    with open(str(src1), "rb") as f1, open(str(src2), "rb") as f2:
        resp = client.post(
            "/tools/pdf-merge/convert",
            files=[
                ("file", ("a.pdf", f1, "application/pdf")),
                ("file2", ("b.pdf", f2, "application/pdf")),
            ],
            data={"divider": "true"},
        )

    assert resp.status_code == 200
    assert resp.headers.get("X-Output-Pages") == "1"


def test_api_rejects_non_pdf(merge_client):
    client, tmp_path = merge_client
    txt = tmp_path / "bad.txt"
    txt.write_text("not a pdf")

    with open(str(txt), "rb") as f:
        resp = client.post(
            "/tools/pdf-merge/convert",
            files={"file": ("bad.txt", f, "text/plain")},
        )

    assert resp.status_code == 400


def test_api_rejects_single_page(merge_client):
    client, tmp_path = merge_client
    src = tmp_path / "1page.pdf"
    _make_single_page_pdf(str(src))

    with open(str(src), "rb") as f:
        resp = client.post(
            "/tools/pdf-merge/convert",
            files={"file": ("1page.pdf", f, "application/pdf")},
        )

    assert resp.status_code == 400


def test_page_registered():
    from tools import TOOL_REGISTRY

    slugs = {t["slug"] for t in TOOL_REGISTRY}
    assert "pdf-merge" in slugs


def test_route_accessible(merge_client):
    client, _ = merge_client
    resp = client.get("/tools/pdf-merge")
    assert resp.status_code == 200
    assert "发票合并" in resp.text
