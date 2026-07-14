"""Shared pytest fixtures and default security env for tests."""

from __future__ import annotations

import os

# Allow weak/dev admin credentials so TestClient can start without production secrets.
# Individual tests may override with monkeypatch + cache clear.
os.environ.setdefault("ALLOW_INSECURE_ADMIN", "1")
os.environ.setdefault("ADMIN_PASSWORD", "test-pass")
os.environ.setdefault("ADMIN_SECRET", "test-secret-for-unit-tests-only")
