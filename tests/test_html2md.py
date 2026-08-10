"""HTML → Markdown plugin: converter unit tests + app end-to-end."""

from __future__ import annotations

import pytest

from plugins.html2md.converter import convert_html


def md(html, **kw):
    return convert_html(html, **kw)["result"]


def test_headings_and_paragraphs():
    assert md("<h1>大标题</h1><p>正文</p><h2>小节</h2>") == "# 大标题\n\n正文\n\n## 小节"


def test_inline_marks():
    out = md("<p>a <strong>b</strong> <em>c</em> <del>d</del> <code>e</code></p>")
    assert out == "a **b** *c* ~~d~~ `e`"


def test_link_and_image():
    out = md(
        '<p><a href="https://x.com/a">链接</a> <img src="/img/p.png" alt="图"></p>'
    )
    assert out == "[链接](https://x.com/a) ![图](/img/p.png)"


def test_base_url_resolves_relative():
    out = md(
        '<a href="docs/guide.html">指南</a> <img src="img/a.png" alt="a">',
        base_url="https://example.com/base/",
    )
    assert out == (
        "[指南](https://example.com/base/docs/guide.html) "
        "![a](https://example.com/base/img/a.png)"
    )


def test_dangerous_href_dropped():
    out = md('<p><a href="javascript:alert(1)">bad</a> ok</p>')
    assert out == "bad ok"


def test_nested_lists():
    out = md("<ul><li>A</li><li>B<ul><li>B1</li><li>B2</li></ul></li><li>C</li></ul>")
    assert out == "- A\n- B\n  - B1\n  - B2\n- C"


def test_ordered_lists_numbered():
    out = md("<ol><li>第一</li><li>第二<ol><li>内1</li></ol></li></ol>")
    assert out == "1. 第一\n2. 第二\n  1. 内1"


def test_code_block_with_language():
    out = md('<pre><code class="language-python">def f():\n    return 1</code></pre>')
    assert out == "```python\ndef f():\n    return 1\n```"


def test_code_block_plain():
    out = md("<pre><code>x &amp; y</code></pre>")
    assert out == "```\nx & y\n```"


def test_table_gfm():
    out = md(
        "<table><tr><th>名字</th><th>类型</th></tr>"
        "<tr><td>html2md</td><td>插件</td></tr></table>"
    )
    assert out == "| 名字 | 类型 |\n| --- | --- |\n| html2md | 插件 |"


def test_table_pipe_escaped():
    out = md("<table><tr><td>a|b</td><td>c</td></tr></table>")
    assert "a\\|b" in out


def test_table_whitespace_between_cells():
    """Newlines/spaces between tags must not produce empty cells."""
    out = md(
        "<table>\n<thead>\n<tr>\n<th>名字</th>\n<th>类型</th>\n</tr>\n</thead>\n"
        "<tbody>\n<tr>\n<td>a</td>\n<td>b</td>\n</tr>\n</tbody>\n</table>"
    )
    assert out == "| 名字 | 类型 |\n| --- | --- |\n| a | b |"


def test_tables_disabled():
    out = md("<table><tr><td>x</td></tr></table>", tables=False)
    assert "x" in out and "|" not in out


def test_blockquote():
    out = md("<blockquote><p>第一段</p><p>第二段</p></blockquote>")
    assert out == "> 第一段\n> 第二段"


def test_entities_decoded():
    out = md("<p>AT&amp;T &lt;b&gt; &#169;</p>")
    assert out == "AT&T <b> ©"


def test_script_style_skipped():
    out = md("<p>可见</p><script>alert(1)</script><style>.a{}</style><p>仍可见</p>")
    assert out == "可见\n\n仍可见"


def test_hr_and_br():
    out = md("<p>第一行<br>第二行</p><hr><p>之后</p>")
    assert out == "第一行\n第二行\n\n---\n\n之后"


def test_link_containing_image():
    out = md('<a href="https://x.com/"><img src="https://x.com/i.png" alt="图"></a>')
    assert out == "[![图](https://x.com/i.png)](https://x.com/)"


def test_convert_stats():
    data = convert_html("<h1>t</h1><ul><li>a</li></ul>")
    assert data["input_chars"] > 0
    assert data["output_chars"] > 0
    assert data["blocks"]["headings"] == 1
    assert data["blocks"]["lists"] == 1
    assert data["blocks"]["list_items"] == 1


def test_empty_input():
    data = convert_html("")
    assert data["result"] == ""
    assert data["blocks"]["headings"] == 0


