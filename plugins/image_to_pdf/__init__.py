"""图片转 PDF — 内置工具迁移为插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "image-to-pdf",
    "name": "图片转 PDF",
    "category": "image",
    "description": "多张图片合成一个 PDF：每图一页，可选原图像素尺寸或 A4 适配，自动校正 EXIF 方向。",
    "icon": "📑",
    "route": "/tools/image-to-pdf",
    "badge": "Image → PDF",
    "features": ["多图一 PDF", "原图/A4", "EXIF 校正", "本地处理"],
    "cta": "开始转换",
    "accent": "rose",
    "order": 13,
}
