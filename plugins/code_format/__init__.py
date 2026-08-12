"""代码格式化 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "code-format",
    "name": "代码格式化",
    "category": "text",
    "description": "多语言代码美化 / 压缩（JSON、JS/TS、Python、HTML/CSS/XML、SQL、YAML 等），选项卡切换。",
    "icon": "🧰",
    "route": "/tools/code-format",
    "badge": "Multi-lang",
    "features": ["多语言选项卡", "美化 / 压缩", "JSON 键排序", "错误定位"],
    "cta": "打开工具",
    "accent": "amber",
    "order": 5,
}
