"""PDF 工具集 — 拆分 / 合并 / 解密 / 抽页 插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "pdf-tools",
    "name": "PDF 工具集",
    "category": "pdf",
    "description": "PDF 常用操作一站式：按页拆分打包 ZIP、多文件合并、移除密码解密、指定页码抽取。",
    "icon": "✂️",
    "route": "/tools/pdf-tools",
    "badge": "Split · Merge · Decrypt",
    "features": ["按页拆分", "多 PDF 合并", "移除密码", "抽页"],
    "cta": "开始处理",
    "accent": "amber",
    "order": 4,
}
