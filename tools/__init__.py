"""工具注册表：按分类组织，供首页与路由挂载。"""

from __future__ import annotations

from typing import Any, Dict, List

from .base64_tool import router as base64_router
from .code_format_tool import router as code_format_router
from .express_tool import router as express_router
from .image_compress_tool import router as image_compress_router
from .image_convert_tool import router as image_convert_router
from .image_grid_tool import router as image_grid_router
from .image_to_pdf_tool import router as image_to_pdf_router
from .json_tool import router as json_legacy_router
from .markdown_tool import router as markdown_router
from .pdf2word import router as pdf2word_router
from .pdf_merge import router as pdf_merge_router
from .qrcode_tool import router as qrcode_router
from .regex_tool import router as regex_router
from .rmb_tool import router as rmb_router
from .timestamp_tool import router as timestamp_router
from .unicode_tool import router as unicode_router
from .word2pdf import router as word2pdf_router

# ---------------------------------------------------------------------------
# Categories (order = homepage display order)
#
# These are the *default* categories. Admins may rename / reorder / delete /
# add categories and reassign tools via the admin console (persisted in
# ``file/tool_catalog.json``). See core/tool_catalog.py.
# ---------------------------------------------------------------------------
TOOL_CATEGORIES: List[Dict[str, Any]] = [
    {
        "id": "pdf",
        "name": "PDF 处理",
        "name_en": "PDF",
        "description": "PDF 与 Word 互转、合并与排版还原",
        "icon": "📕",
        "accent": "rose",
        "route": "/#col-pdf",
        "lead": "PDF ↔ Word 转换、发票合并与版式还原，适合日常办公文档处理。",
    },
    {
        "id": "image",
        "name": "图片处理",
        "name_en": "Image",
        "description": "图片压缩、格式转换、九宫格切图与图片转 PDF",
        "icon": "🖼️",
        "accent": "sky",
        "route": "/#col-image",
        "lead": "图片常用操作：压缩、格式互转、九宫格切图与批量转 PDF。",
    },
    {
        "id": "video",
        "name": "视频处理",
        "name_en": "Video",
        "description": "视频相关工具（可通过后台添加工具并归类到此）",
        "icon": "🎬",
        "accent": "violet",
        "route": "/#col-video",
        "lead": "视频处理工具。",
    },
    {
        "id": "audio",
        "name": "音频处理",
        "name_en": "Audio",
        "description": "音频相关工具（可通过后台添加工具并归类到此）",
        "icon": "🎵",
        "accent": "emerald",
        "route": "/#col-audio",
        "lead": "音频处理工具。",
    },
    {
        "id": "text",
        "name": "文本处理",
        "name_en": "Text",
        "description": "编解码、格式化、时间戳、正则与金额大写等文本工具",
        "icon": "✏️",
        "accent": "amber",
        "route": "/#col-text",
        "lead": "开发调试与日常文本处理：编解码、格式化、时间戳、正则、金额大写。",
    },
]

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
TOOL_REGISTRY: List[Dict[str, Any]] = [
    {
        "name": "PDF 转 Word",
        "slug": "pdf2word",
        "category": "pdf",
        "description": "纯文本 / 表格 PDF 转 Word：合并单元格、嵌套样式、图片嵌入、可选 OCR、批量 ZIP。",
        "icon": "📄",
        "route": "/tools/pdf2word",
        "badge": "PDF → Word",
        "features": ["合并单元格", "嵌套样式", "可选 OCR", "批量 ZIP"],
        "cta": "开始转换",
        "accent": "indigo",
    },
    {
        "name": "Word 转 PDF",
        "slug": "word2pdf",
        "category": "pdf",
        "description": "Word（.docx / .doc）转 PDF：LibreOffice 优先，Windows 可回退 Microsoft Word。",
        "icon": "📝",
        "route": "/tools/word2pdf",
        "badge": "Word → PDF",
        "features": ["LibreOffice", ".docx / .doc", "批量 ZIP", "引擎回退"],
        "cta": "开始转换",
        "accent": "emerald",
    },
    {
        "name": "发票合并",
        "slug": "pdf-merge",
        "category": "pdf",
        "description": "两张发票合并到一张 A4 纸：上下半页、中间分割线；页内预览并直接打印。",
        "icon": "🧾",
        "route": "/tools/pdf-merge",
        "badge": "2→1 A4",
        "features": ["A4 排版", "页内预览", "一键打印", "中间分割线"],
        "cta": "开始合并",
        "accent": "violet",
    },
    {
        "name": "Base64 编解码",
        "slug": "base64",
        "category": "text",
        "description": "文本 / 文件 Base64 编码与解码，支持标准与 URL-safe、换行折叠、多字符集。",
        "icon": "🔑",
        "route": "/tools/base64",
        "badge": "Encode · Decode",
        "features": ["标准 / URL-safe", "UTF-8 等", "文件编码", "一键复制"],
        "cta": "打开工具",
        "accent": "amber",
    },
    {
        "name": "中文 Unicode 还原",
        "slug": "unicode",
        "category": "text",
        "description": "将 \\uXXXX、U+XXXX、HTML 实体等 Unicode 转义还原为中文，也可反向编码。",
        "icon": "文",
        "route": "/tools/unicode",
        "badge": "\\u → 中文",
        "features": ["\\uXXXX 还原", "双重转义", "U+ / HTML", "反向编码"],
        "cta": "打开工具",
        "accent": "amber",
    },
    {
        "name": "代码格式化",
        "slug": "code-format",
        "category": "text",
        "description": "多语言代码美化 / 压缩（JSON、JS/TS、Python、HTML/CSS/XML、SQL、YAML 等），选项卡切换。",
        "icon": "🧰",
        "route": "/tools/code-format",
        "badge": "Multi-lang",
        "features": ["多语言选项卡", "美化 / 压缩", "JSON 键排序", "错误定位"],
        "cta": "打开工具",
        "accent": "amber",
    },
    {
        "name": "Markdown 编辑",
        "slug": "markdown",
        "category": "text",
        "description": "Markdown 左右分栏编辑与实时 HTML 预览，支持表格、代码块，可导出 HTML。",
        "icon": "📓",
        "route": "/tools/markdown",
        "badge": "Edit · Preview",
        "features": ["实时预览", "表格 / 代码块", "XSS 过滤", "导出 HTML"],
        "cta": "打开编辑器",
        "accent": "amber",
    },
    {
        "name": "时间戳转换",
        "slug": "timestamp",
        "category": "text",
        "description": "Unix 时间戳（秒/毫秒）与日期时间互转，本地 / 北京 / UTC 三时区显示，自动识别输入。",
        "icon": "🕒",
        "route": "/tools/timestamp",
        "badge": "Unix ↔ 日期",
        "features": ["秒 / 毫秒", "自动识别", "三时区", "实时转换"],
        "cta": "打开工具",
        "accent": "amber",
    },
    {
        "name": "正则测试",
        "slug": "regex",
        "category": "text",
        "description": "正则表达式匹配 / 捕获 / 替换测试：高亮命中位置、分组信息、常用标志。",
        "icon": "⌗",
        "route": "/tools/regex",
        "badge": "Pattern · 捕获",
        "features": ["匹配高亮", "捕获分组", "替换预览", "常用标志"],
        "cta": "打开工具",
        "accent": "amber",
    },
    {
        "name": "人民币大写",
        "slug": "rmb",
        "category": "text",
        "description": "阿拉伯数字金额转财务规范中文大写，支持角分、千分位与货币符号。",
        "icon": "¥",
        "route": "/tools/rmb",
        "badge": "数字 → 大写",
        "features": ["角分规范", "千分位清洗", "一键复制", "即时转换"],
        "cta": "打开工具",
        "accent": "emerald",
    },
    {
        "name": "二维码生成",
        "slug": "qrcode",
        "category": "text",
        "description": "为网址、文本、Wi-Fi 与邮件生成自定义二维码 PNG，可调整尺寸与容错级别。",
        "icon": "▤",
        "route": "/tools/qrcode",
        "badge": "URL · 文本 · Wi-Fi · 邮件",
        "features": ["四种内容", "自定义尺寸", "容错级别", "PNG 下载"],
        "cta": "开始生成",
        "accent": "amber",
    },
    {
        "name": "图片压缩",
        "slug": "image-compress",
        "category": "image",
        "description": "高观感压缩 JPEG / PNG / GIF / SVG：显著减小体积，尽量保持清晰与细节。",
        "icon": "📉",
        "route": "/tools/image-compress",
        "badge": "JPEG · PNG · GIF · SVG",
        "features": ["近无损观感", "多格式", "去元数据", "压缩对比"],
        "cta": "开始压缩",
        "accent": "violet",
    },
    {
        "name": "图片格式转换",
        "slug": "image-convert",
        "category": "image",
        "description": "JPEG / PNG / WebP / GIF / BMP / TIFF / ICO 互转：透明铺底、动图保留、质量可调。",
        "icon": "🔄",
        "route": "/tools/image-convert",
        "badge": "JPEG · PNG · WebP · …",
        "features": ["七种格式", "保留透明", "动图支持", "质量可调"],
        "cta": "开始转换",
        "accent": "sky",
    },
    {
        "name": "图片转 PDF",
        "slug": "image-to-pdf",
        "category": "image",
        "description": "多张图片合成一个 PDF：每图一页，可选原图像素尺寸或 A4 适配，自动校正 EXIF 方向。",
        "icon": "📑",
        "route": "/tools/image-to-pdf",
        "badge": "Image → PDF",
        "features": ["多图一 PDF", "原图/A4", "EXIF 校正", "本地处理"],
        "cta": "开始转换",
        "accent": "rose",
    },
    {
        "name": "图片九宫格",
        "slug": "image-grid",
        "category": "image",
        "description": "一张图切成 N×N 小块打包 ZIP：3×3 九宫格发朋友圈，按顺序发可无缝拼回原图。",
        "icon": "🔳",
        "route": "/tools/image-grid",
        "badge": "Grid Split",
        "features": ["3×3 九宫格", "自定义行列", "PNG/JPEG/WebP", "无缝拼接"],
        "cta": "开始分割",
        "accent": "indigo",
    },
    {
        "name": "文件快递",
        "slug": "express",
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
    },
]

