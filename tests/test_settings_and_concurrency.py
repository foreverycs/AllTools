"""Settings security policy and conversion concurrency."""

from __future__ import annotations

import asyncio
import time

import pytest

import core.concurrency as concurrency_mod
import core.settings as settings_mod


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_ADMIN", raising=False)
    monkeypatch.delenv("CONVERT_CONCURRENCY", raising=False)
    settings_mod.clear_settings_cache()
    concurrency_mod.reset_semaphore()
    yield
    settings_mod.clear_settings_cache()
    concurrency_mod.reset_semaphore()


def test_missing_password_rejected():
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        settings_mod.validate_security_settings()


def test_weak_password_rejected(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    with pytest.raises(RuntimeError, match="too weak|ADMIN_PASSWORD"):
        settings_mod.validate_security_settings()


def test_strong_credentials_accepted(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "Str0ng-Passw0rd!")
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    s = settings_mod.validate_security_settings()
    assert s.admin_password == "Str0ng-Passw0rd!"
    assert s.convert_concurrency >= 1


def test_insecure_mode_allows_defaults(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    s = settings_mod.validate_security_settings()
    assert s.allow_insecure_admin is True
    assert s.admin_password


def test_convert_concurrency_env(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("CONVERT_CONCURRENCY", "3")
    s = settings_mod.validate_security_settings()
    assert s.convert_concurrency == 3


@pytest.mark.asyncio
async def test_conversion_slot_limits_parallelism(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("CONVERT_CONCURRENCY", "1")
    settings_mod.clear_settings_cache()
    concurrency_mod.reset_semaphore()

    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def job():
        nonlocal active, max_active
        async with concurrency_mod.conversion_slot():
            async with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1

    await asyncio.gather(job(), job(), job())
    assert max_active == 1


@pytest.mark.asyncio
async def test_run_conversion_runs_callable(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("CONVERT_CONCURRENCY", "2")
    settings_mod.clear_settings_cache()
    concurrency_mod.reset_semaphore()

    def add(a, b):
        return a + b

    assert await concurrency_mod.run_conversion(add, 2, 3) == 5
