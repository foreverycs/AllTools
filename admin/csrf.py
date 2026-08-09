"""Double-submit CSRF tokens for admin HTML forms.

While an admin session is active only the session-bound derived token
(``bound_csrf_token``) is accepted, so a CSRF value is only valid for the
session it was issued to. The plain double-submit check is used only for
pre-session pages (login form), where no session token exists yet.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

from fastapi import Request
from starlette.responses import Response

from admin.common import cookie_path
from core.settings import get_settings

COOKIE_NAME = "toolkit_csrf"
FIELD_NAME = "csrf_token"
_TOKEN_BYTES = 32


def _cookie_path() -> str:
    return cookie_path()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _binding_key() -> bytes:
    raw = get_settings().admin_secret
    return hashlib.sha256(f"toolkit-csrf:{raw}".encode("utf-8")).digest()


def bound_csrf_token(session_token: str) -> str:
    """Session-bound CSRF value: HMAC(secret, session token).

    Replacing the plain double-submit cookie with this derived value after
    login ties form submissions to the current session token.
    """
    return hmac.new(
        _binding_key(), session_token.encode("ascii"), hashlib.sha256
    ).hexdigest()


def get_or_create_csrf_token(request: Request) -> str:
    """Reuse a valid existing cookie token so multi-tab forms keep working."""
    existing = (request.cookies.get(COOKIE_NAME) or "").strip()
    if len(existing) >= 16:
        return existing
    return new_csrf_token()


def set_csrf_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=s.admin_session_ttl_sec,
        path=_cookie_path(),
        secure=s.admin_cookie_secure,
    )


def verify_csrf(request: Request, form_token: Optional[str]) -> bool:
    """Return True when the form token matches (constant-time).

    With a live admin session the session-bound derived token is required;
    otherwise (pre-login) the plain double-submit match (cookie == form field)
    is accepted.
    """
    from admin.auth import COOKIE_NAME as SESSION_COOKIE
    from admin.auth import verify_session_token

    cookie = (request.cookies.get(COOKIE_NAME) or "").strip()
    field = (form_token or "").strip()
    if len(cookie) < 16 or len(field) < 16:
        return False
    token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if verify_session_token(token):
        expected = bound_csrf_token(token)
        return hmac.compare_digest(expected, field)
    return hmac.compare_digest(cookie, field)