# Routers to mount on the FastAPI app (order does not matter).
# code_format first; json_legacy only provides 308 redirects for old URLs.
TOOL_ROUTERS = (
    pdf2word_router,
    word2pdf_router,
    pdf_merge_router,
    qrcode_router,
    base64_router,
    code_format_router,
    json_legacy_router,
    markdown_router,
    unicode_router,
    rmb_router,
    regex_router,
    timestamp_router,
    image_compress_router,
    image_convert_router,
    image_to_pdf_router,
    image_grid_router,
    express_router,
)


def is_featured_tool(tool: Dict[str, Any] | None) -> bool:
    """True when a registry entry is a homepage feature (not a module card)."""
    if not tool:
        return False
    return bool(tool.get("featured"))


def enabled_tools(*, include_featured: bool = False) -> List[Dict[str, Any]]:
    """Public catalog: tools not disabled in admin flags.

    Featured tools (e.g. 文件快递) are omitted by default so module grids and
    category pages only show regular tools. Pass ``include_featured=True`` for
    counts / APIs that need the full public set.
    """
    from core.tool_flags import get_disabled_slugs

    disabled = get_disabled_slugs()
    out = []
    for t in TOOL_REGISTRY:
        if str(t.get("slug") or "") in disabled:
            continue
        if is_featured_tool(t) and not include_featured:
            continue
        out.append(t)
    return out


