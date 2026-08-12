"""发票合并 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "pdf-merge",
    "name": "发票合并",
    "category": "pdf",
    "description": "两张发票合并到一张 A4 纸：上下半页、中间分割线；页内预览并直接打印。",
    "icon": "🧾",
    "route": "/tools/pdf-merge",
    "badge": "2→1 A4",
    "features": ["A4 排版", "页内预览", "一键打印", "中间分割线"],
    "cta": "开始合并",
    "accent": "violet",
    "order": 2,
}
