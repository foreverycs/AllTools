"""word2pdf async job path: submit → poll → download."""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from unittest import mock

import pytest
from docx import Document
from fastapi.testclient import TestClient

from core import jobs as jobs_mod


def _make_docx(path: str) -> None:
    doc = Document()
    doc.add_paragraph("hello async word2pdf")
    doc.save(path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-for-unit-tests-only")
    jobs_mod.reset_jobs()
    from app import app

    with TestClient(app) as c:
        yield c
    jobs_mod.reset_jobs()


def _wait_job(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in ("done", "error"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {last}")


def test_convert_async_lifecycle(client, tmp_path):
    src = tmp_path / "a.docx"
    _make_docx(str(src))

    def fake_convert(docx_path, pdf_path):
        Path(pdf_path).write_bytes(b"%PDF-1.4 mock-async")
        return {"engine": "libreoffice", "bytes": 18}

    with mock.patch(
        "tools.word2pdf.engine_info",
        return_value={
            "engines": ["libreoffice"],
            "preferred": "libreoffice",
            "libreoffice_path": "/usr/bin/soffice",
            "ready": True,
        },
    ), mock.patch("tools.word2pdf._convert_one", side_effect=fake_convert):
        with open(src, "rb") as f:
            resp = client.post(
                "/tools/word2pdf/convert-async",
                files={
                    "file": (
                        "a.docx",
                        f,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["id"]
    assert body["mode"] == "async"
    assert body["download_name"] == "a.pdf"

    job = _wait_job(client, body["id"])
    assert job["status"] == "done"
    assert job["has_result"] is True

    dl = client.get(body["download_url"])
    assert dl.status_code == 200
    assert dl.content.startswith(b"%PDF")
    assert dl.headers.get("X-Engine") == "libreoffice"

    # After download, files cleaned; second download may 409/410.
    dl2 = client.get(body["download_url"])
    assert dl2.status_code in (409, 410)


def test_convert_async_rejects_without_engine(client, tmp_path):
    src = tmp_path / "a.docx"
    _make_docx(str(src))
    with mock.patch(
        "tools.word2pdf.engine_info",
        return_value={
            "engines": [],
            "preferred": None,
            "libreoffice_path": None,
            "ready": False,
        },
    ):
        with open(src, "rb") as f:
            resp = client.post(
                "/tools/word2pdf/convert-async",
                files={
                    "file": (
                        "a.docx",
                        f,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
    assert resp.status_code == 503


def test_convert_batch_async_zip(client, tmp_path):
    a = tmp_path / "a.docx"
    b = tmp_path / "b.docx"
    _make_docx(str(a))
    _make_docx(str(b))

    def fake_convert(docx_path, pdf_path):
        Path(pdf_path).write_bytes(b"%PDF-1.4 batch")
        return {"engine": "libreoffice", "bytes": 12}

    with mock.patch(
        "tools.word2pdf.engine_info",
        return_value={
            "engines": ["libreoffice"],
            "preferred": "libreoffice",
            "libreoffice_path": "/usr/bin/soffice",
            "ready": True,
        },
    ), mock.patch("tools.word2pdf._convert_one", side_effect=fake_convert):
        with open(a, "rb") as fa, open(b, "rb") as fb:
            resp = client.post(
                "/tools/word2pdf/convert-batch-async",
                files=[
                    (
                        "files",
                        (
                            "a.docx",
                            fa,
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        ),
                    ),
                    (
                        "files",
                        (
                            "b.docx",
                            fb,
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        ),
                    ),
                ],
            )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["files"] == 2
    job = _wait_job(client, body["id"])
    assert job["status"] == "done"
    dl = client.get(body["download_url"])
    assert dl.status_code == 200
    assert dl.headers.get("X-Files") == "2"
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert len(zf.namelist()) == 2
