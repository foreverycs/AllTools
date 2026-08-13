"""MD → 微信公众号内联样式 HTML — 转换核心。

流程（对应 md-to-weixin 的 htmlExporter，服务端 Python 实现）：

1. Python-Markdown 渲染（复用 ``coding.markdown_render`` 的净化管线，
   XSS 安全、外部链接加 nofollow / target=_blank）；
2. 包裹到 ``<div class="weixin-article">`` 容器；
3. 主题 CSS 由 ``themes.py`` 的字面量变量展开（不用 ``var()``）；
4. 可选 Pygments 代码高亮（内联颜色，无额外 CSS）；
5. 主题 CSS 内联到每个元素的 ``style`` 属性（``css_inliner``）；
6. 后处理：清理空 ``style=""``、兜底图片宽度安全。

净化在注入样式**之前**完成：用户输入里的 ``<script>`` / ``onerror`` /
内联样式先被去掉，随后注入的样式全部来自受信主题模板，公众号粘贴时
样式 100% 保留（无 ``<style>`` 标签、无 CSS 变量、无伪元素依赖）。
"""

from __future__ import annotations

import html as html_mod
import re
from typing import Any, Dict, Optional

from coding.markdown_render import MAX_INPUT_CHARS, render_markdown

from .css_inliner import _pygments_available, inline_css
from .themes import get_theme


class WeixinError(ValueError):
    """Raised when input is invalid or too large."""


def build_theme_css(theme: Dict[str, object]) -> str:
    """Expand a theme's literal CSS variables into a plain CSS string.

    不使用 CSS 变量 ``var()``（公众号富文本编辑器不支持），直接展开字面量。
    """
    v = theme["vars"]

    def c(key: str) -> str:
        return str(v.get(key, ""))

    return f"""
.weixin-article {{
  background-color: {c('--body-bg')};
  padding: {c('--body-padding')};
  color: {c('--text-color')};
  font-size: {c('--text-size')};
  line-height: {c('--line-height')};
  font-family: {c('--font-family')};
  word-break: break-word;
  -webkit-text-size-adjust: 100%;
}}
.weixin-article p {{
  margin: 0 0 1em 0;
  color: {c('--text-color')};
  font-size: {c('--text-size')};
  line-height: {c('--line-height')};
}}
.weixin-article h1 {{
  font-size: {c('--h1-size')};
  color: {c('--h1-color')};
  font-weight: {c('--h1-weight')};
  margin: 1.4em 0 0.6em 0;
  padding-bottom: {c('--h1-padding-bottom')};
  border-bottom: {c('--h1-border-bottom')};
  line-height: 1.4;
}}
.weixin-article h2 {{
  font-size: {c('--h2-size')};
  color: {c('--h2-color')};
  font-weight: {c('--h2-weight')};
  margin: 1.3em 0 0.5em 0;
  padding-left: {c('--h2-padding-left')};
  border-left: {c('--h2-border-left')};
  line-height: 1.4;
}}
.weixin-article h3 {{
  font-size: {c('--h3-size')};
  color: {c('--h3-color')};
  font-weight: {c('--h3-weight')};
  margin: 1.2em 0 0.4em 0;
  line-height: 1.4;
}}
.weixin-article h4, .weixin-article h5, .weixin-article h6 {{
  font-size: 15px;
  color: {c('--h3-color')};
  font-weight: 700;
  margin: 1em 0 0.4em 0;
}}
.weixin-article strong, .weixin-article b {{
  color: {c('--strong-color')};
  font-weight: 700;
}}
.weixin-article em, .weixin-article i {{
  color: {c('--em-color')};
  font-style: italic;
}}
.weixin-article a {{
  color: {c('--a-color')};
  text-decoration: none;
  border-bottom: 1px solid {c('--a-color')};
}}
.weixin-article blockquote {{
  background: {c('--blockquote-bg')};
  border-left: 4px solid {c('--blockquote-border')};
  color: {c('--blockquote-color')};
  margin: 1em 0;
  padding: 12px 16px;
  border-radius: 0 4px 4px 0;
  font-style: italic;
}}
.weixin-article blockquote p {{
  margin: 0;
  color: {c('--blockquote-color')};
}}
.weixin-article code {{
  background: {c('--code-bg')};
  color: {c('--code-color')};
  border: 1px solid {c('--code-border')};
  border-radius: 3px;
  padding: 2px 6px;
  font-size: 13px;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
}}
.weixin-article pre {{
  background: {c('--pre-bg')};
  color: {c('--pre-color')};
  padding: 16px;
  border-radius: 6px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  margin: 1em 0;
  font-size: 13px;
  line-height: 1.6;
}}
.weixin-article pre code {{
  background: transparent;
  color: inherit;
  border: none;
  padding: 0;
  font-size: inherit;
}}
.weixin-article table {{
  width: 100%;
  border-collapse: collapse;
  margin: 1em 0;
  font-size: 14px;
  overflow: hidden;
  border-radius: 6px;
}}
.weixin-article th {{
  background: {c('--table-header-bg')};
  color: {c('--table-header-color')};
  padding: 10px 14px;
  text-align: left;
  font-weight: 700;
  border: 1px solid {c('--table-border')};
}}
.weixin-article td {{
  padding: 9px 14px;
  border: 1px solid {c('--table-border')};
  color: {c('--text-color')};
}}
.weixin-article tr:nth-child(even) td {{
  background: {c('--table-row-even')};
}}
.weixin-article ul, .weixin-article ol {{
  padding-left: 2em;
  margin: 0.5em 0 1em 0;
}}
.weixin-article li {{
  margin: 4px 0;
  color: {c('--text-color')};
  line-height: {c('--line-height')};
}}
.weixin-article img {{
  max-width: 100%;
  height: auto;
  display: block;
  margin: 12px auto;
  border-radius: 4px;
}}
.weixin-article hr {{
  border: none;
  border-top: 1px solid {c('--hr-color')};
  margin: 1.5em 0;
}}
"""