def test_registry_and_end_to_end():
    """The plugin is discovered from the real plugins dir at app import."""
    from fastapi.testclient import TestClient

    from tools import get_tool_by_slug

    from app import app

    tool = get_tool_by_slug("html2md")
    assert tool is not None
    assert tool["route"] == "/tools/html2md"

    client = TestClient(app)
    page = client.get("/tools/html2md")
    assert page.status_code == 200
    assert "HTML 转 Markdown" in page.text

    r = client.post(
        "/tools/html2md/convert",
        data={"html": "<h1>hi</h1><p>text</p>", "tables": "1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "# hi\n\ntext"
    assert body["blocks"]["headings"] == 1
    # Preview: the Markdown is rendered to sanitized HTML server-side.
    assert body["rendered"] is not None
    assert "<h1" in body["rendered"] and "hi</h1>" in body["rendered"]
    assert "<p>text</p>" in body["rendered"]

    # XSS in source HTML never reaches the rendered preview.
    evil = client.post(
        "/tools/html2md/convert",
        data={
            "html": "<h1>ok</h1><script>alert(1)</script>"
            '<img src=x onerror="alert(2)">'
        },
    )
    assert evil.status_code == 200
    eb = evil.json()
    assert "<script>" not in eb["rendered"]
    assert "onerror" not in eb["rendered"]

    # Missing input → 400.
    bad = client.post("/tools/html2md/convert", data={})
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# URL fetching (SSRF guards + main-content extraction + /convert-url).
# ---------------------------------------------------------------------------

def test_check_url_rejects_private_and_non_http():
    from plugins.html2md.fetcher import FetchError, check_url

    for bad in (
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://10.0.0.5/x",
        "http://172.16.0.1/x",
        "http://192.168.1.1/x",
        "http://169.254.1.1/x",
        "http://[::1]/x",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "",
    ):
        with pytest.raises(FetchError):
            check_url(bad)

    assert check_url("https://www.cnblogs.com/zzaz/p/22367216.html")


def test_main_content_extraction():
    from plugins.html2md.fetcher import _main_content

    page = """<html><head><title>t</title></head><body>
    <div id="header"><h1>站名</h1><ul><li>首页</li><li>关于</li></ul></div>
    <div id="cnblogs_post_body">
      <h2>正文标题</h2>
      <p>第一段内容，包含一些文字。</p>
      <p>第二段内容，继续。</p>
      <pre><code>print(1)</code></pre>
    </div>
    <div id="comments">评论列表评论列表评论列表</div>
    <div id="footer">版权所有</div>
    </body></html>"""
    main, extracted = _main_content(page)
    assert extracted is True
    assert "正文标题" in main
    assert "第一段内容" in main
    assert "首页" not in main
    assert "版权所有" not in main


def test_main_content_falls_back_to_whole_page():
    from plugins.html2md.fetcher import _main_content

    page = "<html><body><p>没有正文容器的短页面。</p></body></html>"
    main, extracted = _main_content(page)
    assert extracted is False
    assert "没有正文容器" in main


def test_fetch_page_with_mock_transport(monkeypatch):
    import httpx

    from plugins.html2md import fetcher

    page_html = """<html><head><title>示例页</title></head><body>
      <div id="nav">导航</div>
      <div id="post-content">
        <h1>标题</h1>
        <p>第一段：这是正文内容的第一段文字，用于模拟真实文章。</p>
        <p>第二段：正文通常包含多段文字，例如这段继续说明问题。</p>
        <p>第三段：足够长的内容才能让容器评分超过提取阈值。</p>
      </div>
      <div id="footer">页脚</div>
    </body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=page_html,
            request=request,
        )

    monkeypatch.setattr(
        fetcher,
        "_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    import asyncio

    page = asyncio.run(
        fetcher.fetch_page("https://example.com/post/1")
    )
    assert page["title"] == "示例页"
    assert page["main"] is True
    assert "标题" in page["html"]
    assert "导航" not in page["html"]


def test_convert_url_endpoint(monkeypatch):
    """/convert-url runs fetch → convert and exposes the captured HTML."""
    from fastapi.testclient import TestClient

    import plugins.html2md as plugin_mod
    from plugins.html2md.fetcher import FetchError

    async def fake_fetch(url):
        if "private" in url:
            raise FetchError("blocked address")
        return {
            "url": url,
            "title": "测试页面",
            "html": '<div id="post-content"><h1>抓取标题</h1><p>抓取段落。</p></div>',
            "main": True,
        }

    monkeypatch.setattr(plugin_mod, "fetch_page", fake_fetch)

    from app import app

    client = TestClient(app)
    r = client.post(
        "/tools/html2md/convert-url",
        data={"url": "https://example.com/post/1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["page_title"] == "测试页面"
    assert body["page_html"].startswith("<div")
    assert body["result"] == "# 抓取标题\n\n抓取段落。"
    assert body["rendered"] is not None
    assert body["main_extracted"] is True

    blocked = client.post(
        "/tools/html2md/convert-url",
        data={"url": "http://private.example/x"},
    )
    assert blocked.status_code == 400
    assert "blocked" in blocked.json()["detail"].lower()

    missing = client.post("/tools/html2md/convert-url", data={})
    assert missing.status_code == 400
