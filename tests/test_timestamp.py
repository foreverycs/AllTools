"""Tests for timestamp converter (core + HTTP)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app
from coding import TimestampError, convert, now_snapshot

# A fixed known unix timestamp (seconds).
FIXED_TS = 1754249288


def test_numeric_seconds():
    out = convert(str(FIXED_TS), tz_offset_min=480)
    assert out["kind"] == "timestamp"
    assert out["epoch_seconds"] == FIXED_TS
    assert out["epoch_ms"] == FIXED_TS * 1000
    assert out["is_ms"] is False
    # 1754249288 = 2025-08-03 19:28:08 UTC; +8 → 2025-08-04 03:28:08.
    assert out["utc"] == "2025-08-03 19:28:08"
    assert out["beijing"] == "2025-08-04 03:28:08"
    assert out["local"] == "2025-08-04 03:28:08"
    assert out["weekday"]
    assert out["day_of_year"] >= 1


def test_numeric_milliseconds():
    out = convert(f"{FIXED_TS}123", tz_offset_min=480)
    assert out["is_ms"] is True
    assert out["epoch_seconds"] == FIXED_TS
    assert out["epoch_ms"] == FIXED_TS * 1000 + 123


def test_datetime_roundtrip():
    out = convert(str(FIXED_TS), tz_offset_min=480)
    back = convert(out["local"], tz_offset_min=480)
    assert back["kind"] == "datetime"
    assert back["epoch_seconds"] == FIXED_TS


def test_now_snapshot():
    snap = now_snapshot(tz_offset_min=480)
    assert snap["epoch_seconds"] > 0
    assert snap["local"]


def test_bad_input_raises():
    with pytest.raises(TimestampError):
        convert("not-a-timestamp-or-date")


def test_http_page_renders():
    client = TestClient(app)
    r = client.get("/tools/timestamp")
    assert r.status_code == 200
    assert "时间戳" in r.text


def test_http_convert():
    client = TestClient(app)
    r = client.post(
        "/tools/timestamp/convert",
        data={"value": str(FIXED_TS), "tz_offset": "480"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_seconds"] == FIXED_TS
    assert "beijing" in body


def test_http_convert_bad_input():
    client = TestClient(app)
    r = client.post(
        "/tools/timestamp/convert",
        data={"value": "??", "tz_offset": "480"},
    )
    assert r.status_code == 400


def test_http_now():
    client = TestClient(app)
    r = client.post("/tools/timestamp/now", data={"tz_offset": "480"})
    assert r.status_code == 200
    assert r.json()["epoch_seconds"] > 0
