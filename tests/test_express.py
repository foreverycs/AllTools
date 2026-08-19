"""Tests for file express (取件码) storage and HTTP API."""

from __future__ import annotations

import importlib
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def express_env(tmp_path, monkeypatch):
    d = tmp_path / "file"
    d.mkdir()
    monkeypatch.setenv("UPLOAD_FILE_DIR", str(d))
    monkeypatch.setenv("UPLOAD_RETENTION_DAYS", "5")
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-for-unit-tests-only")
    monkeypatch.setenv("DOTENV_OVERRIDE", "0")
    monkeypatch.setenv("EXPRESS_DEFAULT_TTL_HOURS", "24")
    monkeypatch.setenv("EXPRESS_MAX_TTL_HOURS", "168")

    import core.settings as settings_mod
    import storage.express as ex

    settings_mod.clear_settings_cache()
    ex._last_cleanup_ts = 0.0
    yield ex, d
    settings_mod.clear_settings_cache()
    ex._last_cleanup_ts = 0.0


@pytest.fixture()
def express_client(express_env, monkeypatch):
    ex, d = express_env
    import app as app_mod
    import core.api_rate_limit as rl
    import core.concurrency as concurrency_mod
    import core.settings as settings_mod
    import core.tool_flags as flags_mod

    settings_mod.clear_settings_cache()
    concurrency_mod.reset_semaphore()
    rl.reset_all()
    flags_mod.clear_tool_flags_cache()
    importlib.reload(app_mod)

    client = TestClient(app_mod.app)
    yield client, ex, d
    rl.reset_all()
    flags_mod.clear_tool_flags_cache()
    settings_mod.clear_settings_cache()


def _touch(path: Path, content: bytes = b"hello express") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_create_and_lookup_package(express_env, tmp_path):
    ex, root = express_env
    src = _touch(tmp_path / "note.txt", b"secret payload")
    pkg = ex.create_package(
        src,
        "笔记.txt",
        content_type="text/plain",
        ttl_hours=2,
        max_downloads=3,
        note="给同事",
    )
    assert pkg["code"] and len(pkg["code"]) == 6 and pkg["code"].isdigit()
    assert pkg["available"] is True
    assert pkg["max_downloads"] == 3
    assert pkg["downloads_left"] == 3
    assert pkg["note"] == "给同事"
    assert "stored_rel" not in pkg

    info = ex.get_package_by_code(pkg["code"])
    assert info is not None
    assert info["original_name"] == "笔记.txt"
    assert info["size_bytes"] == len(b"secret payload")
    path = ex.resolve_package_file(info)
    assert path is not None and path.is_file()
    assert path.read_bytes() == b"secret payload"
    # Stored under file/express/
    assert (root / "express").is_dir()


def test_claim_download_and_exhaust(express_env, tmp_path):
    ex, _ = express_env
    src = _touch(tmp_path / "once.bin", b"x" * 20)
    pkg = ex.create_package(src, "once.bin", max_downloads=1)
    code = pkg["code"]

    info1, err1 = ex.claim_download(code)
    assert err1 is None and info1 is not None
    assert info1["download_count"] == 1
    assert Path(info1["_abs_path"]).is_file()

    info2, err2 = ex.claim_download(code)
    assert err2 == "exhausted"
    assert info2 is not None and info2["exhausted"] is True


def test_invalid_code_format(express_env):
    ex, _ = express_env
    assert ex.is_valid_code_format("12345") is False
    assert ex.is_valid_code_format("1234567") is False
    # Spaces are stripped for paste-friendly codes ("12 3456" → "123456")
    assert ex.is_valid_code_format("12 3456") is True
    assert ex.is_valid_code_format("123456") is True
    assert ex.is_valid_code_format("123456") is True
    assert ex.is_valid_code_format("1234567") is False
    assert ex.is_valid_code_format("12345678") is False
    assert ex.get_package_by_code("abcdef") is None
    info, err = ex.claim_download("000000")
    assert err == "invalid" and info is None


