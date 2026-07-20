"""Legacy ``/tools/json`` → permanent redirect to ``/tools/code-format``.

Keeps old bookmarks and scripts working. Prefer :mod:`tools.code_format_tool`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from tools.common import url_path

router = APIRouter(
    prefix="/tools/json",
    tags=["code-format-legacy"],
    include_in_schema=False,
)


def _redirect(request: Request, suffix: str = "") -> RedirectResponse:
    target = url_path(f"/tools/code-format{suffix}", request)
    q = request.url.query
    if q:
        target = f"{target}?{q}"
    return RedirectResponse(url=target, status_code=308)


@router.api_route("", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def legacy_root(request: Request):
    return _redirect(request, "")


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
)
async def legacy_subpath(request: Request, path: str):
    return _redirect(request, f"/{path}" if path else "")


__all__ = ["router"]
