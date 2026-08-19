"""PDF 转图片 — 按页导出 PNG / JPEG 并打包 ZIP。"""

from __future__ import annotations

from .router import router as router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "pdf-to-images",
    "name": "PDF 转图片",
    "category": "pdf",
    "description": "将 PDF 每一页渲染为 PNG 或 JPEG，可指定页码与分辨率，结果打包 ZIP 下载。",
    "icon": "🖼️",
    "route": "/tools/pdf-to-images",
    "badge": "PDF → PNG / JPEG",
    "features": ["按页导出", "DPI 可选", "页码范围", "ZIP 打包"],
    "cta": "开始转换",
    "accent": "sky",
    "order": 6,
}
