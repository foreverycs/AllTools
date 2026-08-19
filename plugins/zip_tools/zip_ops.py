"""ZIP pack / list / extract with zip-slip and zip-bomb guards."""

from __future__ import annotations

import os
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.errors import ValidationError
from core.filename import sanitize_filename

MAX_FILES = 100
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500 MB total expand
MAX_SINGLE_MEMBER = 200 * 1024 * 1024  # 200 MB per entry
MAX_LIST_ENTRIES = 5000
MAX_COMPRESSION_RATIO = 100.0  # compressed → uncompressed


def max_files() -> int:
    return MAX_FILES


def _safe_arcname(name: str, fallback: str = "file") -> str:
    """Normalize a name for use inside a zip (no absolute / parent paths)."""
    raw = (name or "").replace("\\", "/").strip()
    # Drop drive / leading slashes
    raw = raw.lstrip("/")
    if ":" in raw.split("/")[0]:
        raw = raw.split(":", 1)[-1].lstrip("/")
    parts = []
    for p in raw.split("/"):
        if p in ("", ".", ".."):
            continue
        parts.append(sanitize_filename(p, "part", stem_limit=80, ext_limit=20))
    if not parts:
        return sanitize_filename(fallback, "file", stem_limit=80, ext_limit=20)
    return "/".join(parts)


def pack_files(
    paths: Sequence[tuple[str, str]],
    out_path: str,
    *,
    compresslevel: int = 6,
) -> dict[str, Any]:
    """Pack ``(disk_path, arcname)`` pairs into ``out_path``.

    ``arcname`` is sanitized. Empty list raises ValidationError.
    """
    if not paths:
        raise ValidationError("没有可打包的文件。")
    if len(paths) > MAX_FILES:
        raise ValidationError(f"一次最多打包 {MAX_FILES} 个文件。")
    level = max(0, min(9, int(compresslevel)))
    total = 0
    written = 0
    compression = zipfile.ZIP_STORED if level == 0 else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(
        out_path, "w", compression=compression, compresslevel=level
    ) as zf:
        used: set = set()
        for disk, arc in paths:
            if not disk or not os.path.isfile(disk):
                continue
            name = _safe_arcname(arc, os.path.basename(disk) or "file")
            base = name
            n = 1
            while name.lower() in used:
                stem, ext = os.path.splitext(base)
                name = f"{stem}_{n}{ext}"
                n += 1
            used.add(name.lower())
            size = os.path.getsize(disk)
            total += size
            zf.write(disk, name)
            written += 1
    if written == 0:
        raise ValidationError("没有有效文件可打包。")
    out_size = os.path.getsize(out_path)
    return {
        "input_files": written,
        "input_bytes": total,
        "output_bytes": out_size,
        "compresslevel": level,
    }


def list_zip(path: str) -> dict[str, Any]:
    """Return directory listing for a zip without full extract."""
    if not os.path.isfile(path):
        raise ValidationError("ZIP 文件不存在。")
    try:
        with zipfile.ZipFile(path, "r") as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ValidationError("不是有效的 ZIP 文件。") from exc
    except Exception as exc:
        raise ValidationError(f"无法读取 ZIP：{exc}") from exc

    if len(infos) > MAX_LIST_ENTRIES:
        raise ValidationError(
            f"压缩包条目过多（{len(infos)} > {MAX_LIST_ENTRIES}），拒绝预览。"
        )

    entries: list[dict[str, Any]] = []
    total_uncomp = 0
    total_comp = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        is_dir = name.endswith("/") or info.is_dir()
        uncomp = int(info.file_size or 0)
        comp = int(info.compress_size or 0)
        total_uncomp += uncomp
        total_comp += comp
        entries.append(
            {
                "name": name,
                "is_dir": bool(is_dir),
                "size": uncomp,
                "compressed_size": comp,
                "date_time": list(info.date_time) if info.date_time else None,
            }
        )
    return {
        "entries": entries,
        "count": len(entries),
        "total_uncompressed": total_uncomp,
        "total_compressed": total_comp,
        "comment": "",
    }


def _check_member_safe(info: zipfile.ZipInfo, dest_root: Path) -> Path:
    name = info.filename.replace("\\", "/")
    if name.startswith("/") or name.startswith("../") or "/../" in f"/{name}/":
        raise ValidationError(f"不安全的路径（zip-slip）：{name!r}")
    # Resolve under dest
    target = (dest_root / name).resolve()
    try:
        target.relative_to(dest_root.resolve())
    except ValueError as exc:
        raise ValidationError(f"不安全的路径（zip-slip）：{name!r}") from exc
    if int(info.file_size or 0) > MAX_SINGLE_MEMBER:
        raise ValidationError(
            f"条目过大：{name}（>{MAX_SINGLE_MEMBER // (1024 * 1024)} MB）"
        )
    return target


def extract_zip(
    path: str,
    out_dir: str,
    *,
    members: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Safely extract zip into ``out_dir``; returns list of written relative paths."""
    dest_root = Path(out_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    try:
        zf = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as exc:
        raise ValidationError("不是有效的 ZIP 文件。") from exc

    written: list[str] = []
    total = 0
    try:
        infos = zf.infolist()
        if len(infos) > MAX_LIST_ENTRIES:
            raise ValidationError("压缩包条目过多，拒绝解压。")
        want = None
        if members:
            want = {m.replace("\\", "/") for m in members if m}

        for info in infos:
            name = info.filename.replace("\\", "/")
            if info.is_dir() or name.endswith("/"):
                continue
            if want is not None and name not in want:
                continue
            target = _check_member_safe(info, dest_root)
            # Zip bomb: running uncompressed total
            uncomp = int(info.file_size or 0)
            total += uncomp
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ValidationError(
                    f"解压后体积超过上限（{MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB）。"
                )
            comp = max(1, int(info.compress_size or 1))
            if uncomp / comp > MAX_COMPRESSION_RATIO and uncomp > 10 * 1024 * 1024:
                raise ValidationError(
                    f"可疑压缩比，拒绝解压：{name}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                remaining = uncomp if uncomp > 0 else MAX_SINGLE_MEMBER
                chunk = 1024 * 1024
                written_bytes = 0
                while True:
                    buf = src.read(min(chunk, max(1, remaining - written_bytes + chunk)))
                    if not buf:
                        break
                    written_bytes += len(buf)
                    if written_bytes > MAX_SINGLE_MEMBER:
                        raise ValidationError(f"条目写出超过上限：{name}")
                    dst.write(buf)
            rel = str(target.relative_to(dest_root.resolve())).replace("\\", "/")
            written.append(rel)
            if len(written) > MAX_FILES * 5:
                raise ValidationError("解压文件数过多。")
    finally:
        zf.close()

    if not written:
        raise ValidationError("压缩包内没有可解压的文件。")
    return {
        "output_files": len(written),
        "files": written,
        "uncompressed_bytes": total,
    }


__all__ = [
    "MAX_FILES",
    "pack_files",
    "list_zip",
    "extract_zip",
    "max_files",
]
