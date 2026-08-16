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
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        logger.warning("%s=%r is not a number; using default %s", name, raw, default)
        return default


# Drop finished jobs after this many seconds.
_JOB_TTL_SEC = _env_float("JOB_TTL_SEC", 3600.0)
# How long after download before reclaim can drop the job entry (seconds).
_DOWNLOAD_GRACE_SEC = _env_float("JOB_DOWNLOAD_GRACE_SEC", 30.0)
# A queued/running job whose liveness heartbeat (``updated_at``, refreshed by
# every progress update) has not advanced past this many seconds is considered
# stuck (worker crash, OOM, kill) and is reclaimed so its memory/temp files are
# not leaked. Healthy long-running conversions keep updating progress and so
# stay below the threshold regardless of elapsed wall-clock time.
_STALE_JOB_TIMEOUT_SEC = _env_float("JOB_STALE_TIMEOUT_SEC", 3600.0)
# Track background tasks so they are not GC'd mid-flight.
_bg_tasks: set[asyncio.Task] = set()

_REDIS_PREFIX = "toolkit:job:"
_redis_client = None


def jobs_output_dir() -> str:
    """Shared directory for async job work/output files.

    When using the Redis backend this must live on storage visible to every
    worker/instance. Override with ``JOB_OUTPUT_DIR``. The directory is always
    created (also for the configured path) so ``mkdtemp``/downloads on a fresh
    deployment never fail with FileNotFoundError.
    """
    configured = (os.environ.get("JOB_OUTPUT_DIR") or "").strip()
    if configured:
        path = Path(configured)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
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
    if job.status in (JobStatus.done, JobStatus.error):
        if job.downloaded_at and (now - job.downloaded_at) >= _DOWNLOAD_GRACE_SEC:
            return True
        if job.updated_at < now - _JOB_TTL_SEC:
            return True
        return False
    # Stuck tasks: a job that never finished (worker crash / OOM / SIGKILL)
    # must not stay in memory forever. ``updated_at`` is the liveness heartbeat
    # — progress updates refresh it — so only jobs with no progress for the
    # timeout are reclaimed; healthy long-running conversions survive.
    if job.updated_at < now - _STALE_JOB_TIMEOUT_SEC:
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
    """Redis-backed store with graceful degradation.

    When Redis is configured but unavailable at request time (down, wrong
    URL, auth), operations fall back to an in-process memory store so the job
    APIs never 500 (mirroring the rate-limit backend's behavior). The
    fallback is sticky for the process lifetime and logged loudly once; fix
    ``REDIS_URL`` and restart to restore cross-worker visibility.
    """

    def __init__(self) -> None:
        self._ttl = int(_JOB_TTL_SEC + 3600)
        self._grace_ttl = int(_DOWNLOAD_GRACE_SEC + 1)
        self._mem = _MemoryJobStore()
        self._mem_active = False

    def _mem_store(self, exc: BaseException) -> _MemoryJobStore:
        if not self._mem_active:
            self._mem_active = True
            _mark_redis_fallback()
            logger.error(
                "redis job store unavailable; falling back to in-memory jobs "
                "(restart or fix REDIS_URL to restore cross-worker jobs): %s",
                exc,
            )
        return self._mem

    async def create(self, job: Job) -> Job:
        try:
            client = _redis()
            await client.set(
                _key(job.id), json.dumps(_job_to_dict(job)), ex=self._ttl
            )
            return job
        except Exception as exc:
            return await self._mem_store(exc).create(job)

    async def get(self, job_id: str) -> Optional[Job]:
        try:
            client = _redis()
            raw = await client.get(_key(job_id))
        except Exception as exc:
            return await self._mem_store(exc).get(job_id)
        if not raw:
            return None
        try:
            return _job_from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("corrupt job record id=%s: %s", job_id, exc)
            return None

    async def update(self, job_id: str, fields: Dict[str, Any]) -> Optional[Job]:
        try:
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
            await client.set(
                _key(job_id), json.dumps(_job_to_dict(job)), ex=self._ttl
            )
            return job
        except Exception as exc:
            return await self._mem_store(exc).update(job_id, fields)

    async def mark_downloaded(self, job_id: str) -> Optional[Job]:
        try:
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
        except Exception as exc:
            return await self._mem_store(exc).mark_downloaded(job_id)

    async def reclaim(self, now: float) -> int:
        try:
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
        except Exception as exc:
            return await self._mem_store(exc).reclaim(now)

    async def clear(self) -> None:
        try:
            client = _redis()
            removed: List[str] = []
            async for key in client.scan_iter(match=f"{_REDIS_PREFIX}*", count=100):
                removed.append(key)
            if removed:
                await client.delete(*removed)
        except Exception as exc:
            await self._mem_store(exc).clear()


_backend_name: str = "memory"
_store: _MemoryJobStore = _MemoryJobStore()
_backend_warned = False
_redis_fallback_active = False


def _mark_redis_fallback() -> None:
    """Record that the Redis store degraded to in-memory (health reporting)."""
    global _redis_fallback_active
    _redis_fallback_active = True


def jobs_backend_name() -> str:
    """Active backend label for health / docs (memory | redis | redis-fallback)."""
    if _redis_fallback_active:
        return "redis-fallback"
    return _backend_name


def jobs_backend_is_shared() -> bool:
    """True when the job store is shared across workers (Redis, not degraded)."""
    return _backend_name == "redis" and not _redis_fallback_active


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


# Progress throttling: per-page (or per-unit) progress updates are frequent and
# the Redis backend round-trips a GET+SET per call. Track the last applied
# progress per job and skip updates that move less than ``min_delta`` within
# ``min_interval`` seconds. The final done/error update always applies.
_min_delta_p = 0.02  # 2 percentage points
_min_interval_s = 1.0
_progress_lock = threading.Lock()
_last_progress: Dict[str, tuple[float, float]] = {}  # job_id -> (progress, ts)


