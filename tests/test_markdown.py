"""Tests for Markdown render core and HTTP tool."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coding import MarkdownError, render_markdown, sample_markdown
from coding.markdown_render import MAX_INPUT_CHARS


def test_render_basic_and_table():
    src = "# 标题\n\n**粗体** 与 *斜体*\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    out = render_markdown(src)
    assert out["sanitized"] is True
    assert "<h1" in out["html"]
    assert "<strong>" in out["html"] or "<strong" in out["html"]
    assert "<table>" in out["html"]
    assert out["chars"] == len(src)
    assert out["lines"] >= 5


def test_render_fenced_code():
    src = "```python\nprint('hi')\n```\n"
    out = render_markdown(src)
    assert "<pre>" in out["html"]
    assert "print" in out["html"]


def test_render_strips_script_xss():
    src = '<script>alert(1)</script>\n\n**safe**\n<img src=x onerror="alert(1)">'
    out = render_markdown(src)
    html = out["html"].lower()
    assert "<script" not in html
    assert "onerror" not in html
    assert "safe" in out["html"] or "<strong>" in out["html"]


def test_render_empty_ok():
    out = render_markdown("")
    assert out["html"] == ""
    assert out["chars"] == 0
    assert out["lines"] == 0


def test_render_too_large():
    with pytest.raises(MarkdownError):
        render_markdown("x" * (MAX_INPUT_CHARS + 1))


def test_sample_markdown_renders():
    sample = sample_markdown()
    out = render_markdown(sample)
    assert "Markdown" in out["html"] or "markdown" in out["html"].lower()
    assert out["words"] > 0


def test_http_page():
    from app import app

    client = TestClient(app)
    r = client.get("/tools/markdown")
    assert r.status_code == 200
    assert "Markdown" in r.text
    assert "preview" in r.text.lower() or "预览" in r.text


def test_http_render_api():
    from app import app

    client = TestClient(app)
    r = client.post(
        "/tools/markdown/render",
        data={"text": "## Hello\n\n- item", "sanitize": "true"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "<h2" in body["html"]
    assert body["sanitized"] is True
    assert body["lines"] >= 2


def test_http_export_html():
    from app import app

    client = TestClient(app)
    r = client.post(
        "/tools/markdown/export-html",
        data={"text": "# 导出\n\n内容", "title": "Demo"},
    )
    assert r.status_code == 200
    assert "text/html" in (r.headers.get("content-type") or "")
    assert "<!DOCTYPE html>" in r.text
    assert "导出" in r.text
    assert "Demo" in r.text


def test_http_sample():
    from app import app

    client = TestClient(app)
    r = client.get("/tools/markdown/sample")
    assert r.status_code == 200
    assert "text" in r.json()
    assert len(r.json()["text"]) > 20


def test_registry_and_category():
    from tools import TOOL_REGISTRY

    slugs = {t["slug"] for t in TOOL_REGISTRY}
    assert "markdown" in slugs
    tool = next(t for t in TOOL_REGISTRY if t["slug"] == "markdown")
    assert tool["category"] == "coding"
    assert tool["route"] == "/tools/markdown"

    from app import app

    client = TestClient(app)
    r = client.get("/c/coding")
    assert r.status_code == 200
    assert "Markdown" in r.text
    assert "/tools/markdown" in r.text
