"""Word 转 PDF — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "word2pdf",
    "name": "Word 转 PDF",
    "category": "pdf",
    "description": "Word（.docx / .doc）转 PDF：LibreOffice 优先，Windows 可回退 Microsoft Word。",
    "icon": "📝",
    "route": "/tools/word2pdf",
    "badge": "Word → PDF",
    "features": ["LibreOffice", ".docx / .doc", "批量 ZIP", "引擎回退"],
    "cta": "开始转换",
    "accent": "emerald",
    "order": 1,
}
