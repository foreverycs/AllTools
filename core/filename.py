"""Shared safe-filename helpers (upload archive, express packages, download names).

Single home for the sanitize rules previously duplicated in
``storage.history``, ``storage.express`` and ``tools.common`` so the three
layers cannot drift:
- ``sanitize_filename``: full filename (stem + extension) for on-disk storage.
- ``safe_stem``: stem only, for generated download names (``name_out.ext``).
"""

from __future__ import annotations

import os
import re
from typing import Optional

_SAFE_RE = re.compile(r"[^\w\u4e00-\u9fff.\-]+", re.UNICODE)


def sanitize_filename(
    name: Optional[str],
    default: str = "file",
    *,
    stem_limit: int = 80,
    ext_limit: int = 12,
) -> str:
    """Sanitize a filename for safe on-disk storage.

    Basename only (no path traversal), non-word characters replaced with ``_``,
    with separate length caps for the stem and the extension.
    """
    base = os.path.basename(name or default)
    stem, ext = os.path.splitext(base)
    stem = _SAFE_RE.sub("_", stem).strip("._") or default
    ext = re.sub(r"[^\w.]", "", ext)[:ext_limit]
    return (stem[:stem_limit] + ext) if ext else stem[:stem_limit]


def safe_stem(filename: Optional[str], default: str = "output", *, limit: int = 80) -> str:
    """Sanitize a filename down to its (basename) stem for output naming."""
    stem = os.path.splitext(os.path.basename(filename or default))[0]
    stem = _SAFE_RE.sub("_", stem).strip("._") or default
    return stem[:limit]


__all__ = ["sanitize_filename", "safe_stem"]
