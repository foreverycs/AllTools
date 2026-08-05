"""Shared admin helpers: cookie path, etc."""

from __future__ import annotations

from core.settings import get_settings


def cookie_path() -> str:
    """Return the cookie path for admin session / CSRF cookies."""
    return get_settings().root_path or "/"
