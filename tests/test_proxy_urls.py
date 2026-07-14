"""Reverse-proxy URL helpers and template static links."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


def test_join_url_and_root_path(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ROOT_PATH", "/toolkit")
    from core.settings import clear_settings_cache
    from tools.common import join_url, url_path

    clear_settings_cache()
    assert join_url("/toolkit", "/static/css/a.css") == "/toolkit/static/css/a.css"
    assert join_url("", "/static/css/a.css") == "/static/css/a.css"
    assert url_path("/admin") == "/toolkit/admin"
    clear_settings_cache()
    monkeypatch.delenv("ROOT_PATH", raising=False)


def test_home_html_static_links_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-for-unit-tests-only")
    monkeypatch.setenv("UPLOAD_FILE_DIR", str(tmp_path / "file"))
    monkeypatch.delenv("ROOT_PATH", raising=False)

    import importlib

    import core.settings as settings_mod
    import tools.common as common
    import app as app_mod

    settings_mod.clear_settings_cache()
    common.refresh_limits()
    importlib.reload(app_mod)

    client = TestClient(app_mod.app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "/static/css/tokens.css" in html
    assert "/static/css/layout.css" in html
    assert "/static/css/home.css" in html
    # Cache-busting query so reverse-proxy/browser stale CSS cannot stick forever
    assert "tokens.css?v=" in html or "tokens.css?v=" in html.replace("&amp;", "&")
    assert "?v=" in html

    for path in (
        "/static/css/tokens.css",
        "/static/css/layout.css",
        "/static/css/home.css",
    ):
        css = client.get(path)
        assert css.status_code == 200, path
        body = css.text
        assert len(body) > 200, f"{path} too small — likely not real CSS"
        ctype = (css.headers.get("content-type") or "").lower()
        assert "css" in ctype or "text/plain" in ctype or body.lstrip().startswith((":root", "/*", ".", "html", "body", "@"))
