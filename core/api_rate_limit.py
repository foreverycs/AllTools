"""Sliding-window rate limit for public conversion / job APIs.

Two backends (``RATE_LIMIT_BACKEND``):

- ``memory`` (default): process-local, thread-safe. Correct for a single
  uvicorn worker.
- ``redis``: shared across workers/instances. Required for multi-worker /
  multi-instance deployments where the per-IP limit must be enforced globally.
  Uses a fixed-window ``INCR`` + ``EXPIRE`` counter (atomic in Redis), so the
  sliding-window semantics are approximated by bucketing time into windows.

When Redis is configured but unavailable at request time, the check degrades
to the local limiter so the API stays available (with a logged warning).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional, Tuple

from core.rate_limit_base import SlidingWindow

logger = logging.getLogger("toolkit.api_rate_limit")

_hits = SlidingWindow()

_redis_client = None
_backend: str = "memory"
_backend_warned = False


def _configure_backend() -> None:
    """Resolve RATE_LIMIT_BACKEND; Redis is optional and falls back to memory."""
    global _backend, _redis_client, _backend_warned
    raw = (os.environ.get("RATE_LIMIT_BACKEND") or "memory").strip().lower()
    if raw in ("", "memory", "mem", "local"):
        _backend = "memory"
        return
    if raw in ("redis", "remote"):
        url = (os.environ.get("REDIS_URL") or "").strip()
        if not url:
            if not _backend_warned:
                logger.warning(
                    "RATE_LIMIT_BACKEND=redis but REDIS_URL is empty; "
                    "using in-memory rate limit"
                )
                _backend_warned = True
            _backend = "memory"
            return
        try:
            import redis  # noqa: F401
        except ImportError:
            if not _backend_warned:
                logger.warning(
                    "RATE_LIMIT_BACKEND=redis but redis package is not installed; "
                    "using in-memory rate limit. pip install redis to enable."
                )
                _backend_warned = True
            _backend = "memory"
            return
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(url, decode_responses=True)
        _backend = "redis"
        logger.info("Rate limit backend=redis url=%s", url)
        return
    logger.warning("Unknown RATE_LIMIT_BACKEND=%r; using memory", raw)
    _backend = "memory"


_configure_backend()


def active_backend() -> str:
    """The backend actually in use: ``memory`` or ``redis`` (post-fallback)."""
    return _backend


def reset_all() -> None:
    """Drop all local limiter state (tests)."""
    _hits.clear()


async def _check_redis(
    key: str,
    *,
    limit: int,
    window_sec: float,
    now: Optional[float] = None,
) -> Tuple[bool, int, int]:
    client = _redis_client
    t = time.time() if now is None else now
    bucket = int(t / window_sec)
    rkey = f"toolkit:ratelimit:{int(window_sec)}:{key}"
    try:
        pipe = client.pipeline()
        pipe.incr(rkey)
        pipe.expire(rkey, int(window_sec) + 1)
        count, _ = await pipe.execute()
    except Exception:
        logger.warning(
            "redis rate limit unavailable; local fallback key=%s", key, exc_info=True
        )
        return _hits.check(key, limit=limit, window_sec=window_sec, now=now)
    if count > limit:
        window_end = (bucket + 1) * window_sec
        retry = max(1, int(window_end - t) + 1)
        return False, retry, 0
    remaining = max(0, limit - count)
    return True, 0, remaining


async def check_rate(
    key: str,
    *,
    limit: int,
    window_sec: float,
    now: Optional[float] = None,
) -> Tuple[bool, int, int]:
    """Record one hit and return ``(allowed, retry_after_sec, remaining)``."""
    if limit <= 0:
        return True, 0, -1
    if _backend == "redis":
        return await _check_redis(key, limit=limit, window_sec=window_sec, now=now)
    return _hits.check(key, limit=limit, window_sec=window_sec, now=now)
