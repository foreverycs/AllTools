"""ZIP 工具 — 打包 / 预览 / 解压。"""

from __future__ import annotations

import os

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request

from core.concurrency import run_heavy
from core.errors import ValidationError
from tools.common import (
    ZIP_MEDIA,
    check_batch_total,
    check_upload_size_header,
    max_batch_files,
    safe_stem,
    save_upload,
    templates,
    with_nav,
)
from tools.pipeline import TempWorkspace, map_conversion_error

from .zip_ops import extract_zip, list_zip, max_files, pack_files

router = APIRouter(prefix="/tools/zip-tools", tags=["zip-tools"])

_ACTIONS = ("pack", "list", "extract")


@router.get("", response_class=HTMLResponse)
async def tool_page(request: Request):
    return templates.TemplateResponse(
        request,
        "tools/zip-tools.html",
        with_nav(
            {
                "tool": {
                    "name": "ZIP 工具",
                    "slug": "zip-tools",
                    "category": "text",
                },
                "actions": list(_ACTIONS),
                "max_files": min(max_files(), max_batch_files()),
            }
        ),
    )


@router.get("/options")
async def api_options():
    return JSONResponse(
        {
            "actions": list(_ACTIONS),
            "max_files": min(max_files(), max_batch_files()),
            "defaults": {"action": "pack", "compresslevel": 6},
        }
    )


def _parse_action(raw: str | None) -> str:
    a = (raw or "pack").strip().lower()
    if a not in _ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of: {', '.join(_ACTIONS)}",
        )
    return a


def _parse_level(raw: str | None) -> int:
    if raw is None or str(raw).strip() == "":
        return 6
    try:
        n = int(str(raw).strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="compresslevel must be 0-9") from exc
    if n < 0 or n > 9:
        raise HTTPException(status_code=400, detail="compresslevel must be 0-9")
    return n


@router.post("/list")
async def api_list(file: UploadFile = File(...)):
    """Return ZIP directory listing as JSON."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    check_upload_size_header(file)
    ws = TempWorkspace(prefix="ziplist_")
    try:
        work = ws.create()
        in_path = os.path.join(work, "input.zip")
        await save_upload(file, in_path)
        info = await run_heavy(list_zip, in_path)
    except HTTPException:
        ws.cleanup_now()
        raise
    except ValidationError as exc:
        ws.cleanup_now()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc, label="ZIP list failed") from exc
    ws.cleanup_now()
    return JSONResponse({"ok": True, **info})


@router.post("/convert")
async def api_convert(
    background_tasks: BackgroundTasks,
    action: str = Form("pack"),
    files: list[UploadFile] = File(...),
    compresslevel: str | None = Form("6"),
):
    """pack → zip download; extract → zip of extracted tree (or single file)."""
    act = _parse_action(action)
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    limit = min(max_files(), max_batch_files())
    if len(files) > limit:
        raise HTTPException(status_code=413, detail=f"Too many files (max {limit})")
    await check_batch_total(files)
    for f in files:
        check_upload_size_header(f, label=f.filename)

    ws = TempWorkspace(prefix="ziptools_")
    try:
        work = ws.create()
        if act == "pack":
            paths = []
            for idx, f in enumerate(files):
                disk = os.path.join(work, f"in-{idx}")
                await save_upload(f, disk)
                paths.append((disk, f.filename or f"file-{idx + 1}"))
            level = _parse_level(compresslevel)
            out_name = "archive.zip"
            if len(files) == 1 and files[0].filename:
                out_name = f"{safe_stem(files[0].filename)}.zip"
            out_path = ws.join(out_name)
            stats = await run_heavy(pack_files, paths, out_path, compresslevel=level)
            media = ZIP_MEDIA
            headers = {
                "X-Input-Files": str(stats.get("input_files", 0)),
                "X-Input-Bytes": str(stats.get("input_bytes", 0)),
                "X-Output-Bytes": str(stats.get("output_bytes", 0)),
                "Cache-Control": "no-store",
            }
            filename = out_name
        else:
            # extract: expect one zip
            if len(files) != 1:
                raise HTTPException(
                    status_code=400, detail="extract requires exactly one ZIP file"
                )
            f0 = files[0]
            name_l = (f0.filename or "").lower()
            if not name_l.endswith(".zip"):
                raise HTTPException(status_code=400, detail="file must be a .zip")
            in_path = os.path.join(work, "input.zip")
            await save_upload(f0, in_path)
            out_dir = os.path.join(work, "extracted")
            os.makedirs(out_dir, exist_ok=True)
            stats = await run_heavy(extract_zip, in_path, out_dir)
            # Re-pack extracted tree for single download (or single file passthrough)
            names = stats.get("files") or []
            if len(names) == 1:
                only = os.path.join(out_dir, names[0])
                if os.path.isfile(only):
                    out_path = only
                    filename = os.path.basename(names[0])
                    # guess media
                    media = "application/octet-stream"
                    headers = {
                        "X-Output-Files": "1",
                        "X-Uncompressed-Bytes": str(stats.get("uncompressed_bytes", 0)),
                        "Cache-Control": "no-store",
                    }
                else:
                    raise HTTPException(status_code=500, detail="extract missing file")
            else:
                out_name = f"{safe_stem(f0.filename)}_extracted.zip"
                out_path = ws.join(out_name)
                pairs = [
                    (os.path.join(out_dir, n), n)
                    for n in names
                    if os.path.isfile(os.path.join(out_dir, n))
                ]
                await run_heavy(pack_files, pairs, out_path, compresslevel=1)
                media = ZIP_MEDIA
                filename = out_name
                headers = {
                    "X-Output-Files": str(len(names)),
                    "X-Uncompressed-Bytes": str(stats.get("uncompressed_bytes", 0)),
                    "Cache-Control": "no-store",
                }
    except HTTPException:
        ws.cleanup_now()
        raise
    except ValidationError as exc:
        ws.cleanup_now()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        ws.cleanup_now()
        raise map_conversion_error(exc, label="ZIP tools failed") from exc

    ws.schedule_cleanup(background_tasks)
    return FileResponse(
        out_path,
        media_type=media,
        filename=filename,
        headers=headers,
        background=None,
    )
