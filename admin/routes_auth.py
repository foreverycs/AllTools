"""Admin console: login and logout routes."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from admin._common import _admin_url, _redirect, _safe_next, _tpl
from admin.auth import (
    check_password,
    clear_session_cookie,
    create_session_token,
    is_admin,
    set_session_cookie,
)
from admin.csrf import bound_csrf_token, set_csrf_cookie, verify_csrf
from admin.rate_limit import clear_failures, client_key, is_locked, register_failure

router = APIRouter(tags=["admin"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    if is_admin(request):
        return _redirect(_safe_next(next, request))
    from tools import nav_categories

    return _tpl(
        request,
        "admin/login.html",
        next_url=_safe_next(next, request),
        error=error,
        nav_items=nav_categories(),
        active_nav="",
    )


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    next: Optional[str] = Form(None),
    csrf_token: Optional[str] = Form(None),
):
    if not verify_csrf(request, csrf_token):
        dest = (
            _admin_url("/admin/login", request)
            + "?error="
            + quote("invalid session token; refresh and try again")
            + "&next="
            + quote(_safe_next(next, request))
        )
        return _redirect(dest)

    key = client_key(request)
    locked, retry_after = is_locked(key)
    if locked:
        dest = (
            _admin_url("/admin/login", request)
            + "?error="
            + quote(f"too many attempts; retry in {retry_after}s")
            + "&next="
            + quote(_safe_next(next, request))
        )
        return _redirect(dest)

    if not check_password(password):
        locked, retry_after = register_failure(key)
        err = (
            f"too many attempts; retry in {retry_after}s"
            if locked
            else "password error"
        )
        dest = (
            _admin_url("/admin/login", request)
            + "?error="
            + quote(err)
            + "&next="
            + quote(_safe_next(next, request))
        )
        return _redirect(dest)

    clear_failures(key)
    resp = _redirect(_safe_next(next, request))
    session_token = create_session_token()
    set_session_cookie(resp, session_token)
    # Rotate the CSRF cookie to the session-bound value after login.
    set_csrf_cookie(resp, bound_csrf_token(session_token))
    return resp


@router.post("/logout")
async def logout(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    if not verify_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    resp = _redirect(_admin_url("/admin/login", request))
    clear_session_cookie(resp)
    return resp


@router.get("/logout")
async def logout_get(request: Request):
    """GET logout kept for bookmarks; prefer POST with CSRF."""
    resp = _redirect(_admin_url("/admin/login", request))
    clear_session_cookie(resp)
    return resp
