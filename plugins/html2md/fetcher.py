"""URL fetch + main-content extraction for the html2md plugin.

Fetching is guarded against SSRF: only ``http(s)``, private / loopback /
link-local / multicast / reserved addresses are rejected (both IP literals
and DNS-resolved addresses), redirects are capped and re-checked per hop, the
response body is size-bounded, and non-HTML content types are refused.

Main-content extraction is a lightweight heuristic built on regex scanning
(no DOM library): it finds the largest ``div``/``article``/``section``/``main``
container whose id/class matches common content hints, scores candidates by
plain-text length and heading/paragraph density (penalized when the container
looks like navigation / comments / footer), and falls back to the whole page.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Optional
from urllib.parse import urljoin, urlunsplit, urlsplit

import httpx

MAX_REDIRECTS = 5
MAX_BYTES = 5 * 1024 * 1024  # 5 MB body cap
FETCH_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "Alltools-Html2Md/1.0"
)

# Main-content container hints (id/class substrings, lowercase).
_MAIN_HINTS = re.compile(
    r"(cnblogs_post_body|post_body|article[-_]?content|entry[-_]?content|"
    r"post[-_]?content|blog[-_]?post|blogpost|main[-_]?content|content[-_]?main|"
    r"page[-_]?content|rich_media_content|js_content|post[-_]?entry|"
    r"content[-_]?body|article|single[-_]?content|topic[-_]?content)"
)
# Penalize containers that look like chrome (navigation / comments / ads).
_BAD_HINTS = re.compile(
    r"(comment|footer|sidebar|related|recommend|advert|ads?[_-]?|nav|menu|"
    r"widget|share|pagination|breadcrumb|meta|info[-_]?box|toolbar|header)",
    re.I,
)
# Penalize when those bad hints appear on tag attributes inside a candidate.
_BAD_ATTR_RE = re.compile(
    r"<[a-z]+\b[^>]*(?:id|class)=[\"'][^\"']*(?:comment|footer|sidebar|"
    r"related|recommend|advert|ads?[_-]?|\bnav\b|widget|pagination|breadcrumb)"
    r"[^\"']*[\"']",
    re.I,
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*?)?)(/?)>", re.S)
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class FetchError(ValueError):
    """Raised when a URL is rejected or cannot be fetched."""


def _client_factory() -> httpx.AsyncClient:
    """Create the HTTP client used for fetching (monkeypatchable in tests)."""
    return httpx.AsyncClient(
        timeout=FETCH_TIMEOUT, follow_redirects=False
    )


def _reject_ip(ip: ipaddress._BaseAddress) -> None:
    if not ip.is_global or ip.is_multicast or ip.is_unspecified:
        raise FetchError("blocked address (private / loopback / link-local)")


def _check_host(host: str) -> None:
    """Reject loopback / private / link-local / multicast targets (SSRF)."""
    lowered = (host or "").strip().lower().rstrip(".")
    if not lowered:
        raise FetchError("missing host")
    if lowered == "localhost":
        raise FetchError("blocked address (localhost)")
    try:
        ip = ipaddress.ip_address(lowered.split("%")[0])
    except ValueError:
        ip = None
    if ip is not None:
        _reject_ip(ip)
        return
    # Resolve the domain and require every address to be public.
    _resolve_public_ips(lowered)


def _resolve_public_ips(host: str) -> list[str]:
    """Resolve ``host`` and require every address to be public.

    Returns the resolved addresses. The caller should pin the connection to one
    of these IPs (see :func:`_pin_address`) so a second DNS lookup performed by
    the HTTP client cannot race to an internal address (DNS-rebinding / TOCTOU).
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise FetchError(f"unresolvable host: {host}") from exc
    addrs = {info[4][0].split("%")[0] for info in infos}
    if not addrs:
        raise FetchError(f"unresolvable host: {host}")
    for addr in addrs:
        try:
            _reject_ip(ipaddress.ip_address(addr))
        except ValueError as exc:
            raise FetchError(f"blocked address: {addr}") from exc
    return sorted(addrs)


def _pin_address(current: str, ip: str) -> tuple[str, str, Optional[str]]:
    """Return ``(request_url, host_header, sni_host)`` pinned to a public IP.

    ``current`` must already pass :func:`check_url`; ``ip`` is a validated
    public address from :func:`_resolve_public_ips`. The returned request URL
    uses the IP directly so the HTTP client never re-resolves the hostname (no
    DNS-rebinding window). The original hostname is preserved via the ``Host``
    header (and SNI for https) so routing and TLS verification behave as if the
    hostname were used.
    """
    parts = urlsplit(current)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    netloc = f"[{ip}]" if ":" in ip else ip
    if port:
        netloc = f"{netloc}:{port}"
    request_url = urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )
    host_header = host
    if port and not (
        (parts.scheme == "https" and port == 443)
        or (parts.scheme == "http" and port == 80)
    ):
        host_header = f"{host}:{port}"
    sni = host if parts.scheme == "https" else None
    return request_url, host_header, sni


def check_url(url: str) -> str:
    """Validate ``url`` (scheme + host) and return it normalized."""
    raw = (url or "").strip()
    if not raw:
        raise FetchError("missing url")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise FetchError("only http/https URLs are allowed")
    if not parts.hostname:
        raise FetchError("missing host")
    _check_host(parts.hostname)
    return raw