def featured_tools() -> List[Dict[str, Any]]:
    """Enabled tools marked ``featured=True`` (homepage highlight strip)."""
    from core.tool_flags import get_disabled_slugs

    disabled = get_disabled_slugs()
    return [
        t
        for t in TOOL_REGISTRY
        if is_featured_tool(t) and str(t.get("slug") or "") not in disabled
    ]


def tools_by_category(
    *, include_disabled: bool = False, include_featured: bool = False
) -> List[Dict[str, Any]]:
    """Return categories each with a ``tools`` list (only non-empty categories).

    Categories and tool→category placement honor admin customizations stored
    by ``core.tool_catalog`` (rename / reorder / add / delete / reassign).

    By default disabled and featured tools are omitted (homepage / public API).
    Pass ``include_disabled=True`` for the admin console (includes featured).
    Pass ``include_featured=True`` to place featured tools back under categories.
    """
    from core.tool_catalog import get_categories, get_tool_category

    if include_disabled:
        # Full registry for admin (featured tools stay under their category).
        source = list(TOOL_REGISTRY)
    else:
        source = enabled_tools(include_featured=include_featured)
    by_id = {c["id"]: {**c, "tools": []} for c in get_categories()}
    for tool in source:
        # Public category grids never list featured tools unless asked.
        if is_featured_tool(tool) and not include_disabled and not include_featured:
            continue
        cat_id = get_tool_category(tool.get("slug") or "") or ""
        cat = by_id.get(cat_id)
        if cat is not None:
            cat["tools"].append(tool)
        else:
            # Unknown category → attach under a synthetic bucket.
            other = by_id.setdefault(
                "_other",
                {
                    "id": "_other",
                    "name": "其他",
                    "name_en": "Other",
                    "description": "",
                    "icon": "🧩",
                    "accent": "slate",
                    "tools": [],
                },
            )
            other["tools"].append(tool)
    return [c for c in by_id.values() if c["tools"]]


