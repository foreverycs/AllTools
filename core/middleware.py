"""Reusable HTTP middleware: request-id, tool flags, rate limit, security headers.

Most middlewares here are implemented as pure ASGI callables (no
``BaseHTTPMiddleware``) to avoid Starlette's per-request task spawning and
response-body buffering overhead. ``AccessLogMiddleware`` is the lone
exception: it needs to wrap the body iterator of streaming responses, which is
awkward in raw ASGI without re-implementing the ``StreamingResponse`` contract.

Registration order in ``app.register_middleware`` is significant: Starlette
makes the LAST registered middleware the outermost, so the request-id context
is active for every downstream middleware and short-circuit response (403/429).
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
import time
from typing import Optional

from fastapi import Request
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

# Header byte tokens (lowercased) looked up in the raw ASGI header list so the
# pure-ASGI middlewares can avoid building a Starlette ``Request`` on the hot
# path. ``Headers`` does this anyway, but reading the raw list skips a dict
# allocation and the ``Request`` wrapper entirely.
_REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("latin-1")
_X_REQUEST_ID_BYTES = b"x-request-id"
_ACCEPT_BYTES = b"accept"
_TEXT_HTML_BYTES = b"text/html"
_APPLICATION_JSON_BYTES = b"application/json"


def _tool_path(path: str) -> str:
    """Strip ROOT_PATH so the tool-flag check sees the app-relative path."""
    root = (get_settings().root_path or "").rstrip("/")
    if root and path.startswith(root + "/"):
        return path[len(root) :]
    return path


def _scope_path(scope) -> str:
    """Raw request path from the ASGI scope (no Starlette ``Request``)."""
    return scope.get("path") or ""


def _scope_query(scope) -> str:
    return scope.get("query_string", b"").decode("latin-1", "replace")


def _scope_method(scope) -> str:
    return scope.get("method") or ""


def _scope_client_host(scope) -> Optional[str]:
    client = scope.get("client")
    if client:
        return client[0]
    return None


def _header_value(headers, name: bytes) -> Optional[bytes]:
    """Linear scan of the raw ASGI header list (case-insensitive key match)."""
    for key, value in headers:
        if key == name:
            return value
    return None


def _request_accepts_html(scope) -> bool:
    """Fast HTML-acceptance check without constructing a ``Request``."""
    accept = _header_value(scope.get("headers", []), _ACCEPT_BYTES)
    if not accept:
        return False
    lowered = accept.lower()
    return _TEXT_HTML_BYTES in lowered and _APPLICATION_JSON_BYTES not in lowered


class RequestIdMiddleware:
    """Attach a request id, propagate it to the event-loop context, and echo it.

    Pure ASGI: sets the ``ContextVar`` around the downstream call and injects
    the id into the response's ``X-Request-ID`` header by wrapping ``send``.
    Runs outermost so every short-circuit (403/429) and log line carries it.
    """

    # Bound length + charset so a client-supplied header cannot bloat logs,
    # inject fake log lines, or break response headers.
    _SAFE_RID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = scope.get("headers", [])
        incoming = _header_value(headers, _REQUEST_ID_HEADER_BYTES)
        if incoming is None:
            incoming = _header_value(headers, _X_REQUEST_ID_BYTES)
        if incoming is not None:
            try:
                incoming_str = incoming.decode("latin-1")
            except UnicodeDecodeError:
                incoming_str = ""
        else:
            incoming_str = ""
        if incoming_str and self._SAFE_RID_RE.match(incoming_str):
            rid = incoming_str
        else:
            rid = new_request_id()

        # Stash on scope so downstream Starlette ``Request.state`` sees it
        # without re-parsing headers.
        scope.setdefault("state", {})
        scope["state"]["request_id"] = rid

        token = set_request_id(rid)
        header_bytes = REQUEST_ID_HEADER.encode("latin-1")
        rid_bytes = rid.encode("latin-1")

        async def send_with_rid(message):
            if message["type"] == "http.response.start":
                msg_headers = list(message.get("headers", []))
                # setdefault semantics: do not overwrite an id the downstream
                # handler chose to attach itself.
                if not any(k == header_bytes for k, _ in msg_headers):
                    msg_headers.append((header_bytes, rid_bytes))
                message = {**message, "headers": msg_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_rid)
        finally:
            reset_request_id(token)


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

    This is the only ``BaseHTTPMiddleware`` left in the stack: wrapping the
    ``body_iterator`` of a ``StreamingResponse`` to defer logging until the body
    drains is awkward to do correctly in raw ASGI, and the access log needs the
    final status code which is only known after ``call_next`` returns. The
    per-request overhead is acceptable here because the middleware does real
    work (timing + log formatting) on every response.
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


class ToolFlagGateMiddleware:
    """Block disabled tools with 403 (HTML for browsers, JSON for API calls).

    Pure ASGI: short-circuits before the router runs, so a disabled tool never
    reaches FastAPI's route matching. Reads the path straight from the scope and
    the disabled-slug set from the cached ``tool_flags`` module — no Starlette
    ``Request`` object is built for the common allow path.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = _scope_path(scope)
        # Cheap reject: only ``/tools/...`` paths are subject to the gate.
        if not (path.startswith("/tools/") or "/tools/" in path):
            return await self.app(scope, receive, send)

        from core.tool_flags import is_tool_path_enabled, tool_slug_from_path

        check_path = _tool_path(path)
        if is_tool_path_enabled(check_path):
            return await self.app(scope, receive, send)

        rid = get_request_id() or ""
        slug = tool_slug_from_path(check_path) or "tool"
        root = (get_settings().root_path or "").rstrip("/")
        wants_html = _request_accepts_html(scope)
        if _scope_method(scope) == "GET" and wants_html:
            safe_slug = _html.escape(slug)
            content = (
                "<!DOCTYPE html><html lang='zh-CN'><head>"
                "<meta charset='utf-8'/><title>功能已关闭</title></head>"
                "<body style='font-family:system-ui;padding:48px;text-align:center'>"
                f"<h1>功能已关闭</h1><p>「{safe_slug}」已被管理员停用。</p>"
                f"<p><a href='{root or ''}/'>返回首页</a></p>"
                "</body></html>"
            ).encode("utf-8")
            await _send_response(
                send,
                status=403,
                body=content,
                content_type="text/html; charset=utf-8",
                extra_headers=[(REQUEST_ID_HEADER.encode("latin-1"), rid.encode("latin-1"))],
            )
            return
        import json as _json

        body = _json.dumps(
            {
                "detail": f"Tool '{slug}' is disabled by administrator",
                "slug": slug,
                "request_id": rid,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        await _send_response(
            send,
            status=403,
            body=body,
            content_type="application/json",
            extra_headers=[(REQUEST_ID_HEADER.encode("latin-1"), rid.encode("latin-1"))],
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
        "/lookup",  # express metadata query (pickup codes are enumerable)
        "/regex/test",  # user-supplied regex matching is CPU-sensitive (ReDoS)
        "/regex/replace",
    )
    return any(m in path for m in markers)


class PublicRateLimitMiddleware:
    """Sliding-window per-IP limit on convert POSTs and job downloads.

    Pure ASGI: decides whether to short-circuit (429) before dispatching, then
    adds ``X-RateLimit-*`` headers to the downstream response by wrapping
    ``send``. Only paths flagged by ``_is_public_convert_path`` pay any cost.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = _scope_path(scope)
        method = _scope_method(scope)
        is_download = path.rstrip("/").endswith("/download")
        is_pickup = "/pickup" in path
        rate_limited = (
            method == "POST" and _is_public_convert_path(path)
        ) or (
            method == "GET"
            and (is_download or is_pickup)
            and _is_public_convert_path(path)
        )
        if not rate_limited:
            return await self.app(scope, receive, send)

        from core.api_rate_limit import check_rate

        s = get_settings()
        if s.api_rate_limit <= 0:
            return await self.app(scope, receive, send)

        rid = get_request_id() or ""
        # ``client_key_from_request`` only reads ``request.client.host``; build
        # a lightweight Starlette ``Request`` just for that helper rather than
        # reimplementing client-IP extraction (keeps proxy-header rewriting in
        # one place).
        host = _scope_client_host(scope) or "unknown"
        key = f"api:{host}"
        allowed, retry_after, remaining = await check_rate(
            key, limit=s.api_rate_limit, window_sec=float(s.api_rate_window_sec)
        )
        if not allowed:
            import json as _json

            body = _json.dumps(
                {
                    "detail": f"Too many requests. Retry after {retry_after}s",
                    "request_id": rid,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            await _send_response(
                send,
                status=429,
                body=body,
                content_type="application/json",
                extra_headers=[
                    (REQUEST_ID_HEADER.encode("latin-1"), rid.encode("latin-1")),
                    (b"retry-after", str(retry_after).encode("latin-1")),
                    (b"x-ratelimit-limit", str(s.api_rate_limit).encode("latin-1")),
                    (b"x-ratelimit-remaining", b"0"),
                ],
            )
            return

        limit_bytes = str(s.api_rate_limit).encode("latin-1")
        remaining_bytes = str(remaining).encode("latin-1")

        async def send_with_ratelimit(message):
            if message["type"] == "http.response.start":
                msg_headers = list(message.get("headers", []))
                if not any(k == b"x-ratelimit-limit" for k, _ in msg_headers):
                    msg_headers.append((b"x-ratelimit-limit", limit_bytes))
                if not any(k == b"x-ratelimit-remaining" for k, _ in msg_headers):
                    msg_headers.append((b"x-ratelimit-remaining", remaining_bytes))
                message = {**message, "headers": msg_headers}
            await send(message)

        await self.app(scope, receive, send_with_ratelimit)


class SecurityHeadersMiddleware:
    """Baseline hardening headers + static / catalog cache policy.

    Pure ASGI: wraps ``send`` so the security headers are appended to
    ``http.response.start`` without buffering the response body (unlike
    ``BaseHTTPMiddleware`` which materializes the whole response before the
    headers can be touched). Static-asset cache policy is decided from the
    request path + query on the way in.
    """

    _BASE_HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"x-frame-options", b"SAMEORIGIN"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    ]

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = _scope_path(scope)
        query = _scope_query(scope)
        is_static = "/static/" in path
        is_home = path == "/"
        has_version = bool(query) and "v=" in query

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                msg_headers = list(message.get("headers", []))
                existing = {k for k, _ in msg_headers}
                for name, value in self._BASE_HEADERS:
                    if name not in existing:
                        msg_headers.append((name, value))
                        existing.add(name)
                if is_static:
                    cc = (
                        b"public, max-age=31536000, immutable"
                        if has_version
                        else b"public, max-age=300, must-revalidate"
                    )
                    # Overwrite any upstream Cache-Control for static assets.
                    msg_headers = [
                        (k, v) for k, v in msg_headers if k != b"cache-control"
                    ]
                    msg_headers.append((b"cache-control", cc))
                elif is_home and message.get("status") == 200:
                    if b"cache-control" not in existing:
                        msg_headers.append(
                            (
                                b"cache-control",
                                b"private, max-age=30, stale-while-revalidate=120",
                            )
                        )
                message = {**message, "headers": msg_headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLarge(Exception):
    """Internal signal: request body exceeded the global cap."""


class MaxRequestBodySizeMiddleware:
    """Reject request bodies larger than ``max_bytes`` (defense in depth).

    Endpoint-level checks rely on ``Content-Length`` (``check_upload_size_header``),
    which chunked-encoded bodies can bypass; this ASGI middleware counts the
    streamed body regardless of framing, so a huge request can never be fully
    buffered into memory. ``max_bytes`` is read from ``MAX_REQUEST_BODY_BYTES``
    (default 512 MiB — generous enough for batch uploads).
    """

    def __init__(self, app, max_bytes: Optional[int] = None):
        self.app = app
        if max_bytes is None:
            max_bytes = int(os.environ.get("MAX_REQUEST_BODY_BYTES") or str(512 * 1024 * 1024))
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Fast path: a declared Content-Length over the cap is rejected up front.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await _send_response(
                            send,
                            status=413,
                            body=b'{"detail":"Request body too large"}',
                            content_type="application/json",
                        )
                        return
                except ValueError:
                    pass
                break

        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _send_response(
                send,
                status=413,
                body=b'{"detail":"Request body too large"}',
                content_type="application/json",
            )


async def _send_response(
    send,
    *,
    status: int,
    body: bytes,
    content_type: str,
    extra_headers: Optional[list] = None,
) -> None:
    """Emit a short-circuit HTTP response directly on the ASGI ``send``.

    Shared by the pure-ASGI middlewares (rate limit / tool flag / body cap) so
    short-circuit paths do not pay for a Starlette ``Response`` object. Headers
    are emitted as the latin-1 byte pairs ASGI expects.
    """
    headers = [
        (b"content-type", content_type.encode("latin-1")),
        (b"content-length", str(len(body)).encode("latin-1")),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})
