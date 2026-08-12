"""文件快递 — 内置工具迁移为插件（特色工具，主页高亮）。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "express",
    "name": "文件快递",
    "category": "text",
    # Featured: shown as a homepage highlight, not listed under module grids.
    "featured": True,
    "description": "上传文件生成 6 位取件码，对方输入取件码即可下载；可设有效期与下载次数。",
    "icon": "📦",
    "route": "/tools/express",
    "badge": "特色 · 取件码分享",
    "features": ["6 位取件码", "有效期", "下载次数", "一键复制"],
    "cta": "开始寄送",
    "accent": "indigo",
    "lead": "临时传文件无需账号：生成取件码，对方输入即可下载。",
    "order": 15,
}
