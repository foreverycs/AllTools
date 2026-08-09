"""Async job store and request-id middleware."""

from __future__ import annotations

import os
import time

import pytest

from core import jobs as jobs_mod
from core.jobs import JobStatus, create_job, get_job, job_public_dict, run_job, update_job
from core.request_id import get_request_id, new_request_id, reset_request_id, set_request_id


@pytest.fixture(autouse=True)
def _reset_jobs():
    jobs_mod.reset_jobs()
    yield
    jobs_mod.reset_jobs()


def test_request_id_context():
    token = set_request_id("abc123")
    try:
        assert get_request_id() == "abc123"
    finally:
        reset_request_id(token)
    assert get_request_id() is None
    assert len(new_request_id()) == 16


@pytest.mark.asyncio
async def test_job_lifecycle():
    job = await create_job("pdf2word", download_name="a.docx")
    assert job.status == JobStatus.queued
    got = await get_job(job.id)
    assert got is not None
    assert got.id == job.id

    def work():
        return {"pages": 1}

    await run_job(job.id, work)
    done = await get_job(job.id)
    assert done is not None
    assert done.status == JobStatus.done
    assert done.result == {"pages": 1}
    pub = job_public_dict(done)
    assert pub["id"] == job.id
    assert pub["status"] == "done"
    assert "output_path" not in pub


@pytest.mark.asyncio
async def test_job_error_status():
    job = await create_job("word2pdf")

    def boom():
        raise RuntimeError("fail")

    await run_job(job.id, boom)
    got = await get_job(job.id)
    assert got is not None
    assert got.status == JobStatus.error
    assert "fail" in (got.error or "")


@pytest.mark.asyncio
async def test_update_job_fields():
    job = await create_job("pdf-merge")
    await update_job(job.id, progress=0.5, message="halfway")
    got = await get_job(job.id)
    assert got is not None
    assert got.progress == 0.5
    assert got.message == "halfway"


def test_api_request_id_header_and_jobs_404():
    from fastapi.testclient import TestClient
    from app import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID")

    r2 = client.get("/health", headers={"X-Request-ID": "client-rid-01"})
    assert r2.headers.get("X-Request-ID") == "client-rid-01"

    missing = client.get("/api/jobs/does-not-exist")
    assert missing.status_code == 404
    detail = missing.json().get("detail") or ""
    assert "workers" in detail.lower() or "过期" in detail or "不存在" in detail

    health = client.get("/health?format=json").json()
    assert health.get("jobs", {}).get("single_worker_required") is True


@pytest.mark.asyncio
async def test_mark_downloaded_clears_files(tmp_path):
    work = tmp_path / "jobw"
    work.mkdir()
    out = work / "out.docx"
    out.write_bytes(b"PK mock")
    job = await create_job(
        "pdf2word",
        work_dir=str(work),
        output_path=str(out),
        download_name="out.docx",
    )
    await update_job(job.id, status=JobStatus.done)
    from core.jobs import mark_downloaded

    await mark_downloaded(job.id)
    got = await get_job(job.id)
    assert got is not None
    assert got.output_path is None
    assert got.work_dir is None
    assert got.downloaded_at is not None
    assert not work.exists()
    pub = job_public_dict(got)
    assert pub["has_result"] is False


@pytest.mark.asyncio
async def test_stale_running_job_is_reclaimed():
    job = await create_job("pdf2word")
    await update_job(job.id, status=JobStatus.running)
    # Backdate the liveness heartbeat (update_job always resets it to now).
    jobs_mod._store._jobs[job.id].updated_at = time.time() - 99999
    removed = await jobs_mod.reclaim_expired()
    assert removed >= 1
    assert await get_job(job.id) is None


@pytest.mark.asyncio
async def test_stuck_queued_job_is_reclaimed():
    job = await create_job("pdf2word")
    # Backdate the stored updated_at (update_job always resets it to now).
    jobs_mod._store._jobs[job.id].updated_at = time.time() - 99999
    removed = await jobs_mod.reclaim_expired()
    assert removed >= 1
    assert await get_job(job.id) is None


@pytest.mark.asyncio
async def test_fresh_running_job_not_reclaimed():
    job = await create_job("pdf2word")
    await update_job(job.id, status=JobStatus.running)
    removed = await jobs_mod.reclaim_expired()
    assert removed == 0
    assert await get_job(job.id) is not None


@pytest.mark.asyncio
async def test_active_progress_keeps_running_job_alive():
    job = await create_job("pdf2word")
    await update_job(job.id, status=JobStatus.running)
    # Simulate a long job that keeps reporting progress: advance the heartbeat
    # past the stale timeout, then verify a fresh update keeps it alive.
    jobs_mod._store._jobs[job.id].updated_at = time.time() - 99999
    await update_job(job.id, progress=0.5, message="still working")
    removed = await jobs_mod.reclaim_expired()
    assert removed == 0
    assert await get_job(job.id) is not None


@pytest.mark.asyncio
async def test_sweep_orphan_job_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("JOB_SWEEP_GRACE_SEC", "3600")
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    (orphan / "leak.tmp").write_bytes(b"x")
    (tmp_path / "loose.bin").write_bytes(b"y")

    live = tmp_path / "live"
    live.mkdir()
    job = await create_job(
        "pdf2word",
        work_dir=str(live),
        output_path=str(live / "out.docx"),
    )

    # Backdate the orphans so they fall outside the grace period deterministically.
    old = time.time() - 99999
    os.utime(orphan, (old, old))
    os.utime(orphan / "leak.tmp", (old, old))
    os.utime(tmp_path / "loose.bin", (old, old))

    removed = await jobs_mod.sweep_orphan_job_dirs()
    assert removed == 2
    assert not orphan.exists()
    assert not (tmp_path / "loose.bin").exists()
    assert live.exists()
    assert await get_job(job.id) is not None


@pytest.mark.asyncio
async def test_sweep_respects_grace_period(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("JOB_SWEEP_GRACE_SEC", "3600")
    recent = tmp_path / "recent"
    recent.mkdir()
    (recent / "f").write_bytes(b"x")
    removed = await jobs_mod.sweep_orphan_job_dirs()
    assert removed == 0
    assert recent.exists()
