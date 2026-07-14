"""Global limit for heavy conversion jobs (PDF/Word/LibreOffice, invoice merge)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from .settings import get_settings

_sem: Optional[asyncio.Semaphore] = None
_sem_limit: int = 0


def _get_semaphore() -> asyncio.Semaphore:
    global _sem, _sem_limit
    limit = get_settings().convert_concurrency
    if _sem is None or _sem_limit != limit:
        _sem = asyncio.Semaphore(limit)
        _sem_limit = limit
    return _sem


def reset_semaphore() -> None:
    """Reset after settings reload (tests)."""
    global _sem, _sem_limit
    _sem = None
    _sem_limit = 0


@asynccontextmanager
async def conversion_slot() -> AsyncIterator[None]:
    """Acquire a global conversion slot; wait if the pool is full.

    Use around ``asyncio.to_thread`` (or other heavy work) so concurrent
    LibreOffice / PDF jobs cannot exhaust memory unboundedly.
    """
    sem = _get_semaphore()
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()


async def run_conversion(func, /, *args, **kwargs):
    """Run ``func`` in a worker thread while holding a conversion slot."""
    async with conversion_slot():
        return await asyncio.to_thread(func, *args, **kwargs)
