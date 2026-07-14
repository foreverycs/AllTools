"""Central configuration from environment variables.

Security policy (admin):
- By default, ``ADMIN_PASSWORD`` and ``ADMIN_SECRET`` must be strong and explicit.
- Weak defaults like ``admin123`` are rejected unless ``ALLOW_INSECURE_ADMIN=1``
  (local/dev/tests only).

Optional project-root ``.env`` is loaded automatically (does not override
already-set process environment variables).
"""

from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, Optional

# Known-bad passwords that must never be used without ALLOW_INSECURE_ADMIN.
_WEAK_PASSWORDS: FrozenSet[str] = frozenset(
    {
        "admin",
        "admin123",
        "password",
        "password123",
        "123456",
        "12345678",
        "qwerty",
        "letmein",
        "toolkit",
        "root",
        "pass",
        "test",
        "test-pass",
        "secret",
        "changeme",
    }
)

_MIN_PASSWORD_LEN = 12
_MIN_SECRET_LEN = 24

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_dotenv_loaded = False


def load_dotenv(path: Optional[os.PathLike | str] = None, *, override: bool = False) -> bool:
    """Load KEY=VALUE pairs from a ``.env`` file into ``os.environ``.

    Existing environment variables are kept unless ``override=True``.
    Returns True if a file was found and parsed.
    """
    global _dotenv_loaded
    env_path = Path(path) if path else _PROJECT_ROOT / ".env"
    if not env_path.is_file():
        _dotenv_loaded = True
        return False
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    _dotenv_loaded = True
    return True


def _ensure_dotenv() -> None:
    if not _dotenv_loaded:
        load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 64) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def password_strength_errors(password: str) -> list[str]:
    """Return human-readable reasons why a password is weak (empty if OK)."""
    errors: list[str] = []
    pw = password or ""
    if len(pw) < _MIN_PASSWORD_LEN:
        errors.append(f"at least {_MIN_PASSWORD_LEN} characters")
    if pw.lower() in _WEAK_PASSWORDS:
        errors.append("must not be a common/default password")
    if pw.isdigit():
        errors.append("must not be digits-only")
    # Require some mix: letter + digit or letter + symbol
    has_letter = bool(re.search(r"[A-Za-z\u4e00-\u9fff]", pw))
    has_digit = bool(re.search(r"\d", pw))
    has_symbol = bool(re.search(r"[^A-Za-z0-9\u4e00-\u9fff]", pw))
    if has_letter and not (has_digit or has_symbol):
        errors.append("include a digit or symbol")
    if not has_letter and has_digit:
        errors.append("include a letter")
    return errors


def secret_strength_errors(secret: str) -> list[str]:
    errors: list[str] = []
    s = secret or ""
    if len(s) < _MIN_SECRET_LEN:
        errors.append(f"ADMIN_SECRET must be at least {_MIN_SECRET_LEN} characters")
    if s.lower() in _WEAK_PASSWORDS or s.lower() in {"test-secret", "secret"}:
        errors.append("ADMIN_SECRET must not be a weak/default value")
    return errors


@dataclass(frozen=True)
class Settings:
    """Runtime settings snapshot."""

    admin_password: str
    admin_secret: str
    admin_session_ttl_sec: int
    allow_insecure_admin: bool
    admin_cookie_secure: bool

    max_upload_bytes: int
    max_batch_files: int
    upload_chunk_size: int
    convert_concurrency: int

    upload_retention_days: int
    upload_file_dir: Optional[str]

    # Public URL prefix when reverse-proxied under a subpath (e.g. "/toolkit").
    # Leave empty when the domain root points at this app (typical Baota setup).
    root_path: str

    def admin_security_summary(self) -> dict:
        return {
            "ALLOW_INSECURE_ADMIN": self.allow_insecure_admin,
            "ADMIN_PASSWORD": "set (strong)" if not self.allow_insecure_admin else "set (insecure mode allowed)",
            "ADMIN_SECRET": "set",
            "ADMIN_SESSION_TTL": str(self.admin_session_ttl_sec),
            "ADMIN_COOKIE_SECURE": self.admin_cookie_secure,
            "CONVERT_CONCURRENCY": str(self.convert_concurrency),
            "ROOT_PATH": self.root_path or "(none)",
        }


