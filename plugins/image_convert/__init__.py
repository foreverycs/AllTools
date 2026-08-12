"""图片格式转换 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "image-convert",
    "name": "图片格式转换",
    "category": "image",
    "description": "JPEG / PNG / WebP / GIF / BMP / TIFF / ICO 互转：透明铺底、动图保留、质量可调。",
    "icon": "🔄",
    "route": "/tools/image-convert",
    "badge": "JPEG · PNG · WebP · …",
    "features": ["七种格式", "保留透明", "动图支持", "质量可调"],
    "cta": "开始转换",
    "accent": "sky",
    "order": 12,
}
