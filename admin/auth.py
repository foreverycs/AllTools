"""Admin session auth (cookie-based, password from settings/env)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Optional
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse

from admin.common import cookie_path
from core.settings import get_settings

# Cookie name for signed admin session.
COOKIE_NAME = "toolkit_admin"

# PBKDF2 parameters for ADMIN_PASSWORD_HASH (opaque string; see hash_password).
_HASH_ITERATIONS = 310_000
_HASH_ALGO = "sha256"


def admin_password() -> str:
    return get_settings().admin_password


def hash_password(password: str, *, iterations: int = _HASH_ITERATIONS) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt>$<digest>`` for .env use."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        _HASH_ALGO, password.encode("utf-8"), salt, iterations
    )
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(dk).decode("ascii"),
    )


def _verify_password_hash(password: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_b64, digest_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac(
        _HASH_ALGO, password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(dk, expected)


def _secret() -> bytes:
    raw = get_settings().admin_secret
    return hashlib.sha256(f"toolkit-admin:{raw}".encode("utf-8")).digest()


def create_session_token() -> str:
    """Return ``exp.nonce.sig`` token."""
    ttl = get_settings().admin_session_ttl_sec
    exp = int(time.time()) + max(ttl, 300)
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
    """Constant-time compare; never raises on length mismatch.

    Prefers a PBKDF2 ``ADMIN_PASSWORD_HASH`` when configured; otherwise falls
    back to comparing against the plaintext ``ADMIN_PASSWORD``.
    """
    stored_hash = get_settings().admin_password_hash
    if stored_hash:
        return _verify_password_hash(password, stored_hash)
    a = (password or "").encode("utf-8")
    b = (admin_password() or "").encode("utf-8")
    if len(a) != len(b):
        # Still do a dummy compare so timing is closer for wrong-length guesses.
        hmac.compare_digest(a, a)
        return False
    return hmac.compare_digest(a, b)


def is_admin(request: Request) -> bool:
    return verify_session_token(request.cookies.get(COOKIE_NAME))


def require_admin(request: Request) -> Optional[RedirectResponse]:
    """Return a login redirect if not authenticated; otherwise None."""
    if is_admin(request):
        return None
    from tools.common import url_path

    nxt = request.url.path
    if request.url.query:
        nxt = f"{nxt}?{request.url.query}"
    login = url_path("/admin/login", request)
    return RedirectResponse(
        url=f"{login}?next={quote(nxt, safe='/?&=')}",
        status_code=303,
    )


def set_session_cookie(response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=s.admin_session_ttl_sec,
        path=cookie_path(),
        secure=s.admin_cookie_secure,
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path=cookie_path())
