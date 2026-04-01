from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tiestim.logger import append_stim_row, row_from_params
from tiestim.models import StimParams, StimRequest
from tiestim.session import BaseSession
from tiestim.waveform import build_waveforms, peak_amplitudes, waveform_to_amps

router = APIRouter()

_last_params: StimParams | None = None
_run_started_monotonic: float | None = None
_poller_task: asyncio.Task | None = None
_poller_last_json: str | None = None
_user_stop = threading.Event()


def _state_core(app: Any) -> dict:
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


def snapshot_payload(app: Any) -> dict:
    return {"type": "snapshot", **_state_core(app)}


async def push_snapshot(ws, app: Any) -> None:
    await ws.send_json(snapshot_payload(app))


async def broadcast(app: Any) -> None:
    data = json.dumps(snapshot_payload(app))
    dead = []
    for ws in list(app.state.ws_clients):
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in app.state.ws_clients:
            app.state.ws_clients.remove(ws)


async def emit_log(app: Any, message: str) -> None:
    payload = {"type": "log", "message": message, "ts": time.time()}
    data = json.dumps(payload)
    dead = []
    for ws in list(app.state.ws_clients):
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in app.state.ws_clients:
            app.state.ws_clients.remove(ws)


def schedule_log(app: Any, message: str) -> None:
    loop = getattr(app.state, "loop", None)
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(emit_log(app, message), loop)


def _chunk_sleep(total_s: float, step: float = 0.05) -> bool:
    """Return True if interrupted (_user_stop)."""
    if total_s <= 0:
        return False
    end = time.monotonic() + total_s
    while time.monotonic() < end:
        if _user_stop.is_set():
            return True
        time.sleep(min(step, end - time.monotonic()))
    return False


def start_phase_log_thread(app: Any) -> None:
    pr = _last_params
    if pr is None:
        return

    def run() -> None:
        if pr.repetitions == 0:
            schedule_log(app, "Stimulation started (continuous until STOP)")
            return
        N = int(pr.repetitions)
        pre, stim, post = pr.pre_stim_s, pr.stim_time_s, pr.post_stim_s
        schedule_log(app, f"Stimulation started ({N} repetition(s))")
        for i in range(N):
            if _user_stop.is_set():
                return
            schedule_log(app, f"Repetition {i + 1}/{N} started")
            schedule_log(app, "Pre-stim block started")
            if _chunk_sleep(pre):
                return
            schedule_log(app, "Stimulation segment started")
            if _chunk_sleep(stim):
                return
            schedule_log(app, "Post-stim block started")
            if _chunk_sleep(post):
                return

    threading.Thread(target=run, daemon=True).start()


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
        path = append_stim_row(
            row_from_params(
                _last_params,
                devs[0].serial if devs else "?",
                devs[1].serial if len(devs) > 1 else "?",
                "ok",
                "",
                dur,
            )
        )
        schedule_log(app, f"Stimulation ended; log saved to {path}")
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
        snap = json.dumps(snapshot_payload(app))
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
    return snapshot_payload(request.app)


@router.post("/connect")
async def connect(request: Request):
    _ensure_poller(request.app)
    try:
        devs = request.app.state.session.connect()
    except Exception as e:
        request.app.state.last_error = str(e)
        await broadcast(request.app)
        schedule_log(request.app, f"Connect failed: {e}")
        raise HTTPException(503, str(e)) from e
    request.app.state.last_error = None
    await broadcast(request.app)
    sns = [d.serial for d in devs if d.serial]
    schedule_log(request.app, f"Device(s) connected: {' / '.join(sns)}")
    try:
        diag = request.app.state.session.diagnostics()
        for d in diag:
            schedule_log(request.app, f"  Gen {d.get('slot')}: {d}")
    except Exception:
        pass
    return {"ok": True, "devices": sns}


@router.get("/diag")
async def diag(request: Request):
    sess = request.app.state.session
    try:
        return {"generators": sess.diagnostics()}
    except Exception as e:
        return {"error": str(e)}


class PreviewOut(BaseModel):
    ch1: list[float]
    ch2: list[float]
    sum_v: list[float] | None = None
    t_seconds: list[float]
    cycle_duration_s: float
    n_cycles_shown: int
    t_total_plot_s: float
    y_max: float
    mode: str
    show_sum: bool
    pre_stim_s: float
    stim_time_s: float
    post_stim_s: float


_PREVIEW_MAX_SAMPLES = 200_000


def _preview_sample_rate(p) -> float:
    """Choose a preview sample rate high enough for visual fidelity but
    capped so a single buffer stays manageable in RAM."""
    freqs = []
    if p.mode == "ti":
        if p.carrier_hz:
            freqs.append(p.carrier_hz)
        if p.carrier_hz and p.delta_f_hz:
            freqs.append(abs(p.carrier_hz + p.delta_f_hz))
    else:
        if p.ch1 and p.ch1.enabled:
            freqs.append(p.ch1.frequency_hz)
        if p.ch2 and p.ch2.enabled:
            freqs.append(p.ch2.frequency_hz)
    max_f = max(freqs) if freqs else 100.0
    ideal = max_f * 200
    total_s = p.total_time_s
    if total_s > 0 and ideal * total_s > _PREVIEW_MAX_SAMPLES:
        ideal = _PREVIEW_MAX_SAMPLES / total_s
    ideal = max(ideal, max_f * 4)
    return min(ideal, p.sample_rate_hz)


