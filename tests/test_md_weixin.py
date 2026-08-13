"""MD 转公众号插件：css_inliner 单元测试 + 转换核心 + 端到端 API。"""

from __future__ import annotations

import importlib
import io

import pytest

# 插件目录名为 md-weixin（含连字符），与 core.plugins 一致地用 importlib 加载。
_core = importlib.import_module("plugins.md-weixin.core")
_inliner = importlib.import_module("plugins.md-weixin.css_inliner")
_themes = importlib.import_module("plugins.md-weixin.themes")

inline_css = _inliner.inline_css
WeixinError = _core.WeixinError
build_theme_css = _core.build_theme_css
export_weixin_document = _core.export_weixin_document
export_weixin_html = _core.export_weixin_html
sample_markdown = _core.sample_markdown
THEMES = _themes.THEMES

# ---------------------------------------------------------------------------
# css_inliner
# ---------------------------------------------------------------------------

CSS = """
.wrap p { color: #333333; font-size: 15px; }
.wrap h1 { font-size: 24px; color: #1a1a1a; }
h1 { color: red; }
.wrap tr:nth-child(even) td { background: #f9f9f9; }
.wrap .tone { font-weight: 700; }
"""


def test_inline_basic():
    out = inline_css('<div class="wrap"><p>正文</p></div>', CSS, highlight_code=False)
    assert '<p style="color: #333333; font-size: 15px;">正文</p>' in out


def test_inline_specificity_beats_source_order():
    """`.wrap h1` (higher specificity) wins over `h1` regardless of order."""
    out = inline_css('<div class="wrap"><h1>标题</h1></div>', CSS, highlight_code=False)
    assert "font-size: 24px" in out
    assert "color: #1a1a1a" in out
    assert "color: red" not in out
    assert "font-size: 15px" not in out


def test_inline_nth_child_even():
    html = (
        '<div class="wrap"><table><tbody>'
        "<tr><td>1</td></tr><tr><td>2</td></tr></tbody></table></div>"
    )
    out = inline_css(html, CSS, highlight_code=False)
    assert out.count("background: #f9f9f9") == 1
    assert "background: #f9f9f9" in out


def test_inline_selector_list_and_class():
    out = inline_css(
        '<div class="wrap"><em>斜</em><i>斜2</i><span class="tone">重</span></div>',
        CSS,
        highlight_code=False,
    )
    assert '<em style="font-style: italic;">' not in out  # .wrap em 未定义 → 无样式
    assert 'class="tone" style="font-weight: 700;"' in out


def test_inline_escapes_text_and_attr():
    css = ".a b { color: red; }"
    out = inline_css('<div class="a"><b>1 &lt; 2</b></div>', css, highlight_code=False)
    assert "1 &lt; 2" in out and 'style="color: red;"' in out


def test_inline_unsupported_selector_ignored():
    css = ".a + .b { color: red; } .c { color: blue; }"
    out = inline_css('<div class="a"></div><div class="b">x</div><div class="c">y</div>', css, highlight_code=False)
    assert "color: red" not in out
    assert '<div class="c" style="color: blue;">y</div>' in out


def test_inline_existing_style_wins():
    """User inline style (allowed by sanitizer) wins over rules (CSS cascade)."""
    css = ".a p { color: #333333; }"
    out = inline_css(
        '<div class="a"><p style="color: #ff0000">x</p></div>',
        css,
        highlight_code=False,
    )
    assert "color: #ff0000" in out and "#333333" not in out


def test_inline_code_highlight_pygments():
    html = '<pre><code class="language-python">import os</code></pre>'
    out = inline_css(html, ".x pre { background: #000; }", highlight_code=True)
    assert '<span style="color:' in out
    # pygments 转义代码内容，仍保持安全
    assert "import" in out


def test_inline_code_no_pygments_fallback(monkeypatch):
    monkeypatch.setattr(_inliner, "_pygments_available", lambda: False)
    html = '<pre><code class="language-python">import os</code></pre>'
    out = inline_css(html, ".x pre { background: #000; }", highlight_code=True)
    assert "import" in out and '<span style="color:' not in out


