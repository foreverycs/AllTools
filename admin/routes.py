"""Admin console: assembles the per-domain routers into the /admin app.

Handlers live in ``admin/routes_*.py``; this module just builds the single
``router`` (mounted under ``/admin``) that ``app.py`` includes, and re-exports
``schedule_health_warm`` for the app lifespan.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from admin._common import (
    _tpl,
    get_cached_health,
    schedule_health_warm,  # noqa: F401 (re-export for app.py)
)
from admin.auth import require_admin
from admin.routes_auth import router as auth_router
from admin.routes_catalog import router as catalog_router
from admin.routes_donation import router as donation_router
from admin.routes_express import router as express_router
from admin.routes_system import router as system_router
from admin.routes_tools import router as tools_router
from admin.routes_uploads import router as uploads_router
from storage import list_records, storage_stats
from tools import get_registry, tools_by_category

router = APIRouter(prefix="/admin", tags=["admin"])
router.include_router(auth_router)
router.include_router(system_router)
router.include_router(uploads_router)
router.include_router(express_router)
router.include_router(tools_router)
router.include_router(catalog_router)
router.include_router(donation_router)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    redir = require_admin(request)
    if redir:
        return redir
    # Never block the dashboard on LibreOffice/Tesseract probes — use cache
    # and refresh engines in the browser via /admin/api/stats when cold.
    health = get_cached_health()
    if health is None:
        schedule_health_warm()
    return _tpl(
        request,
        "admin/dashboard.html",
        active="dashboard",
        stats=storage_stats(),
        health=health,
        health_pending=health is None,
        recent=list_records(limit=8),
        tools=get_registry(),
        categories=tools_by_category(include_disabled=True),
    )


__all__ = ["router", "schedule_health_warm"]
