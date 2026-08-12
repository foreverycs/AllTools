"""时间戳转换 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "timestamp",
    "name": "时间戳转换",
    "category": "text",
    "description": "Unix 时间戳（秒/毫秒）与日期时间互转，本地 / 北京 / UTC 三时区显示，自动识别输入。",
    "icon": "🕒",
    "route": "/tools/timestamp",
    "badge": "Unix ↔ 日期",
    "features": ["秒 / 毫秒", "自动识别", "三时区", "实时转换"],
    "cta": "打开工具",
    "accent": "amber",
    "order": 7,
}
