"""Admin console: donation (打赏) configuration."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from admin._common import _admin_url, _redirect, _tpl, admin_post, bust_health_cache
from admin.auth import require_admin
from storage.donation import (
    get_config,
    remove_qr_image,
    save_config,
    save_qr_image,
)

router = APIRouter(tags=["admin"])


@router.get("/donation", response_class=HTMLResponse)
async def donation_page(request: Request):
    """管理打赏功能：启用开关、文案与二维码图片。"""
    redir = require_admin(request)
    if redir:
        return redir

    return _tpl(
        request,
        "admin/donation.html",
        active="donation",
        cfg=get_config(),
        flash=request.query_params.get("msg"),
    )


@router.post("/donation")
@admin_post
async def donation_save(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """保存打赏设置（可同时更换二维码图片 / 删除二维码）。"""
    form = await request.form()

    enabled = str(form.get("enabled") or "").strip().lower() in (
        "1",
        "true",
        "on",
        "yes",
        "enabled",
    )
    title = str(form.get("title") or "").strip()
    subtitle = str(form.get("subtitle") or "").strip()

    msgs: list[str] = []

    # 上传新二维码（如有）。
    qr_file: UploadFile = form.get("qr")
    if qr_file is not None and getattr(qr_file, "filename", ""):
        data = await qr_file.read()
        if not data:
            msgs.append("未选择有效图片")
        else:
            err = save_qr_image(data)
            if err:
                msgs.append(err)
            else:
                msgs.append("二维码已更新")

    # 显式删除二维码（与上传互斥，删除优先）。
    if str(form.get("remove_qr") or "").strip().lower() in (
        "1",
        "true",
        "on",
        "yes",
    ):
        if get_config().get("has_qr"):
            remove_qr_image()
            msgs.append("二维码已删除")
        else:
            msgs.append("当前无二维码可删除")

    save_config(enabled=enabled, title=title, subtitle=subtitle)
    msgs.append("已开启打赏" if enabled else "已关闭打赏")

    bust_health_cache()

    return _redirect(
        _admin_url("/admin/donation", request) + "?msg=" + quote("；".join(msgs))
    )
