"""Shared cached engine/OCR/flag snapshot for the admin console and /health.

The expensive parts (LibreOffice probe, Tesseract probe) are cached again at
their source (``word2pdf.engine_info``, ``converter.ocr_info``). This module
composes them with tool-flag state and caches the composition so the admin
dashboard and /health renders never block on probes.

Single source of truth: previously ``admin._common`` and ``app`` each kept an
independent cache for the same data with different TTLs.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("toolkit.health")

_cache: dict = {}
_cache_ts: float = 0.0
_TTL: float = 300.0
_warming: bool = False
# RLock: schedule_health_warm reads get_cached_health() while holding it.
_lock = threading.RLock()


def _build(force: bool = False) -> dict:
    from converter import ocr_info
    from core.tool_flags import flags_status
    from tools import get_registry, tools_by_category
    from word2pdf import engine_info

    w2p = engine_info(force=force)
    ocr = ocr_info()
    flags = flags_status()
    return {
        "word2pdf": w2p,
        "ocr": ocr,
        "tools": flags["enabled_count"],
        "tools_registered": len(get_registry()),
        "tools_disabled": len(flags["disabled"]),
        "categories": len(tools_by_category()),
    }


def get_cached_health() -> Optional[dict]:
    """Return the cached snapshot, or None when cold/expired (cheap, no probe)."""
    with _lock:
        if not _cache:
            return None
        if time.monotonic() - _cache_ts >= _TTL:
            return None
        return _cache


def get_health_snapshot(*, force: bool = False) -> dict:
    """Compose (and cache) the engine/OCR/flag snapshot.

    The LibreOffice/Tesseract probe runs OUTSIDE the lock so a background warm
    (or a concurrent reader) is never blocked by a multi-second probe, which
    would otherwise freeze async handlers (e.g. /admin/api/stats) on the
    event loop while they wait for the lock.
    """
    global _cache, _cache_ts
    with _lock:
        if not force and _cache and time.monotonic() - _cache_ts < _TTL:
            return _cache
    snap = _build(force=force)
    with _lock:
        _cache = snap
        _cache_ts = time.monotonic()
    return snap


def schedule_health_warm() -> None:
    """Fire-and-forget probe so the first dashboard / health hit is not blocked."""
    global _warming
    with _lock:
        if _warming:
            return
        if get_cached_health() is not None:
            return
        _warming = True

    def _run() -> None:
        global _warming
        try:
            get_health_snapshot(force=True)
        except Exception:
            logger.warning("health warm failed", exc_info=True)
        finally:
            with _lock:
                _warming = False

    threading.Thread(target=_run, name="health-warm", daemon=True).start()


def bust_health_cache() -> None:
    """Invalidate the cached snapshot (after flag/category/plugin edits)."""
    global _cache, _cache_ts
    with _lock:
        _cache = {}
        _cache_ts = 0.0
