"""中文 Unicode 还原 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "unicode",
    "name": "中文 Unicode 还原",
    "category": "text",
    "description": "将 \\uXXXX、U+XXXX、HTML 实体等 Unicode 转义还原为中文，也可反向编码。",
    "icon": "文",
    "route": "/tools/unicode",
    "badge": "\\u → 中文",
    "features": ["\\uXXXX 还原", "双重转义", "U+ / HTML", "反向编码"],
    "cta": "打开工具",
    "accent": "amber",
    "order": 4,
}
