"""工具注册表基础设施：分类、注册、公开快照与插件协调。

内置工具已全部迁移为插件（``plugins/``），本包不再持有工具路由或注册表
条目，仅保留：

- 默认分类定义（``TOOL_CATEGORIES``，admin 可覆盖）；
- 注册表访问器（``get_registry`` / ``get_tool_by_slug`` / 快照缓存）；
- 插件合并（``refresh_plugins_registry``，启动与热重载时重绑 ``TOOL_REGISTRY``）。

唯一仍内置的路由是 ``json_tool`` 的遗留 308 重定向（无注册表条目，
不适合插件化），故 ``TOOL_ROUTERS`` 只剩该路由。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .json_tool import router as json_legacy_router

# ---------------------------------------------------------------------------
# Categories (order = homepage display order)
#
# These are the *default* categories. Admins may rename / reorder / delete /
# add categories and reassign tools via the admin console (persisted in
# ``file/tool_catalog.json``). See core/tool_catalog.py.
# ---------------------------------------------------------------------------
TOOL_CATEGORIES: List[Dict[str, Any]] = [
    {
        "id": "pdf",
        "name": "PDF 处理",
        "name_en": "PDF",
        "description": "PDF 与 Word 互转、合并与排版还原",
        "icon": "📕",
        "accent": "rose",
        "route": "/#col-pdf",
        "lead": "PDF ↔ Word 转换、发票合并与版式还原，适合日常办公文档处理。",
    },
    {
        "id": "image",
        "name": "图片处理",
        "name_en": "Image",
        "description": "图片压缩、格式转换、九宫格切图与图片转 PDF",
        "icon": "🖼️",
        "accent": "sky",
        "route": "/#col-image",
        "lead": "图片常用操作：压缩、格式互转、九宫格切图与批量转 PDF。",
    },
    {
        "id": "video",
        "name": "视频处理",
        "name_en": "Video",
        "description": "视频相关工具（可通过后台添加工具并归类到此）",
        "icon": "🎬",
        "accent": "violet",
        "route": "/#col-video",
        "lead": "视频处理工具。",
    },
    {
        "id": "audio",
        "name": "音频处理",
        "name_en": "Audio",
        "description": "音频相关工具（可通过后台添加工具并归类到此）",
        "icon": "🎵",
        "accent": "emerald",
        "route": "/#col-audio",
        "lead": "音频处理工具。",
    },
    {
        "id": "text",
        "name": "文本处理",
        "name_en": "Text",
        "description": "编解码、格式化、时间戳、正则与金额大写等文本工具",
        "icon": "✏️",
        "accent": "amber",
        "route": "/#col-text",
        "lead": "开发调试与日常文本处理：编解码、格式化、时间戳、正则、金额大写。",
    },
]

# ---------------------------------------------------------------------------
# Tools
#
# 内置工具的注册表条目已随各工具迁移到 ``plugins/<slug>/`` 的 TOOL 清单
# （启动时经 ``refresh_plugins_registry`` 合并进本注册表）。此处初始为空；
# 展示顺序由各插件清单的 ``order`` 字段控制（见 core/plugins.py）。
# ---------------------------------------------------------------------------
TOOL_REGISTRY: List[Dict[str, Any]] = []

# Routers mounted directly on the FastAPI app (order does not matter).
# json_legacy only provides 308 redirects for old /tools/json URLs; every other
# tool router is installed through the plugin container (see app.py / core.plugins).
TOOL_ROUTERS = (json_legacy_router,)


def is_featured_tool(tool: Dict[str, Any] | None) -> bool:
    """True when a registry entry is a homepage feature (not a module card)."""
    if not tool:
        return False
    return bool(tool.get("featured"))


def enabled_tools(*, include_featured: bool = False) -> List[Dict[str, Any]]:
    """Public catalog: tools not disabled in admin flags.

    Featured tools (e.g. 文件快递) are omitted by default so module grids and
    category pages only show regular tools. Pass ``include_featured=True`` for
    counts / APIs that need the full public set.
    """
    from core.tool_flags import get_disabled_slugs

    disabled = get_disabled_slugs()
    out = []
    for t in TOOL_REGISTRY:
        if str(t.get("slug") or "") in disabled:
            continue
        if is_featured_tool(t) and not include_featured:
            continue
        out.append(t)
    return out


def featured_tools() -> List[Dict[str, Any]]:
    """Enabled tools marked ``featured=True`` (homepage highlight strip)."""
    from core.tool_flags import get_disabled_slugs

    disabled = get_disabled_slugs()
    return [
        t
        for t in TOOL_REGISTRY
        if is_featured_tool(t) and str(t.get("slug") or "") not in disabled
    ]


def tools_by_category(
    *, include_disabled: bool = False, include_featured: bool = False
) -> List[Dict[str, Any]]:
    """Return categories each with a ``tools`` list (only non-empty categories).

    Categories and tool→category placement honor admin customizations stored
    by ``core.tool_catalog`` (rename / reorder / add / delete / reassign).

    By default disabled and featured tools are omitted (homepage / public API).
    Pass ``include_disabled=True`` for the admin console (includes featured).
    Pass ``include_featured=True`` to place featured tools back under categories.
    """
    from core.tool_catalog import get_categories, get_tool_category

    if include_disabled:
        # Full registry for admin (featured tools stay under their category).
        source = list(TOOL_REGISTRY)
    else:
        source = enabled_tools(include_featured=include_featured)
    by_id = {c["id"]: {**c, "tools": []} for c in get_categories()}
    for tool in source:
        # Public category grids never list featured tools unless asked.
        if is_featured_tool(tool) and not include_disabled and not include_featured:
            continue
        cat_id = get_tool_category(tool.get("slug") or "") or ""
        cat = by_id.get(cat_id)
        if cat is not None:
            cat["tools"].append(tool)
        else:
            # Unknown category → attach under a synthetic bucket.
            other = by_id.setdefault(
                "_other",
                {
                    "id": "_other",
                    "name": "其他",
                    "name_en": "Other",
                    "description": "",
                    "icon": "🧩",
                    "accent": "slate",
                    "tools": [],
                },
            )
            other["tools"].append(tool)
    return [c for c in by_id.values() if c["tools"]]


def nav_categories(*, include_disabled: bool = False) -> List[Dict[str, Any]]:
    """Top-nav menu items (all registered categories, including empty)."""
    from core.tool_catalog import get_categories

    # Featured tools (e.g. 文件快递) are rendered as cards in the homepage grid
    # under their assigned category, so count them too. Otherwise a category
    # holding only a featured tool (via an admin assignment) would show a
    # misleading count of 0. Admin view already covers featured via the full
    # registry (include_disabled=True).
    by_id = {
        c["id"]: c
        for c in tools_by_category(
            include_disabled=include_disabled,
            include_featured=not include_disabled,
        )
    }
    items = []
    for c in get_categories():
        filled = by_id.get(c["id"])
        tools = list((filled or {}).get("tools") or [])
        items.append(
            {
                **c,
                "tool_count": len(tools),
                "tool_names": [t.get("name") for t in tools if t.get("name")],
                "tools": tools,
            }
        )
    return items


def get_tool_by_slug(slug: str) -> Dict[str, Any] | None:
    """Lookup a registry entry by slug (ignores enable flags)."""
    s = (slug or "").strip()
    if not s:
        return None
    for tool in TOOL_REGISTRY:
        if tool.get("slug") == s:
            return tool
    return None


def get_registry() -> List[Dict[str, Any]]:
    """Return the CURRENT registry (builtin tools + loaded plugins).

    Prefer this over ``from tools import TOOL_REGISTRY`` in modules that outlive
    a plugin hot reload: ``refresh_plugins_registry`` REBINDS the module-level
    name, so an earlier ``from ... import`` keeps a stale list object. Reading
    ``tools.TOOL_REGISTRY`` (module attribute) at call time also works.
    """
    return TOOL_REGISTRY


# Public-catalog snapshot cache, keyed by the disabled-slug set (itself cached
# by tool_flags.json mtime). Rebuilt only when admin enable/disable changes, so
# hot paths (homepage / /api/tools / /health) avoid per-request list rebuilds.
_snap_cache: Optional[Dict[str, Any]] = None
_snap_key: Optional[frozenset[str]] = None


def public_snapshot() -> Dict[str, Any]:
    """Cached enabled/featured/categories/nav/catalog for public pages."""
    global _snap_cache, _snap_key
    from core.tool_flags import get_disabled_slugs
    from core.tool_catalog import catalog_revision
    from .common import build_tools_catalog

    disabled = get_disabled_slugs()
    snap_key = (disabled, catalog_revision())
    if _snap_cache is not None and _snap_key == snap_key:
        return _snap_cache

    public: List[Dict[str, Any]] = []
    featured: List[Dict[str, Any]] = []
    for t in TOOL_REGISTRY:
        if str(t.get("slug") or "") in disabled:
            continue
        if is_featured_tool(t):
            featured.append(t)
        else:
            public.append(t)

    _snap_cache = {
        "public": public,
        "featured": featured,
        "categories": tools_by_category(),
        "nav": nav_categories(),
        "catalog": build_tools_catalog(),
        "module_count": len(public),
        "featured_count": len(featured),
        "tool_count": len(public) + len(featured),
    }
    _snap_key = snap_key
    # Return shallow copies so callers cannot mutate the shared cache.
    return {
        k: (list(v) if isinstance(v, list) else v) for k, v in _snap_cache.items()
    }

def clear_public_snapshot() -> None:
    """Drop the catalog snapshot (tests / after external flag edits)."""
    global _snap_cache, _snap_key
    _snap_cache = None
    _snap_key = None


# ---------------------------------------------------------------------------
# Plugins: merge discovered plugins (including the converted builtin tools)
# into the public registry. Plugin ROUTERS are NOT merged into TOOL_ROUTERS —
# app.py mounts them through a dedicated container so hot reload can swap
# routes without a restart. A broken or conflicting plugin is skipped (see
# core.plugins) — the app always starts with the builtin infra regardless.
# ---------------------------------------------------------------------------
from core.plugins import PluginDiscovery, discover_plugins

# No builtin tools remain: the registry is populated entirely by plugins.
_BUILTIN_REGISTRY: List[Dict[str, Any]] = []
_BUILTIN_SLUGS = set()


def refresh_plugins_registry() -> PluginDiscovery:
    """(Re)discover plugins and rebind ``TOOL_REGISTRY`` + public snapshot.

    Used both at startup and by the admin hot-reload endpoint. ``force=True``
    re-executes changed plugin modules; the builtin registry is untouched.
    """
    global TOOL_REGISTRY
    disc = discover_plugins(reserved_slugs=_BUILTIN_SLUGS, force=True)
    TOOL_REGISTRY = _BUILTIN_REGISTRY + disc.entries
    clear_public_snapshot()
    from tools.common import set_plugin_template_dirs

    set_plugin_template_dirs(disc.template_dirs)
    return disc


_PLUGINS = refresh_plugins_registry()


__all__ = [
    "TOOL_CATEGORIES",
    "TOOL_REGISTRY",
    "TOOL_ROUTERS",
    "is_featured_tool",
    "enabled_tools",
    "featured_tools",
    "tools_by_category",
    "nav_categories",
    "get_tool_by_slug",
    "get_registry",
    "public_snapshot",
    "clear_public_snapshot",
    "json_legacy_router",
]
