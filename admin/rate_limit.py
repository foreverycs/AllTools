"""Simple in-memory rate limiter for admin login (and similar hot paths).

Not a substitute for reverse-proxy rate limits; protects a single process
against password stuffing and accidental loops.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

from core.rate_limit_base import SlidingWindow, client_key_from_request

# Stable public alias for the base implementation.
client_key = client_key_from_request

# Defaults: 8 failures / 10 minutes → lock 15 minutes
DEFAULT_MAX_FAILURES = 8
DEFAULT_WINDOW_SEC = 600.0
DEFAULT_LOCKOUT_SEC = 900.0

_lock = threading.Lock()
_lockouts: Dict[str, float] = {}
_failures = SlidingWindow()

# Sweep expired lockout entries at most this often (per-process).
_SWEEP_INTERVAL_SEC = 60.0
_last_sweep: float = 0.0


def _sweep_lockouts(t: float) -> None:
    """Drop expired lockout entries so the dict cannot grow unbounded.

    Callers hold ``_lock``. Throttled so the common (no-lockout) login attempt
    is a single timestamp compare instead of a full dict scan.
    """
    global _last_sweep
    if t - _last_sweep < _SWEEP_INTERVAL_SEC:
        return
    _last_sweep = t
    stale = [k for k, until in _lockouts.items() if until <= t]
    for k in stale:
        _lockouts.pop(k, None)


def is_locked(
    key: str,
    *,
    now: Optional[float] = None,
) -> Tuple[bool, int]:
    """Return ``(locked, retry_after_seconds)``."""
    t = time.monotonic() if now is None else now
    with _lock:
        _sweep_lockouts(t)
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

    exceeded, retry = _failures.record(key, limit=max_f, window_sec=win, now=t)
    if exceeded:
        until = t + lock_sec
        with _lock:
            _sweep_lockouts(t)
            _lockouts[key] = until
        return True, max(1, int(lock_sec))
    return False, 0


def clear_failures(key: str) -> None:
    """Clear failure history after a successful login."""
    with _lock:
        _lockouts.pop(key, None)
    _failures.discard(key)


def reset_all() -> None:
    """Drop all limiter state (tests)."""
    global _last_sweep
    with _lock:
        _lockouts.clear()
        _last_sweep = 0.0
    _failures.clear()
