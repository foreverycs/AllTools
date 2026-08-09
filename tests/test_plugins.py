"""Plugin discovery and integration."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from core.plugins import discover_plugins

GOOD_PLUGIN = """\
from fastapi import APIRouter

PLUGIN_VERSION = "2.0"
TOOL = {"slug": "t-plug", "name": "测试插件", "category": "text", "description": "d"}
router = APIRouter(prefix="/tools/t-plug", tags=["t-plug"])
"""


@pytest.fixture()
def plugin_base(tmp_path, monkeypatch):
    """A temp ``plugins`` package so temp plugins import via ``plugins.<name>``."""
    base = tmp_path / "plugins"
    base.mkdir()
    fake = types.ModuleType("plugins")
    fake.__path__ = [str(base)]
    monkeypatch.setitem(sys.modules, "plugins", fake)
    monkeypatch.setenv("PLUGINS_DIR", str(base))
    return base


def _write_plugin(base: Path, name: str, source: str, *, static: bool = False):
    d = base / name
    d.mkdir()
    (d / "__init__.py").write_text(source, encoding="utf-8")
    if static:
        (d / "static").mkdir()
        (d / "static" / "x.js").write_text("", encoding="utf-8")
    return d


def test_discover_loads_valid_plugin(plugin_base):
    _write_plugin(plugin_base, "good", GOOD_PLUGIN)
    out = discover_plugins(reserved_slugs={"pdf2word"}, base=plugin_base)
    assert [e["slug"] for e in out.entries] == ["t-plug"]
    assert out.routers
    # ``route`` falls back to /tools/<slug> when the manifest omits it.
    assert out.entries[0]["name"] == "测试插件"
    assert out.entries[0]["route"] == "/tools/t-plug"
    st = out.statuses[0]
    assert st.loaded and st.slug == "t-plug" and st.version == "2.0"


def test_discover_defaults_name_and_route(plugin_base):
    _write_plugin(
        plugin_base,
        "minimal",
        'TOOL = {"slug": "minimal-tool"}\n'
        'from fastapi import APIRouter\n'
        'router = APIRouter(prefix="/tools/minimal-tool", tags=["minimal-tool"])\n',
    )
    out = discover_plugins(reserved_slugs=set(), base=plugin_base)
    assert out.entries[0]["name"] == "minimal"
    assert out.entries[0]["route"] == "/tools/minimal-tool"


def test_discover_custom_plugins_dir_without_fake_package(tmp_path, monkeypatch):
    """A custom PLUGINS_DIR loads through the real ``plugins`` package."""
    base = tmp_path / "myplugins"
    base.mkdir()
    monkeypatch.setenv("PLUGINS_DIR", str(base))
    _write_plugin(base, "extratool", GOOD_PLUGIN)
    out = discover_plugins(reserved_slugs=set(), base=base)
    assert [e["slug"] for e in out.entries] == ["t-plug"]
    assert out.entries[0]["route"] == "/tools/t-plug"
    assert out.statuses[0].loaded


def test_discover_skips_broken_plugin(plugin_base):
    _write_plugin(plugin_base, "boom", 'raise RuntimeError("nope")\n')
    _write_plugin(plugin_base, "good", GOOD_PLUGIN)
    out = discover_plugins(reserved_slugs=set(), base=plugin_base)
    assert [s.slug for s in out.statuses if s.loaded] == ["t-plug"]
    boom = next(s for s in out.statuses if s.name == "boom")
    assert not boom.loaded and "import failed" in boom.error


def test_discover_rejects_missing_manifest(plugin_base):
    _write_plugin(plugin_base, "nomanifest", "x = 1\n")
    out = discover_plugins(reserved_slugs=set(), base=plugin_base)
    st = out.statuses[0]
    assert not st.loaded and "TOOL" in st.error


def test_discover_rejects_reserved_slug(plugin_base):
    _write_plugin(plugin_base, "clash", 'TOOL = {"slug": "pdf2word", "name": "x"}\n')
    out = discover_plugins(reserved_slugs={"pdf2word"}, base=plugin_base)
    st = out.statuses[0]
    assert not st.loaded and "already registered" in st.error


def test_discover_rejects_invalid_slug(plugin_base):
    _write_plugin(plugin_base, "badslug", 'TOOL = {"slug": "Bad Slug!", "name": "x"}\n')
    out = discover_plugins(reserved_slugs=set(), base=plugin_base)
    st = out.statuses[0]
    assert not st.loaded and "invalid slug" in st.error


def test_discover_collects_static_mount(plugin_base):
    _write_plugin(plugin_base, "good", GOOD_PLUGIN, static=True)
    out = discover_plugins(reserved_slugs=set(), base=plugin_base)
    assert out.static_mounts == [("t-plug", plugin_base / "good" / "static")]


def test_discover_empty_dir(plugin_base):
    out = discover_plugins(reserved_slugs=set(), base=plugin_base)
    assert out.entries == [] and out.statuses == []


def test_discover_features_default_not_shared(plugin_base):
    _write_plugin(plugin_base, "good", GOOD_PLUGIN)
    _write_plugin(
        plugin_base,
        "good2",
        GOOD_PLUGIN.replace('"t-plug"', '"t-plug2"').replace(
            "/tools/t-plug", "/tools/t-plug2"
        ).replace("测试插件", "测试插件2"),
    )
    out = discover_plugins(reserved_slugs=set(), base=plugin_base)
    e1 = next(e for e in out.entries if e["slug"] == "t-plug")
    e2 = next(e for e in out.entries if e["slug"] == "t-plug2")
    e1["features"].append("mutated")
    assert "mutated" not in e2["features"]


def test_plugin_registered_end_to_end():
    """The bundled example plugin is loaded into the real app at import time."""
    from fastapi.testclient import TestClient

    from tools import get_tool_by_slug

    from app import app

    assert get_tool_by_slug("text-lines") is not None
    client = TestClient(app)

    page = client.get("/tools/text-lines")
    assert page.status_code == 200
    assert "文本行处理" in page.text

    r = client.post(
        "/tools/text-lines/process",
        data={"text": "b\na\nb\n", "dedupe": "1", "strip_empty": "1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "b\na"
    assert body["lines"] == 2

    css = client.get("/plugins/text-lines/static/plugin.css")
    assert css.status_code == 200

    tools = client.get("/api/tools").json()
    assert any(t["slug"] == "text-lines" for t in tools["tools"])
