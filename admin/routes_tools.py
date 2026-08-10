"""Admin console: enable / disable public tools."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from admin._common import _admin_url, _redirect, _tpl, admin_post, bust_health_cache
from admin.auth import require_admin
from core.tool_flags import (
    flags_status,
    get_tool_flags,
    is_tool_enabled,
    save_tool_flags,
    set_tool_enabled,
)
from tools import get_registry, tools_by_category

router = APIRouter(tags=["admin"])


@router.get("/tools", response_class=HTMLResponse)
async def tools_flags_page(request: Request):
    """Admin UI: enable / disable public tools."""
    redir = require_admin(request)
    if redir:
        return redir

    flags = get_tool_flags()
    # Shallow-copy tools so we never mutate TOOL_REGISTRY entries in place.
    cats = []
    for cat in tools_by_category(include_disabled=True):
        tools = []
        for tool in cat.get("tools") or []:
            slug = str(tool.get("slug") or "")
            tools.append({**tool, "enabled": flags.get(slug, True)})
        cats.append({**cat, "tools": tools})

    return _tpl(
        request,
        "admin/tools.html",
        active="tools",
        categories=cats,
        flags=flags,
        flags_meta=flags_status(),
        flash=request.query_params.get("msg"),
        total=len(get_registry()),
        enabled_count=sum(1 for v in flags.values() if v),
    )


@router.post("/tools")
@admin_post
async def tools_flags_save(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Save enable/disable checkboxes for all tools."""
    form = await request.form()
    # Checkboxes: only checked boxes are submitted. Build full map from registry.
    enabled_slugs = {
        str(v).strip()
        for v in form.getlist("enabled")
        if str(v).strip()
    }
    enabled_map = {
        str(t.get("slug") or ""): str(t.get("slug") or "") in enabled_slugs
        for t in get_registry()
        if t.get("slug")
    }
    save_tool_flags(enabled_map)
    # Bust admin health cache so dashboard counts refresh immediately.
    bust_health_cache()

    on_n = sum(1 for v in enabled_map.values() if v)
    off_n = len(enabled_map) - on_n
    msg = f"saved: {on_n} enabled, {off_n} disabled"
    return _redirect(_admin_url("/admin/tools", request) + "?msg=" + quote(msg))


@router.post("/tools/{slug}/toggle")
@admin_post
async def tools_flag_toggle(
    request: Request,
    slug: str,
    csrf_token: Optional[str] = Form(None),
    enabled: Optional[str] = Form(None),
):
    """Toggle a single tool (form or JSON-friendly)."""
    s = (slug or "").strip()
    # Form may send enabled=1/0; if omitted, flip current state.
    if enabled is None or str(enabled).strip() == "":
        new_val = not is_tool_enabled(s)
    else:
        new_val = str(enabled).strip().lower() in ("1", "true", "on", "yes", "enabled")

    ok = set_tool_enabled(s, new_val)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {s}")

    bust_health_cache()

    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return JSONResponse({"slug": s, "enabled": new_val})
    state = "enabled" if new_val else "disabled"
    return _redirect(
        _admin_url("/admin/tools", request) + "?msg=" + quote(f"{s} {state}")
    )
