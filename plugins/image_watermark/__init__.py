"""图片加水印 — 文字 / Logo 水印插件。"""

from __future__ import annotations

from .router import router

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "image-watermark",
    "name": "图片加水印",
    "category": "image",
    "description": "为图片添加文字或 Logo 水印：可调透明度、颜色、旋转角度、位置，支持斜向平铺铺满全图。",
    "icon": "💧",
    "route": "/tools/image-watermark",
    "badge": "Text · Logo",
    "features": ["文字水印", "Logo 水印", "透明度调节", "平铺模式"],
    "cta": "开始添加",
    "accent": "cyan",
    "order": 15,
}
