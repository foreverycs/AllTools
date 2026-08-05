"""Admin-customizable tool categories and per-tool category assignment.

Defaults come from ``tools.TOOL_CATEGORIES`` and each registry tool's
``category``. Admins can override them through the admin console, persisted as
JSON at ``file/tool_catalog.json`` (same volume as ``tool_flags.json``):

- add / rename / reorder / delete categories,
- reassign which category each tool belongs to.

Missing or corrupt store ⇒ the built-in defaults are used, so nothing changes
out of the box.

The stored ``categories`` list is authoritative when present: saving writes a
full snapshot of the current categories (built-ins and custom alike). The
``assignments`` map holds only explicit tool→category overrides; any tool
without an override falls back to its registry default.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("toolkit.tool_catalog")

CATALOG_FILENAME = "tool_catalog.json"
_lock = threading.RLock()

# In-memory cache: (path_str, mtime_ns or -1, categories, assignments)
_cache: Optional[tuple] = None

# Accent choices available to admins (must match CSS token names).
ACCENT_CHOICES: List[str] = [
    "indigo",
    "emerald",
    "violet",
    "amber",
    "rose",
    "sky",
    "slate",
    "teal",
    "orange",
]

def _store_path() -> Path:
    from storage.history import ensure_file_dir

    return ensure_file_dir() / CATALOG_FILENAME


def _cache_key(path: Path):
    try:
        st = path.stat()
        return str(path.resolve()), int(
            getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        )
    except OSError:
        return str(path), -1


def _default_categories() -> List[Dict[str, Any]]:
    """Copies of the built-in categories, tagged as built-in."""
    from tools import TOOL_CATEGORIES

    return [
        {
            **dict(c),
            "builtin": True,
            "synthetic": False,
        }
        for c in TOOL_CATEGORIES
    ]


def _read_store(path: Path):
    """Return ``(categories, assignments)`` from disk, or ``None``."""
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("tool_catalog: failed to read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None

    categories = data.get("categories")
    if isinstance(categories, list) and categories:
        normalized = []
        for c in categories:
            if isinstance(c, dict) and c.get("id"):
                normalized.append(
                    {
                        "id": str(c["id"]),
                        "name": str(c.get("name") or c["id"]),
                        "name_en": str(c.get("name_en") or ""),
                        "description": str(c.get("description") or ""),
                        "icon": str(c.get("icon") or "🧩"),
                        "accent": str(c.get("accent") or "indigo"),
                        "route": str(c.get("route") or f"/#col-{c['id']}"),
                        "builtin": bool(c.get("builtin")),
                        "synthetic": False,
                    }
                )
        if normalized:
            categories = normalized
        else:
            categories = None
    else:
        categories = None

    assignments = data.get("assignments")
    if not isinstance(assignments, dict):
        assignments = {}

    return categories, assignments


def _load():
    """Cached (categories, assignments); defaults when store absent."""
    global _cache
    path = _store_path()
    key = _cache_key(path)
    with _lock:
        if _cache is not None and _cache[0] == key[0] and _cache[1] == key[1]:
            return _cache[2], _cache[3]
        data = _read_store(path)
        if data is None:
            categories, assignments = _default_categories(), {}
        else:
            categories, assignments = data
        _cache = (key[0], key[1], categories, assignments)
        return categories, assignments


def get_categories() -> List[Dict[str, Any]]:
    """Effective ordered category list (admin overrides or defaults)."""
    cats, _ = _load()
    return [dict(c) for c in cats]


def get_tool_category(slug: str) -> Optional[str]:
    """Effective category id for a tool (assignment override or default)."""
    from tools import get_tool_by_slug

    s = (slug or "").strip()
    if not s:
        return None
    _, assignments = _load()
    override = assignments.get(s)
    if override:
        return str(override)
    reg = get_tool_by_slug(s)
    if reg and reg.get("category"):
        return str(reg["category"])
    return None


def get_assignments() -> Dict[str, str]:
    """Explicit tool→category overrides (does not include registry defaults)."""
    _, assignments = _load()
    return dict(assignments)


def catalog_revision() -> int:
    """Revision used to bust the public-catalog snapshot cache."""
    return _cache_key(_store_path())[1]


def save_catalog(categories: List[Dict[str, Any]], assignments: Dict[str, str]) -> Path:
    """Persist the full category list plus tool assignments (atomic write)."""
    global _cache
    path = _store_path()
    payload = {
        "version": 1,
        "categories": [
            {
                "id": c["id"],
                "name": c.get("name") or c["id"],
                "name_en": c.get("name_en") or "",
                "description": c.get("description") or "",
                "icon": c.get("icon") or "🧩",
                "accent": c.get("accent") or "indigo",
                "route": c.get("route") or f"/#col-{c['id']}",
                "builtin": bool(c.get("builtin")),
            }
            for c in categories
            if c.get("id")
        ],
        "assignments": {
            k: v for k, v in (assignments or {}).items() if k and v
        },
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        try:
            st = path.stat()
            mtime = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        except OSError:
            mtime = -1
        _cache = (
            str(path.resolve()),
            mtime,
            [dict(c) for c in payload["categories"]],
            dict(payload["assignments"]),
        )
    logger.info(
        "tool_catalog saved categories=%s assignments=%s path=%s",
        len(payload["categories"]),
        len(payload["assignments"]),
        path,
    )
    return path


def save_assignments(assignments: Dict[str, str]) -> Path:
    """Persist tool→category overrides, keeping current categories intact."""
    cats, _ = _load()
    return save_catalog(cats, assignments)


def reset_catalog() -> None:
    """Restore built-in defaults (delete the store file)."""
    path = _store_path()
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        logger.warning("tool_catalog: failed to remove %s", path)
    clear_cache()


def clear_cache() -> None:
    """Drop in-memory cache (tests / external edits)."""
    global _cache
    with _lock:
        _cache = None
