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
    d.mkdir(exist_ok=True)
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


def test_discover_force_reloads_changed_module(plugin_base):
    _write_plugin(plugin_base, "vplug", GOOD_PLUGIN + '\nPLUGIN_VERSION = "1"\n')
    out1 = discover_plugins(reserved_slugs=set(), base=plugin_base)
    assert out1.statuses[0].version == "1"

    # In-place edit that keeps the file size identical (a known .pyc-cache trap
    # on coarse-timestamp filesystems): force reload must serve the new code.
    _write_plugin(plugin_base, "vplug", GOOD_PLUGIN + '\nPLUGIN_VERSION = "2"\n')
    out2 = discover_plugins(reserved_slugs=set(), base=plugin_base, force=True)
    assert out2.statuses[0].version == "2"


def test_install_plugin_routes_swaps_on_reload(monkeypatch):
    from fastapi import APIRouter, FastAPI
    from fastapi.responses import JSONResponse
    from starlette.testclient import TestClient

    import app as app_mod
    import core.plugins as plugins_mod
    from core.plugins import PluginDiscovery

    def make_router(slug):
        r = APIRouter()

        @r.get(f"/{slug}")
        async def h():
            return JSONResponse({"slug": slug})

        return r

    disc1 = PluginDiscovery()
    disc1.routers.append(make_router("one"))
    monkeypatch.setattr(plugins_mod, "_discovery", disc1)

    mini = FastAPI()
    saved_routes = app_mod._installed_plugin_routes
    app_mod._installed_plugin_routes = []
    try:
        app_mod.install_plugin_routes(mini)
        c = TestClient(mini)
        assert c.get("/one").status_code == 200
        assert c.get("/two").status_code == 404

        # Swap discovery and re-install: old route gone, new route live.
        disc2 = PluginDiscovery()
        disc2.routers.append(make_router("two"))
        monkeypatch.setattr(plugins_mod, "_discovery", disc2)
        app_mod.install_plugin_routes(mini)
        assert c.get("/one").status_code == 404
        assert c.get("/two").status_code == 200
    finally:
        app_mod._installed_plugin_routes = saved_routes


HOT_PLUGIN = """\
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

TOOL = {"slug": "hotplug", "name": "热插拔", "category": "text", "description": "d"}
router = APIRouter(prefix="/tools/hotplug", tags=["hotplug"])


@router.get("", response_class=HTMLResponse)
async def page():
    return HTMLResponse("<h1>hot</h1>")
"""


def test_hot_reload_plugins_end_to_end(tmp_path, monkeypatch):
    """Add/remove plugins via hot_reload_plugins without restarting the app."""
    import shutil

    from fastapi.testclient import TestClient

    import app as app_mod
    import core.plugins as plugins_mod
    from tools import get_tool_by_slug

    real_pkg = sys.modules.get("plugins")
    real_dir = plugins_mod.plugins_dir()

    base = tmp_path / "plugins"
    base.mkdir()
    fake = types.ModuleType("plugins")
    fake.__path__ = [str(base)]
    monkeypatch.setitem(sys.modules, "plugins", fake)
    monkeypatch.setenv("PLUGINS_DIR", str(base))
    _write_plugin(base, "hotplug", HOT_PLUGIN)

    try:
        disc = app_mod.hot_reload_plugins()
        assert any(s.slug == "hotplug" and s.loaded for s in disc.statuses)
        assert get_tool_by_slug("hotplug") is not None
        assert get_tool_by_slug("text-lines") is None

        client = TestClient(app_mod.app)
        assert client.get("/tools/hotplug").status_code == 200
        assert client.get("/tools/text-lines").status_code == 404

        # Remove the plugin directory and reload → route disappears.
        shutil.rmtree(base / "hotplug")
        disc2 = app_mod.hot_reload_plugins()
        assert get_tool_by_slug("hotplug") is None
        assert client.get("/tools/hotplug").status_code == 404
    finally:
        # Restore the bundled plugins so other tests are unaffected.
        if real_pkg is not None:
            sys.modules["plugins"] = real_pkg
        else:
            sys.modules.pop("plugins", None)
        monkeypatch.setenv("PLUGINS_DIR", str(real_dir))
        app_mod.hot_reload_plugins()
        assert get_tool_by_slug("text-lines") is not None
