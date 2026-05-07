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
from tiestim.waveform import (
    _max_signal_frequency_hz,
    build_waveforms,
    choose_hardware_sample_rate,
    peak_amplitudes,
    waveform_to_amps,
)

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
    n_cycles_requested: int
    t_total_plot_s: float
    y_max: float
    mode: str
    show_sum: bool
    pre_stim_s: float
    stim_time_s: float
    post_stim_s: float
    hw_sample_rate_hz: float
    hw_sample_rate_note: str = ""
    preview_sample_rate_hz: float


# Max source samples allocated for **one cycle** of preview (overview or
# zoom). Chosen so a typical long TI run (e.g. 120 s @ ~100 kHz ≈ 12.1 M
# samples) still builds at the **hardware** sample rate; going far above
# that only wastes RAM without exceeding what the AWG will output.
_PREVIEW_SOURCE_BUDGET_SAMPLES_PER_CYCLE = 20_000_000
# Target source density: samples per period of the highest signal frequency
# (capped by hardware rate and the budget above).
_PREVIEW_TARGET_SAMPLES_PER_CYCLE = 50


def _preview_source_sr(
    p: StimParams,
    *,
    source_budget: int,
    hw_sr_cap: float,
) -> float:
    """Pick the source sample rate for building preview waveform samples.

    The preview **never samples denser than the AWG will** for these
    parameters: the result is ``min(ideal, hw_sr_cap)`` where ``ideal`` comes
    from a fidelity target and an optional per-cycle sample budget.

    ``p.sample_rate_hz`` is ignored here — pass the hardware rate explicitly
    as ``hw_sr_cap`` so this cannot drift from ``choose_hardware_sample_rate``.
    """
    fmax = _max_signal_frequency_hz(p)
    ideal = fmax * _PREVIEW_TARGET_SAMPLES_PER_CYCLE
    total_s = p.total_time_s
    if total_s > 0 and ideal * total_s > source_budget:
        ideal = source_budget / total_s
    ideal = max(ideal, fmax * 4)
    return float(min(ideal, hw_sr_cap))


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


def _build_preview_response(
    p: StimParams,
    *,
    hw_sr: float,
    hw_note: str,
    preview_sr: float,
    n_cycles_requested: int,
    n_cycles_shown: int,
    show_sum: bool,
    t: np.ndarray,
    v1: np.ndarray,
    v2: np.ndarray,
    sum_arr: np.ndarray | None,
) -> PreviewOut:
    """Pack the decimated arrays into the wire format. Centralised so the
    overview and windowed branches return identical shapes."""
    y_max = float(
        max(
            np.max(np.abs(v1)) if len(v1) else 0.0,
            np.max(np.abs(v2)) if len(v2) else 0.0,
            np.max(np.abs(sum_arr)) if sum_arr is not None and len(sum_arr) else 0.0,
        )
    )
    if y_max <= 0:
        y_max = 1.0
    return PreviewOut(
        ch1=v1.tolist(),
        ch2=v2.tolist(),
        sum_v=sum_arr.tolist() if sum_arr is not None else None,
        t_seconds=t.tolist(),
        cycle_duration_s=p.total_time_s,
        n_cycles_shown=n_cycles_shown,
        n_cycles_requested=n_cycles_requested,
        t_total_plot_s=float(t[-1] - t[0]) if len(t) else 0.0,
        y_max=y_max,
        mode=p.mode,
        show_sum=show_sum,
        pre_stim_s=p.pre_stim_s,
        stim_time_s=p.stim_time_s,
        post_stim_s=p.post_stim_s,
        hw_sample_rate_hz=hw_sr,
        hw_sample_rate_note=hw_note,
        preview_sample_rate_hz=preview_sr,
    )