_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def _ensure_img_widths(html: str) -> str:
    """兜底：任何 ``<img>`` 都带上宽度安全样式（主题 CSS 已覆盖，双保险）。"""

    def repl(match: "re.Match[str]") -> str:
        tag = match.group(0)
        if "max-width" in tag:
            return tag
        safe = 'style="max-width:100%;height:auto;display:block;margin:12px auto;"'
        if tag.rstrip().endswith("/>"):
            idx = tag.rfind("/>")
            return tag[:idx] + " " + safe + "/>"
        return tag[:-1] + " " + safe + ">"

    return _IMG_RE.sub(repl, html)


def _count_stats(text: str, code_blocks: int) -> Dict[str, int]:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z0-9_]+", text))
    return {
        "chars": len(text),
        "words": cjk + latin,
        "lines": len(text.splitlines()) if text else 0,
        "code_blocks": code_blocks,
    }


def export_weixin_html(
    text: Optional[str],
    theme_id: str = "default",
) -> Dict[str, Any]:
    """    Convert Markdown to a WeChat-ready inline-styled HTML fragment.

    Returns ``{"html", "theme_id", "theme_name", "highlight", **stats}``
    where ``html`` is the ``<div class="weixin-article">`` fragment ready to
    paste into the 公众号 rich-text editor.
    """
    if text is None:
        raise WeixinError("请输入 Markdown")
    if len(text) > MAX_INPUT_CHARS:
        raise WeixinError(f"输入过长（最多 {MAX_INPUT_CHARS} 字符）")

    theme = get_theme(theme_id)
    base = render_markdown(text or "", sanitize=True)["html"]
    wrapped = f'<div class="weixin-article">{base}</div>'
    css = build_theme_css(theme)
    inlined = inline_css(
        wrapped,
        css,
        highlight_code=True,
        pygments_style=str(theme.get("pygments_style") or "default"),
    )
    inlined = _ensure_img_widths(inlined)

    return {
        "html": inlined,
        "theme_id": str(theme["id"]),
        "theme_name": str(theme["name"]),
        "highlight": bool(_pygments_available()),
        **_count_stats(text, len(re.findall(r"<pre\b", inlined))),
    }


def export_weixin_document(
    text: Optional[str],
    theme_id: str = "default",
    title: str = "文章",
) -> str:
    """Standalone HTML document (for download) with the inlined fragment."""
    data = export_weixin_html(text, theme_id)
    safe_title = html_mod.escape((title or "文章").strip()[:120] or "文章")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{safe_title}</title>
</head>
<body style="margin:0;background:#eceff1;">
{data["html"]}
</body>
</html>
"""


def sample_markdown() -> str:
    """Demo document showcasing the WeChat themes."""
    return """# 公众号文章排版示例

欢迎使用 **MD 转公众号**。左侧书写 Markdown，右侧实时预览公众号效果，
一键复制即可粘贴到公众号后台，**样式 100% 保留**。

## 段落与强调

这是一段普通正文，支持 **加粗**、*斜体*、`行内代码` 与 [链接](https://example.com)。

> 引用块：排版优雅，重点突出，适合摘要、金句或注意事项。

## 列表

- 无序列表第一项
- 第二项
  - 嵌套子项
- 第三项

1. 有序列表第一项
2. 有序列表第二项

## 代码块

```python
def hello(name: str) -> str:
    \"\"\"Say hello to someone.\"\"\"
    return f\"Hello, {name}!\"
```

## 表格

| 主题 | 风格 | 适合场景 |
|------|------|----------|
| 微信默认 | 简洁经典 | 通用文章 |
| 科技蓝 | 深色代码块 | 技术教程 |
| 文艺清新 | 暖色衬线 | 读书笔记 |

---

任务清单：

- [x] 主题切换
- [x] 代码高亮
- [ ] 一键复制到公众号
"""
