from __future__ import annotations

import asyncio
import os
import sys

# Disable .pyc bytecode cache writes. Setting os.environ alone is not enough:
# Python only reads PYTHONDONTWRITEBYTECODE at interpreter startup to set
# sys.dont_write_bytecode, so we must set the runtime flag directly.
sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from core.errors import ToolkitError
from core.logging_setup import configure_logging, get_logger
from core.request_id import REQUEST_ID_HEADER, get_request_id
from core.settings import get_settings, load_dotenv, validate_security_settings
from core.version import __version__
from storage import (
    ensure_file_dir,
    get_record,
    list_records,
    record_count,
    resolve_stored,
    retention_days,
)
from admin import admin_router
from admin.auth import is_admin
from tools import (
    get_registry,
    json_legacy_router,
    nav_categories,
)
from tools.common import build_tools_catalog, content_disposition, templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env early so ROOT_PATH is available without forcing full security load.
load_dotenv()
configure_logging()
logger = get_logger("toolkit.app")


def _import_root_path() -> str:
    """Read ROOT_PATH without full credential validation (import-time safe)."""
    from core.settings import _normalize_root_path

    return _normalize_root_path(os.environ.get("ROOT_PATH") or "")


def _donation_public() -> dict:
    """Public donation block for page footers (disabled ⇒ {enabled: False})."""
    from storage.donation import donation_public

    return donation_public()


