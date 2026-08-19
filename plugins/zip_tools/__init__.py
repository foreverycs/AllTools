"""ZIP 工具 — 打包 / 解压预览 / 解压下载。"""

from __future__ import annotations

from .router import router as router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "zip-tools",
    "name": "ZIP 工具",
    "category": "text",
    "description": "多文件打包 ZIP、查看压缩包目录、安全解压下载（防路径穿越与炸弹）。",
    "icon": "📁",
    "route": "/tools/zip-tools",
    "badge": "Pack · List · Extract",
    "features": ["多文件打包", "目录预览", "安全解压", "压缩级别"],
    "cta": "开始处理",
    "accent": "amber",
    "order": 40,
}
