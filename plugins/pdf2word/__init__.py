"""PDF 转 Word — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "pdf2word",
    "name": "PDF 转 Word",
    "category": "pdf",
    "description": "纯文本 / 表格 PDF 转 Word：合并单元格、嵌套样式、图片嵌入、可选 OCR、批量 ZIP。",
    "icon": "📄",
    "route": "/tools/pdf2word",
    "badge": "PDF → Word",
    "features": ["合并单元格", "嵌套样式", "可选 OCR", "批量 ZIP"],
    "cta": "开始转换",
    "accent": "indigo",
    "order": 0,
}
