"""Async conversion job store (memory or Redis backend).

Memory backend is process-local and requires a single uvicorn worker. The
Redis backend (``JOBS_BACKEND=redis`` + ``REDIS_URL``) shares job metadata
across workers/instances so poll/download can be handled by any worker.

Result files live on shared storage (``JOB_OUTPUT_DIR``, default ``file/jobs``)
which must be a shared volume across instances when using the Redis backend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

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


# Drop finished jobs after this many seconds.
_JOB_TTL_SEC = float(os.environ.get("JOB_TTL_SEC") or "3600")
# How long after download before reclaim can drop the job entry (seconds).
_DOWNLOAD_GRACE_SEC = float(os.environ.get("JOB_DOWNLOAD_GRACE_SEC") or "30")
# Track background tasks so they are not GC'd mid-flight.
_bg_tasks: set[asyncio.Task] = set()

_REDIS_PREFIX = "toolkit:job:"
_redis_client = None


def jobs_output_dir() -> str:
    """Shared directory for async job work/output files.

    When using the Redis backend this must live on storage visible to every
    worker/instance. Override with ``JOB_OUTPUT_DIR``.
    """
    configured = (os.environ.get("JOB_OUTPUT_DIR") or "").strip()
    if configured:
        return configured
    from storage.history import file_dir

    path = file_dir() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _key(job_id: str) -> str:
    return f"{_REDIS_PREFIX}{job_id}"


def _redis() -> Any:
    """Lazily build the async Redis client (decode responses as strings)."""
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis

        url = (os.environ.get("REDIS_URL") or "").strip() or "redis://localhost:6379/0"
        _redis_client = aioredis.from_url(url, decode_responses=True)
    return _redis_client


def _job_to_dict(job: Job) -> dict:
    d = asdict(job)
    d["status"] = job.status.value
    return d


def _job_from_dict(d: dict) -> Job:
    data = dict(d)
    data["status"] = JobStatus(data["status"])
    return Job(**data)


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


def _reclaim_should_drop(job: Job, now: float) -> bool:
    if job.status not in (JobStatus.done, JobStatus.error):
        return False
    if job.downloaded_at and (now - job.downloaded_at) >= _DOWNLOAD_GRACE_SEC:
        return True
    if job.updated_at < now - _JOB_TTL_SEC:
        return True
    return False


class _MemoryJobStore:
    """Process-local store (single worker)."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()

    def _reclaim_locked(self, now: float) -> int:
        dead = [jid for jid, j in self._jobs.items() if _reclaim_should_drop(j, now)]
        for jid in dead:
            job = self._jobs.pop(jid, None)
            if job:
                _cleanup_job_files(job)
        return len(dead)

    def clear_sync(self) -> None:
        """Synchronous clear for tests (no running loop needed)."""
        for job in list(self._jobs.values()):
            _cleanup_job_files(job)
        self._jobs.clear()

    async def create(self, job: Job) -> Job:
        async with self._lock:
            self._reclaim_locked(_now())
            self._jobs[job.id] = job
        return job

    async def get(self, job_id: str) -> Optional[Job]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job_id: str, fields: Dict[str, Any]) -> Optional[Job]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for k, v in fields.items():
                if hasattr(job, k):
                    setattr(job, k, v)
            job.updated_at = _now()
            return job

    async def mark_downloaded(self, job_id: str) -> Optional[Job]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            _cleanup_job_files(job)
            job.downloaded_at = _now()
            job.updated_at = _now()
            job.message = "downloaded"
            return job

    async def reclaim(self, now: float) -> int:
        async with self._lock:
            return self._reclaim_locked(now)

    async def clear(self) -> None:
        async with self._lock:
            for job in list(self._jobs.values()):
                _cleanup_job_files(job)
            self._jobs.clear()


