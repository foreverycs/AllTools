"""二维码生成 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "qrcode",
    "name": "二维码生成",
    "category": "text",
    "description": "为网址、文本、Wi-Fi 与邮件生成自定义二维码 PNG，可调整尺寸与容错级别。",
    "icon": "▤",
    "route": "/tools/qrcode",
    "badge": "URL · 文本 · Wi-Fi · 邮件",
    "features": ["四种内容", "自定义尺寸", "容错级别", "PNG 下载"],
    "cta": "开始生成",
    "accent": "amber",
    "order": 10,
}
