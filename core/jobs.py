"""In-process async conversion job store (foundation for long-running work).

Jobs are process-local and lost on restart. Suitable for a single uvicorn
worker; multi-worker deployments need a shared backend later.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


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
    # Absolute paths owned by the job (cleaned by reclaim).
    work_dir: Optional[str] = None
    output_path: Optional[str] = None
    download_name: Optional[str] = None
    media_type: Optional[str] = None


_jobs: Dict[str, Job] = {}
_lock = asyncio.Lock()
# Drop finished jobs after this many seconds.
_JOB_TTL_SEC = 3600.0


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


def _reclaim_unlocked() -> int:
    cutoff = _now() - _JOB_TTL_SEC
    dead = [
        jid
        for jid, j in _jobs.items()
        if j.status in (JobStatus.done, JobStatus.error) and j.updated_at < cutoff
    ]
    for jid in dead:
        job = _jobs.pop(jid, None)
        if job and job.work_dir:
            import shutil

            shutil.rmtree(job.work_dir, ignore_errors=True)
    return len(dead)


async def reclaim_expired() -> int:
    async with _lock:
        return _reclaim_unlocked()


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
        await update_job(
            job_id,
            status=JobStatus.error,
            progress=1.0,
            message="error",
            error=str(exc) or type(exc).__name__,
        )


def job_public_dict(job: Job) -> Dict[str, Any]:
    """JSON-safe view for clients (no absolute paths)."""
    return {
        "id": job.id,
        "tool": job.tool,
        "status": job.status.value,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "has_result": job.status == JobStatus.done and bool(job.output_path),
        "download_name": job.download_name,
    }


def reset_jobs() -> None:
    """Clear all jobs (tests)."""
    _jobs.clear()