def nav_categories(*, include_disabled: bool = False) -> List[Dict[str, Any]]:
    """Top-nav menu items (all registered categories, including empty)."""
    from core.tool_catalog import get_categories

    # Featured tools (e.g. 文件快递) are rendered as cards in the homepage grid
    # under their assigned category, so count them too. Otherwise a category
    # holding only a featured tool (via an admin assignment) would show a
    # misleading count of 0. Admin view already covers featured via the full
    # registry (include_disabled=True).
    by_id = {
        c["id"]: c
        for c in tools_by_category(
            include_disabled=include_disabled,
            include_featured=not include_disabled,
        )
    }
    items = []
    for c in get_categories():
        filled = by_id.get(c["id"])
        tools = list((filled or {}).get("tools") or [])
        items.append(
            {
                **c,
                "tool_count": len(tools),
                "tool_names": [t.get("name") for t in tools if t.get("name")],
                "tools": tools,
            }
        )
    return items


def get_tool_by_slug(slug: str) -> Dict[str, Any] | None:
    """Lookup a registry entry by slug (ignores enable flags)."""
    s = (slug or "").strip()
    if not s:
        return None
    for tool in TOOL_REGISTRY:
        if tool.get("slug") == s:
            return tool
    return None


# Public-catalog snapshot cache, keyed by the disabled-slug set (itself cached
# by tool_flags.json mtime). Rebuilt only when admin enable/disable changes, so
# hot paths (homepage / /api/tools / /health) avoid per-request list rebuilds.
_snap_cache: Optional[Dict[str, Any]] = None
_snap_key: Optional[frozenset[str]] = None


def public_snapshot() -> Dict[str, Any]:
    """Cached enabled/featured/categories/nav/catalog for public pages."""
    global _snap_cache, _snap_key
    from core.tool_flags import get_disabled_slugs
    from core.tool_catalog import catalog_revision
    from .common import build_tools_catalog

    disabled = get_disabled_slugs()
    snap_key = (disabled, catalog_revision())
    if _snap_cache is not None and _snap_key == snap_key:
        return _snap_cache

    public: List[Dict[str, Any]] = []
    featured: List[Dict[str, Any]] = []
    for t in TOOL_REGISTRY:
        if str(t.get("slug") or "") in disabled:
            continue
        if is_featured_tool(t):
            featured.append(t)
        else:
            public.append(t)

    _snap_cache = {
        "public": public,
        "featured": featured,
        "categories": tools_by_category(),
        "nav": nav_categories(),
        "catalog": build_tools_catalog(),
        "module_count": len(public),
        "featured_count": len(featured),
        "tool_count": len(public) + len(featured),
    }
    _snap_key = snap_key
    # Return shallow copies so callers cannot mutate the shared cache.
    return {
        k: (list(v) if isinstance(v, list) else v) for k, v in _snap_cache.items()
    }

def clear_public_snapshot() -> None:
    """Drop the catalog snapshot (tests / after external flag edits)."""
    global _snap_cache, _snap_key
    _snap_cache = None
    _snap_key = None


__all__ = [
    "TOOL_CATEGORIES",
    "TOOL_REGISTRY",
    "TOOL_ROUTERS",
    "is_featured_tool",
    "enabled_tools",
    "featured_tools",
    "tools_by_category",
    "nav_categories",
    "get_tool_by_slug",
    "public_snapshot",
    "clear_public_snapshot",
    "pdf2word_router",
    "word2pdf_router",
    "pdf_merge_router",
    "qrcode_router",
    "base64_router",
    "unicode_router",
    "regex_router",
    "code_format_router",
    "json_legacy_router",
    "markdown_router",
    "rmb_router",
    "timestamp_router",
    "image_compress_router",
    "image_convert_router",
    "image_to_pdf_router",
    "image_grid_router",
    "express_router",
]
