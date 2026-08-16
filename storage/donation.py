"""打赏（赞助）配置与二维码存储。

配置持久化为 JSON（``file/donation.json``），二维码图片存放于
``file/donation/``（上传后统一重编码为 PNG，便于前台直接以固定
``image/png`` 提供）。

默认关闭且无二维码；仅当 ``enabled`` 为真且存在二维码图片时，前台
页面底部才会渲染打赏模块。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image

logger = logging.getLogger("toolkit.donation")

CONFIG_FILENAME = "donation.json"
QR_FILENAME = "qr.png"
_defaults = {
    "version": 1,
    "enabled": False,
    "title": "赞助支持",
    "subtitle": "如果工具集帮到了你，欢迎打赏支持一下，感谢！",
}

_lock = threading.RLock()

# In-memory cache: (config_path_str, mtime_ns, cfg_dict) — busted on config writes.
_cache: tuple | None = None


def donation_dir() -> Path:
    from storage.history import file_dir

    return file_dir() / "donation"


def ensure_donation_dir() -> Path:
    path = donation_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    from storage.history import file_dir

    return file_dir() / CONFIG_FILENAME


def qr_path() -> Path:
    return donation_dir() / QR_FILENAME


def _read_config() -> Dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return dict(_defaults)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("donation: failed to read %s: %s", path, exc)
        return dict(_defaults)
    if not isinstance(data, dict):
        return dict(_defaults)
    cfg = dict(_defaults)
    cfg.update({k: v for k, v in data.items() if k in _defaults and v is not None})
    return cfg


def _cache_key() -> tuple[str, int]:
    path = config_path()
    try:
        st = path.stat()
        return str(path.resolve()), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except OSError:
        return str(path), -1


def _cached_config() -> Dict[str, Any]:
    """Config read with an mtime-based cache (footer renders on every page)."""
    global _cache
    key = _cache_key()
    with _lock:
        if _cache is not None and _cache[0] == key[0] and _cache[1] == key[1]:
            return dict(_cache[2])
        cfg = _read_config()
        _cache = (key[0], key[1], cfg)
        return dict(cfg)


def _write_config(cfg: Dict[str, Any]) -> Path:
    global _cache
    path = config_path()
    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    _cache = None
    return path


def get_config() -> Dict[str, Any]:
    """完整配置，含二维码文件是否存在及路径（供后台）。"""
    with _lock:
        cfg = _cached_config()
    qr = qr_path()
    cfg["has_qr"] = qr.is_file()
    cfg["qr_path"] = str(qr) if qr.is_file() else None
    return cfg


def donation_public() -> Dict[str, Any]:
    """前台可见的打赏模块（禁用或无码时返回 ``{enabled: False}``）。"""
    cfg = get_config()
    if not cfg.get("enabled") or not cfg.get("has_qr"):
        return {"enabled": False}
    return {
        "enabled": True,
        "title": (cfg.get("title") or _defaults["title"]).strip(),
        "subtitle": (cfg.get("subtitle") or "").strip(),
        "qr_url": "/donation/qr",
    }


def save_config(*, enabled: bool, title: str = "", subtitle: str = "") -> Path:
    """持久化开关与文案（不影响已上传的二维码）。"""
    with _lock:
        cfg = _read_config()
        cfg["enabled"] = bool(enabled)
        title = (title or "").strip()
        subtitle = (subtitle or "").strip()
        if title:
            cfg["title"] = title[:60]
        if subtitle is not None:
            cfg["subtitle"] = subtitle[:200]
        cfg["updated_at"] = _iso_now()
        return _write_config(cfg)


def save_qr_image(data: bytes) -> Optional[str]:
    """校验并保存二维码（重编码为 PNG）。返回 None 或错误提示。

    仅在图片可被 Pillow 解码时成功；非图片 / 损坏图片会被拒绝，避免
    前台展示任意用户文件（潜在的存储型 XSS / 资源滥用）。
    """
    if not data:
        return "请选择图片文件"
    try:
        with Image.open(BytesIO(data)) as im:
            im.verify()
        with Image.open(BytesIO(data)) as im:
            # Bound decoded pixels so a decompression bomb cannot OOM the
            # admin process; QR codes are small (typically < 4K px total).
            if (im.size[0] or 0) * (im.size[1] or 0) > 16_000_000:
                return "图片尺寸过大，请使用 4000×4000 以内的二维码图片"
            im.load()
            # 规范为 RGB/RGBA → PNG，统一前台 Content-Type。
            out = BytesIO()
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            im.save(out, format="PNG")
            payload = out.getvalue()
    except Exception as exc:
        logger.warning("donation: invalid QR image: %s", exc)
        return "无效的图片文件，请上传 PNG / JPEG 二维码图片"

    qr = qr_path()
    tmp = qr.with_suffix(qr.suffix + ".tmp")
    with _lock:
        ensure_donation_dir()
        tmp.write_bytes(payload)
        tmp.replace(qr)
    logger.info("donation QR saved bytes=%s", len(payload))
    return None


def remove_qr_image() -> None:
    """删除已上传的二维码图片（保留配置与开关状态）。"""
    qr = qr_path()
    with _lock:
        try:
            if qr.is_file():
                qr.unlink()
        except OSError:
            logger.warning("donation: failed to remove %s", qr)


def qr_media_type() -> str:
    return "image/png"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "donation_dir",
    "ensure_donation_dir",
    "get_config",
    "donation_public",
    "save_config",
    "save_qr_image",
    "remove_qr_image",
    "qr_path",
    "qr_media_type",
]
