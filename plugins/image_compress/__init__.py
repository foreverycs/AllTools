"""图片压缩 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "image-compress",
    "name": "图片压缩",
    "category": "image",
    "description": "高观感压缩 JPEG / PNG / GIF / SVG：显著减小体积，尽量保持清晰与细节。",
    "icon": "📉",
    "route": "/tools/image-compress",
    "badge": "JPEG · PNG · GIF · SVG",
    "features": ["近无损观感", "多格式", "去元数据", "压缩对比"],
    "cta": "开始压缩",
    "accent": "violet",
    "order": 11,
}
