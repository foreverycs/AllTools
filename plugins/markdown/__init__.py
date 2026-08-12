"""Markdown 编辑 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "markdown",
    "name": "Markdown 编辑",
    "category": "text",
    "description": "Markdown 左右分栏编辑与实时 HTML 预览，支持表格、代码块，可导出 HTML。",
    "icon": "📓",
    "route": "/tools/markdown",
    "badge": "Edit · Preview",
    "features": ["实时预览", "表格 / 代码块", "XSS 过滤", "导出 HTML"],
    "cta": "打开编辑器",
    "accent": "amber",
    "order": 6,
}
