"""Admin console: category management and tool→category assignments."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from admin._common import (
    _admin_url,
    _bust_public_and_health,
    _redirect,
    _sanitize_category_id,
    _tpl,
    admin_post,
)
from admin.auth import require_admin
from core.tool_catalog import (
    ACCENT_CHOICES,
    get_assignments,
    get_categories,
    get_tool_category,
    reset_catalog,
    save_assignments,
    save_catalog,
)
from tools import get_registry, get_tool_by_slug

router = APIRouter(tags=["admin"])


@router.get("/categories", response_class=HTMLResponse)
async def categories_page(request: Request):
    """Admin UI: customize categories and assign tools to categories."""
    redir = require_admin(request)
    if redir:
        return redir

    cats = get_categories()
    tools = []
    for t in get_registry():
        slug = str(t.get("slug") or "")
        if not slug:
            continue
        tools.append(
            {
                **t,
                "category": get_tool_category(slug) or "",
            }
        )

    return _tpl(
        request,
        "admin/categories.html",
        active="categories",
        categories=cats,
        tools=tools,
        accent_choices=ACCENT_CHOICES,
        flash=request.query_params.get("msg"),
    )


@router.post("/categories")
@admin_post
async def categories_save(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Save category definitions (name / icon / accent / description / order)."""
    form = await request.form()
    ids = [str(v).strip() for v in form.getlist("cat_id")]
    names = [str(v).strip() for v in form.getlist("cat_name")]
    icons = [str(v).strip() for v in form.getlist("cat_icon")]
    accents = [str(v).strip() for v in form.getlist("cat_accent")]
    descs = [str(v).strip() for v in form.getlist("cat_desc")]
    to_remove = {str(v).strip() for v in form.getlist("cat_remove") if str(v).strip()}
    previous = {c["id"]: c for c in get_categories()}

    cats = []
    seen_ids = set()
    for i in range(len(ids)):
        cid = _sanitize_category_id(ids[i])
        if not cid or cid == "_other" or cid in to_remove or cid in seen_ids:
            continue
        seen_ids.add(cid)
        name = names[i] if i < len(names) else ""
        cats.append(
            {
                "id": cid,
                "name": name or cid,
                "name_en": str((previous.get(cid) or {}).get("name_en") or ""),
                "description": descs[i] if i < len(descs) else "",
                "icon": (icons[i] if i < len(icons) else "") or "🧩",
                "accent": (
                    accents[i] if i < len(accents) and accents[i] in ACCENT_CHOICES else "indigo"
                ),
                "route": f"/#col-{cid}",
                "builtin": bool((previous.get(cid) or {}).get("builtin")),
            }
        )

    # Drop assignments pointing to categories that were removed / don't exist.
    keep_ids = {c["id"] for c in cats}
    assignments = {
        k: v
        for k, v in get_assignments().items()
        if v in keep_ids
    }
    save_catalog(cats, assignments)
    # Bust caches so dashboard / health counts refresh immediately.
    _bust_public_and_health()

    msg = f"saved {len(cats)} categories"
    return _redirect(_admin_url("/admin/categories", request) + "?msg=" + quote(msg))


@router.post("/categories/assignments")
@admin_post
async def categories_assignments_save(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Save which category each tool belongs to."""
    form = await request.form()
    valid_ids = {c["id"] for c in get_categories()}
    assignments = {}
    for t in get_registry():
        slug = str(t.get("slug") or "")
        if not slug:
            continue
        raw = str(form.get("assign_" + slug) or "").strip()
        default_cat = str((get_tool_by_slug(slug) or {}).get("category") or "")
        # Persist an override only when it points to a valid category and
        # actually differs from the registry default. Clearing to default (or
        # an unknown target) drops any prior override for this slug.
        if raw and raw in valid_ids and raw != default_cat:
            assignments[slug] = raw
        else:
            assignments.pop(slug, None)
    save_assignments(assignments)
    _bust_public_and_health()

    msg = f"saved assignments for {len(assignments)} tools"
    return _redirect(_admin_url("/admin/categories", request) + "?msg=" + quote(msg))


@router.post("/categories/reset")
@admin_post
async def categories_reset(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Restore built-in default categories and assignments."""
    reset_catalog()
    _bust_public_and_health()
    return _redirect(
        _admin_url("/admin/categories", request) + "?msg=" + quote("reset to defaults")
    )
