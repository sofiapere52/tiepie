from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from tiestim.api import routes
from tiestim.session import create_session


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.loop = asyncio.get_running_loop()
    app.state.session = create_session()
    app.state.ws_clients = []
    app.state.last_error = None
    routes.register_run_finished(app)
    yield
    try:
        app.state.session.close()
    except Exception:
        pass


app = FastAPI(title="Tiestim", lifespan=lifespan)
app.include_router(routes.router, prefix="/api")


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    app.state.ws_clients.append(websocket)
    try:
        await routes.push_snapshot(websocket, app)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in app.state.ws_clients:
            app.state.ws_clients.remove(websocket)


_ROOT = Path(__file__).resolve().parents[3]
_static = _ROOT / "static"
if _static.is_dir():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