def _load_settings() -> Settings:
    allow_insecure = _env_bool("ALLOW_INSECURE_ADMIN", False)

    password = (os.environ.get("ADMIN_PASSWORD") or "").strip()
    secret = (os.environ.get("ADMIN_SECRET") or "").strip()

    if not password:
        if allow_insecure:
            password = "admin123"
            warnings.warn(
                "ADMIN_PASSWORD unset; using insecure default because "
                "ALLOW_INSECURE_ADMIN=1. Do not use in production.",
                UserWarning,
                stacklevel=2,
            )
        else:
            raise RuntimeError(
                "ADMIN_PASSWORD is required. Set a strong password "
                f"(at least {_MIN_PASSWORD_LEN} chars, not a common default), "
                "or copy .env.example to .env and set ALLOW_INSECURE_ADMIN=1 "
                "for local development only."
            )

    if not allow_insecure:
        pw_errs = password_strength_errors(password)
        if pw_errs:
            raise RuntimeError(
                "ADMIN_PASSWORD is too weak: "
                + "; ".join(pw_errs)
                + ". Use a long random phrase, or set ALLOW_INSECURE_ADMIN=1 "
                "for local/dev only."
            )

    if not secret:
        if allow_insecure:
            # Deterministic dev fallback — still better than empty.
            import hashlib

            secret = hashlib.sha256(
                f"toolkit-dev-secret:{password}".encode("utf-8")
            ).hexdigest()
            warnings.warn(
                "ADMIN_SECRET unset; derived a dev secret because "
                "ALLOW_INSECURE_ADMIN=1. Set ADMIN_SECRET in production.",
                UserWarning,
                stacklevel=2,
            )
        else:
            raise RuntimeError(
                "ADMIN_SECRET is required and must be independent of the password "
                f"(at least {_MIN_SECRET_LEN} random characters), "
                "or copy .env.example to .env and set ALLOW_INSECURE_ADMIN=1 "
                "for local development only."
            )

    if not allow_insecure:
        sec_errs = secret_strength_errors(secret)
        if sec_errs:
            raise RuntimeError(
                "ADMIN_SECRET is too weak: "
                + "; ".join(sec_errs)
                + " Generate a long random string for production."
            )

    return Settings(
        admin_password=password,
        admin_secret=secret,
        admin_session_ttl_sec=_env_int(
            "ADMIN_SESSION_TTL", 12 * 3600, minimum=300, maximum=7 * 24 * 3600
        ),
        allow_insecure_admin=allow_insecure,
        admin_cookie_secure=_env_bool("ADMIN_COOKIE_SECURE", False),
        max_upload_bytes=_env_int(
            "MAX_UPLOAD_BYTES",
            50 * 1024 * 1024,
            minimum=1024 * 1024,
            maximum=500 * 1024 * 1024,
        ),
        max_batch_files=_env_int("MAX_BATCH_FILES", 20, minimum=1, maximum=100),
        upload_chunk_size=_env_int(
            "UPLOAD_CHUNK_SIZE", 1024 * 1024, minimum=64 * 1024, maximum=8 * 1024 * 1024
        ),
        convert_concurrency=_env_int("CONVERT_CONCURRENCY", 2, minimum=1, maximum=16),
        upload_retention_days=_env_int(
            "UPLOAD_RETENTION_DAYS", 5, minimum=1, maximum=365
        ),
        upload_file_dir=(os.environ.get("UPLOAD_FILE_DIR") or "").strip() or None,
        root_path=_normalize_root_path(os.environ.get("ROOT_PATH") or ""),
    )


def _normalize_root_path(raw: str) -> str:
    """Return '' or a leading-slash path without trailing slash."""
    p = (raw or "").strip()
    if not p or p == "/":
        return ""
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return _load_settings()


def clear_settings_cache() -> None:
    """Drop cached settings (for tests after env changes)."""
    get_settings.cache_clear()


def validate_security_settings() -> Settings:
    """Load and return settings; raises RuntimeError if insecure for production."""
    clear_settings_cache()
    return get_settings()
