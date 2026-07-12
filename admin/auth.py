"""Admin session auth (cookie-based, password from env)."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse

# Cookie name for signed admin session.
COOKIE_NAME = "toolkit_admin"
# Default password for local/dev — override with ADMIN_PASSWORD in production.
DEFAULT_PASSWORD = "admin123"
SESSION_TTL_SEC = int(os.environ.get("ADMIN_SESSION_TTL", str(12 * 3600)))


def admin_password() -> str:
    return (os.environ.get("ADMIN_PASSWORD") or DEFAULT_PASSWORD).strip() or DEFAULT_PASSWORD


def _secret() -> bytes:
    raw = os.environ.get("ADMIN_SECRET") or admin_password()
    return hashlib.sha256(f"toolkit-admin:{raw}".encode("utf-8")).digest()


def create_session_token() -> str:
    """Return ``exp.ts.nonce.sig`` token."""
    exp = int(time.time()) + max(SESSION_TTL_SEC, 300)
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}"
    sig = hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: Optional[str]) -> bool:
    if not token or token.count(".") != 2:
        return False
    exp_s, nonce, sig = token.split(".", 2)
    if not exp_s.isdigit() or not nonce or not sig:
        return False
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    payload = f"{exp_s}.{nonce}"
    expect = hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, sig)


def check_password(password: str) -> bool:
    return hmac.compare_digest(password or "", admin_password())


def is_admin(request: Request) -> bool:
    return verify_session_token(request.cookies.get(COOKIE_NAME))


def require_admin(request: Request) -> Optional[RedirectResponse]:
    """Return a login redirect if not authenticated; otherwise None."""
    if is_admin(request):
        return None
    nxt = request.url.path
    if request.url.query:
        nxt = f"{nxt}?{request.url.query}"
    return RedirectResponse(
        url=f"/admin/login?next={nxt}",
        status_code=303,
    )


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SEC,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