def _page_ctx(
    *,
    active_nav: str = "home",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Common template context for pages that share the top menu.

    Nav and tool counts come from a cached public-catalog snapshot (rebuilt
    only when admin enable/disable flags change), so admin flags still take
    effect without restart.
    """
    from tools import public_snapshot

    snap = public_snapshot()
    ctx: Dict[str, Any] = {
        "nav_items": snap["nav"],
        "active_nav": active_nav,
        # Homepage stats: modules + featured (featured is outside module grids).
        "module_count": snap["module_count"],
        "featured_count": snap["featured_count"],
        "tool_count": snap["tool_count"],
        "tools_catalog": snap["catalog"],
        # Feature spotlight: show only while the featured tool is enabled.
        "express_enabled": any(
            str(t.get("slug")) == "express" for t in snap["featured"]
        ),
        # Donation block (bottom of page) — enabled + QR set by admin.
        "donation": _donation_public(),
    }
    if extra:
        ctx.update(extra)
    return ctx


@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.concurrency import shutdown_pools
    from core.jobs import reclaim_expired, sweep_orphan_job_dirs
    from storage.history import _do_cleanup

    # Project-root .env for local runs (does not override real process env).
    load_dotenv()
    configure_logging()
    # Fail fast on weak/missing admin credentials (unless ALLOW_INSECURE_ADMIN=1).
    validate_security_settings()
    ensure_file_dir()
    try:
        _do_cleanup()
    except Exception:
        pass
    # File express: expiry only blocks user pickup; packages are retained for
    # admin indefinitely (no automatic purge on startup).
    try:
        await reclaim_expired()
    except Exception:
        pass
    # After a crash, leftover job work/output dirs are not tracked by any live
    # job — reclaim them once so they do not accumulate across restarts.
    try:
        await sweep_orphan_job_dirs()
    except Exception:
        pass
    logger.info(
        "toolkit started version=%s tools=%s",
        __version__,
        len(get_registry()),
    )

    # Periodic housekeeping off the request path: history cleanup is already
    # rate-limited per write, this guarantees a cadence even without traffic.
    cleanup_task: Optional[asyncio.Task] = None

    async def _periodic_cleanup() -> None:
        while True:
            await asyncio.sleep(600)
            try:
                await asyncio.to_thread(_do_cleanup)
                await reclaim_expired()
            except Exception:
                logger.warning("periodic cleanup failed", exc_info=True)

    cleanup_task = asyncio.create_task(_periodic_cleanup())

    # Optional plugin hot reload on file change (PLUGIN_AUTO_RELOAD=1).
    # Off by default: route swaps happen in a background thread between requests.
    plugin_watch_task: Optional[asyncio.Task] = None
    if (os.environ.get("PLUGIN_AUTO_RELOAD") or "").strip().lower() in (
        "1",
        "true",
        "on",
        "yes",
    ):
        async def _plugin_watcher() -> None:
            last = plugin_runtime.fingerprint()
            while True:
                await asyncio.sleep(3)
                try:
                    snap = plugin_runtime.fingerprint()
                    if snap != last:
                        last = snap
                        await asyncio.to_thread(hot_reload_plugins)
                        logger.info("plugin hot reload applied (files changed)")
                except Exception:
                    logger.warning("plugin watcher failed", exc_info=True)

        plugin_watch_task = asyncio.create_task(_plugin_watcher())
        logger.info("plugin auto-reload watcher enabled")

    # Probe LibreOffice / OCR off the request path so admin dashboard is snappy.
    try:
        from admin.routes import schedule_health_warm

        schedule_health_warm()
    except Exception:
        pass
    try:
        yield
    finally:
        # Release ProcessPoolExecutor workers if any were started.
        shutdown_pools(wait=False)
        if cleanup_task is not None:
            cleanup_task.cancel()
        if plugin_watch_task is not None:
            plugin_watch_task.cancel()
        # Release cached SQLite connections.
        try:
            from storage.express import close_db_connections as close_express

            close_express()
        except Exception:
            pass
        try:
            from storage.history import close_db_connections as close_history

            close_history()
        except Exception:
            pass
        logger.info("toolkit stopped")


app = FastAPI(
    title="工具集",
    version=__version__,
    lifespan=lifespan,
    root_path=_import_root_path(),
)

app.add_middleware(GZipMiddleware, minimum_size=500)


@app.exception_handler(ToolkitError)
async def toolkit_error_handler(request: Request, exc: ToolkitError):
    """Map unified ToolkitError hierarchy to HTTP responses."""
    rid = get_request_id()
    body: dict = {"detail": exc.detail}
    if rid:
        body["request_id"] = rid
    headers = {REQUEST_ID_HEADER: rid} if rid else {}
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=headers,
    )


# Middleware order matters: RequestId must be outermost so downstream middleware
# can attach the id to early responses (403 / 429). Starlette makes the LAST
# registered middleware the outermost, hence the registration order below.
def register_middleware(app: FastAPI) -> None:
    """Register the app's reusable HTTP middlewares (see core.middleware)."""
    from core.middleware import (
        AccessLogMiddleware,
        MaxRequestBodySizeMiddleware,
        PublicRateLimitMiddleware,
        RequestIdMiddleware,
        SecurityHeadersMiddleware,
        ToolFlagGateMiddleware,
    )

    app.add_middleware(SecurityHeadersMiddleware)
    # Global request-body cap (defense in depth): endpoint checks rely on
    # Content-Length, which chunked bodies can bypass. Register early so this
    # runs before the rate limiter reads the body.
    app.add_middleware(MaxRequestBodySizeMiddleware)
    app.add_middleware(PublicRateLimitMiddleware)
    app.add_middleware(ToolFlagGateMiddleware)
    # Access log sits just inside RequestId so it sees rid + final status.
    app.add_middleware(AccessLogMiddleware)
    # Trust X-Forwarded-* ONLY from configured reverse proxies (Baota/Nginx).
    # Defaults to loopback (single-host proxy); configure TRUSTED_PROXY_HOSTS
    # with the actual proxy IP/host when it runs on another machine. Never use
    # "*" — it lets any remote client spoof its IP and bypass the IP rate
    # limiter. Registered here so it runs BEFORE PublicRateLimitMiddleware:
    # per-IP limits must key on the real client IP (rewritten from
    # X-Forwarded-*), not on the proxy's direct peer address.
    try:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        from core.settings import parse_proxy_hosts

        app.add_middleware(
            ProxyHeadersMiddleware,
            trusted_hosts=list(
                parse_proxy_hosts(os.environ.get("TRUSTED_PROXY_HOSTS"))
            ),
        )
    except Exception:
        pass
    app.add_middleware(RequestIdMiddleware)


register_middleware(app)


# ---------------------------------------------------------------------------
# Plugin runtime: routers / static / templates are installed through a mutable
# container so hot reload (admin「插件重载」, or PLUGIN_AUTO_RELOAD watcher) can
# swap plugins without restarting the app. The FastAPI-facing wiring lives in
# core/plugin_runtime.py; discovery lives in core/plugins.py.
# ---------------------------------------------------------------------------
from core.plugin_runtime import PluginRuntime

plugin_runtime = PluginRuntime(app)


def hot_reload_plugins():
    """Re-discover plugins and swap registry, routes, templates and static.

    Thin delegate kept for the admin reload endpoint and the auto-reload
    watcher; see ``core.plugin_runtime.PluginRuntime.reload``.
    """
    return plugin_runtime.reload()


# Static assets (shared CSS, etc.)
static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Plugin static assets (mounts grow on hot reload; idempotent).
plugin_runtime.mount_statics()

# All tool routers are plugins (installed through the plugin container below);
# the only builtin router is the legacy /tools/json redirect.
app.include_router(json_legacy_router)

# Install plugin routers after the builtin ones (startup; reload swaps later).
plugin_runtime.install_routes()

# Admin console (password-protected)
app.include_router(admin_router)


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Service worker at site root so scope covers the whole app."""
    path = os.path.join(BASE_DIR, "static", "sw.js")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="sw missing")
    return FileResponse(
        path,
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
async def web_manifest(request: Request):
    """PWA manifest with start_url respecting reverse-proxy root_path."""
    from tools.common import url_path

    root = url_path("/", request)
    if not root.endswith("/"):
        # start_url should be a path the browser can open
        start = root if root else "/"
    else:
        start = root
    body = {
        "name": "工具集",
        "short_name": "工具集",
        "description": "本地部署的办公与开发工具台：PDF/Word、发票合并、编码工具与文件快递",
        "start_url": start,
        "scope": start if start.endswith("/") else (start + "/" if start != "/" else "/"),
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0b1220",
        "theme_color": "#4f46e5",
        "lang": "zh-CN",
        "categories": ["productivity", "utilities"],
        "icons": [
            {
                "src": url_path("/static/icons/icon-192.png", request),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": url_path("/static/icons/icon-512.png", request),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": url_path("/static/icons/icon-maskable-512.png", request),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    return JSONResponse(
        body,
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        _page_ctx(active_nav="home"),
    )


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt(request: Request):
    """Crawl directives. Honors SEO_INDEXABLE and SITE_ORIGIN."""
    from core.seo import robots_response

    return robots_response(request)


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request):
    """XML sitemap of homepage + currently-enabled public tool pages."""
    from core.seo import sitemap_response

    return sitemap_response(request)


@app.get("/donation/qr", include_in_schema=False)
async def donation_qr(request: Request):
    """Serve the admin-uploaded donation QR code (image/png)."""
    from storage.donation import qr_media_type, qr_path

    path = qr_path()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Donation QR not set")
    return FileResponse(
        path,
        media_type=qr_media_type(),
        headers={"Cache-Control": "private, max-age=600"},
    )


@app.get("/api/tools")
async def api_tools():
    """Machine-readable tool catalog (enabled tools only)."""
    from tools import public_snapshot

    snap = public_snapshot()
    return JSONResponse(
        {
            "version": app.version,
            "categories": snap["categories"],
            "nav": snap["nav"],
            "tools": snap["public"],
            "featured": snap["featured"],
            "counts": {
                "module": snap["module_count"],
                "featured": snap["featured_count"],
                "total": snap["tool_count"],
            },
        }
    )


def _active_rate_backend() -> str:
    """The rate-limit backend actually in use (memory or redis, post-fallback)."""
    from core.api_rate_limit import active_backend

    return active_backend()


# Lightweight cache for expensive /health details (engines + storage).
_health_detail_cache: dict = {}
_health_detail_ts: float = 0.0
_HEALTH_DETAIL_TTL: float = 60.0


def _health_details(*, force: bool = False) -> dict:
    """Engine/OCR/storage snapshot; cached so probes stay cheap.

    The engine/OCR portion is sourced from the shared ``core.health`` snapshot
    (single cache, also used by the admin console); storage/jobs/rate settings
    are cheap and kept fresh on the local TTL.
    """
    global _health_detail_cache, _health_detail_ts
    import time

    now = time.monotonic()
    if (
        not force
        and _health_detail_cache
        and now - _health_detail_ts < _HEALTH_DETAIL_TTL
    ):
        return _health_detail_cache

    from core.health import get_health_snapshot

    w2p = get_health_snapshot()["word2pdf"]
    ocr = get_health_snapshot()["ocr"]
    from core.jobs import jobs_backend_is_shared, jobs_backend_name

    settings = get_settings()
    _health_detail_cache = {
        "word2pdf": {
            "ready": w2p.get("ready", False),
            "engines": w2p.get("engines") or [],
            "preferred": w2p.get("preferred"),
        },
        "ocr": {
            "available": ocr.get("available", False),
            "lang": ocr.get("lang"),
        },
        "upload_history": {
            "retention_days": retention_days(),
            "count": record_count(),
        },
        "convert_concurrency": settings.convert_concurrency,
        "root_path": settings.root_path or "",
        "jobs": {
            "backend": jobs_backend_name(),
            "shared": jobs_backend_is_shared(),
            "note": (
                "process-local; use a single uvicorn worker"
                if not jobs_backend_is_shared()
                else "shared via Redis; run multiple workers"
            ),
        },
        "api_rate_limit": settings.api_rate_limit,
        "api_rate_window_sec": settings.api_rate_window_sec,
        "api_rate_backend": _active_rate_backend(),
    }
    _health_detail_ts = now
    return _health_detail_cache


# Shown when async job is missing (restart, TTL, or multi-worker routing).
_JOB_MISSING_DETAIL = (
    "任务不存在或已过期。可能因服务重启、任务清理（TTL），或使用了多个"
    "未启用 Redis 共享任务存储的 uvicorn worker。"
)


def _jobs_health_note() -> dict:
    """Health payload for the async job backend."""
    from core.jobs import jobs_backend_is_shared, jobs_backend_name

    shared = jobs_backend_is_shared()
    if shared:
        return {
            "backend": jobs_backend_name(),
            "single_worker_required": False,
            "note": (
                "Async conversion jobs are stored in Redis and shared across "
                "workers; ensure JOB_OUTPUT_DIR is on shared storage."
            ),
            "note_zh": "异步任务通过 Redis 共享，可多 worker 部署（需共享 JOB_OUTPUT_DIR）。",
        }
    return {
        "backend": jobs_backend_name(),
        "single_worker_required": True,
        "note": (
            "Async conversion jobs are in-process; run uvicorn with --workers 1 "
            "(multi-worker causes job 404 on poll/download)."
        ),
        "note_zh": "异步任务为进程内存存储，请使用 --workers 1。",
    }


def _health_body(detail: bool) -> dict:
    """Monitoring payload shared by the JSON probe and the HTML dashboard."""
    from tools import get_registry, public_snapshot

    snap = public_snapshot()
    body: dict = {
        "status": "ok",
        "version": app.version,
        # Public inventory = module catalog + homepage featured tools.
        "tools": snap["tool_count"],
        "tools_module": snap["module_count"],
        "tools_featured": snap["featured_count"],
        "tools_registered": len(get_registry()),
        # Ops hint: async convert jobs are process-local (or Redis-shared).
        "jobs": _jobs_health_note(),
    }
    if detail:
        body["categories"] = [
            {
                "id": c["id"],
                "name": c["name"],
                "count": len(c["tools"]),
                "route": c.get("route"),
            }
            for c in snap["categories"]
        ]
        body.update(_health_details())
    return body


@app.get("/health")
async def health(request: Request, format: str = Query("html"), detail: int = Query(0, ge=0, le=1)):
    """Liveness probe. Defaults to the graphical monitoring dashboard (HTML).

    - ``/health`` → HTML dashboard
    - ``/health?format=json`` → JSON payload for uptime probes / scripts
    - ``?detail=1`` includes engines, OCR, categories and storage stats.
    """
    if format == "json":
        return JSONResponse(_health_body(detail=bool(detail)))
    body = _health_body(detail=True)
    cats = body.get("categories") or []
    max_count = max([c["count"] for c in cats] or [1])
    return templates.TemplateResponse(
        request,
        "monitor.html",
        {
            "status": body.get("status"),
            "version": body.get("version"),
            "tools": body.get("tools"),
            "tools_module": body.get("tools_module"),
            "tools_featured": body.get("tools_featured"),
            "tools_registered": body.get("tools_registered"),
            "jobs": body.get("jobs"),
            "categories": cats,
            "max_count": max_count,
            "word2pdf": body.get("word2pdf"),
            "ocr": body.get("ocr"),
            "upload_history": body.get("upload_history"),
            "convert_concurrency": body.get("convert_concurrency"),
            "mount_path": body.get("root_path"),
            "api_rate_limit": body.get("api_rate_limit"),
            "api_rate_window_sec": body.get("api_rate_window_sec"),
            "api_rate_backend": body.get("api_rate_backend"),
            "nav_items": nav_categories(),
            "active_nav": "",
        },
    )


@app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str):
    """Poll an async conversion job (in-process store; lost on restart)."""
    from core.jobs import get_job, job_public_dict

    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=_JOB_MISSING_DETAIL)
    return JSONResponse(job_public_dict(job))


@app.get("/api/jobs/{job_id}/download")
async def api_job_download(job_id: str):
    """Download the result file for a completed job (if still on disk).

    The result is streamed and cleanup runs in the generator's ``finally``,
    which fires reliably even under ``BaseHTTPMiddleware`` (background tasks
    attached to a streaming response are not guaranteed to run there).
    """
    from core.jobs import JobStatus, get_job, mark_downloaded

    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=_JOB_MISSING_DETAIL)
    if job.status != JobStatus.done or not job.output_path:
        raise HTTPException(
            status_code=409,
            detail="任务尚未完成，暂无可下载结果。请稍候再试或重新提交。",
        )
    if not os.path.isfile(job.output_path):
        raise HTTPException(
            status_code=410,
            detail="结果文件已过期或已清理，请重新转换。",
        )
    path = job.output_path
    filename = job.download_name or os.path.basename(path)
    media = job.media_type or "application/octet-stream"
    headers = dict(job.response_headers or {})
    headers.setdefault("Content-Disposition", content_disposition(filename))

    async def _stream() -> None:
        # Reads run in a worker thread so the event loop is not blocked.
        import anyio

        try:
            with open(path, "rb") as f:
                while True:
                    chunk = await anyio.to_thread.run_sync(f.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                await mark_downloaded(job_id)
            except Exception:
                logger.exception("job download cleanup failed id=%s", job_id)

    return StreamingResponse(
        _stream(),
        media_type=media,
        headers=headers,
    )


@app.get("/api/uploads")
async def api_uploads(request: Request, limit: int = Query(50, ge=1, le=200)):
    """JSON list of recent uploads (admin only; last retention window)."""
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse(
        {
            "retention_days": retention_days(),
            "items": list_records(limit=limit),
        }
    )


@app.get("/api/uploads/{record_id}/download")
async def download_upload(request: Request, record_id: str):
    """Download the archived input file for a history record (admin only)."""
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    rec = get_record(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Record not found")
    rel = rec.get("input_rel")
    if not rel:
        raise HTTPException(status_code=404, detail="No input file stored")
    path = resolve_stored(str(rel))
    if path is None:
        raise HTTPException(status_code=404, detail="File missing on disk")
    name = rec.get("original_name") or path.name
    return FileResponse(path, filename=str(name))


if __name__ == "__main__":
    import uvicorn

    # Propagate to the uvicorn reloader child process so it also skips .pyc.
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
