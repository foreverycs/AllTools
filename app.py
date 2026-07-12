from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from tools import TOOL_REGISTRY, pdf2word_router, word2pdf_router

app = FastAPI(title="工具箱", version="0.3.0")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Register tool routers
app.include_router(pdf2word_router)
app.include_router(word2pdf_router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"tools": TOOL_REGISTRY}
    )


@app.get("/health")
async def health():
    from word2pdf import engine_info

    w2p = engine_info()
    return JSONResponse(
        {
            "status": "ok",
            "version": app.version,
            "tools": len(TOOL_REGISTRY),
            "word2pdf": {
                "ready": w2p.get("ready", False),
                "engines": w2p.get("engines") or [],
                "preferred": w2p.get("preferred"),
            },
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
