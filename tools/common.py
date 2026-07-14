"""Shared helpers for tool HTTP routes: templates, uploads, naming."""

from __future__ import annotations

import os
import re
from typing import Optional

from fastapi import HTTPException, UploadFile
from fastapi.templating import Jinja2Templates

from core.settings import get_settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

_SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff.\-]+", re.UNICODE)

# Media types
DOCX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
PDF_MEDIA = "application/pdf"
ZIP_MEDIA = "application/zip"

# Sensible defaults for modules that still read constants (updated via refresh_limits).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_BATCH_FILES = 20


def max_upload_bytes() -> int:
    return get_settings().max_upload_bytes


def max_batch_files() -> int:
    return get_settings().max_batch_files


def upload_chunk_size() -> int:
    return get_settings().upload_chunk_size


def refresh_limits() -> None:
    """Refresh module-level limit constants after settings cache clear (tests)."""
    global MAX_UPLOAD_BYTES, MAX_BATCH_FILES
    s = get_settings()
    MAX_UPLOAD_BYTES = s.max_upload_bytes
    MAX_BATCH_FILES = s.max_batch_files


def safe_stem(filename: Optional[str], default: str = "output") -> str:
    stem = os.path.splitext(os.path.basename(filename or default))[0]
    stem = _SAFE_NAME_RE.sub("_", stem).strip("._") or default
    return stem[:80]


async def save_upload(
    file: UploadFile,
    dest: str,
    *,
    max_bytes: Optional[int] = None,
) -> int:
    """Stream an upload to ``dest``, enforcing size limit. Returns byte count."""
    limit = max_bytes if max_bytes is not None else max_upload_bytes()
    chunk_size = upload_chunk_size()
    total = 0
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large (max {limit // (1024 * 1024)} MB)",
                )
            out.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    return total


def check_upload_size_header(file: UploadFile, *, label: Optional[str] = None) -> None:
    """Reject early when Content-Length / starlette size exceeds the limit."""
    limit = max_upload_bytes()
    if file.size is not None and file.size > limit:
        name = label or file.filename or "file"
        raise HTTPException(
            status_code=413,
            detail=f"{name}: file too large (max {limit // (1024 * 1024)} MB)",
        )


__all__ = [
    "BASE_DIR",
    "TEMPLATES_DIR",
    "templates",
    "DOCX_MEDIA",
    "PDF_MEDIA",
    "ZIP_MEDIA",
    "MAX_UPLOAD_BYTES",
    "MAX_BATCH_FILES",
    "max_upload_bytes",
    "max_batch_files",
    "upload_chunk_size",
    "refresh_limits",
    "safe_stem",
    "save_upload",
    "check_upload_size_header",
]