# ---------------------------------------------------------------------------
# 转换核心
# ---------------------------------------------------------------------------

MD = """# 标题

正文 **加粗** 和 `代码`。

```python
print("hi")
```

| a | b |
|---|---|
| 1 | 2 |
"""


def test_theme_css_no_var_no_style_tag():
    for theme in THEMES:
        css = build_theme_css(theme)
        assert "var(--" not in css
        assert "<style>" not in css
        assert "weixin-article" in css


def test_export_returns_wrapped_inline_html():
    data = export_weixin_html(MD, theme_id="default")
    html = data["html"]
    assert html.startswith('<div class="weixin-article"')
    assert 'style="' in html
    assert "<style>" not in html
    assert data["theme_id"] == "default"
    assert data["theme_name"] == "微信默认"
    assert data["chars"] > 0
    assert data["code_blocks"] == 1
    # 表格偶数行 + 表头样式已内联
    assert "background:" in html
    # 所有图片都有宽度安全样式
    assert 'max-width:100%' in html or "<img" not in html


def test_export_xss_sanitized():
    data = export_weixin_html('<script>alert(1)</script>\n\n# ok\n\n<img src=x onerror="alert(2)">')
    html = data["html"]
    assert "<script" not in html
    assert "onerror" not in html
    assert "<img src=x" not in html  # 危险属性被剥离
    assert 'src="x"' in html  # 图片标签本身保留，事件处理器被删除


def test_export_highlight_on():
    data = export_weixin_html(MD, theme_id="tech-blue")
    assert data["highlight"] is True
    assert '<span style="color:' in data["html"]


def test_export_document_standalone():
    doc = export_weixin_document(MD, theme_id="literary", title="我的文章")
    assert "<!DOCTYPE html>" in doc
    assert "<title>我的文章</title>" in doc
    assert "weixin-article" in doc
    # 标题被转义
    doc2 = export_weixin_document("# x", title='a<b>c')
    assert "a&lt;b&gt;c" in doc2


def test_export_empty():
    data = export_weixin_html("")
    assert data["html"].startswith('<div class="weixin-article"')
    assert data["chars"] == 0


def test_export_too_large():
    with pytest.raises(WeixinError):
        export_weixin_html("x" * (512 * 1024 + 1))


def test_unknown_theme_falls_back():
    data = export_weixin_html("# t", theme_id="nope")
    assert data["theme_id"] == "default"


def test_sample_markdown_renders():
    data = export_weixin_html(sample_markdown())
    assert data["html"].startswith('<div class="weixin-article"')
    assert data["code_blocks"] >= 1


# ---------------------------------------------------------------------------
# 注册与端到端 API
# ---------------------------------------------------------------------------

def test_registry_and_end_to_end():
    from fastapi.testclient import TestClient

    from tools import get_tool_by_slug

    from app import app

    tool = get_tool_by_slug("md-weixin")
    assert tool is not None
    assert tool["route"] == "/tools/md-weixin"
    assert tool["category"] == "text"

    client = TestClient(app)
    page = client.get("/tools/md-weixin")
    assert page.status_code == 200
    assert "MD 转公众号" in page.text
    assert "微信默认" in page.text

    r = client.post("/tools/md-weixin/render", data={"text": "# 你好\n\n正文", "theme": "default"})
    assert r.status_code == 200
    body = r.json()
    assert body["html"].startswith('<div class="weixin-article"')
    assert "你好" in body["html"]
    assert body["theme_name"] == "微信默认"

    # 主题切换
    r2 = client.post("/tools/md-weixin/render", data={"text": "# 你好", "theme": "dark"})
    assert r2.status_code == 200
    assert "暗黑酷炫" == r2.json()["theme_name"]

    # XSS 不进入输出
    evil = client.post(
        "/tools/md-weixin/render",
        data={"text": "# ok\n\n<script>alert(1)</script>"},
    )
    assert evil.status_code == 200
    assert "<script>" not in evil.json()["html"]

    # 缺少输入 → 400
    bad = client.post("/tools/md-weixin/render", data={"theme": "default"})
    assert bad.status_code == 400

    # 导出文档
    exp = client.post(
        "/tools/md-weixin/export",
        data={"text": "# 标题", "theme": "default", "title": "示例"},
    )
    assert exp.status_code == 200
    assert "<!DOCTYPE html>" in exp.text
    assert "weixin-article" in exp.text
    assert 'filename="weixin-export.html"' in exp.headers.get("content-disposition", "")

    # 主题列表
    th = client.get("/tools/md-weixin/themes")
    assert th.status_code == 200
    assert len(th.json()["themes"]) == 5