def _prune_progress_watermark(now: float) -> None:
    """Drop stale throttle entries (keeps the dict bounded over long uptime)."""
    if len(_last_progress) <= 512:
        return
    cutoff = now - 600.0
    for jid in [j for j, (_, ts) in _last_progress.items() if ts < cutoff]:
        _last_progress.pop(jid, None)


async def update_job_progress(
    job_id: str,
    progress: float,
    message: Optional[str] = None,
    *,
    min_delta: float = _min_delta_p,
    min_interval: float = _min_interval_s,
) -> Optional[Job]:
    """Update ``progress``/``message``, skipping redundant intermediate states.

    Consecutive calls that only nudge progress by less than ``min_delta`` and
    arrive within ``min_interval`` seconds are coalesced. Called from worker
    threads (progress callbacks), hence the lock around the watermark map.
    """
    now = time.monotonic()
    with _progress_lock:
        prev = _last_progress.get(job_id)
        if prev is not None:
            last_progress, last_ts = prev
            if (
                progress - last_progress < min_delta
                and now - last_ts < min_interval
            ):
                return None
        _last_progress[job_id] = (progress, now)
        # Prune under the lock: another worker thread may be mutating the dict
        # at the same time (prune outside the lock would raise on concurrent
        # modification during iteration).
        _prune_progress_watermark(now)
    fields: Dict[str, Any] = {"progress": progress}
    if message is not None:
        fields["message"] = message
    return await update_job(job_id, **fields)


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


def _redact_error(error: Optional[str]) -> str:
    """Strip filesystem paths / env hints from a job error for client display.

    Full exception detail (with paths) stays in the server logs via
    ``_set_error``'s ``logger.exception``; only a safe summary is returned to
    the poll/download API.
    """
    if not error:
        return ""
    text = (error or "").strip()
    # Keep only the first line (tracebacks/multi-line internals are internal).
    text = text.splitlines()[0].strip() if text else ""
    # Drop Windows drive paths and POSIX absolute paths.
    text = re.sub(r"[A-Za-z]:\\[^\s\"']*", "…", text)
    text = re.sub(r"(?:/[A-Za-z0-9_./-]+)+/", "…/", text)
    return (text or "转换失败")[:500]


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
        "error": _redact_error(job.error),
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


def _job_ref_paths(job: Job, out: set) -> None:
    """Collect file paths owned by a job into ``out`` (normalized)."""
    for p in (job.work_dir, job.output_path):
        if p:
            out.add(_norm_path(p))


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


# TempWorkspace prefixes used by job plugins to create work dirs under
# ``JOB_OUTPUT_DIR``. Sweeping only these prevents ``sweep_orphan_job_dirs``
# from deleting unrelated data if ``JOB_OUTPUT_DIR`` is misconfigured.
_JOB_WORK_PREFIXES = (
    "word2pdf_async_",
    "word2pdf_batch_async_",
    "pdf2word_async_",
    "pdf2word_batch_async_",
)


def _is_job_work_entry(name: str) -> bool:
    """True when ``name`` looks like a job work dir (``<prefix><random>``)."""
    return any(name.startswith(p) for p in _JOB_WORK_PREFIXES)


async def sweep_orphan_job_dirs() -> int:
    """Remove orphaned entries under ``JOB_OUTPUT_DIR`` at startup.

    Only entries that look like job work dirs (see :func:`_is_job_work_entry`)
    are considered; anything else under the directory is never touched. Entries
    still referenced by a live job (in-memory dict or Redis keys) are kept.
    Entries touched within ``JOB_SWEEP_GRACE_SEC`` (default 600s) are also kept:
    in multi-instance (Redis) deployments a worker on another instance may be
    streaming an upload into a work dir before its job record lands in Redis.
    """
    grace = float(os.environ.get("JOB_SWEEP_GRACE_SEC") or "600")
    referenced: set = set()
    if isinstance(_store, _MemoryJobStore):
        async with _store._lock:
            for job in _store._jobs.values():
                _job_ref_paths(job, referenced)
    elif isinstance(_store, _RedisJobStore):
        try:
            client = _redis()
            async for key in client.scan_iter(match=f"{_REDIS_PREFIX}*", count=100):
                raw = await client.get(key)
                if not raw:
                    continue
                try:
                    job = _job_from_dict(json.loads(raw))
                except (ValueError, TypeError, KeyError):
                    continue
                _job_ref_paths(job, referenced)
        except Exception:
            # Redis unreachable at startup: skip the reference scan and keep all
            # entries (grace period still applies) instead of failing startup.
            logger.warning(
                "sweep_orphan_job_dirs: redis unreachable; skipping reference scan",
                exc_info=True,
            )
    root = jobs_output_dir()
    if not os.path.isdir(root):
        return 0
    now = time.time()
    removed = 0
    for entry in Path(root).iterdir():
        if not _is_job_work_entry(entry.name):
            continue
        if _norm_path(str(entry)) in referenced:
            continue
        try:
            if now - entry.stat().st_mtime < grace:
                continue
        except OSError:
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                os.remove(entry)
            removed += 1
        except OSError:
            logger.warning("orphan sweep failed path=%s", entry, exc_info=True)
    if removed:
        logger.info("orphan job dir sweep removed=%s dir=%s", removed, root)
    return removed


def reset_jobs() -> None:
    """Clear all jobs (tests). Memory store clears synchronously; Redis best-effort."""
    if isinstance(_store, _MemoryJobStore):
        _store.clear_sync()
    else:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(_store.clear())
    for t in list(_bg_tasks):
        t.cancel()
    _bg_tasks.clear()
