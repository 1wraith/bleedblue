"""
FastAPI backend entry point.

Run (from the recon/ directory):
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Endpoints:
    GET  /api/capabilities          what hardware/tools are present (flags gaps)
    GET  /api/status                per-source running state
    POST /api/sources/{key}/start   start one source (409 if unavailable & sim off)
    POST /api/sources/{key}/stop    stop one source
    POST /api/sources/start_all     start everything that can run
    POST /api/sources/stop_all
    WS   /ws                        live event stream (sends a snapshot first)

APP_SIMULATE=0 disables simulation, so missing hardware yields a hard 409 with
its reason instead of synthetic data. Default is 1 (on) for laptop development.
"""
from __future__ import annotations

import contextlib
import os
import pathlib

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import enrich
from capabilities import probe_all
from hub import Hub
from sources import SOURCES, SourceUnavailable

SIMULATE = os.getenv("APP_SIMULATE", "1") != "0"
OUI_FILE = os.getenv("OUI_FILE", "")            # optional IEEE oui.txt / Wireshark manuf
_FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "index.html"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if OUI_FILE:
        n = enrich.load_oui_file(OUI_FILE)
        print(f"[bleedblue] loaded {n} OUI entries from {OUI_FILE}")
    caps = await probe_all()
    hub = Hub()
    sources = {cls.key: cls(hub, caps) for cls in SOURCES}
    app.state.caps = caps
    app.state.hub = hub
    app.state.sources = sources
    yield
    for s in sources.values():
        with contextlib.suppress(Exception):
            await s.stop()


app = FastAPI(title="2.4 GHz Recon Backend", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def _source(request: Request, key: str):
    src = request.app.state.sources.get(key)
    if not src:
        raise HTTPException(404, f"unknown source '{key}'")
    return src


@app.get("/", include_in_schema=False)
async def root():
    if _FRONTEND.exists():
        return FileResponse(_FRONTEND)
    return {"service": "bleedblue backend", "simulate": SIMULATE,
            "endpoints": ["/api/capabilities", "/api/status", "/ws"]}


@app.get("/api/capabilities")
async def capabilities(request: Request):
    caps = request.app.state.caps
    return {"simulate": SIMULATE, "capabilities": [c.dict() for c in caps.values()]}


@app.get("/api/status")
async def status(request: Request):
    srcs = request.app.state.sources
    return {"simulate": SIMULATE,
            "subscribers": request.app.state.hub.n_subscribers,
            "sources": [s.status() for s in srcs.values()]}


@app.post("/api/sources/{key}/start")
async def start(request: Request, key: str):
    src = _source(request, key)
    try:
        await src.start(allow_sim=SIMULATE)
    except SourceUnavailable as e:
        # This is the "your PC can't do this" signal.
        raise HTTPException(409, {"error": "unavailable", "source": key, "reason": str(e)})
    return src.status()


@app.post("/api/sources/{key}/stop")
async def stop(request: Request, key: str):
    src = _source(request, key)
    await src.stop()
    return src.status()


@app.post("/api/sources/start_all")
async def start_all(request: Request):
    out = []
    for src in request.app.state.sources.values():
        try:
            await src.start(allow_sim=SIMULATE)
        except SourceUnavailable:
            pass
        out.append(src.status())
    return {"sources": out}


@app.post("/api/sources/stop_all")
async def stop_all(request: Request):
    for src in request.app.state.sources.values():
        await src.stop()
    return {"sources": [s.status() for s in request.app.state.sources.values()]}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    app = websocket.app
    sub = app.state.hub.subscribe()
    # snapshot first so a fresh client can render current state immediately
    await websocket.send_json({
        "type": "snapshot",
        "simulate": SIMULATE,
        "capabilities": [c.dict() for c in app.state.caps.values()],
        "sources": [s.status() for s in app.state.sources.values()],
    })
    try:
        async for event in sub:
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        app.state.hub.unsubscribe(sub)