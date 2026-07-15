"""In-process async conversion job store (foundation for long-running work).

Jobs are process-local and lost on restart. Suitable for a single uvicorn
worker; multi-worker deployments need a shared backend (see JOBS_BACKEND).

Optional Redis backend is selected via ``JOBS_BACKEND=redis`` + ``REDIS_URL``
when redis is installed; otherwise memory is used and a warning is logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("toolkit.jobs")


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"


@dataclass
class Job:
    id: str
    tool: str
    status: JobStatus = JobStatus.queued
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: float = 0.0
    message: str = ""
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    # Absolute paths owned by the job (cleaned by reclaim / error paths).
    work_dir: Optional[str] = None
    output_path: Optional[str] = None
    download_name: Optional[str] = None
    media_type: Optional[str] = None
    # Extra response headers for the download (e.g. X-Pages).
    response_headers: Optional[Dict[str, str]] = None
    # After a successful download, files are removed; meta may remain briefly.
    downloaded_at: Optional[float] = None


_jobs: Dict[str, Job] = {}
_lock = asyncio.Lock()
# Drop finished jobs after this many seconds.
_JOB_TTL_SEC = float(os.environ.get("JOB_TTL_SEC") or "3600")
# How long after download before reclaim can drop the job entry (seconds).
_DOWNLOAD_GRACE_SEC = float(os.environ.get("JOB_DOWNLOAD_GRACE_SEC") or "30")
# Track background tasks so they are not GC'd mid-flight.
_bg_tasks: set[asyncio.Task] = set()
_backend_name: str = "memory"
_backend_warned = False


def jobs_backend_name() -> str:
    """Active backend label for health / docs (memory | redis | redis-fallback)."""
    return _backend_name


def _configure_backend() -> None:
    """Resolve JOBS_BACKEND; Redis is optional and falls back to memory."""
    global _backend_name, _backend_warned
    raw = (os.environ.get("JOBS_BACKEND") or "memory").strip().lower()
    if raw in ("", "memory", "mem", "local"):
        _backend_name = "memory"
        return
    if raw in ("redis", "remote"):
        url = (os.environ.get("REDIS_URL") or "").strip()
        if not url:
            if not _backend_warned:
                logger.warning(
                    "JOBS_BACKEND=redis but REDIS_URL is empty; using in-memory jobs"
                )
                _backend_warned = True
            _backend_name = "redis-fallback"
            return
        try:
            import redis  # noqa: F401
        except ImportError:
            if not _backend_warned:
                logger.warning(
                    "JOBS_BACKEND=redis but redis package is not installed; "
                    "using in-memory jobs. pip install redis to enable."
                )
                _backend_warned = True
            _backend_name = "redis-fallback"
            return
        # Shared Redis job store is not fully wired yet: keep memory but advertise
        # that the operator requested redis (multi-worker still needs workers=1
        # until a full redis implementation ships).
        if not _backend_warned:
            logger.warning(
                "JOBS_BACKEND=redis is reserved for multi-instance deployments; "
                "this build still uses process-local memory. Run a single uvicorn "
                "worker (--workers 1). Full Redis job store is planned."
            )
            _backend_warned = True
        _backend_name = "redis-fallback"
        return
    logger.warning("Unknown JOBS_BACKEND=%r; using memory", raw)
    _backend_name = "memory"


_configure_backend()


def _now() -> float:
    return time.time()


async def create_job(tool: str, **extra: Any) -> Job:
    jid = secrets.token_hex(12)
    job = Job(id=jid, tool=tool)
    for k, v in extra.items():
        if hasattr(job, k):
            setattr(job, k, v)
    async with _lock:
        _reclaim_unlocked()
        _jobs[jid] = job
    return job


async def get_job(job_id: str) -> Optional[Job]:
    async with _lock:
        return _jobs.get(job_id)


async def update_job(job_id: str, **fields: Any) -> Optional[Job]:
    async with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        for k, v in fields.items():
            if hasattr(job, k):
                setattr(job, k, v)
        job.updated_at = _now()
        return job


def _cleanup_work_dir(work_dir: Optional[str]) -> None:
    if work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)


def _cleanup_job_files(job: Job) -> None:
    """Remove work dir / output files; clear path fields on the job object."""
    if job.work_dir:
        _cleanup_work_dir(job.work_dir)
    elif job.output_path and os.path.isfile(job.output_path):
        try:
            os.remove(job.output_path)
        except OSError:
            pass
    job.work_dir = None
    job.output_path = None


def _reclaim_unlocked() -> int:
    cutoff = _now() - _JOB_TTL_SEC
    dead = []
    for jid, j in _jobs.items():
        if j.status not in (JobStatus.done, JobStatus.error):
            continue
        # Prefer reclaim shortly after download when files already gone.
        if j.downloaded_at and (_now() - j.downloaded_at) >= _DOWNLOAD_GRACE_SEC:
            dead.append(jid)
        elif j.updated_at < cutoff:
            dead.append(jid)
    for jid in dead:
        job = _jobs.pop(jid, None)
        if job:
            _cleanup_job_files(job)
    return len(dead)


async def reclaim_expired() -> int:
    async with _lock:
        return _reclaim_unlocked()


async def mark_downloaded(job_id: str) -> Optional[Job]:
    """After a successful download: delete files, keep metadata briefly.

    Status stays ``done`` so a second download within grace may 410 if
    files are gone — clients should download once.
    """
    async with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        _cleanup_job_files(job)
        job.downloaded_at = _now()
        job.updated_at = _now()
        job.message = "downloaded"
        return job


async def run_job(
    job_id: str,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    """Execute ``func`` in a worker thread; update job status around it.

    ``func`` should return a dict suitable for ``Job.result`` (or None).
    """
    await update_job(job_id, status=JobStatus.running, progress=0.05, message="running")
    try:
        result = await asyncio.to_thread(func, *args, **kwargs)
        await update_job(
            job_id,
            status=JobStatus.done,
            progress=1.0,
            message="done",
            result=result if isinstance(result, dict) else {"value": result},
        )
    except Exception as exc:
        job = await get_job(job_id)
        if job and job.work_dir:
            _cleanup_work_dir(job.work_dir)
            await update_job(job_id, work_dir=None, output_path=None)
        await update_job(
            job_id,
            status=JobStatus.error,
            progress=1.0,
            message="error",
            error=str(exc) or type(exc).__name__,
        )


async def run_job_async(
    job_id: str,
    coro_factory: Callable[[], Awaitable[Optional[Dict[str, Any]]]],
) -> None:
    """Run an async coroutine for a job; apply result fields when done.

    ``coro_factory`` should return a dict of optional job field updates
    (e.g. ``result``, ``response_headers``, ``output_path``) or None.
    On failure the job ``work_dir`` is deleted.
    """
    await update_job(
        job_id, status=JobStatus.running, progress=0.05, message="running"
    )
    try:
        updates = await coro_factory()
        fields: Dict[str, Any] = {
            "status": JobStatus.done,
            "progress": 1.0,
            "message": "done",
        }
        if isinstance(updates, dict):
            fields.update(updates)
            if "result" in updates and not isinstance(updates.get("result"), dict):
                fields["result"] = {"value": updates["result"]}
        await update_job(job_id, **fields)
    except Exception as exc:
        logger.exception("job_failed id=%s", job_id)
        job = await get_job(job_id)
        if job and job.work_dir:
            _cleanup_work_dir(job.work_dir)
            await update_job(job_id, work_dir=None, output_path=None)
        detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
        await update_job(
            job_id,
            status=JobStatus.error,
            progress=1.0,
            message="error",
            error=str(detail),
        )


def schedule_job(
    job_id: str,
    coro_factory: Callable[[], Awaitable[Optional[Dict[str, Any]]]],
) -> None:
    """Fire-and-forget ``run_job_async`` on the running event loop."""

    async def _runner() -> None:
        await run_job_async(job_id, coro_factory)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop (sync tests): run inline is not possible for async factory.
        raise RuntimeError("schedule_job requires a running event loop")

    task = loop.create_task(_runner())
    _bg_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _bg_tasks.discard(t)
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error("background job task crashed id=%s: %s", job_id, exc)

    task.add_done_callback(_done)


def job_public_dict(job: Job) -> Dict[str, Any]:
    """JSON-safe view for clients (no absolute paths)."""
    has_file = (
        job.status == JobStatus.done
        and bool(job.output_path)
        and not job.downloaded_at
    )
    body: Dict[str, Any] = {
        "id": job.id,
        "tool": job.tool,
        "status": job.status.value,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "has_result": has_file,
        "download_name": job.download_name,
        "media_type": job.media_type,
    }
    if job.result:
        # Expose safe stats for UI (pages, tables, warnings, …).
        safe = {
            k: v
            for k, v in job.result.items()
            if k
            in (
                "pages",
                "tables",
                "text_blocks",
                "images",
                "lines",
                "warnings",
                "files",
                "batch",
                "engine",
                "bytes",
            )
        }
        if safe:
            body["result"] = safe
    return body


def reset_jobs() -> None:
    """Clear all jobs (tests)."""
    for job in list(_jobs.values()):
        _cleanup_job_files(job)
    _jobs.clear()
    for t in list(_bg_tasks):
        t.cancel()
    _bg_tasks.clear()
