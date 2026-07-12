"""Persist recent upload records under ``file/`` (input files only).

Layout::

    file/
      records.json          # metadata list (newest first)
      2026-07-12/
        20260712T153045_a1b2_in.pdf

Only the uploaded input is archived. Conversion outputs are not stored.
Files and index entries older than ``RETENTION_DAYS`` are deleted on each
write and can also be purged explicitly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
FILE_DIR = Path(os.environ.get("UPLOAD_FILE_DIR", str(BASE_DIR / "file")))
RECORDS_NAME = "records.json"
RETENTION_DAYS = int(os.environ.get("UPLOAD_RETENTION_DAYS", "5"))

_SAFE_RE = re.compile(r"[^\w\u4e00-\u9fff.\-]+", re.UNICODE)
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _safe_name(name: str, default: str = "file") -> str:
    base = os.path.basename(name or default)
    stem, ext = os.path.splitext(base)
    stem = _SAFE_RE.sub("_", stem).strip("._") or default
    ext = re.sub(r"[^\w.]", "", ext)[:12]
    return (stem[:80] + ext) if ext else stem[:80]


def ensure_file_dir() -> Path:
    FILE_DIR.mkdir(parents=True, exist_ok=True)
    return FILE_DIR


def _records_path() -> Path:
    return ensure_file_dir() / RECORDS_NAME


def _load_records() -> List[Dict[str, Any]]:
    path = _records_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _save_records(records: List[Dict[str, Any]]) -> None:
    path = _records_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _cutoff() -> datetime:
    return _now() - timedelta(days=max(RETENTION_DAYS, 1))


def _is_expired(record: Dict[str, Any], cutoff: datetime) -> bool:
    ts = _parse_iso(str(record.get("created_at") or ""))
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < cutoff


def _unlink_quiet(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _remove_record_files(record: Dict[str, Any]) -> None:
    # Prefer input_rel; also drop legacy output_rel if present from older builds.
    for key in ("input_rel", "output_rel"):
        rel = record.get(key)
        if not rel:
            continue
        candidate = (FILE_DIR / str(rel)).resolve()
        try:
            candidate.relative_to(FILE_DIR.resolve())
        except ValueError:
            continue
        _unlink_quiet(candidate)


def cleanup_expired() -> int:
    """Delete records/files older than retention. Returns removed count."""
    ensure_file_dir()
    cutoff = _cutoff()
    removed = 0
    with _lock:
        records = _load_records()
        kept: List[Dict[str, Any]] = []
        for rec in records:
            if _is_expired(rec, cutoff):
                _remove_record_files(rec)
                removed += 1
            else:
                kept.append(rec)
        if removed:
            _save_records(kept)

        for child in list(FILE_DIR.iterdir()):
            if not child.is_dir():
                continue
            try:
                day = datetime.strptime(child.name, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            day_start = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
            if day >= day_start:
                continue
            try:
                newest = max(
                    (p.stat().st_mtime for p in child.rglob("*") if p.is_file()),
                    default=0,
                )
            except OSError:
                newest = 0
            if newest == 0 or datetime.fromtimestamp(
                newest, tz=timezone.utc
            ) < cutoff:
                shutil.rmtree(child, ignore_errors=True)
    return removed


def archive_conversion(
    *,
    tool: str,
    original_name: str,
    input_path: str,
    extra: Optional[Dict[str, Any]] = None,
    **_ignored: Any,
) -> Optional[Dict[str, Any]]:
    """Copy the uploaded input into ``file/`` and append a record.

    Conversion outputs are intentionally not stored. Extra keyword args
    (e.g. legacy ``output_path``) are ignored for compatibility.

    Never raises to conversion callers — history failures must not break downloads.
    """
    try:
        return _archive_conversion(
            tool=tool,
            original_name=original_name,
            input_path=input_path,
            extra=extra,
        )
    except Exception:
        return None


def _archive_conversion(
    *,
    tool: str,
    original_name: str,
    input_path: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    src_in = Path(input_path)
    if not src_in.is_file():
        raise FileNotFoundError(input_path)

    now = _now()
    day = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y%m%dT%H%M%S")
    short = uuid.uuid4().hex[:6]
    uid = f"{stamp}_{short}"

    day_dir = ensure_file_dir() / day
    day_dir.mkdir(parents=True, exist_ok=True)

    in_name = _safe_name(original_name, "input")
    in_ext = Path(in_name).suffix or src_in.suffix
    stored_in = f"{uid}_in{in_ext}"
    dest_in = day_dir / stored_in
    shutil.copy2(src_in, dest_in)

    record: Dict[str, Any] = {
        "id": uid,
        "tool": tool,
        "original_name": original_name or in_name,
        "created_at": _iso(now),
        "input_rel": f"{day}/{stored_in}",
        "input_bytes": dest_in.stat().st_size,
    }
    if extra:
        for k, v in extra.items():
            if k in record:
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                record[k] = v
            else:
                record[k] = str(v)

    with _lock:
        records = _load_records()
        records.insert(0, record)
        cutoff = _cutoff()
        kept: List[Dict[str, Any]] = []
        for rec in records:
            if _is_expired(rec, cutoff):
                if rec is not record:
                    _remove_record_files(rec)
            else:
                kept.append(rec)
        _save_records(kept)

    try:
        cleanup_expired()
    except Exception:
        pass

    return record


def list_records(limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent records (newest first), purging expired first."""
    try:
        cleanup_expired()
    except Exception:
        pass
    with _lock:
        records = _load_records()
    limit = max(1, min(int(limit), 200))
    out: List[Dict[str, Any]] = []
    for rec in records[:limit]:
        item = dict(rec)
        rel = rec.get("input_rel")
        item["input_exists"] = bool(rel) and (FILE_DIR / str(rel)).is_file()
        out.append(item)
    return out


def resolve_stored(rel: str) -> Optional[Path]:
    """Resolve a relative stored path under ``file/`` safely."""
    if not rel:
        return None
    parts = Path(rel.replace("\\", "/")).parts
    if ".." in parts:
        return None
    candidate = (FILE_DIR / rel).resolve()
    try:
        candidate.relative_to(FILE_DIR.resolve())
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None
