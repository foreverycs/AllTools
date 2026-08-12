"""Tests for the QR code generator tool (URL / text / Wi-Fi / email)."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from tools import TOOL_REGISTRY
from plugins.qrcode.router import _build_payload, render_qr_png


def _png_bytes(data_uri: str) -> bytes:
    assert data_uri.startswith("data:image/png;base64,")
    return base64.b64decode(data_uri.split(",", 1)[1])


def _valid_png(data: bytes) -> bool:
    # PNG signature + enough bytes for a 1x1 image.
    return data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 100


@pytest.mark.parametrize(
    "kind,data,expected",
    [
        ("url", {"url": "https://example.com"}, "https://example.com"),
        ("text", {"text": "hello 世界"}, "hello 世界"),
        (
            "wifi",
            {"wifi_ssid": "MyWiFi", "wifi_password": "s3cr3t", "wifi_enc": "WPA"},
            "WIFI:T:WPA;S:MyWiFi;P:s3cr3t;;",
        ),
        (
            "wifi",
            {"wifi_ssid": "My;WiFi", "wifi_password": "p:w", "wifi_enc": "WEP"},
            r"WIFI:T:WEP;S:My\;WiFi;P:p\:w;;",
        ),
        (
            "wifi",
            {"wifi_ssid": "Open", "wifi_password": "", "wifi_enc": "NOPASS"},
            "WIFI:T:NOPASS;S:Open;;",
        ),
        (
            "email",
            {"email_addr": "a@b.com", "email_subject": "hi", "email_body": "body"},
            "mailto:a@b.com?subject=hi&body=body",
        ),
    ],
)
def test_build_payload(kind, data, expected):
    assert _build_payload(kind, data) == expected


@pytest.mark.parametrize(
    "kind,data",
    [
        ("url", {"url": ""}),
        ("text", {"text": "   "}),
        ("wifi", {"wifi_ssid": "", "wifi_password": "x", "wifi_enc": "WPA"}),
        ("email", {"email_addr": ""}),
    ],
)
def test_build_payload_requires_content(kind, data):
    with pytest.raises(ValueError):
        _build_payload(kind, data)


def test_render_qr_png_valid():
    png = render_qr_png("https://example.com", size=384, ec="M")
    assert _valid_png(png)


def test_registry_has_qrcode():
    slugs = {t["slug"] for t in TOOL_REGISTRY}
    assert "qrcode" in slugs
    tool = next(t for t in TOOL_REGISTRY if t["slug"] == "qrcode")
    assert tool["category"] == "text"
    assert tool["route"] == "/tools/qrcode"


def test_generate_page_and_api():
    from app import app

    client = TestClient(app)
    page = client.get("/tools/qrcode")
    assert page.status_code == 200
    assert "二维码生成" in page.text

    r = client.post(
        "/tools/qrcode/generate",
        data={"type": "url", "url": "https://example.com", "size": "384", "ec": "M"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "https://example.com"
    assert _valid_png(_png_bytes(body["image"]))


def test_generate_wifi_and_email_api():
    from app import app

    client = TestClient(app)

    wifi = client.post(
        "/tools/qrcode/generate",
        data={
            "type": "wifi",
            "wifi_ssid": "Office",
            "wifi_password": "pass:123",
            "wifi_enc": "WPA",
        },
    )
    assert wifi.status_code == 200
    assert wifi.json()["content"] == "WIFI:T:WPA;S:Office;P:pass\\:123;;"
    assert _valid_png(_png_bytes(wifi.json()["image"]))

    mail = client.post(
        "/tools/qrcode/generate",
        data={"type": "email", "email_addr": "a@b.com", "email_subject": "hi", "email_body": "b"},
    )
    assert mail.status_code == 200
    assert "mailto:a@b.com" in mail.json()["content"]


def test_generate_validation_errors():
    from app import app

    client = TestClient(app)
    empty = client.post("/tools/qrcode/generate", data={"type": "url", "url": ""})
    assert empty.status_code == 400
    bad = client.post("/tools/qrcode/generate", data={"type": "nope", "text": "x"})
    assert bad.status_code == 400