@router.post("/waveform/preview", response_model=PreviewOut)
async def waveform_preview(body: StimRequest):
    p = body.params

    # Match what the hardware will actually do: replace the user's nominal
    # sample rate with the auto-picked one. If the request fails the fidelity
    # floor (would also fail at /arm), surface the same error here so the
    # user sees it before hitting Load.
    try:
        hw_sr, hw_note = choose_hardware_sample_rate(p)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    p_hw = p.model_copy(update={"sample_rate_hz": hw_sr})

    n_cycles_requested = int(p.repetitions) if p.repetitions > 0 else 1
    cycle_dur = float(p.total_time_s)
    total_dur = cycle_dur * max(1, n_cycles_requested)
    show_sum = p.mode == "ti"

    # ---- Windowed (zoom) mode ------------------------------------------------
    # Triggered when both endpoints of a meaningful sub-range are provided.
    # We build ONE cycle at a higher source sample rate (memory: bounded by
    # `_PREVIEW_SOURCE_ZOOM_MAX_SAMPLES`) and slice the touched cycles to
    # exactly the requested window. Because the window is small, the carrier
    # appears smooth even at deep zoom — that is the whole point of this
    # branch.
    if (
        body.t_start_s is not None
        and body.t_end_s is not None
        and body.t_end_s > body.t_start_s
        and cycle_dur > 0
    ):
        t0 = max(0.0, float(body.t_start_s))
        t1 = min(total_dur, float(body.t_end_s))
        # Only treat as a "real" zoom when the window is a meaningful slice.
        # Within ~0.1 % of the full extent, fall through to overview to avoid
        # spurious zoom-mode renders on the initial draw.
        if t1 > t0 and (t1 - t0) < total_dur * 0.999:
            preview_sr = _preview_source_sr(
                p_hw,
                source_budget=_PREVIEW_SOURCE_BUDGET_SAMPLES_PER_CYCLE,
                hw_sr_cap=hw_sr,
            )
            preview_params = p_hw.model_copy(update={"sample_rate_hz": preview_sr})
            wf = build_waveforms(preview_params)
            a1, a2 = peak_amplitudes(preview_params)
            v1_one, v2_one = waveform_to_amps(wf, a1, a2)
            n_per_cycle = len(v1_one)
            t_one = np.arange(n_per_cycle, dtype=np.float64) / preview_sr
            sum_one = (v1_one + v2_one).astype(np.float64) if show_sum else None

            kmin = max(0, int(t0 // cycle_dur))
            kmax = min(n_cycles_requested - 1, int(t1 // cycle_dur))
            v1_parts, v2_parts, sum_parts, t_parts = [], [], [], []
            for k in range(kmin, kmax + 1):
                cstart = max(0.0, t0 - k * cycle_dur)
                cend = min(cycle_dur, t1 - k * cycle_dur)
                i0 = int(np.floor(cstart * preview_sr))
                i1 = min(n_per_cycle, int(np.ceil(cend * preview_sr)))
                if i1 <= i0:
                    continue
                v1_parts.append(v1_one[i0:i1])
                v2_parts.append(v2_one[i0:i1])
                if sum_one is not None:
                    sum_parts.append(sum_one[i0:i1])
                t_parts.append(t_one[i0:i1] + k * cycle_dur)

            if not t_parts:
                # Window fell entirely outside any built sample (defensive;
                # the kmin/kmax computation makes this essentially unreachable).
                big_t = np.array([t0, t1], dtype=np.float64)
                big_v1 = np.zeros(2)
                big_v2 = np.zeros(2)
                big_sum = np.zeros(2) if show_sum else None
            else:
                big_t = np.concatenate(t_parts)
                big_v1 = np.concatenate(v1_parts)
                big_v2 = np.concatenate(v2_parts)
                big_sum = np.concatenate(sum_parts) if sum_parts else None

            to_dec = [big_v1, big_v2]
            if big_sum is not None:
                to_dec.append(big_sum)
            dec_arrs, big_t = _decimate_minmax(to_dec, big_t, body.preview_max_points)
            big_v1, big_v2 = dec_arrs[0], dec_arrs[1]
            if big_sum is not None:
                big_sum = dec_arrs[2]

            return _build_preview_response(
                p,
                hw_sr=hw_sr,
                hw_note=hw_note,
                preview_sr=preview_sr,
                n_cycles_requested=n_cycles_requested,
                n_cycles_shown=(kmax - kmin + 1),
                show_sum=show_sum,
                t=big_t,
                v1=big_v1,
                v2=big_v2,
                sum_arr=big_sum,
            )

    # ---- Overview mode -------------------------------------------------------
    # Build ONE cycle, decimate it to `pts_per_cycle = max_pts // n_cycles`
    # display points, and tile that decimated cycle in display space. This
    # keeps source memory bounded by a single cycle no matter how many
    # repetitions the user asked for, so all reps are always shown.
    preview_sr = _preview_source_sr(
        p_hw,
        source_budget=_PREVIEW_SOURCE_BUDGET_SAMPLES_PER_CYCLE,
        hw_sr_cap=hw_sr,
    )
    preview_params = p_hw.model_copy(update={"sample_rate_hz": preview_sr})
    wf = build_waveforms(preview_params)
    a1, a2 = peak_amplitudes(preview_params)
    v1_one, v2_one = waveform_to_amps(wf, a1, a2)
    n_per_cycle = len(v1_one)
    t_one = np.arange(n_per_cycle, dtype=np.float64) / preview_sr
    sum_one = (v1_one + v2_one).astype(np.float64) if show_sum else None

    n_cycles = max(1, n_cycles_requested)
    pts_per_cycle = max(2, body.preview_max_points // n_cycles)

    to_dec = [v1_one, v2_one]
    if sum_one is not None:
        to_dec.append(sum_one)
    dec_arrs, dec_t = _decimate_minmax(to_dec, t_one, pts_per_cycle)
    n_pts = len(dec_t)

    big_t = np.empty(n_pts * n_cycles, dtype=np.float64)
    for i in range(n_cycles):
        big_t[i * n_pts : (i + 1) * n_pts] = dec_t + i * cycle_dur
    big_v1 = np.tile(dec_arrs[0], n_cycles)
    big_v2 = np.tile(dec_arrs[1], n_cycles)
    big_sum = np.tile(dec_arrs[2], n_cycles) if sum_one is not None else None

    return _build_preview_response(
        p,
        hw_sr=hw_sr,
        hw_note=hw_note,
        preview_sr=preview_sr,
        n_cycles_requested=n_cycles_requested,
        n_cycles_shown=n_cycles,
        show_sum=show_sum,
        t=big_t,
        v1=big_v1,
        v2=big_v2,
        sum_arr=big_sum,
    )


@router.post("/plan")
async def plan(params: StimParams):
    """Return the hardware sample rate that would be used for these params,
    plus an optional human-readable note. Used by the GUI to show the chosen
    rate next to the total-time display, without building a waveform."""
    try:
        sr, note = choose_hardware_sample_rate(params)
        return {
            "ok": True,
            "sample_rate_hz": sr,
            "sample_rate_note": note,
            "total_time_s": params.total_time_s,
            "buffer_samples": int(round(params.total_time_s * sr)),
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/arm")
async def arm(request: Request, params: StimParams):
    global _last_params
    _ensure_poller(request.app)
    sess: BaseSession = request.app.state.session
    schedule_log(request.app, "Loading waveforms to device(s)…")
    try:
        # Auto-select an HS5 sample rate that fits the AWG buffer and gives
        # at least TARGET_SAMPLES_PER_CYCLE samples per period of the highest
        # signal frequency. Whatever the user/UI sent in `params.sample_rate_hz`
        # is overridden by the chosen value so long stimulations no longer
        # overflow the 64 Mi-sample buffer.
        sr, note = choose_hardware_sample_rate(params)
        eff = params.model_copy(update={"sample_rate_hz": sr})
        wf = build_waveforms(eff)
        sess.arm(eff, wf)
        # Store the effective params (with the chosen sample rate) so the CSV
        # log column `sample_rate_hz` reflects what actually played, not the
        # value originally posted by the GUI.
        _last_params = eff
        request.app.state.last_error = None
    except Exception as e:
        request.app.state.last_error = str(e)
        await broadcast(request.app)
        schedule_log(request.app, f"Load failed: {e}")
        raise HTTPException(400, str(e)) from e
    await broadcast(request.app)
    sr_msg = f"Hardware sample rate: {sr:,.0f} Hz"
    if note:
        sr_msg += f"  ({note})"
    schedule_log(request.app, sr_msg)
    schedule_log(request.app, "Loaded — ready for Start")
    if params.mode == "ti":
        schedule_log(
            request.app,
            "TI sync active — ensure CMI cable connects both HS5 units",
        )
    return {"ok": True, "sample_rate_hz": sr, "sample_rate_note": note}


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
