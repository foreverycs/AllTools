"""Shared rate-limit building blocks.

Provides:
- ``client_key_from_request`` — extract best-effort client IP from a request.
- ``SlidingWindow`` — generic thread-safe sliding-window counter.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple


def client_key_from_request(request) -> str:
    """Best-effort client identity for rate limiting.

    Keys on ``request.client.host`` — the direct peer address that uvicorn
    sees. ``ProxyHeadersMiddleware`` rewrites it to the real client IP only
    when the connecting peer is a trusted proxy (see ``TRUSTED_PROXY_HOSTS``),
    so this value is never attacker-controlled via a forged ``X-Forwarded-For``.
    """
    if request.client and getattr(request.client, "host", None):
        return request.client.host
    return "unknown"


class SlidingWindow:
    """Thread-safe sliding-window counter over a per-key deque of timestamps."""

    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()

    def discard(self, key: str) -> None:
        """Remove tracked hits for a key (best-effort)."""
        with self._lock:
            self._hits.pop(key, None)

    def check(
        self,
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
        with self._lock:
            q = self._hits.setdefault(key, deque())
            cutoff = t - window_sec
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, int(window_sec - (t - q[0])) + 1)
                return False, retry, 0
            q.append(t)
            remaining = max(0, limit - len(q))
            return True, 0, remaining

    def record(
        self,
        key: str,
        *,
        limit: int,
        window_sec: float,
        now: Optional[float] = None,
    ) -> Tuple[bool, int]:
        """Append one hit and return ``(exceeded, retry_after_sec)``.

        Unlike ``check``, this always records the hit first, then reports
        whether the limit is now exceeded. Useful for failure-counting where
        the triggering event itself should count toward the limit.
        """
        if limit <= 0:
            return False, 0
        t = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.setdefault(key, deque())
            cutoff = t - window_sec
            while q and q[0] < cutoff:
                q.popleft()
            q.append(t)
            if len(q) >= limit:
                retry = max(1, int(window_sec - (t - q[0])) + 1)
                return True, retry
            return False, 0
