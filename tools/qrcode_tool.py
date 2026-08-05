"""二维码生成 — 网址 / 文本 / Wi-Fi / 邮件，页面与 API。

生成逻辑基于 ``qrcode`` + Pillow，返回 PNG 的 base64 data URI 供前端
展示与下载。
"""

from __future__ import annotations

import base64
import io
import re
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import Request

from tools.common import templates, to_bool, with_nav

router = APIRouter(prefix="/tools/qrcode", tags=["qrcode"])

MAX_TEXT_CHARS = 600
MAX_URL_CHARS = 2048
MAX_SSID_CHARS = 32
MAX_WIFI_PASS_CHARS = 63
MAX_EMAIL_CHARS = 256
MAX_EMAIL_SUBJECT_CHARS = 200
MAX_EMAIL_BODY_CHARS = 1000

ERROR_CORRECTION = {
    "L": "L",
    "M": "M",
    "Q": "Q",
    "H": "H",
}

_WIFI_ESCAPE = re.compile(r'([\\;,:"])')


def _build_payload(kind: str, data: Dict[str, str]) -> str:
    """Build the QR payload string for the given kind."""
    kind = (kind or "text").strip().lower()
    if kind == "url":
        url = (data.get("url") or "").strip()
        if not url:
            raise ValueError("请输入网址")
        return url
    if kind == "wifi":
        ssid = (data.get("wifi_ssid") or "").strip()
        if not ssid:
            raise ValueError("请输入 Wi-Fi 名称（SSID）")
        password = data.get("wifi_password") or ""
        enc = (data.get("wifi_enc") or "WPA").strip().upper()
        if enc not in ("WPA", "WEP", "NOPASS"):
            enc = "WPA"
        if enc == "NOPASS":
            password = ""
        hidden = to_bool(data.get("wifi_hidden"), False)
        parts = ["WIFI:", f"T:{enc};", f"S:{_escape_wifi(ssid)};"]
        if password:
            parts.append(f"P:{_escape_wifi(password)};")
        if hidden:
            parts.append("H:true;")
        parts.append(";")
        return "".join(parts)
    if kind == "email":
        addr = (data.get("email_addr") or "").strip()
        if not addr:
            raise ValueError("请输入收件人邮箱")
        subject = (data.get("email_subject") or "").strip()
        body = (data.get("email_body") or "").strip()
        q = urlencode({"subject": subject, "body": body})
        return f"mailto:{addr}?{q}"
    # text (default)
    text = (data.get("text") or "").strip()
    if not text:
        raise ValueError("请输入文本内容")
    return text


def _escape_wifi(value: str) -> str:
    """Escape reserved characters in Wi-Fi QR payload per the spec."""
    return _WIFI_ESCAPE.sub(r"\\\1", value)


def _check_len(value: Optional[str], max_len: int, label: str) -> str:
    text = (value or "").strip()
    if len(text) > max_len:
        raise HTTPException(
            status_code=413, detail=f"{label}过长（最多 {max_len} 字符）"
        )
    return text


def render_qr_png(payload: str, size: int = 384, ec: str = "M") -> bytes:
    """Render ``payload`` to a PNG byte string sized close to ``size`` px."""
    import qrcode
    from qrcode.constants import (
        ERROR_CORRECT_H,
        ERROR_CORRECT_L,
        ERROR_CORRECT_M,
        ERROR_CORRECT_Q,
    )

    ec_map = {
        "L": ERROR_CORRECT_L,
        "M": ERROR_CORRECT_M,
        "Q": ERROR_CORRECT_Q,
        "H": ERROR_CORRECT_H,
    }
    ec = (ec or "M").strip().upper()
    err_correct = ec_map.get(ec, ERROR_CORRECT_M)
    size = max(128, min(1024, int(size or 384)))
    border = 2
    box_size = 8

    qr = qrcode.QRCode(
        version=None,
        error_correction=err_correct,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    modules = qr.modules_count

    # Choose box_size so the rendered width lands as close to `size` as possible
    # without shrinking below the module grid (keep it sharp for scanning).
    bs = max(2, round(size / (modules + 2 * border)))
    qr = qrcode.QRCode(
        version=None,
        error_correction=err_correct,
        box_size=bs,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/qrcode.html",
        with_nav({
            "tool": {
                "name": "二维码生成",
                "slug": "qrcode",
                "category": "text",
            }
        }),
    )


@router.post("/generate")
async def api_generate(
    type: str = Form("text"),
    url: str = Form(""),
    text: str = Form(""),
    wifi_ssid: str = Form(""),
    wifi_password: str = Form(""),
    wifi_enc: str = Form("WPA"),
    wifi_hidden: str = Form("0"),
    email_addr: str = Form(""),
    email_subject: str = Form(""),
    email_body: str = Form(""),
    size: int = Form(384),
    ec: str = Form("M"),
):
    """Generate a QR code PNG (data URI) for the given content type."""
    kind = (type or "text").strip().lower()
    if kind not in ("url", "text", "wifi", "email"):
        raise HTTPException(status_code=400, detail="不支持的二维码类型")

    data: Dict[str, str] = {
        "url": _check_len(url, MAX_URL_CHARS, "网址"),
        "text": _check_len(text, MAX_TEXT_CHARS, "文本"),
        "wifi_ssid": _check_len(wifi_ssid, MAX_SSID_CHARS, "Wi-Fi 名称"),
        "wifi_password": _check_len(wifi_password, MAX_WIFI_PASS_CHARS, "Wi-Fi 密码"),
        "wifi_enc": wifi_enc,
        "wifi_hidden": wifi_hidden,
        "email_addr": _check_len(email_addr, MAX_EMAIL_CHARS, "邮箱"),
        "email_subject": _check_len(email_subject, MAX_EMAIL_SUBJECT_CHARS, "邮件主题"),
        "email_body": _check_len(email_body, MAX_EMAIL_BODY_CHARS, "邮件正文"),
    }

    try:
        payload = _build_payload(kind, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not payload:
        raise HTTPException(status_code=400, detail="内容不能为空")

    try:
        png = render_qr_png(payload, size=size, ec=ec)
    except Exception as exc:  # defensive: malformed payload → clear 400
        raise HTTPException(status_code=400, detail=f"生成失败：{exc}") from exc

    b64 = base64.b64encode(png).decode("ascii")
    return JSONResponse(
        {
            "image": f"data:image/png;base64,{b64}",
            "mime": "image/png",
            "content": payload,
            "type": kind,
            "size": size,
            "bytes": len(png),
        }
    )
