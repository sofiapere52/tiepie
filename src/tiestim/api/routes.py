from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tiestim.logger import append_stim_row, row_from_params
from tiestim.models import StimParams, StimRequest
from tiestim.session import BaseSession
from tiestim.waveform import build_waveforms, peak_voltages

router = APIRouter()

_last_params: StimParams | None = None
_run_started_monotonic: float | None = None
_poller_task: asyncio.Task | None = None
_poller_last_json: str | None = None
_user_stop = threading.Event()


def _payload(app: Any) -> dict:
    import os

    sess: BaseSession = app.state.session
    devs = sess.status()
    return {
        "devices": [
            {
                "slot": d.slot,
                "serial": d.serial,
                "state": d.ui_state,
                "detail": d.detail,
            }
            for d in devs
        ],
        "last_error": getattr(app.state, "last_error", None),
        "mock": os.environ.get("TIESTIM_MOCK", "").lower() in ("1", "true", "yes"),
    }


async def push_snapshot(ws, app: Any) -> None:
    await ws.send_json(_payload(app))


async def broadcast(app: Any) -> None:
    data = json.dumps(_payload(app))
    dead = []
    for ws in list(app.state.ws_clients):
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in app.state.ws_clients:
            app.state.ws_clients.remove(ws)


async def _burst_logged(app: Any) -> None:
    global _last_params, _run_started_monotonic
    if _user_stop.is_set():
        await broadcast(app)
        return
    if _last_params is not None:
        sess: BaseSession = app.state.session
        devs = sess.status()
        dur = None
        if _run_started_monotonic is not None:
            dur = time.monotonic() - _run_started_monotonic
        append_stim_row(
            row_from_params(
                _last_params,
                devs[0].serial if devs else "?",
                devs[1].serial if len(devs) > 1 else "?",
                "ok",
                "",
                dur,
            )
        )
    _run_started_monotonic = None
    await broadcast(app)


def register_run_finished(app: Any) -> None:
    def on_done():
        loop = getattr(app.state, "loop", None)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(_burst_logged(app), loop)

    try:
        app.state.session.on_run_finished(on_done)
    except Exception:
        pass


async def _poller(app: Any) -> None:
    global _poller_last_json
    while True:
        await asyncio.sleep(0.15)
        snap = json.dumps(_payload(app))
        if snap != _poller_last_json:
            _poller_last_json = snap
            await broadcast(app)


def _ensure_poller(app: Any) -> None:
    global _poller_task
    if _poller_task is None or _poller_task.done():
        _poller_task = asyncio.create_task(_poller(app))


@router.get("/health")
async def health(request: Request):
    _ensure_poller(request.app)
    return _payload(request.app)


@router.post("/connect")
async def connect(request: Request):
    _ensure_poller(request.app)
    try:
        devs = request.app.state.session.connect()
    except Exception as e:
        request.app.state.last_error = str(e)
        await broadcast(request.app)
        raise HTTPException(503, str(e)) from e
    request.app.state.last_error = None
    await broadcast(request.app)
    return {"ok": True, "devices": [d.serial for d in devs]}


class PreviewOut(BaseModel):
    ch1: list[float]
    ch2: list[float]
    n_samples: int
    peak_v_ch1: float
    peak_v_ch2: float


@router.post("/waveform/preview", response_model=PreviewOut)
async def waveform_preview(body: StimRequest):
    wf = build_waveforms(body.params)
    p = body.params
    m = body.preview_max_points
    step = max(1, wf.n_samples // m)
    idx = slice(0, wf.n_samples, step)
    a1, a2 = peak_voltages(p)
    return PreviewOut(
        ch1=wf.ch1[idx].tolist(),
        ch2=wf.ch2[idx].tolist(),
        n_samples=wf.n_samples,
        peak_v_ch1=a1,
        peak_v_ch2=a2,
    )


@router.post("/arm")
async def arm(request: Request, params: StimParams):
    global _last_params
    _ensure_poller(request.app)
    sess: BaseSession = request.app.state.session
    try:
        wf = build_waveforms(params)
        sess.arm(params, wf)
        _last_params = params
        request.app.state.last_error = None
    except Exception as e:
        request.app.state.last_error = str(e)
        await broadcast(request.app)
        raise HTTPException(400, str(e)) from e
    await broadcast(request.app)
    return {"ok": True}


@router.post("/start")
async def start_run(request: Request):
    global _run_started_monotonic
    _ensure_poller(request.app)
    _user_stop.clear()
    sess: BaseSession = request.app.state.session
    try:
        sess.start()
        _run_started_monotonic = time.monotonic()
        request.app.state.last_error = None
    except Exception as e:
        request.app.state.last_error = str(e)
        await broadcast(request.app)
        raise HTTPException(400, str(e)) from e
    await broadcast(request.app)
    return {"ok": True}


@router.post("/stop")
async def stop_run(request: Request):
    global _last_params, _run_started_monotonic
    _ensure_poller(request.app)
    _user_stop.set()
    sess: BaseSession = request.app.state.session
    try:
        sess.stop()
        if _last_params is not None:
            devs = sess.status()
            dur = None
            if _run_started_monotonic is not None:
                dur = time.monotonic() - _run_started_monotonic
            append_stim_row(
                row_from_params(
                    _last_params,
                    devs[0].serial if devs else "?",
                    devs[1].serial if len(devs) > 1 else "?",
                    "aborted",
                    "operator_stop",
                    dur,
                )
            )
        _run_started_monotonic = None
        request.app.state.last_error = None
    except Exception as e:
        request.app.state.last_error = str(e)
    await broadcast(request.app)
    return {"ok": True}
