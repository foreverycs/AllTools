"""Tests for admin-controlled tool enable/disable flags."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def flags_client(tmp_path, monkeypatch):
    d = tmp_path / "file"
    d.mkdir()
    monkeypatch.setenv("UPLOAD_FILE_DIR", str(d))
    monkeypatch.setenv("UPLOAD_RETENTION_DAYS", "5")
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-for-unit-tests-only")
    monkeypatch.setenv("DOTENV_OVERRIDE", "0")

    import core.settings as settings_mod
    import core.concurrency as concurrency_mod
    import core.tool_flags as flags_mod
    import storage.history as h
    import admin.auth as auth
    import admin.routes as routes
    import admin.rate_limit as rate_limit
    import tools.common as common
    import app as app_mod

    settings_mod.clear_settings_cache()
    concurrency_mod.reset_semaphore()
    rate_limit.reset_all()
    common.refresh_limits()
    flags_mod.clear_tool_flags_cache()
    importlib.reload(auth)
    importlib.reload(routes)
    importlib.reload(app_mod)

    client = TestClient(app_mod.app)
    yield client, flags_mod, d
    flags_mod.clear_tool_flags_cache()
    # Re-enable everything so other tests are not polluted if cache leaked.
    try:
        flags_mod.save_tool_flags(
            {s: True for s in flags_mod.get_tool_flags()}
        )
    except Exception:
        pass
    flags_mod.clear_tool_flags_cache()
    rate_limit.reset_all()
    settings_mod.clear_settings_cache()


def _csrf_token(client: TestClient) -> str:
    page = client.get("/admin/login")
    assert page.status_code == 200
    token = client.cookies.get("toolkit_csrf")
    assert token
    return token


def _login(client: TestClient, password: str = "test-pass"):
    token = _csrf_token(client)
    return client.post(
        "/admin/login",
        data={
            "password": password,
            "next": "/admin",
            "csrf_token": token,
        },
        follow_redirects=False,
    )


def test_default_all_enabled(flags_client):
    client, flags_mod, _ = flags_client
    assert flags_mod.get_disabled_slugs() == frozenset()
    r = client.get("/api/tools")
    assert r.status_code == 200
    body = r.json()
    slugs = {t["slug"] for t in body["tools"]}
    assert "pdf2word" in slugs
    assert "markdown" in slugs
    assert "image-compress" in slugs


def test_disable_hides_from_catalog_and_blocks_route(flags_client):
    client, flags_mod, file_dir = flags_client
    ok = flags_mod.set_tool_enabled("markdown", False)
    assert ok is True
    assert "markdown" in flags_mod.get_disabled_slugs()
    assert (file_dir / "tool_flags.json").is_file()

    catalog = client.get("/api/tools")
    assert catalog.status_code == 200
    slugs = {t["slug"] for t in catalog.json()["tools"]}
    assert "markdown" not in slugs
    assert "json" in slugs

    # API / page blocked
    page = client.get("/tools/markdown", headers={"Accept": "text/html"})
    assert page.status_code == 403
    assert "功能已关闭" in page.text or "disabled" in page.text.lower()

    api = client.post(
        "/tools/markdown/render",
        data={"text": "# hi"},
    )
    assert api.status_code == 403
    detail = api.json().get("detail", "")
    assert "disabled" in detail.lower() or "markdown" in detail.lower()

    # Other tools still work
    ok_page = client.get("/tools/json")
    assert ok_page.status_code == 200


def test_admin_tools_page_and_save(flags_client):
    client, flags_mod, _ = flags_client
    # Unauthenticated → redirect
    r = client.get("/admin/tools", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/admin/login" in r.headers.get("location", "")

    assert _login(client).status_code in (302, 303, 307)

    page = client.get("/admin/tools")
    assert page.status_code == 200
    assert "功能开关" in page.text
    assert "pdf2word" in page.text
    assert "markdown" in page.text

    token = client.cookies.get("toolkit_csrf")
    assert token

    # Save with only pdf2word + json enabled (omit the rest)
    save = client.post(
        "/admin/tools",
        data={
            "csrf_token": token,
            "enabled": ["pdf2word", "json"],
        },
        follow_redirects=False,
    )
    assert save.status_code in (302, 303, 307)
    assert "/admin/tools" in save.headers.get("location", "")

    flags = flags_mod.get_tool_flags()
    assert flags["pdf2word"] is True
    assert flags["json"] is True
    assert flags["markdown"] is False
    assert flags["word2pdf"] is False

    catalog = client.get("/api/tools").json()
    public = {t["slug"] for t in catalog["tools"]}
    assert public == {"pdf2word", "json"}


def test_toggle_single_tool(flags_client):
    client, flags_mod, _ = flags_client
    _login(client)
    token = client.cookies.get("toolkit_csrf")

    # Disable via toggle
    r = client.post(
        "/admin/tools/rmb/toggle",
        data={"csrf_token": token, "enabled": "0"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 307)
    assert flags_mod.is_tool_enabled("rmb") is False

    # Re-enable by flipping (omit enabled)
    r2 = client.post(
        "/admin/tools/rmb/toggle",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert r2.status_code in (302, 303, 307)
    assert flags_mod.is_tool_enabled("rmb") is True


def test_unknown_slug_toggle_404(flags_client):
    client, _, _ = flags_client
    _login(client)
    token = client.cookies.get("toolkit_csrf")
    r = client.post(
        "/admin/tools/not-a-real-tool/toggle",
        data={"csrf_token": token, "enabled": "0"},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_health_reflects_enabled_count(flags_client):
    client, flags_mod, _ = flags_client
    from tools import TOOL_REGISTRY, enabled_tools, featured_tools

    total = len(TOOL_REGISTRY)
    h0 = client.get("/health").json()
    # tools = module catalog + featured (both enabled)
    assert h0["tools"] == total
    assert h0.get("tools_module") == len(enabled_tools())
    assert h0.get("tools_featured") == len(featured_tools())
    assert h0["tools_module"] + h0["tools_featured"] == h0["tools"]
    assert h0.get("tools_registered") == total

    flags_mod.set_tool_enabled("base64", False)
    flags_mod.set_tool_enabled("rmb", False)
    h1 = client.get("/health").json()
    assert h1["tools"] == total - 2
    assert h1["tools_module"] + h1["tools_featured"] == h1["tools"]
    assert h1["tools_registered"] == total


def test_path_helpers():
    from core.tool_flags import is_tool_path_enabled, tool_slug_from_path

    assert tool_slug_from_path("/tools/pdf2word") == "pdf2word"
    assert tool_slug_from_path("/tools/pdf2word/convert") == "pdf2word"
    assert tool_slug_from_path("/tools/image-compress/compress") == "image-compress"
    assert tool_slug_from_path("/api/tools") is None
    assert tool_slug_from_path("/admin/tools") is None
    # Without flags file, everything known is enabled
    assert is_tool_path_enabled("/tools/json") is True
