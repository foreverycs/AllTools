"""PDF 压缩 — 去冗余 / 重采样图片。"""

from __future__ import annotations

from .router import router as router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "pdf-compress",
    "name": "PDF 压缩",
    "category": "pdf",
    "description": "压缩 PDF 体积：清理冗余对象、可选重采样嵌入图片，尽量保持可读性。",
    "icon": "🗜️",
    "route": "/tools/pdf-compress",
    "badge": "Compress · Slim",
    "features": ["轻量清理", "图片重采样", "三档强度", "压缩对比"],
    "cta": "开始压缩",
    "accent": "rose",
    "order": 5,
}
