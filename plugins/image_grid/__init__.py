"""图片九宫格 — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "image-grid",
    "name": "图片九宫格",
    "category": "image",
    "description": "一张图切成 N×N 小块打包 ZIP：3×3 九宫格发朋友圈，按顺序发可无缝拼回原图。",
    "icon": "🔳",
    "route": "/tools/image-grid",
    "badge": "Grid Split",
    "features": ["3×3 九宫格", "自定义行列", "PNG/JPEG/WebP", "无缝拼接"],
    "cta": "开始分割",
    "accent": "indigo",
    "order": 14,
}
