"""Public API rate limit middleware."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core import api_rate_limit as rl
from core.settings import clear_settings_cache


@pytest.fixture(autouse=True)
def _reset_limiter():
    rl.reset_all()
    clear_settings_cache()
    yield
    rl.reset_all()
    clear_settings_cache()


def test_check_rate_basic():
    ok, retry, rem = rl.check_rate("k1", limit=2, window_sec=60.0)
    assert ok and rem == 1
    ok2, _, rem2 = rl.check_rate("k1", limit=2, window_sec=60.0)
    assert ok2 and rem2 == 0
    ok3, retry3, rem3 = rl.check_rate("k1", limit=2, window_sec=60.0)
    assert not ok3 and retry3 >= 1 and rem3 == 0


def test_check_rate_disabled():
    ok, _, rem = rl.check_rate("k2", limit=0, window_sec=60.0)
    assert ok and rem == -1


def test_middleware_returns_429(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-for-unit-tests-only")
    monkeypatch.setenv("API_RATE_LIMIT", "2")
    monkeypatch.setenv("API_RATE_WINDOW_SEC", "60")
    clear_settings_cache()
    rl.reset_all()

    from app import app

    client = TestClient(app)
    # Non-pdf body → 400 but still counts toward rate limit.
    for _ in range(2):
        r = client.post(
            "/tools/pdf2word/convert-async",
            files={"file": ("x.txt", b"nope", "text/plain")},
        )
        assert r.status_code in (400, 429)
    r3 = client.post(
        "/tools/pdf2word/convert-async",
        files={"file": ("x.txt", b"nope", "text/plain")},
    )
    assert r3.status_code == 429
    assert r3.headers.get("Retry-After")
