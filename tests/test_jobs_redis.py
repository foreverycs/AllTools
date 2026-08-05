"""Tests for the Redis job store backend (uses an in-memory fake client)."""

from __future__ import annotations

import asyncio

import pytest

from core import jobs as jobs_mod
from core.jobs import (
    Job,
    JobStatus,
    _RedisJobStore,
    _job_from_dict,
    _job_to_dict,
    create_job,
    get_job,
    jobs_backend_is_shared,
    mark_downloaded,
    update_job,
)


class _FakeClient:
    """Minimal in-memory stand-in for the async Redis client."""

    def __init__(self):
        self._data = {}

    async def get(self, key):
        return self._data.get(key)

    async def set(self, key, value, ex=None):
        self._data[key] = value

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self._data:
                del self._data[k]
                n += 1
        return n

    async def scan_iter(self, match="*", count=100):
        import fnmatch

        for k in list(self._data):
            if fnmatch.fnmatch(k, match):
                yield k


def _round_trip(job: Job) -> Job:
    return _job_from_dict(_job_to_dict(job))


def test_job_serialization_round_trip():
    job = Job(
        id="abc",
        tool="pdf2word",
        status=JobStatus.done,
        progress=0.9,
        result={"pages": 2},
        download_name="a.docx",
        response_headers={"X-Pages": "2"},
    )
    restored = _round_trip(job)
    assert restored.id == job.id
    assert restored.status == JobStatus.done
    assert restored.progress == 0.9
    assert restored.result == {"pages": 2}
    assert restored.download_name == "a.docx"
    assert restored.response_headers == {"X-Pages": "2"}


@pytest.mark.asyncio
async def test_redis_store_crud(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(jobs_mod, "_redis", lambda: fake)
    store = _RedisJobStore()

    job = Job(id="jid1", tool="pdf2word")
    await store.create(job)

    got = await store.get("jid1")
    assert got is not None and got.id == "jid1"

    await store.update("jid1", {"progress": 0.5, "message": "halfway"})
    got = await store.get("jid1")
    assert got.progress == 0.5 and got.message == "halfway"

    await store.mark_downloaded("jid1")
    got = await store.get("jid1")
    assert got.downloaded_at is not None
    assert got.message == "downloaded"


@pytest.mark.asyncio
async def test_redis_store_reclaim_drops_expired(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(jobs_mod, "_redis", lambda: fake)
    store = _RedisJobStore()

    old = Job(id="old", tool="pdf2word", status=JobStatus.done, updated_at=0.0)
    fresh = Job(id="fresh", tool="pdf2word", status=JobStatus.running, updated_at=9999999999)
    await store.create(old)
    await store.create(fresh)

    removed = await store.reclaim(now=1_000_000_000)
    assert removed == 1
    assert await store.get("old") is None
    assert await store.get("fresh") is not None


@pytest.mark.asyncio
async def test_redis_store_missing_returns_none(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(jobs_mod, "_redis", lambda: fake)
    store = _RedisJobStore()
    assert await store.get("nope") is None
    assert await store.update("nope", {"progress": 1.0}) is None


def test_jobs_backend_is_shared_flag():
    assert jobs_backend_is_shared() is (jobs_mod.jobs_backend_name() == "redis")
