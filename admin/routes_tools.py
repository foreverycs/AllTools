"""Admin console: unified tool management (enable flags + category + source).

Every registered tool (builtin + plugins) is one row with an enable switch,
a category dropdown, its source badge and (for plugins) load status. Saving
writes the flags file and the category-override assignments in one submit.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from admin._common import _admin_url, _redirect, _tpl, admin_post, bust_health_cache
from admin.auth import require_admin
from core.plugins import get_plugin_discovery, plugins_dir
from core.tool_catalog import (
    get_assignments,
    get_categories,
    get_tool_category,
    save_assignments,
)
from core.tool_flags import (
    flags_status,
    get_tool_flags,
    is_tool_enabled,
    save_tool_flags,
    set_tool_enabled,
)
from tools import get_registry

router = APIRouter(tags=["admin"])


def _tool_rows():
    """One dict per registered tool: metadata + enabled + effective category.

    ``source`` is "plugin" when the slug comes from a loaded plugin entry,
    otherwise "builtin". Plugin rows also carry load status / version.
    """
    flags = get_tool_flags()
    disc = get_plugin_discovery()
    plugin_by_slug = {str(e.get("slug") or ""): e for e in disc.entries}
    plugin_status = {str(s.slug or ""): s for s in disc.statuses}
    assignments = get_assignments()
    cat_names = {c["id"]: f"{c.get('icon') or ''} {c['name']}".strip() for c in get_categories()}
    rows = []
    for tool in get_registry():
        slug = str(tool.get("slug") or "")
        if not slug:
            continue
        entry = plugin_by_slug.get(slug)
        status = plugin_status.get(slug)
        eff_cat = get_tool_category(slug) or ""
        rows.append(
            {
                **tool,
                "enabled": flags.get(slug, True),
                "category": eff_cat,
                "category_name": cat_names.get(eff_cat, eff_cat),
                "category_assigned": assignments.get(slug, ""),
                "source": "plugin" if entry else "builtin",
                "plugin_version": status.version if status else "",
                "plugin_loaded": bool(status and status.loaded),
                "plugin_error": (status.error if status else "") or "",
            }
        )
    return rows


@router.get("/tools", response_class=HTMLResponse)
async def tools_page(request: Request):
    """Admin UI: unified tool management table."""
    redir = require_admin(request)
    if redir:
        return redir

    flags = get_tool_flags()
    return _tpl(
        request,
        "admin/tools.html",
        active="tools",
        tools=_tool_rows(),
        categories=get_categories(),
        categories_json=[
            {"id": c["id"], "name": c["name"], "icon": c.get("icon") or ""}
            for c in get_categories()
        ],
        flags=flags,
        flags_meta=flags_status(),
        plugin_dir=str(plugins_dir()),
        static_mounts=get_plugin_discovery().static_mounts,
        flash=request.query_params.get("msg"),
        total=len(get_registry()),
        enabled_count=sum(1 for v in flags.values() if v),
    )


@router.post("/tools")
@admin_post
async def tools_save(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Save enable flags + category assignments for all tools (one submit)."""
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

    # Category assignments (same storage as admin/categories, field assign_{slug}).
    valid_ids = {c["id"] for c in get_categories()}
    assignments = {}
    for t in get_registry():
        slug = str(t.get("slug") or "")
        if not slug:
            continue
        raw = str(form.get("assign_" + slug) or "").strip()
        default_cat = str(t.get("category") or "")
        if raw and raw in valid_ids and raw != default_cat:
            assignments[slug] = raw
        else:
            assignments.pop(slug, None)
    save_assignments(assignments)

    # Bust caches so dashboard / health / public catalog refresh immediately.
    # (_bust_public_and_health already includes bust_health_cache.)
    from admin._common import _bust_public_and_health

    _bust_public_and_health()

    on_n = sum(1 for v in enabled_map.values() if v)
    off_n = len(enabled_map) - on_n
    msg = f"saved: {on_n} enabled, {off_n} disabled, {len(assignments)} categorized"
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
