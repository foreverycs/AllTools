"""Reusable HTTP middleware: request-id, tool flags, rate limit, security headers.

Each concern is its own ``BaseHTTPMiddleware`` so it can be registered, ordered,
and reasoned about independently. The request-id context must run outermost so
downstream middleware can attach the id to early responses (403 / 429).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from core.request_id import (
    REQUEST_ID_HEADER,
    get_request_id,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from core.settings import get_settings

access_log = logging.getLogger("toolkit.access")


def _tool_path(path: str) -> str:
    """Strip ROOT_PATH so the tool-flag check sees the app-relative path."""
    root = (get_settings().root_path or "").rstrip("/")
    if root and path.startswith(root + "/"):
        return path[len(root) :]
    return path


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request id, propagate it to the event-loop context, and echo it."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER) or request.headers.get(
            "X-Request-Id"
        )
        rid = (incoming or "").strip() or new_request_id()
        token = set_request_id(rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers.setdefault(REQUEST_ID_HEADER, rid)
        return response


# Paths that would only spam the access log (static assets, probes, PWA files).
# The match is applied against the ROOT_PATH-stripped path so reverse-proxy
# mounts (e.g. "/toolkit/health") are still filtered.
_NOISY_PATH_PREFIXES = ("/static/", "/health", "/sw.js", "/manifest.webmanifest", "/favicon")


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Structured request log: method, path, status, latency, request id.

    Registered just inside ``RequestIdMiddleware`` so the id context is active
    and the final response status is known. Static assets and health probes are
    logged at DEBUG to keep the default log readable. For streaming responses
    (e.g. job downloads) the line is emitted after the body finishes, so ``ms``
    reflects the full transfer time rather than time-to-first-byte.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            ms = (time.perf_counter() - start) * 1000
            access_log.exception(
                "access method=%s path=%s status=500 ms=%.1f request_id=%s",
                request.method,
                request.url.path,
                ms,
                get_request_id() or "-",
            )
            raise
        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is not None:
            # Streaming response: defer the log line until the whole body has
            # been transferred so slow downloads are visible in the log.
            async def _wrapped_iterator():
                try:
                    async for chunk in body_iterator:
                        yield chunk
                finally:
                    self._emit_log(request, response.status_code, start)

            response.body_iterator = _wrapped_iterator()
            return response
        self._emit_log(request, response.status_code, start)
        return response

    @staticmethod
    def _emit_log(request: Request, status: int, start: float) -> None:
        ms = (time.perf_counter() - start) * 1000
        app_path = _tool_path(request.url.path)
        if app_path.startswith(_NOISY_PATH_PREFIXES):
            level = logging.DEBUG
        else:
            level = logging.ERROR if status >= 500 else logging.INFO
        access_log.log(
            level,
            "access method=%s path=%s status=%s ms=%.1f request_id=%s",
            request.method,
            app_path,
            status,
            ms,
            get_request_id() or "-",
        )


class ToolFlagGateMiddleware(BaseHTTPMiddleware):
    """Block disabled tools with 403 (HTML for browsers, JSON for API calls)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if not (path.startswith("/tools/") or "/tools/" in path):
            return await call_next(request)

        from core.tool_flags import is_tool_path_enabled, tool_slug_from_path

        check_path = _tool_path(path)
        if is_tool_path_enabled(check_path):
            return await call_next(request)

        rid = get_request_id() or ""
        slug = tool_slug_from_path(check_path) or "tool"
        root = (get_settings().root_path or "").rstrip("/")
        accept = (request.headers.get("accept") or "").lower()
        wants_html = "text/html" in accept and "application/json" not in accept
        if request.method == "GET" and wants_html:
            return HTMLResponse(
                content=(
                    "<!DOCTYPE html><html lang='zh-CN'><head>"
                    "<meta charset='utf-8'/><title>功能已关闭</title></head>"
                    "<body style='font-family:system-ui;padding:48px;text-align:center'>"
                    f"<h1>功能已关闭</h1><p>「{slug}」已被管理员停用。</p>"
                    f"<p><a href='{root or ''}/'>返回首页</a></p>"
                    "</body></html>"
                ),
                status_code=403,
                headers={REQUEST_ID_HEADER: rid},
            )
        return JSONResponse(
            status_code=403,
            content={
                "detail": f"Tool '{slug}' is disabled by administrator",
                "slug": slug,
                "request_id": rid,
            },
            headers={REQUEST_ID_HEADER: rid},
        )


def _is_public_convert_path(path: str) -> bool:
    """Paths that accept heavy uploads / create jobs (rate-limited)."""
    if not path:
        return False
    if path.startswith("/api/jobs") and path.rstrip("/").endswith("/download"):
        return True  # download is lighter but still abuse-sensitive
    markers = (
        "/convert-async",
        "/convert-batch-async",
        "/convert-batch",
        "/convert",
        "/compress",  # image-compress (and future media tools)
        "/send",  # file express upload
        "/pickup",  # file express download
    )
    return any(m in path for m in markers)


class PublicRateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-IP limit on convert POSTs and job downloads."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        is_download = path.rstrip("/").endswith("/download")
        rate_limited = (
            request.method == "POST" and _is_public_convert_path(path)
        ) or (request.method == "GET" and is_download and _is_public_convert_path(path))
        if not rate_limited:
            return await call_next(request)

        from core.api_rate_limit import check_rate
        from core.rate_limit_base import client_key_from_request

        s = get_settings()
        if s.api_rate_limit <= 0:
            return await call_next(request)

        rid = get_request_id() or ""
        key = f"api:{client_key_from_request(request)}"
        allowed, retry_after, remaining = await check_rate(
            key, limit=s.api_rate_limit, window_sec=float(s.api_rate_window_sec)
        )
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Too many requests. Retry after {retry_after}s",
                    "request_id": rid,
                },
                headers={
                    REQUEST_ID_HEADER: rid,
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(s.api_rate_limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
        request.state.rate_limit_remaining = remaining
        response = await call_next(request)
        response.headers.setdefault("X-RateLimit-Limit", str(s.api_rate_limit))
        response.headers.setdefault("X-RateLimit-Remaining", str(remaining))
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers + static / catalog cache policy."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        path = request.url.path
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        # Templates use static_url(...?v=mtime); long cache for versioned assets.
        if "/static/" in path:
            if request.url.query and "v=" in request.url.query:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
        elif response.status_code == 200 and path == "/":
            response.headers.setdefault(
                "Cache-Control", "private, max-age=30, stale-while-revalidate=120"
            )
        return response