def test_expiry_blocks_user_but_admin_retains(express_env, tmp_path):
    """Expiry is user-side only; records stay until explicit admin purge."""
    ex, _ = express_env
    src = _touch(tmp_path / "old.txt", b"old")
    pkg = ex.create_package(src, "old.txt", ttl_hours=1)
    code = pkg["code"]
    pkg_id = pkg["id"]

    import sqlite3

    db_path = ex.express_root() / "express.db"
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE packages SET expires_at = ? WHERE code = ?", (past, code)
    )
    conn.commit()
    conn.close()

    # Non-force cleanup is a no-op (no automatic purge)
    assert ex.cleanup_express() == 0
    assert ex.cleanup_express(force=False) == 0

    info = ex.get_package_by_code(code)
    assert info is not None
    assert info["expired"] is True
    assert info["available"] is False

    _, err = ex.claim_download(code)
    assert err == "expired"

    # Admin still sees the package and file
    admin = ex.get_package_by_id(pkg_id)
    assert admin is not None
    assert admin["expired"] is True
    assert admin["file_exists"] is True
    listed = ex.list_packages(status="expired")
    assert any(p["id"] == pkg_id for p in listed)

    # Explicit admin purge removes it
    removed = ex.cleanup_express(force=True)
    assert removed >= 1
    assert ex.get_package_by_code(code) is None
    assert ex.get_package_by_id(pkg_id) is None


def test_resolve_blocks_traversal(express_env):
    ex, _ = express_env
    assert ex.resolve_package_file({"stored_rel": "../secrets"}) is None
    assert ex.resolve_package_file({"stored_rel": "..\\secrets"}) is None
    assert ex.resolve_package_file({"stored_rel": ""}) is None


def test_registry_has_express(tmp_path, monkeypatch):
    # Isolate from any local file/tool_catalog.json so the default registry
    # (express → text) is used regardless of the developer's custom categories.
    d = tmp_path / "file"
    d.mkdir()
    monkeypatch.setenv("UPLOAD_FILE_DIR", str(d))
    import core.settings as settings_mod
    from core import tool_catalog as tc

    settings_mod.clear_settings_cache()
    tc.clear_cache()

    from tools import (
        TOOL_REGISTRY,
        enabled_tools,
        featured_tools,
        tools_by_category,
    )

    slugs = {t["slug"] for t in TOOL_REGISTRY}
    assert "express" in slugs
    tool = next(t for t in TOOL_REGISTRY if t["slug"] == "express")
    assert tool["category"] == "text"
    assert tool.get("featured") is True
    assert tool["route"] == "/tools/express"

    # Featured: not in module catalog / category grids; only in featured list.
    assert "express" not in {t["slug"] for t in enabled_tools()}
    assert "express" in {t["slug"] for t in featured_tools()}
    for cat in tools_by_category():
        assert "express" not in {t["slug"] for t in cat["tools"]}
    # Admin still sees it under its category when include_disabled=True
    text_admin = next(
        c for c in tools_by_category(include_disabled=True) if c["id"] == "text"
    )
    assert "express" in {t["slug"] for t in text_admin["tools"]}


def test_featured_tool_counts_in_assigned_category(tmp_path, monkeypatch):
    """Regression: a featured tool (文件快递) assigned to a custom category
    must be counted in nav/tab counts (the homepage grid renders it there),
    even though it stays out of the module catalog."""
    d = tmp_path / "file"
    d.mkdir()
    monkeypatch.setenv("UPLOAD_FILE_DIR", str(d))
    monkeypatch.setenv("UPLOAD_RETENTION_DAYS", "5")

    import core.settings as settings_mod
    from core import tool_catalog as tc
    from tools import TOOL_CATEGORIES, nav_categories, tools_by_category

    settings_mod.clear_settings_cache()
    tc.clear_cache()

    cats = [dict(c) for c in TOOL_CATEGORIES]
    cats.append(
        {
            "id": "custommsg8remw",
            "name": "创意",
            "name_en": "",
            "description": "",
            "icon": "🧩",
            "accent": "indigo",
            "route": "/#col-custommsg8remw",
            "builtin": False,
        }
    )
    tc.save_catalog(cats, {"express": "custommsg8remw"})

    nav = {c["id"]: c for c in nav_categories()}
    assert nav["custommsg8remw"]["tool_count"] == 1
    assert "文件快递" in nav["custommsg8remw"]["tool_names"]
    # Featured stays out of the public module catalog (unchanged design).
    for cat in tools_by_category():
        assert "express" not in {t["slug"] for t in cat["tools"]}


