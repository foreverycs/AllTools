"""Per-tool enable/disable flags for the public catalog and APIs.

State is stored as JSON under the upload archive directory
(``file/tool_flags.json`` by default) so it survives restarts and
shares the same volume as Docker ``./file`` mounts.

Missing / corrupt file ⇒ all tools enabled.
Only known registry slugs are accepted; unknown keys are ignored.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger("toolkit.tool_flags")

FLAGS_FILENAME = "tool_flags.json"
_lock = threading.RLock()

# In-memory cache: (path_str, mtime_ns or -1, frozenset of disabled slugs)
_cache: tuple[str, int, frozenset[str]] | None = None


def _flags_path() -> Path:
    from storage.history import ensure_file_dir

    return ensure_file_dir() / FLAGS_FILENAME


# Historical slug renames: old key in tool_flags.json → current registry slug.
_SLUG_ALIASES: Dict[str, str] = {
    "json": "code-format",
}


def _canonical_slug(slug: str) -> str:
    s = (slug or "").strip()
    return _SLUG_ALIASES.get(s, s)


def _known_slugs() -> Set[str]:
    from tools import TOOL_REGISTRY

    return {str(t.get("slug") or "") for t in TOOL_REGISTRY if t.get("slug")}


def _read_disabled_from_disk(path: Path) -> frozenset[str]:
    if not path.is_file():
        return frozenset()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("tool_flags: failed to read %s: %s", path, exc)
        return frozenset()

    if not isinstance(data, dict):
        return frozenset()

    known = _known_slugs()
    disabled: Set[str] = set()

    # Preferred shape: {"version": 1, "disabled": ["slug", ...]}
    raw_disabled = data.get("disabled")
    if isinstance(raw_disabled, list):
        for item in raw_disabled:
            s = _canonical_slug(str(item or "").strip())
            if s in known:
                disabled.add(s)

    # Also accept {"tools": {"slug": true/false, ...}}
    tools_map = data.get("tools")
    if isinstance(tools_map, dict):
        for key, val in tools_map.items():
            s = _canonical_slug(str(key or "").strip())
            if s not in known:
                continue
            if val is False or val == 0 or str(val).strip().lower() in (
                "0",
                "false",
                "off",
                "disabled",
                "no",
            ):
                disabled.add(s)
            elif val is True or val == 1 or str(val).strip().lower() in (
                "1",
                "true",
                "on",
                "enabled",
                "yes",
            ):
                disabled.discard(s)

    return frozenset(disabled)


def _cache_key(path: Path) -> tuple[str, int]:
    try:
        st = path.stat()
        return str(path.resolve()), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except OSError:
        return str(path), -1


def get_disabled_slugs() -> frozenset[str]:
    """Return the set of currently disabled tool slugs (cached by file mtime)."""
    global _cache
    path = _flags_path()
    key = _cache_key(path)
    with _lock:
        if _cache is not None and _cache[0] == key[0] and _cache[1] == key[1]:
            return _cache[2]
        disabled = _read_disabled_from_disk(path)
        _cache = (key[0], key[1], disabled)
        return disabled


def clear_tool_flags_cache() -> None:
    """Drop in-memory cache (tests / after external file edits)."""
    global _cache
    with _lock:
        _cache = None


def is_tool_enabled(slug: str) -> bool:
    """True if the tool is enabled (unknown slugs treated as enabled)."""
    s = _canonical_slug(slug or "")
    if not s:
        return True
    if s not in _known_slugs():
        return True
    return s not in get_disabled_slugs()


def get_tool_flags() -> Dict[str, bool]:
    """Map every known registry slug → enabled (True/False)."""
    disabled = get_disabled_slugs()
    return {slug: slug not in disabled for slug in sorted(_known_slugs())}


def set_tool_enabled(slug: str, enabled: bool) -> bool:
    """Enable or disable one tool. Returns False if slug is unknown."""
    s = _canonical_slug(slug or "")
    known = _known_slugs()
    if s not in known:
        return False
    flags = get_tool_flags()
    flags[s] = bool(enabled)
    save_tool_flags(flags)
    return True


def set_tools_enabled(updates: Dict[str, bool]) -> List[str]:
    """Apply a partial map of slug → enabled. Returns list of applied slugs."""
    known = _known_slugs()
    flags = get_tool_flags()
    applied: List[str] = []
    for raw_slug, val in (updates or {}).items():
        s = _canonical_slug(str(raw_slug or "").strip())
        if s not in known:
            continue
        flags[s] = bool(val)
        applied.append(s)
    if applied:
        save_tool_flags(flags)
    return applied


def save_tool_flags(enabled_map: Dict[str, bool]) -> Path:
    """Persist full enable map; only known slugs written as disabled list."""
    global _cache
    known = _known_slugs()
    disabled = sorted(
        s for s, on in (enabled_map or {}).items() if s in known and not on
    )
    path = _flags_path()
    payload = {
        "version": 1,
        "disabled": disabled,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    # Atomic replace
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
        _cache = (str(path.resolve()), mtime, frozenset(disabled))
    logger.info("tool_flags saved disabled=%s path=%s", disabled, path)
    return path


def tool_slug_from_path(path: str) -> Optional[str]:
    """Extract tool slug from ``/tools/{slug}/...`` (app-relative path).

    Returns the **canonical** registry slug (legacy path segments are mapped).
    """
    if not path:
        return None
    # Normalize; ignore query. ROOT_PATH is usually stripped by ASGI.
    p = path.split("?", 1)[0]
    parts = [x for x in p.split("/") if x]
    if len(parts) >= 2 and parts[0] == "tools":
        return _canonical_slug(parts[1])
    return None


def is_tool_path_enabled(path: str) -> bool:
    """True if path is not a tool route, or the tool is enabled."""
    slug = tool_slug_from_path(path)
    if not slug:
        return True
    known = _known_slugs()
    if slug not in known:
        return True  # let router 404
    return is_tool_enabled(slug)


def flags_status() -> Dict[str, Any]:
    """Diagnostics for admin / health."""
    path = _flags_path()
    disabled = sorted(get_disabled_slugs())
    known = sorted(_known_slugs())
    return {
        "path": str(path),
        "exists": path.is_file(),
        "disabled": disabled,
        "enabled_count": len(known) - len(disabled),
        "total_count": len(known),
    }
