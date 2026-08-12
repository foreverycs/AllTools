"""Base64 编解码 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "base64",
    "name": "Base64 编解码",
    "category": "text",
    "description": "文本 / 文件 Base64 编码与解码，支持标准与 URL-safe、换行折叠、多字符集。",
    "icon": "🔑",
    "route": "/tools/base64",
    "badge": "Encode · Decode",
    "features": ["标准 / URL-safe", "UTF-8 等", "文件编码", "一键复制"],
    "cta": "打开工具",
    "accent": "amber",
    "order": 3,
}