def test_api_send_lookup_pickup(express_client):
    client, ex, _ = express_client

    page = client.get("/tools/express")
    assert page.status_code == 200
    assert "文件快递" in page.text or "取件码" in page.text
    # Frontend must parse FastAPI validation errors (not show [object Object])
    assert "errDetail" in page.text or "Array.isArray(detail)" in page.text

    empty = client.post(
        "/tools/express/send",
        files={"file": ("empty.txt", b"", "text/plain")},
        data={"ttl_hours": "24"},
    )
    assert empty.status_code == 400
    assert "空" in empty.json().get("detail", "")

    missing = client.post("/tools/express/send", data={"ttl_hours": "24"})
    assert missing.status_code in (400, 422)

    send = client.post(
        "/tools/express/send",
        files={"file": ("hello.txt", b"hello world", "text/plain")},
        data={"ttl_hours": "24", "max_downloads": "2", "note": "test"},
    )
    assert send.status_code == 200, send.text
    body = send.json()
    assert body["ok"] is True
    code = body["code"]
    assert len(code) == 6 and code.isdigit()
    assert body["original_name"] == "hello.txt"
    assert body["max_downloads"] == 2
    assert "pickup_url" in body

    lookup = client.post("/tools/express/lookup", data={"code": code})
    assert lookup.status_code == 200
    meta = lookup.json()
    assert meta["ok"] is True
    assert meta["code"] == code
    assert meta["size_bytes"] == len(b"hello world")
    assert "stored_rel" not in meta

    dl = client.get(f"/tools/express/pickup/{code}")
    assert dl.status_code == 200
    assert dl.content == b"hello world"
    assert "attachment" in (dl.headers.get("content-disposition") or "").lower()

    dl2 = client.post("/tools/express/pickup", data={"code": code})
    assert dl2.status_code == 200
    assert dl2.content == b"hello world"

    # max_downloads=2 exhausted
    dl3 = client.get(f"/tools/express/pickup/{code}")
    assert dl3.status_code == 410


def test_api_multi_file_and_burn(express_client):
    client, ex, _ = express_client

    multi = client.post(
        "/tools/express/send",
        files=[
            ("files", ("a.txt", b"aaa", "text/plain")),
            ("files", ("b.txt", b"bbb", "text/plain")),
        ],
        data={"ttl_hours": "24", "max_downloads": "3"},
    )
    assert multi.status_code == 200, multi.text
    body = multi.json()
    assert body["file_count"] == 2
    assert body["original_name"].endswith(".zip")
    code = body["code"]
    dl = client.get(f"/tools/express/pickup/{code}")
    assert dl.status_code == 200
    assert zipfile.is_zipfile(io.BytesIO(dl.content))

    burn = client.post(
        "/tools/express/send",
        files={"file": ("once.txt", b"secret", "text/plain")},
        data={"ttl_hours": "24", "burn_after": "1"},
    )
    assert burn.status_code == 200, burn.text
    bbody = burn.json()
    assert bbody["burn_after"] is True
    assert bbody["max_downloads"] == 1
    bcode = bbody["code"]
    dl1 = client.get(f"/tools/express/pickup/{bcode}")
    assert dl1.status_code == 200
    assert dl1.content == b"secret"
    dl2 = client.get(f"/tools/express/pickup/{bcode}")
    assert dl2.status_code in (404, 410)


def test_api_bad_code_and_ttl(express_client):
    client, _, _ = express_client

    bad = client.post("/tools/express/lookup", data={"code": "12"})
    assert bad.status_code == 400

    missing = client.post("/tools/express/lookup", data={"code": "999999"})
    assert missing.status_code == 404

    ttl = client.post(
        "/tools/express/send",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"ttl_hours": "99999"},
    )
    assert ttl.status_code == 400