class _RedisJobStore:

    def __init__(self) -> None:
        self._ttl = int(_JOB_TTL_SEC + 3600)
        self._grace_ttl = int(_DOWNLOAD_GRACE_SEC + 1)

    async def create(self, job: Job) -> Job:
        client = _redis()
        await client.set(_key(job.id), json.dumps(_job_to_dict(job)), ex=self._ttl)
        return job

    async def get(self, job_id: str) -> Optional[Job]:
        client = _redis()
        raw = await client.get(_key(job_id))
        if not raw:
            return None
        try:
            return _job_from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("corrupt job record id=%s: %s", job_id, exc)
            return None

    async def update(self, job_id: str, fields: Dict[str, Any]) -> Optional[Job]:
        client = _redis()
        raw = await client.get(_key(job_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning("corrupt job record id=%s: %s", job_id, exc)
            return None
        for k, v in fields.items():
            if k in Job.__dataclass_fields__:
                data[k] = v
        data["updated_at"] = _now()
        job = _job_from_dict(data)
        await client.set(_key(job_id), json.dumps(_job_to_dict(job)), ex=self._ttl)
        return job

    async def mark_downloaded(self, job_id: str) -> Optional[Job]:
        client = _redis()
        raw = await client.get(_key(job_id))
        if not raw:
            return None
        try:
            job = _job_from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("corrupt job record id=%s: %s", job_id, exc)
            return None
        _cleanup_job_files(job)
        job.downloaded_at = _now()
        job.updated_at = _now()
        job.message = "downloaded"
        # Short-lived after download so poll/download reuse is brief.
        await client.set(
            _key(job_id), json.dumps(_job_to_dict(job)), ex=self._grace_ttl
        )
        return job

    async def reclaim(self, now: float) -> int:
        client = _redis()
        removed = 0
        async for key in client.scan_iter(match=f"{_REDIS_PREFIX}*", count=100):
            raw = await client.get(key)
            if not raw:
                continue
            try:
                job = _job_from_dict(json.loads(raw))
            except (ValueError, TypeError, KeyError):
                await client.delete(key)
                removed += 1
                continue
            if _reclaim_should_drop(job, now):
                _cleanup_job_files(job)
                await client.delete(key)
                removed += 1
        return removed

    async def clear(self) -> None:
        client = _redis()
        removed: List[str] = []
        async for key in client.scan_iter(match=f"{_REDIS_PREFIX}*", count=100):
            removed.append(key)
        if removed:
            await client.delete(*removed)


_backend_name: str = "memory"
_store: _MemoryJobStore = _MemoryJobStore()
_backend_warned = False


def jobs_backend_name() -> str:
    """Active backend label for health / docs (memory | redis | redis-fallback)."""
    return _backend_name


def jobs_backend_is_shared() -> bool:
    """True when the job store is shared across workers (Redis)."""
    return _backend_name == "redis"


def _configure_backend() -> None:
    """Resolve JOBS_BACKEND; Redis is optional and falls back to memory."""
    global _backend_name, _store, _backend_warned
    raw = (os.environ.get("JOBS_BACKEND") or "memory").strip().lower()
    if raw in ("", "memory", "mem", "local"):
        _backend_name = "memory"
        _store = _MemoryJobStore()
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
            _store = _MemoryJobStore()
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
            _store = _MemoryJobStore()
            return
        _backend_name = "redis"
        _store = _RedisJobStore()
        logger.info(
            "Job store backend=redis url=%s output_dir=%s",
            url,
            jobs_output_dir(),
        )
        return
    logger.warning("Unknown JOBS_BACKEND=%r; using memory", raw)
    _backend_name = "memory"
    _store = _MemoryJobStore()


_configure_backend()


def _now() -> float:
    return time.time()


async def create_job(tool: str, **extra: Any) -> Job:
    jid = secrets.token_hex(12)
    job = Job(id=jid, tool=tool)
    for k, v in extra.items():
        if hasattr(job, k):
            setattr(job, k, v)
    await _store.create(job)
    return job


async def get_job(job_id: str) -> Optional[Job]:
    return await _store.get(job_id)


async def update_job(job_id: str, **fields: Any) -> Optional[Job]:
    return await _store.update(job_id, fields)


async def mark_downloaded(job_id: str) -> Optional[Job]:
    return await _store.mark_downloaded(job_id)


async def reclaim_expired() -> int:
    return await _store.reclaim(_now())


async def _set_running(job_id: str) -> None:
    await update_job(job_id, status=JobStatus.running, progress=0.05, message="running")


async def _set_done_from_func(job_id: str, result: Any) -> None:
    fields: Dict[str, Any] = {
        "status": JobStatus.done,
        "progress": 1.0,
        "message": "done",
    }
    if isinstance(result, dict):
        fields["result"] = result
    elif result is not None:
        fields["result"] = {"value": result}
    await update_job(job_id, **fields)


async def _set_done_from_updates(
    job_id: str, updates: Optional[Dict[str, Any]]
) -> None:
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


async def _set_error(job_id: str, exc: BaseException) -> None:
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


async def run_job(
    job_id: str,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    """Execute ``func`` in a worker thread; update job status around it.

    ``func`` should return a dict suitable for ``Job.result`` (or None).
    """
    await _set_running(job_id)
    try:
        result = await asyncio.to_thread(func, *args, **kwargs)
        await _set_done_from_func(job_id, result)
    except Exception as exc:
        await _set_error(job_id, exc)


async def run_job_async(
    job_id: str,
    coro_factory: Callable[[], Awaitable[Optional[Dict[str, Any]]]],
) -> None:
    """Run an async coroutine for a job; apply result fields when done.

    ``coro_factory`` should return a dict of optional job field updates
    (e.g. ``result``, ``response_headers``, ``output_path``) or None.
    On failure the job ``work_dir`` is deleted.
    """
    await _set_running(job_id)
    try:
        updates = await coro_factory()
        await _set_done_from_updates(job_id, updates)
    except Exception as exc:
        await _set_error(job_id, exc)


def schedule_job(
    job_id: str,
    coro_factory: Callable[[], Awaitable[Optional[Dict[str, Any]]]],
) -> None:
    """Fire-and-forget ``run_job_async`` on the running event loop.

    Note: with the Redis backend the processing still runs on *this* worker;
    a separate task queue (e.g. RQ/Celery) is out of scope. The Redis backend's
    value is shared metadata so any worker can poll/download a running job.
    """

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
    """Clear all jobs (tests). Memory store clears synchronously; Redis best-effort."""
    if isinstance(_store, _MemoryJobStore):
        _store.clear_sync()
    else:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_store.clear())
        except (RuntimeError, Exception):
            pass
    for t in list(_bg_tasks):
        t.cancel()
    _bg_tasks.clear()
