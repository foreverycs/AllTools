"""Simple in-memory rate limiter for admin login (and similar hot paths).

Not a substitute for reverse-proxy rate limits; protects a single process
against password stuffing and accidental loops.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

_lock = threading.Lock()
# key -> timestamps of recent failures (monotonic)
_failures: Dict[str, Deque[float]] = defaultdict(deque)
# key -> locked-until monotonic timestamp
_lockouts: Dict[str, float] = {}

# Defaults: 8 failures / 10 minutes → lock 15 minutes
DEFAULT_MAX_FAILURES = 8
DEFAULT_WINDOW_SEC = 600.0
DEFAULT_LOCKOUT_SEC = 900.0


def client_key(request) -> str:
    """Best-effort client identity for rate limiting."""
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def is_locked(
    key: str,
    *,
    now: Optional[float] = None,
) -> Tuple[bool, int]:
    """Return ``(locked, retry_after_seconds)``."""
    t = time.monotonic() if now is None else now
    with _lock:
        until = _lockouts.get(key)
        if until is None:
            return False, 0
        if until <= t:
            _lockouts.pop(key, None)
            return False, 0
        return True, max(1, int(until - t))


def register_failure(
    key: str,
    *,
    max_failures: Optional[int] = None,
    window_sec: Optional[float] = None,
    lockout_sec: Optional[float] = None,
    now: Optional[float] = None,
) -> Tuple[bool, int]:
    """Record a failed attempt. Returns ``(locked, retry_after_seconds)``."""
    max_f = DEFAULT_MAX_FAILURES if max_failures is None else max_failures
    win = DEFAULT_WINDOW_SEC if window_sec is None else window_sec
    lock_sec = DEFAULT_LOCKOUT_SEC if lockout_sec is None else lockout_sec
    t = time.monotonic() if now is None else now
    with _lock:
        q = _failures[key]
        cutoff = t - win
        while q and q[0] < cutoff:
            q.popleft()
        q.append(t)
        if len(q) >= max_f:
            until = t + lock_sec
            _lockouts[key] = until
            q.clear()
            return True, max(1, int(lock_sec))
        return False, 0


def clear_failures(key: str) -> None:
    """Clear failure history after a successful login."""
    with _lock:
        _failures.pop(key, None)
        _lockouts.pop(key, None)


def reset_all() -> None:
    """Drop all limiter state (tests)."""
    with _lock:
        _failures.clear()
        _lockouts.clear()
