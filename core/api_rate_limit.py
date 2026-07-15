"""Sliding-window rate limit for public conversion / job APIs.

Process-local (not shared across workers). For multi-instance deployments put
limits at the reverse proxy as well. Optional Redis multi-key counting can be
added later; this module is intentionally dependency-free.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

_lock = threading.Lock()
# key -> timestamps of recent hits (monotonic)
_hits: Dict[str, Deque[float]] = defaultdict(deque)


def reset_all() -> None:
    """Drop all limiter state (tests)."""
    with _lock:
        _hits.clear()


def check_rate(
    key: str,
    *,
    limit: int,
    window_sec: float,
    now: Optional[float] = None,
) -> Tuple[bool, int, int]:
    """Record one hit and return ``(allowed, retry_after_sec, remaining)``.

    When ``limit <= 0`` the check is disabled (always allowed).
    """
    if limit <= 0:
        return True, 0, -1
    t = time.monotonic() if now is None else now
    with _lock:
        q = _hits[key]
        cutoff = t - window_sec
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            retry = max(1, int(window_sec - (t - q[0])) + 1)
            return False, retry, 0
        q.append(t)
        remaining = max(0, limit - len(q))
        return True, 0, remaining


def client_key_from_request(request) -> str:
    """Best-effort client identity (same idea as admin rate_limit)."""
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"
