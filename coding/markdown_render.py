"""Markdown → HTML rendering with XSS-safe sanitization."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import bleach
import markdown as md_lib
from bleach.css_sanitizer import CSSSanitizer

MAX_INPUT_CHARS = 512 * 1024  # 512K chars — enough for long notes

# CommonMark-ish + GFM-friendly extensions bundled with Python-Markdown.
# Keep the set dependency-light (no Pygments required).
_EXTENSIONS = [
    "markdown.extensions.fenced_code",
    "markdown.extensions.tables",
    "markdown.extensions.nl2br",
    "markdown.extensions.sane_lists",
    "markdown.extensions.toc",
    "markdown.extensions.smarty",
]

_EXTENSION_CONFIGS = {
    "markdown.extensions.toc": {
        "permalink": False,
    },
}

# Tags / attributes allowed after bleach (subset of safe HTML from MD).
_ALLOWED_TAGS: List[str] = list(
    bleach.sanitizer.ALLOWED_TAGS
) + [
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "br",
    "hr",
    "pre",
    "code",
    "blockquote",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "img",
    "span",
    "div",
    "dl",
    "dt",
    "dd",
    "sup",
    "sub",
    "del",
    "ins",
    "kbd",
    "samp",
    "var",
    "details",
    "summary",
]

_ALLOWED_ATTRIBUTES: Dict[str, List[str]] = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "a": ["href", "title", "rel", "name", "id"],
    "img": ["src", "alt", "title", "width", "height"],
    "code": ["class"],
    "pre": ["class"],
    "span": ["class"],
    "div": ["class", "id"],
    "th": ["align", "colspan", "rowspan"],
    "td": ["align", "colspan", "rowspan"],
    "h1": ["id"],
    "h2": ["id"],
    "h3": ["id"],
    "h4": ["id"],
    "h5": ["id"],
    "h6": ["id"],
    "table": ["class"],
}

_ALLOWED_PROTOCOLS = ["http", "https", "mailto", "data"]

_CSS_SANITIZER = CSSSanitizer(
    allowed_css_properties=[
        "color",
        "background-color",
        "font-weight",
        "font-style",
        "text-align",
        "text-decoration",
    ]
)


class MarkdownError(ValueError):
    """Raised when Markdown input is invalid or too large."""


def _count_stats(text: str) -> Dict[str, int]:
    lines = text.splitlines()
    # Rough word count: CJK chars + latin words.
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_words = len(re.findall(r"[A-Za-z0-9_]+", text))
    return {
        "chars": len(text),
        "chars_no_ws": len(re.sub(r"\s+", "", text)),
        "lines": len(lines) if text else 0,
        "words": cjk + latin_words,
    }


def render_markdown(
    text: str,
    *,
    sanitize: bool = True,
    extensions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Convert Markdown source to HTML.

    Parameters
    ----------
    text:
        Markdown source.
    sanitize:
        When True (default), strip dangerous HTML via bleach.
    extensions:
        Optional override list of Python-Markdown extension names.
    """
    if text is None:
        raise MarkdownError("请输入 Markdown")
    if len(text) > MAX_INPUT_CHARS:
        raise MarkdownError(f"输入过长（最多 {MAX_INPUT_CHARS} 字符）")

    ext = list(extensions) if extensions is not None else list(_EXTENSIONS)
    try:
        html = md_lib.markdown(
            text,
            extensions=ext,
            extension_configs=_EXTENSION_CONFIGS,
            output_format="html",
        )
    except Exception as exc:
        raise MarkdownError(f"Markdown 渲染失败：{exc}") from exc

    sanitized = False
    if sanitize:
        html = bleach.clean(
            html,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRIBUTES,
            protocols=_ALLOWED_PROTOCOLS,
            css_sanitizer=_CSS_SANITIZER,
            strip=True,
        )
        # Soften external links a bit when present.
        html = bleach.linkify(
            html,
            callbacks=[bleach.callbacks.nofollow, bleach.callbacks.target_blank],
            skip_tags=["pre", "code"],
            parse_email=False,
        )
        sanitized = True

    stats = _count_stats(text)
    return {
        "html": html,
        "sanitized": sanitized,
        "extensions": [e.split(".")[-1] for e in ext],
        **stats,
    }


def sample_markdown() -> str:
    """Demo document for the editor."""
    return """# Markdown 预览

欢迎使用 **Markdown** 编辑器。左侧书写，右侧实时渲染。

## 常用语法

| 语法 | 效果 |
|------|------|
| `**粗体**` | **粗体** |
| `*斜体*` | *斜体* |
| `` `代码` `` | `代码` |
| `[链接](url)` | [示例链接](https://example.com) |

### 列表

- 无序一项
- 另一项
  - 嵌套

1. 有序第一
2. 有序第二

### 引用与代码

> 引用文字：简洁、可读、可移植。

```python
def hello(name: str) -> str:
    return f"Hello, {name}!"
```

---

任务清单：

- [x] 标题与段落
- [x] 表格
- [ ] 导出 HTML
"""
