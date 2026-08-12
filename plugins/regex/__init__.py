"""正则测试 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "regex",
    "name": "正则测试",
    "category": "text",
    "description": "正则表达式匹配 / 捕获 / 替换测试：高亮命中位置、分组信息、常用标志。",
    "icon": "⌗",
    "route": "/tools/regex",
    "badge": "Pattern · 捕获",
    "features": ["匹配高亮", "捕获分组", "替换预览", "常用标志"],
    "cta": "打开工具",
    "accent": "amber",
    "order": 8,
}
