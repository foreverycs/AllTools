"""MD 转公众号 — 插件。

Markdown 一键排版微信公众号文章：Python-Markdown 渲染 → 净化 → 主题 CSS
内联（css_inliner）→ 可选 Pygments 代码高亮 → 输出可直接粘贴到公众号
富文本编辑器的 HTML（样式 100% 保留，无 <style> / CSS 变量 / 伪元素依赖）。

端点：
- GET  /tools/md-weixin             工具页面
- POST /tools/md-weixin/render      渲染为内联样式 HTML 片段（JSON）
- POST /tools/md-weixin/export      导出独立 HTML 文档（下载）
- POST /tools/md-weixin/compress    图片上传 → base64 嵌入（含 /compress 标记，限流）
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.requests import Request

from core.concurrency import run_heavy
from tools.common import (
    check_upload_size_header,
    content_disposition,
    templates,
    with_nav,
)

from .core import (
    MAX_INPUT_CHARS,
    WeixinError,
    export_weixin_document,
    export_weixin_html,
    sample_markdown,
)
from .themes import theme_options

PLUGIN_VERSION = "1.0.0"

TOOL = {
    "slug": "md-weixin",
    "name": "MD 转公众号",
    "name_en": "MD to WeChat",
    "category": "text",
    "description": "Markdown 一键排版微信公众号文章：多套主题、代码高亮、图片嵌入，一键复制内联样式 HTML。",
    "icon": "📰",
    "route": "/tools/md-weixin",
    "badge": "MD → 公众号",
    "features": ["5 套主题", "代码高亮", "图片嵌入", "一键复制 HTML"],
    "cta": "开始排版",
    "accent": "emerald",
    "order": 7,
}

router = APIRouter(prefix="/tools/md-weixin", tags=["md-weixin"])

# 图片嵌入上限（压缩后 base64 会膨胀约 33%，限制原始体积）。
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
# 超过该体积的位图自动压缩（对齐 md-to-weixin 的 800KB 阈值）。
_COMPRESS_THRESHOLD = 800 * 1024
# 嵌入图片的最长边上限。
_MAX_IMAGE_SIDE = 1600

_MIME_BY_FORMAT = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


class ImageTooLarge(ValueError):
    """Raised when an uploaded image exceeds the app pixel cap (→ 413)."""


def _process_upload(data: bytes, filename: str) -> dict:
    """Validate / downscale / compress an image → base64 data URI.

    Module-level so it can run under ``run_heavy``. Pixel count is bounded
    *before* full decode (decompression-bomb guard). Non-browser formats
    (bmp/tiff/ico) are converted to PNG; oversized JPEG/PNG are re-encoded
    (GIF/WebP are kept as-is to preserve animation/encoding).
    """
    from PIL import Image, ImageOps

    from tools.common import MAX_IMAGE_PIXELS, detect_image_format

    fmt = detect_image_format(data, filename)
    im = Image.open(BytesIO(data))
    if im.width * im.height > MAX_IMAGE_PIXELS:
        raise ImageTooLarge(
            f"图片过大（{im.width}×{im.height} 像素，上限 "
            f"{MAX_IMAGE_PIXELS}），请压缩后重试"
        )
    im.load()
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass

    original_bytes = len(data)
    orig_width, orig_height = im.size
    need_reencode = fmt not in _MIME_BY_FORMAT or (
        original_bytes > _COMPRESS_THRESHOLD and fmt in ("jpeg", "png")
    )
    if need_reencode:
        if max(im.size) > _MAX_IMAGE_SIDE:
            im.thumbnail((_MAX_IMAGE_SIDE, _MAX_IMAGE_SIDE), Image.LANCZOS)
        buf = BytesIO()
        if fmt not in _MIME_BY_FORMAT:
            # 非公众号常用格式 → 转 PNG（保留透明）。
            out_fmt = "png"
            mime = "image/png"
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            im.save(buf, "PNG", optimize=True)
        elif im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            out_fmt = "png"
            mime = "image/png"
            im.save(buf, "PNG", optimize=True)
        else:
            # 不透明大图 → JPEG，MIME 与实际字节保持一致。
            out_fmt = "jpeg"
            mime = "image/jpeg"
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.save(buf, "JPEG", quality=82, optimize=True)
        data = buf.getvalue()
        width, height = im.size
    else:
        out_fmt = fmt
        mime = _MIME_BY_FORMAT.get(fmt, "image/png")
        width, height = orig_width, orig_height

    b64 = base64.b64encode(data).decode("ascii")
    return {
        "data_uri": f"data:{mime};base64,{b64}",
        "format": out_fmt,
        "width": width,
        "height": height,
        "bytes": len(data),
        "original_bytes": original_bytes,
    }


def _check_text(text: Optional[str]) -> None:
    if text is None:
        raise HTTPException(status_code=400, detail="请输入 Markdown")
    if len(text) > MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"输入过长（最多 {MAX_INPUT_CHARS} 字符）",
        )


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/md-weixin.html",
        with_nav({
            "tool": {
                "name": "MD 转公众号",
                "slug": "md-weixin",
                "category": "text",
            },
            "themes": theme_options(),
            "sample": sample_markdown(),
            "max_chars": MAX_INPUT_CHARS,
        }),
    )


@router.post("/render")
async def api_render(
    text: Optional[str] = Form(None),
    theme: str = Form("default"),
):
    """Render Markdown → inline-styled HTML fragment (JSON).

    渲染较重（Markdown + 净化 + CSS 内联 + Pygments），放到线程池执行，
    避免阻塞事件循环。
    """
    _check_text(text)
    try:
        data = await run_heavy(export_weixin_html, text, theme_id=theme)
    except WeixinError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(data)


@router.post("/export")
async def api_export(
    text: Optional[str] = Form(None),
    theme: str = Form("default"),
    title: str = Form("文章"),
):
    """Download a standalone HTML document with the inlined fragment."""
    _check_text(text)
    try:
        doc = await run_heavy(
            export_weixin_document, text, theme_id=theme, title=title
        )
    except WeixinError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PlainTextResponse(
        doc,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": content_disposition("weixin-export.html"),
        },
    )


@router.post("/compress")
async def api_upload_image(file: Optional[UploadFile] = File(None)):
    """Upload an image → base64 data URI for embedding in the article.

    Path contains ``/compress`` ⇒ rate-limited (see PublicRateLimitMiddleware).
    """
    if file is None or not file.filename:
        raise HTTPException(status_code=400, detail="请选择要上传的图片")
    check_upload_size_header(file, max_bytes=MAX_UPLOAD_BYTES)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"图片过大（最多 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB）",
        )
    try:
        result = await run_heavy(
            _process_upload,
            raw,
            file.filename or "image.png",
            file_size=len(raw),
        )
    except HTTPException:
        raise
    except ImageTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail="图片处理失败") from exc
    return JSONResponse(result)

@router.get("/themes")
async def api_themes():
    return JSONResponse({"themes": theme_options(), "default": "default"})
