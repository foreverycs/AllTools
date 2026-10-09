"""Public API rate limit middleware."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core import api_rate_limit as rl
from core.settings import clear_settings_cache


@pytest.fixture(autouse=True)
def _reset_limiter():
    from plugins.express.router import reset_code_guard

    rl.reset_all()
    reset_code_guard()
    clear_settings_cache()
    yield
    rl.reset_all()
    reset_code_guard()
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

def test_middleware_proxy_headers_before_rate_limit():
    """ProxyHeaders must rewrite request.client BEFORE PublicRateLimit keys the
    per-IP bucket (otherwise all users behind a reverse proxy share one bucket).
    Starlette: the LAST registered middleware is the OUTERMOST.
    """
    from app import app

    names = [m.cls.__name__ for m in app.user_middleware]
    assert names[0] == "RequestIdMiddleware"
    assert names.index("ProxyHeadersMiddleware") < names.index(
        "PublicRateLimitMiddleware"
    )


def test_rate_limit_keys_on_forwarded_client_ip(monkeypatch, tmp_path):
    """Behind a trusted proxy, per-IP limits must key on the real client
    (X-Forwarded-For), not on the proxy peer address."""
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-for-unit-tests-only")
    monkeypatch.setenv("DOTENV_OVERRIDE", "0")
    monkeypatch.setenv("API_RATE_LIMIT", "2")
    monkeypatch.setenv("API_RATE_WINDOW_SEC", "60")
    monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "testclient")

    import importlib

    import app as app_mod
    import core.settings as settings_mod

    settings_mod.clear_settings_cache()
    importlib.reload(app_mod)
    rl.reset_all()

    client = TestClient(app_mod.app)

    def send(ip: str):
        return client.post(
            "/tools/pdf2word/convert-async",
            headers={"X-Forwarded-For": ip},
            files={"file": ("x.txt", b"nope", "text/plain")},
        ).status_code

    # Same forwarded IP: first two count, third is throttled.
    assert [send("1.2.3.4") for _ in range(3)] == [400, 400, 429]
    # A different forwarded IP gets a fresh bucket.
    assert send("5.6.7.8") == 400

    # GET express pickup download is rate-limited too (abuse-sensitive).
    g = [client.get("/tools/express/pickup/000000").status_code for _ in range(3)]
    assert g == [404, 404, 429]
    rl.reset_all()


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


class _FakePipe:
    """Minimal pipeline stand-in matching redis.asyncio usage in _check_redis."""

    def __init__(self, client):
        self._client = client
        self._ops = []

    def incr(self, key):
        self._ops.append(("incr", key))

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))

    async def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "incr":
                self._client.data[op[1]] = self._client.data.get(op[1], 0) + 1
                out.append(self._client.data[op[1]])
            else:
                self._client.ttl_calls.append((op[1], op[2]))
                out.append(True)
        return out


class _FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttl_calls = []

    def pipeline(self):
        return _FakePipe(self)


@pytest.mark.asyncio
async def test_redis_fixed_window_never_stretches_ttl(monkeypatch):
    """Per-bucket key + window-end TTL: an active client cannot stretch the
    window forever (the old bug: single key + EXPIRE refreshed per hit →
    permanent 429), and the counter rolls over at the bucket boundary."""
    fake = _FakeRedis()
    monkeypatch.setattr(rl, "_redis_client", fake)
    monkeypatch.setattr(rl, "_backend", "redis")

    # t=1000..1019 lie in bucket 16 (960..1020) of a 60s window.
    ok, _, rem = await rl.check_rate("api:9.9.9.9", limit=2, window_sec=60.0, now=1000.0)
    assert ok and rem == 1
    ok2, _, rem2 = await rl.check_rate(
        "api:9.9.9.9", limit=2, window_sec=60.0, now=1010.0
    )
    assert ok2 and rem2 == 0
    ok3, retry3, _ = await rl.check_rate(
        "api:9.9.9.9", limit=2, window_sec=60.0, now=1015.0
    )
    assert not ok3 and retry3 == 6  # 1020 (window end) - 1015 + 1

    # TTL counts down to the window end instead of being reset per hit.
    assert [ttl for _, ttl in fake.ttl_calls] == [21, 11, 6]

    # Next bucket gets a fresh counter even though the client kept hammering.
    ok4, _, rem4 = await rl.check_rate(
        "api:9.9.9.9", limit=2, window_sec=60.0, now=1021.0
    )
    assert ok4 and rem4 == 1
    assert len([k for k in fake.data if k.endswith(":9.9.9.9")]) == 2


def test_read_endpoints_rate_limited(monkeypatch, tmp_path):
    """GET /tools/express/read/{code} and POST /tools/express/read used to
    bypass the public limiter entirely, allowing unthrottled code guessing."""
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-for-unit-tests-only")
    monkeypatch.setenv("API_RATE_LIMIT", "2")
    monkeypatch.setenv("API_RATE_WINDOW_SEC", "60")
    clear_settings_cache()
    rl.reset_all()

    from app import app

    client = TestClient(app)
    g = [client.get("/tools/express/read/000000").status_code for _ in range(3)]
    assert g == [404, 404, 429]
    rl.reset_all()
    p = [
        client.post("/tools/express/read", data={"code": "000000"}).status_code
        for _ in range(3)
    ]
    assert p == [404, 404, 429]
    rl.reset_all()
