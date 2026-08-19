"""Tests for ZIP tools plugin."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plugins.zip_tools.zip_ops import ValidationError, extract_zip, list_zip, pack_files


def test_pack_list_extract(tmp_path: Path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("hello", encoding="utf-8")
    b.write_text("world", encoding="utf-8")
    zpath = tmp_path / "out.zip"
    stats = pack_files([(str(a), "a.txt"), (str(b), "dir/b.txt")], str(zpath))
    assert stats["input_files"] == 2
    assert zpath.is_file()

    info = list_zip(str(zpath))
    names = {e["name"] for e in info["entries"]}
    assert "a.txt" in names
    assert "dir/b.txt" in names

    out_dir = tmp_path / "ex"
    ex = extract_zip(str(zpath), str(out_dir))
    assert ex["output_files"] == 2
    assert (out_dir / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (out_dir / "dir" / "b.txt").read_text(encoding="utf-8") == "world"


def test_zip_slip_blocked(tmp_path: Path):
    zpath = tmp_path / "evil.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../evil.txt", b"nope")
    with pytest.raises(ValidationError):
        extract_zip(str(zpath), str(tmp_path / "safe"))


def test_api_pack_and_list(client_env):
    client = client_env
    r = client.get("/tools/zip-tools")
    assert r.status_code == 200
    r = client.post(
        "/tools/zip-tools/convert",
        data={"action": "pack", "compresslevel": "6"},
        files=[
            ("files", ("a.txt", b"aaa", "text/plain")),
            ("files", ("b.txt", b"bbb", "text/plain")),
        ],
    )
    assert r.status_code == 200, r.text
    assert "zip" in (r.headers.get("content-type") or "")
    raw = r.content
    assert zipfile.is_zipfile(io.BytesIO(raw))

    r2 = client.post(
        "/tools/zip-tools/list",
        files={"file": ("x.zip", raw, "application/zip")},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["count"] >= 2


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    d = tmp_path / "file"
    d.mkdir()
    monkeypatch.setenv("UPLOAD_FILE_DIR", str(d))
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-for-unit-tests-only")
    monkeypatch.setenv("DOTENV_OVERRIDE", "0")
    import importlib

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
    return TestClient(app_mod.app)