def _decimate_minmax(
    arrays: list[np.ndarray], t_axis: np.ndarray, max_pts: int
) -> tuple[list[np.ndarray], np.ndarray]:
    """Min-max envelope decimation.

    Each bucket of consecutive samples contributes its min and max value,
    preserving the true amplitude envelope even when the carrier frequency
    is far above the display resolution.
    """
    n = len(t_axis)
    if n <= max_pts:
        return arrays, t_axis
    n_buckets = max(1, max_pts // 2)
    bsz = n // n_buckets
    trim = n_buckets * bsz

    t2d = t_axis[:trim].reshape(n_buckets, bsz)
    out_t = np.empty(n_buckets * 2, dtype=t_axis.dtype)
    out_t[0::2] = t2d[:, 0]
    out_t[1::2] = t2d[:, -1]

    out_arrays: list[np.ndarray] = []
    for arr in arrays:
        a2d = arr[:trim].reshape(n_buckets, bsz)
        out = np.empty(n_buckets * 2, dtype=arr.dtype)
        out[0::2] = a2d.min(axis=1)
        out[1::2] = a2d.max(axis=1)
        out_arrays.append(out)

    return out_arrays, out_t


@router.post("/waveform/preview", response_model=PreviewOut)
async def waveform_preview(body: StimRequest):
    p = body.params
    preview_sr = _preview_sample_rate(p)
    preview_params = p.model_copy(update={"sample_rate_hz": preview_sr})
    wf = build_waveforms(preview_params)
    a1, a2 = peak_amplitudes(preview_params)
    v1, v2 = waveform_to_amps(wf, a1, a2)
    n_cycles = int(p.repetitions) if p.repetitions > 0 else 1
    n_cycles = min(n_cycles, 3)
    V1 = np.tile(v1, n_cycles)
    V2 = np.tile(v2, n_cycles)
    t_axis = np.arange(len(V1), dtype=np.float64) / preview_sr
    show_sum = p.mode == "ti"
    sum_arr = (V1 + V2).astype(np.float64) if show_sum else None

    max_pts = body.preview_max_points
    to_dec = [V1, V2]
    if sum_arr is not None:
        to_dec.append(sum_arr)
    dec_arrs, t_axis = _decimate_minmax(to_dec, t_axis, max_pts)
    V1 = dec_arrs[0]
    V2 = dec_arrs[1]
    if sum_arr is not None:
        sum_arr = dec_arrs[2]

    y_max = float(
        max(
            np.max(np.abs(V1)) if len(V1) else 0.0,
            np.max(np.abs(V2)) if len(V2) else 0.0,
            np.max(np.abs(sum_arr)) if sum_arr is not None and len(sum_arr) else 0.0,
        )
    )
    if y_max <= 0:
        y_max = 1.0
    t_l = t_axis.tolist()
    c1 = V1.tolist()
    c2 = V2.tolist()
    sum_l: list[float] | None = None
    if sum_arr is not None:
        sum_l = sum_arr.tolist()
    return PreviewOut(
        ch1=c1,
        ch2=c2,
        sum_v=sum_l,
        t_seconds=t_l,
        cycle_duration_s=p.total_time_s,
        n_cycles_shown=n_cycles,
        t_total_plot_s=float(t_axis[-1]) if len(t_axis) else 0.0,
        y_max=y_max,
        mode=p.mode,
        show_sum=show_sum,
        pre_stim_s=p.pre_stim_s,
        stim_time_s=p.stim_time_s,
        post_stim_s=p.post_stim_s,
    )


@router.post("/arm")
async def arm(request: Request, params: StimParams):
    global _last_params
    _ensure_poller(request.app)
    sess: BaseSession = request.app.state.session
    schedule_log(request.app, "Loading waveforms to device(s)…")
    try:
        wf = build_waveforms(params)
        sess.arm(params, wf)
        _last_params = params
        request.app.state.last_error = None
    except Exception as e:
        request.app.state.last_error = str(e)
        await broadcast(request.app)
        schedule_log(request.app, f"Load failed: {e}")
        raise HTTPException(400, str(e)) from e
    await broadcast(request.app)
    schedule_log(request.app, "Loaded — ready for Start")
    if params.mode == "ti":
        schedule_log(
            request.app,
            "TI sync active — ensure CMI cable connects both HS5 units",
        )
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
        schedule_log(request.app, f"Start failed: {e}")
        raise HTTPException(400, str(e)) from e
    await broadcast(request.app)
    start_phase_log_thread(request.app)
    return {"ok": True}


@router.post("/stop")
async def stop_run(request: Request):
    global _last_params, _run_started_monotonic
    _ensure_poller(request.app)
    _user_stop.set()
    schedule_log(request.app, "STOP requested")
    sess: BaseSession = request.app.state.session
    try:
        sess.stop()
        if _last_params is not None:
            devs = sess.status()
            dur = None
            if _run_started_monotonic is not None:
                dur = time.monotonic() - _run_started_monotonic
            path = append_stim_row(
                row_from_params(
                    _last_params,
                    devs[0].serial if devs else "?",
                    devs[1].serial if len(devs) > 1 else "?",
                    "aborted",
                    "operator_stop",
                    dur,
                )
            )
            schedule_log(request.app, f"Stimulation stopped; log saved to {path}")
        _run_started_monotonic = None
        request.app.state.last_error = None
    except Exception as e:
        request.app.state.last_error = str(e)
    await broadcast(request.app)
    return {"ok": True}
