"""人民币大写 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "rmb",
    "name": "人民币大写",
    "category": "text",
    "description": "阿拉伯数字金额转财务规范中文大写，支持角分、千分位与货币符号。",
    "icon": "¥",
    "route": "/tools/rmb",
    "badge": "数字 → 大写",
    "features": ["角分规范", "千分位清洗", "一键复制", "即时转换"],
    "cta": "打开工具",
    "accent": "emerald",
    "order": 9,
}
