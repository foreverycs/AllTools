"""Tests for admin console auth and pages."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def admin_client(tmp_path, monkeypatch):
    d = tmp_path / "file"
    d.mkdir()
    monkeypatch.setenv("UPLOAD_FILE_DIR", str(d))
    monkeypatch.setenv("UPLOAD_RETENTION_DAYS", "5")
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-for-unit-tests-only")

    import core.settings as settings_mod
    import core.concurrency as concurrency_mod
    import storage.history as h
    import storage as s
    import admin.auth as auth
    import admin.routes as routes
    import tools.common as common
    import app as app_mod

    settings_mod.clear_settings_cache()
    concurrency_mod.reset_semaphore()
    common.refresh_limits()
    importlib.reload(h)
    importlib.reload(s)
    importlib.reload(auth)
    importlib.reload(routes)
    importlib.reload(app_mod)

    client = TestClient(app_mod.app)
    yield client, h, tmp_path


@pytest.fixture()
def hist_only(tmp_path, monkeypatch):
    d = tmp_path / "file"
    d.mkdir()
    monkeypatch.setenv("UPLOAD_FILE_DIR", str(d))
    monkeypatch.setenv("UPLOAD_RETENTION_DAYS", "5")
    import storage.history as h
    import storage as s

    importlib.reload(h)
    importlib.reload(s)
    return h, tmp_path


def _login(client: TestClient, password: str = "test-pass"):
    return client.post(
        "/admin/login",
        data={"password": password, "next": "/admin"},
        follow_redirects=False,
    )


def test_admin_requires_login(admin_client):
    client, _, _ = admin_client
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code in (303, 307, 302)
    assert "/admin/login" in r.headers.get("location", "")


def test_admin_login_and_dashboard(admin_client):
    client, _, _ = admin_client
    bad = _login(client, "wrong")
    assert bad.status_code in (303, 307, 302)
    assert "error" in bad.headers.get("location", "")

    ok = _login(client)
    assert ok.status_code in (303, 307, 302)
    loc = ok.headers.get("location", "")
    assert "/admin" in loc

    dash = client.get("/admin")
    assert dash.status_code == 200
    assert "仪表盘" in dash.text
    assert "上传" in dash.text


def test_admin_uploads_delete_and_download(admin_client):
    client, h, tmp_path = admin_client
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF-test")
    rec = h.archive_conversion(
        tool="pdf2word",
        original_name="a.pdf",
        input_path=str(src),
    )
    assert rec is not None

    _login(client)
    page = client.get("/admin/uploads")
    assert page.status_code == 200
    assert "a.pdf" in page.text
    assert rec["id"] in page.text

    dl = client.get(f"/admin/uploads/{rec['id']}/download")
    assert dl.status_code == 200
    assert dl.content.startswith(b"%PDF")

    deleted = client.post(
        f"/admin/uploads/{rec['id']}/delete",
        follow_redirects=False,
    )
    assert deleted.status_code in (303, 307, 302)
    assert h.get_record(rec["id"]) is None


def test_admin_uploads_preview(admin_client):
    client, h, tmp_path = admin_client
    src = tmp_path / "sample.pdf"
    src.write_bytes(b"%PDF-1.4 test content")
    rec = h.archive_conversion(
        tool="pdf2word",
        original_name="sample.pdf",
        input_path=str(src),
    )
    assert rec is not None

    _login(client)

    pv = client.get(f"/admin/uploads/{rec['id']}/preview")
    assert pv.status_code == 200
    assert pv.headers.get("content-type") == "application/pdf"
    assert "inline" in pv.headers.get("content-disposition", "")
    assert pv.content.startswith(b"%PDF")

    assert client.get("/admin/uploads/nonexistent/preview").status_code == 404


def test_admin_api_stats_unauthorized(admin_client):
    client, _, _ = admin_client
    r = client.get("/admin/api/stats")
    assert r.status_code == 401


def test_admin_api_stats_ok(admin_client):
    client, _, _ = admin_client
    _login(client)
    r = client.get("/admin/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert "storage" in body
    assert "health" in body


def test_storage_delete_and_stats(hist_only):
    h, tmp_path = hist_only
    src = tmp_path / "b.pdf"
    src.write_bytes(b"data")
    rec = h.archive_conversion(
        tool="pdf2word",
        original_name="b.pdf",
        input_path=str(src),
        extra={"pages": 1},
    )
    assert rec is not None
    stats = h.storage_stats()
    assert stats["record_count"] >= 1
    assert "pdf2word" in stats["by_tool"]
    assert h.get_record(rec["id"]) is not None
    assert h.delete_record(rec["id"]) is True
    assert h.get_record(rec["id"]) is None
    assert h.delete_record("no-such") is False