def test_inline_code_keeps_element_children():
    """Raw HTML inside <code> must survive highlighting (no data loss)."""
    out = inline_css(
        "<pre><code><b>keep me</b></code></pre>",
        ".x pre { background: #000; }",
        highlight_code=True,
    )
    assert "<b>keep me</b>" in out
    assert '<span style="color:' not in out


def test_upload_image_embed(monkeypatch):
    from fastapi.testclient import TestClient

    from app import app

    buf = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (64, 64), (7, 193, 96)).save(buf, "PNG")
    png = buf.getvalue()

    client = TestClient(app)
    r = client.post(
        "/tools/md-weixin/compress",
        files={"file": ("pic.png", png, "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["data_uri"].startswith("data:image/png;base64,")
    assert body["format"] == "png"
    assert body["width"] == 64 and body["height"] == 64
    assert body["bytes"] > 0

    # 非图片 → 400
    bad = client.post(
        "/tools/md-weixin/compress",
        files={"file": ("a.txt", b"not an image", "text/plain")},
    )
    assert bad.status_code == 400

    # 缺文件 → 400
    empty = client.post("/tools/md-weixin/compress")
    assert empty.status_code == 400


def _make_png(width, height, *, pixels=None):
    import os
    import struct
    import zlib

    from PIL import Image

    if pixels is not None:
        im = Image.frombytes("RGB", (width, height), pixels)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    def chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


def test_upload_opaque_png_reencoded_as_jpeg():
    """大体积不透明 PNG 压缩为 JPEG 时，MIME 与字节必须一致。"""
    import base64
    import os

    from fastapi.testclient import TestClient

    from app import app

    raw = os.urandom(900 * 900 * 3)
    png = _make_png(900, 900, pixels=raw)
    assert len(png) > 800 * 1024  # 确保超过压缩阈值

    client = TestClient(app)
    r = client.post(
        "/tools/md-weixin/compress",
        files={"file": ("big.png", png, "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["data_uri"].startswith("data:image/jpeg;base64,")
    assert body["format"] == "jpeg"
    head = base64.b64decode(body["data_uri"].split(",", 1)[1])[:3]
    assert head == b"\xff\xd8\xff"  # JPEG magic


def test_upload_pixel_bomb_rejected():
    """声明超大尺寸的 PNG（未到 Pillow 硬上限）必须在解码前被拒绝。"""
    import warnings

    from fastapi.testclient import TestClient

    from PIL import Image

    from app import app

    client = TestClient(app)
    with warnings.catch_warnings():
        # PIL 只警告不报错的炸弹尺寸（<178M 像素），由本插件在解码前拦截。
        warnings.simplefilter("ignore", category=Image.DecompressionBombWarning)
        r = client.post(
            "/tools/md-weixin/compress",
            files={"file": ("bomb.png", _make_png(10000, 10000), "image/png")},
        )
    assert r.status_code == 413
    assert "像素" in r.json()["detail"]


def test_upload_wide_gif_reports_original_dims():
    """保留原字节的 GIF 返回真实尺寸（缩略图不改写数据）。"""
    from fastapi.testclient import TestClient

    from PIL import Image

    from app import app

    buf = io.BytesIO()
    Image.new("RGB", (2000, 10), (1, 2, 3)).save(buf, "GIF")
    gif = buf.getvalue()

    client = TestClient(app)
    r = client.post(
        "/tools/md-weixin/compress",
        files={"file": ("w.gif", gif, "image/gif")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "gif"
    assert body["width"] == 2000 and body["height"] == 10
    assert body["data_uri"].startswith("data:image/gif;base64,")