def test_list_delete_packages_admin_api(express_env, tmp_path):
    ex, _ = express_env
    src1 = _touch(tmp_path / "a.txt", b"aaa")
    src2 = _touch(tmp_path / "b.txt", b"bbb")
    p1 = ex.create_package(src1, "a.txt", note="alpha", max_downloads=1)
    p2 = ex.create_package(src2, "b.txt", note="beta")
    listed = ex.list_packages(limit=50)
    ids = {p["id"] for p in listed}
    assert p1["id"] in ids and p2["id"] in ids
    assert all("file_exists" in p for p in listed)
    assert all(p.get("file_exists") for p in listed if p["id"] in ids)

    by_q = ex.list_packages(q="alpha")
    assert len(by_q) == 1 and by_q[0]["id"] == p1["id"]

    got = ex.get_package_by_id(p1["id"])
    assert got is not None and got["code"] == p1["code"]
    assert got["file_exists"] is True

    assert ex.delete_package(p1["id"]) is True
    assert ex.get_package_by_id(p1["id"]) is None
    assert ex.delete_packages([p2["id"], "missing"]) == 1
    assert ex.get_package_by_id(p2["id"]) is None
    assert ex.delete_packages([]) == 0


def test_text_package_create_and_claim(express_env):
    ex, _ = express_env
    pkg = ex.create_text_package(
        "你好，小纸条", note="问候", max_downloads=2
    )
    assert pkg["is_text"] is True
    assert pkg["file_count"] == 1
    assert pkg["original_name"] == ""
    code = pkg["code"]

    info, err = ex.claim_text(code)
    assert err is None and info is not None
    assert info["_text"] == "你好，小纸条"
    assert info["download_count"] == 1

    info2, err2 = ex.claim_text(code)
    assert err2 is None and info2["_text"] == "你好，小纸条"
    # exhausted now
    _, err3 = ex.claim_text(code)
    assert err3 == "exhausted"

    # lookup exposes preview, not full text
    pub = ex.get_package_by_code(code)
    assert pub["is_text"] is True
    assert pub["text_preview"]


def test_text_package_burn_after(express_env):
    ex, _ = express_env
    pkg = ex.create_text_package("阅后即焚正文", burn_after=True)
    assert pkg["max_downloads"] == 1
    code = pkg["code"]
    info, err = ex.claim_text(code)
    assert err is None and info["_text"] == "阅后即焚正文"
    # second claim: payload cleared -> missing (already burned)
    _, err2 = ex.claim_text(code)
    assert err2 in ("missing", "exhausted")


def test_api_send_text_and_read(express_client):
    client, _, _ = express_client

    r = client.post(
        "/tools/express/send-text",
        data={
            "text": "第一行\n第二行",
            "ttl_hours": "24",
            "max_downloads": "2",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_text"] is True
    code = body["code"]

    lookup = client.post("/tools/express/lookup", data={"code": code})
    assert lookup.status_code == 200
    meta = lookup.json()
    assert meta["is_text"] is True
    assert "text" not in meta  # full body not leaked via lookup

    read = client.get(f"/tools/express/read/{code}")
    assert read.status_code == 200
    read_body = read.json()
    assert read_body["text"] == "第一行\n第二行"

    read2 = client.post("/tools/express/read", data={"code": code})
    assert read2.status_code == 200
    assert read2.json()["text"] == "第一行\n第二行"

    # exhausted (max_downloads=2)
    read3 = client.get(f"/tools/express/read/{code}")
    assert read3.status_code in (404, 410)


def test_api_text_burn(express_client):
    client, _, _ = express_client
    r = client.post(
        "/tools/express/send-text",
        data={"text": "阅后即焚内容", "burn_after": "1"},
    )
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    r1 = client.get(f"/tools/express/read/{code}")
    assert r1.status_code == 200 and r1.json()["text"] == "阅后即焚内容"
    r2 = client.get(f"/tools/express/read/{code}")
    assert r2.status_code in (404, 410)
