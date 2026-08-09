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


@pytest.mark.asyncio
async def test_check_rate_basic():
    ok, retry, rem = await rl.check_rate("k1", limit=2, window_sec=60.0)
    assert ok and rem == 1
    ok2, _, rem2 = await rl.check_rate("k1", limit=2, window_sec=60.0)
    assert ok2 and rem2 == 0
    ok3, retry3, rem3 = await rl.check_rate("k1", limit=2, window_sec=60.0)
    assert not ok3 and retry3 >= 1 and rem3 == 0


@pytest.mark.asyncio
async def test_check_rate_disabled():
    ok, _, rem = await rl.check_rate("k2", limit=0, window_sec=60.0)
    assert ok and rem == -1


def test_active_backend_memory_by_default():
    # With no RATE_LIMIT_BACKEND, the limiter reports the memory backend.
    assert rl.active_backend() == "memory"


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

def test_sliding_window_sweeps_stale_keys():
    from core.rate_limit_base import _MAX_KEY_IDLE_SEC, SlidingWindow

    w = SlidingWindow()
    t0 = 1000.0
    assert w.check("ip-a", limit=10, window_sec=60.0, now=t0)[0] is True
    assert w.check("ip-b", limit=10, window_sec=60.0, now=t0)[0] is True
    assert len(w._hits) == 2

    # Force a sweep at a much later time: idle keys are dropped, fresh kept.
    w._last_sweep = 0.0
    t1 = t0 + _MAX_KEY_IDLE_SEC + 100
    assert w.check("ip-b", limit=10, window_sec=60.0, now=t1)[0] is True
    assert "ip-a" not in w._hits
    assert "ip-b" in w._hits


def test_admin_lockouts_sweep_expired():
    import time

    from admin import rate_limit as al

    al.reset_all()
    key = "9.9.9.9"
    # Register a failure far in the past so its lockout is already expired.
    old = time.monotonic() - 9999
    locked, _ = al.register_failure(
        key, max_failures=1, window_sec=60.0, lockout_sec=0.0, now=old
    )
    assert locked is True
    assert key in al._lockouts

    # A later is_locked call sweeps the already-expired lockout entry.
    locked2, _ = al.is_locked(key, now=time.monotonic())
    assert locked2 is False
    assert key not in al._lockouts
    al.reset_all()