async def _read_capped(resp: httpx.Response) -> str:
    """Stream the body, refusing to buffer more than MAX_BYTES."""
    chunks: list = []
    total = 0
    async for chunk in resp.aiter_bytes(64 * 1024):
        total += len(chunk)
        if total > MAX_BYTES:
            raise FetchError(f"page too large (>{MAX_BYTES // 1024 // 1024} MB)")
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - decode with fallback anyway
        raise FetchError(f"decode failed: {exc}") from exc


async def _dns_resolve(host: str) -> str:
    """Resolve ``host`` in a worker thread and return one validated public IP."""
    ips = await asyncio.to_thread(_resolve_public_ips, host)
    return ips[0]


async def fetch_page(url: str) -> dict:
    """Fetch a public web page and return its (main) HTML.

    Returns ``{"url", "title", "html", "main"}`` where ``url`` is the final URL
    after redirects and ``main`` is True when the body was extracted from a
    detected content container (False → whole-page fallback).
    Raises :class:`FetchError` on SSRF rejection / network / size / type errors.
    """
    target = check_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    async with _client_factory() as client:
        current = target
        last_resp = None
        for hop in range(MAX_REDIRECTS + 1):
            # Resolve the host once and pin the connection to a validated
            # public IP so the client never re-resolves (DNS-rebinding guard).
            parts = urlsplit(current)
            ip = await _dns_resolve(parts.hostname or "")
            request_url, host_header, sni = _pin_address(current, ip)
            req_headers = dict(headers)
            req_headers["Host"] = host_header
            request_extensions = {}
            if sni:
                request_extensions["sni_hostname"] = sni
            try:
                resp = await client.get(
                    request_url,
                    headers=req_headers,
                    extensions=request_extensions,
                )
            except httpx.RequestError as exc:
                raise FetchError(f"请求失败: {exc.__class__.__name__}") from exc
            finally:
                # Close the previous hop's response so its connection is
                # returned to the pool instead of leaking.
                if last_resp is not None and last_resp is not resp:
                    await last_resp.aclose()
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    raise FetchError("redirect response without Location")
                current = check_url(urljoin(current, loc))  # SSRF check per hop
                if hop == MAX_REDIRECTS:
                    raise FetchError("too many redirects")
                last_resp = resp
                continue
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code}")
            last_resp = resp
            break

        resp = last_resp
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and ctype not in ("text/html", "application/xhtml+xml"):
            raise FetchError(f"非 HTML 内容类型: {ctype or 'unknown'}")
        body = await _read_capped(resp)

    title_match = _TITLE_RE.search(body)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    main, extracted = _main_content(body)
    return {
        "url": current,
        "title": title[:200],
        "html": main,
        "main": extracted,
    }


# ---------------------------------------------------------------------------
# Main-content extraction (regex-based, dependency-free).
# ---------------------------------------------------------------------------

def _strip_chrome(html: str) -> str:
    html = _COMMENT_RE.sub("", html)
    html = _SCRIPT_STYLE_RE.sub("", html)
    return html


def _close_pos(html: str, start: int, tag: str) -> int:
    """Position just after the matching ``</tag>`` (or end of input)."""
    depth = 0
    for m in _TAG_RE.finditer(html, start):
        if m.group(2).lower() != tag:
            continue
        if m.group(1):  # closing
            if depth == 0:
                return m.end()
            depth -= 1
        elif m.group(4) or tag in _VOID_TAGS:
            continue
        else:
            depth += 1
    return len(html)


def _score_fragment(frag: str) -> int:
    text = _TAG_RE.sub(" ", frag)
    plain = len(" ".join(text.split()))
    score = plain
    score += 25 * frag.count("<p")
    score += 30 * len(re.findall(r"<h[1-3][\s>]", frag, re.I))
    score -= 150 * len(_BAD_ATTR_RE.findall(frag))
    return score


def _main_content(html: str) -> tuple[str, bool]:
    """Return (content_html, extracted_flag).

    Picks the best-scoring container whose id/class hints at main content;
    ``extracted`` is False when the whole page is returned as a fallback.
    """
    cleaned = _strip_chrome(html)
    candidates = []
    for m in _TAG_RE.finditer(cleaned):
        if m.group(1) or m.group(4):
            continue  # closing / self-closing
        tag = m.group(2).lower()
        if tag not in ("div", "article", "section", "main"):
            continue
        attrs = m.group(3) or ""
        if tag == "article":
            candidates.append((m.start(), m.end(), tag, attrs))
            continue
        if _MAIN_HINTS.search(attrs) and not _BAD_HINTS.search(attrs):
            candidates.append((m.start(), m.end(), tag, attrs))

    best_frag = ""
    best_score = 0
    for start, tag_start, tag, _attrs in candidates:
        end = _close_pos(cleaned, tag_start, tag)
        frag = cleaned[start:end]
        score = _score_fragment(frag)
        if score > best_score:
            best_score = score
            best_frag = frag

    if best_score >= 120:  # require a meaningful amount of content
        return best_frag, True
    return cleaned, False
