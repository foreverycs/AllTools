"""Tests for PDF tools plugin (split / merge / decrypt / extract)."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader

from app import app
from plugins.pdf_tools.pdf_ops import (
    MAX_PAGES,
    decrypt_pdf,
    extract_pages,
    merge_pdfs,
    split_pdf,
)
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def _pdf_bytes(n_pages: int = 3, *, password: str = "") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i in range(n_pages):
        c.setFont("Helvetica", 20)
        c.drawString(72, 720, f"Page {i + 1}")
        c.showPage()
    c.save()
    data = buf.getvalue()
    if password:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(data)))
        writer.encrypt(password)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    return data


def _png_bytes(size=(80, 60)) -> bytes:
    img = Image.new("RGB", size, (40, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def test_split_pdf(tmp_path):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(_pdf_bytes(4))
    out_dir = tmp_path / "pages"
    out_dir.mkdir()
    stats = split_pdf(str(pdf), str(out_dir))
    assert stats["input_pages"] == 4
    assert stats["output_files"] == 4
    assert len(list(out_dir.iterdir())) == 4
    for name in stats["files"]:
        with PdfReader(str(out_dir / name)) as r:
            assert len(r.pages) == 1


def test_split_pdf_with_ranges(tmp_path):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(_pdf_bytes(5))
    out_dir = tmp_path / "pages"
    out_dir.mkdir()
    stats = split_pdf(str(pdf), str(out_dir), ranges="1,3-4")
    assert stats["output_files"] == 3
    names = sorted(stats["files"])
    assert names == ["page-001.pdf", "page-003.pdf", "page-004.pdf"]


def test_merge_pdfs(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(_pdf_bytes(2))
    b.write_bytes(_pdf_bytes(3))
    out = tmp_path / "out.pdf"
    stats = merge_pdfs([str(a), str(b)], str(out))
    assert stats["input_files"] == 2
    assert stats["output_pages"] == 5
    with PdfReader(str(out)) as r:
        assert len(r.pages) == 5


def test_decrypt_pdf(tmp_path):
    pdf = tmp_path / "enc.pdf"
    pdf.write_bytes(_pdf_bytes(2, password="secret"))
    out = tmp_path / "dec.pdf"
    stats = decrypt_pdf(str(pdf), str(out), "secret")
    assert stats["output_pages"] == 2
    with PdfReader(str(out)) as r:
        assert not r.is_encrypted


def test_decrypt_wrong_password(tmp_path):
    pdf = tmp_path / "enc.pdf"
    pdf.write_bytes(_pdf_bytes(1, password="secret"))
    out = tmp_path / "dec.pdf"
    from core.errors import ValidationError

    with pytest.raises(ValidationError):
        decrypt_pdf(str(pdf), str(out), "wrong")


def test_extract_pages(tmp_path):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(_pdf_bytes(6))
    out = tmp_path / "ext.pdf"
    stats = extract_pages(str(pdf), str(out), "2-3,5")
    assert stats["output_pages"] == 3
    with PdfReader(str(out)) as r:
        assert len(r.pages) == 3
        text = r.pages[0].extract_text() or ""
        assert "Page 2" in text


def test_extract_invalid_range(tmp_path):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(_pdf_bytes(3))
    out = tmp_path / "ext.pdf"
    from core.errors import ValidationError

    with pytest.raises(ValidationError):
        extract_pages(str(pdf), str(out), "99")


def test_invalid_page_spec():
    from core.errors import ValidationError
    from plugins.pdf_tools.pdf_ops import _parse_page_ranges

    with pytest.raises(ValidationError):
        _parse_page_ranges("abc", 10)


def test_non_pdf_rejected(tmp_path):
    raw = tmp_path / "x.txt"
    raw.write_bytes(b"not a pdf")
    out = tmp_path / "o.pdf"
    from core.errors import PDFParseError

    with pytest.raises(PDFParseError):
        split_pdf(str(raw), str(tmp_path))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def test_http_options():
    client = TestClient(app)
    r = client.get("/tools/pdf-tools/options")
    assert r.status_code == 200
    body = r.json()
    assert "split" in body["actions"]
    assert "merge" in body["actions"]
    assert "decrypt" in body["actions"]
    assert "extract" in body["actions"]
    assert body["max_pages"] == MAX_PAGES


def test_http_page_renders():
    client = TestClient(app)
    r = client.get("/tools/pdf-tools")
    assert r.status_code == 200
    assert "PDF 工具集" in r.text
    assert "actionRow" in r.text


def test_http_split_zip():
    client = TestClient(app)
    raw = _pdf_bytes(3)
    r = client.post(
        "/tools/pdf-tools/convert",
        files={"files": ("doc.pdf", raw, "application/pdf")},
        data={"action": "split"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("application/zip")
    assert r.headers.get("X-Input-Pages") == "3"
    assert r.headers.get("X-Output-Files") == "3"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert len(zf.namelist()) == 3
        assert all(n.endswith(".pdf") for n in zf.namelist())


def test_http_merge():
    client = TestClient(app)
    a = _pdf_bytes(2)
    b = _pdf_bytes(3)
    r = client.post(
        "/tools/pdf-tools/convert",
        files=[
            ("files", ("a.pdf", a, "application/pdf")),
            ("files", ("b.pdf", b, "application/pdf")),
        ],
        data={"action": "merge"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert r.headers.get("X-Output-Pages") == "5"
    with PdfReader(io.BytesIO(r.content)) as reader:
        assert len(reader.pages) == 5


def test_http_merge_requires_two():
    client = TestClient(app)
    raw = _pdf_bytes(2)
    r = client.post(
        "/tools/pdf-tools/convert",
        files={"files": ("a.pdf", raw, "application/pdf")},
        data={"action": "merge"},
    )
    assert r.status_code == 400


def test_http_decrypt():
    client = TestClient(app)
    raw = _pdf_bytes(2, password="secret")
    r = client.post(
        "/tools/pdf-tools/convert",
        files={"files": ("enc.pdf", raw, "application/pdf")},
        data={"action": "decrypt", "password": "secret"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("application/pdf")
    with PdfReader(io.BytesIO(r.content)) as reader:
        assert not reader.is_encrypted
        assert len(reader.pages) == 2


def test_http_decrypt_wrong_password():
    client = TestClient(app)
    raw = _pdf_bytes(2, password="secret")
    r = client.post(
        "/tools/pdf-tools/convert",
        files={"files": ("enc.pdf", raw, "application/pdf")},
        data={"action": "decrypt", "password": "wrong"},
    )
    assert r.status_code == 400


def test_http_extract():
    client = TestClient(app)
    raw = _pdf_bytes(5)
    r = client.post(
        "/tools/pdf-tools/convert",
        files={"files": ("doc.pdf", raw, "application/pdf")},
        data={"action": "extract", "page_spec": "2,4"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Output-Pages") == "2"
    with PdfReader(io.BytesIO(r.content)) as reader:
        assert len(reader.pages) == 2


def test_http_extract_missing_spec():
    client = TestClient(app)
    raw = _pdf_bytes(3)
    r = client.post(
        "/tools/pdf-tools/convert",
        files={"files": ("doc.pdf", raw, "application/pdf")},
        data={"action": "extract"},
    )
    assert r.status_code == 400


def test_http_rejects_non_pdf():
    client = TestClient(app)
    r = client.post(
        "/tools/pdf-tools/convert",
        files={"files": ("x.txt", b"not a pdf", "text/plain")},
        data={"action": "split"},
    )
    assert r.status_code == 400
