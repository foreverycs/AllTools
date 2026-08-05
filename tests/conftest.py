"""Shared pytest fixtures and default security env for tests."""

from __future__ import annotations

import os

# Allow weak/dev admin credentials so TestClient can start without production secrets.
# Individual tests may override with monkeypatch + cache clear.
#
# Force these values (not setdefault) so a developer project-root ``.env`` with
# DOTENV_OVERRIDE=1 cannot overwrite test credentials when the app lifespan
# calls load_dotenv().
os.environ["ALLOW_INSECURE_ADMIN"] = "1"
os.environ["ADMIN_PASSWORD"] = "test-pass"
os.environ["ADMIN_SECRET"] = "test-secret-for-unit-tests-only"
# Prefer process env over .env during tests (see core.settings.load_dotenv).
os.environ["DOTENV_OVERRIDE"] = "0"
