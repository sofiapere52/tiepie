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
    SESSION_MARKER_AMP_FRAC,
    SESSION_MARKER_S,
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
    fus_active_channel: int | None = None


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
        fus_active_channel=p.fus.channel if p.mode == "fus" and p.fus is not None else None,
    )


def _stim_only_samples(
    p_hw: StimParams, preview_sr: float
) -> tuple[np.ndarray, np.ndarray]:
    """Build the AWG's stim-phase samples for one repetition at preview_sr.

    Control / TI / TBS → the ``stim_time_s`` window.
    fUS → the full ``n_pulses × ISI`` train (one ISI cycle tiled
    ``n_pulses`` times in display space).

    No pre/post padding here — that is added by ``_one_rep_envelope`` so
    the preview honours the silent gaps before and after the AWG runs.
    """
    p_prev = p_hw.model_copy(update={"sample_rate_hz": preview_sr})
    wf = build_waveforms(p_prev)
    a1, a2 = peak_amplitudes(p_prev)
    v1, v2 = waveform_to_amps(wf, a1, a2)
    if p_hw.mode == "fus" and p_hw.fus is not None and p_hw.fus.n_pulses > 1:
        v1 = np.tile(v1, p_hw.fus.n_pulses)
        v2 = np.tile(v2, p_hw.fus.n_pulses)
    return v1, v2


def _one_rep_envelope(
    p_hw: StimParams,
    preview_sr: float,
    *,
    with_marker: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build one full repetition envelope at preview_sr.

    Layout: ``[pre_stim zeros]  |  [stim samples]  |  [post_stim zeros]``.
    The total length is ``total_time_s × preview_sr`` so the preview
    accurately depicts the AWG-off windows around the stim — that is what
    fixes the "no silence between reps" artefact.

    When ``with_marker`` is True **and** ``trigger_out`` is on, the very
    first ``SESSION_MARKER_S`` of the pre_stim zeros is overwritten with
    a constant DC value of ``SESSION_MARKER_AMP_FRAC × peak`` on each
    active channel — that is exactly what the AWG plays during the
    session prologue. When only ``trigger_in`` is on, the prologue is a
    silent primer; the envelope is identical to the non-marker case
    (no visible change in the plot).
    """
    v1_stim, v2_stim = _stim_only_samples(p_hw, preview_sr)
    n_stim = len(v1_stim)
    n_pre = int(round(p_hw.pre_stim_s * preview_sr))
    n_post = int(round(p_hw.post_stim_s * preview_sr))
    n_total = n_pre + n_stim + n_post
    v1 = np.zeros(n_total, dtype=np.float64)
    v2 = np.zeros(n_total, dtype=np.float64)
    if n_stim > 0:
        v1[n_pre : n_pre + n_stim] = v1_stim
        v2[n_pre : n_pre + n_stim] = v2_stim

    if with_marker and p_hw.trigger_out:
        n_marker = max(1, int(round(SESSION_MARKER_S * preview_sr)))
        # Never overwrite the stim block, even if pre_stim is suspiciously
        # short (the validator guarantees pre_stim_s ≥ 105 ms here, but
        # we keep this defensive).
        n_marker = min(n_marker, n_pre)
        a1, a2 = peak_amplitudes(p_hw)
        # For fUS the inactive channel's peak is 0 → marker is invisible
        # on that trace (matches the actual gen.set_data on the AWG).
        v1[:n_marker] = SESSION_MARKER_AMP_FRAC * a1
        v2[:n_marker] = SESSION_MARKER_AMP_FRAC * a2

    t = np.arange(n_total, dtype=np.float64) / preview_sr
    return t, v1, v2


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
    needs_prologue = bool(p_hw.trigger_in or p_hw.trigger_out)
    preview_sr = _preview_source_sr(
        p_hw,
        source_budget=_PREVIEW_SOURCE_BUDGET_SAMPLES_PER_CYCLE,
        hw_sr_cap=hw_sr,
    )

    # ---- Windowed (zoom) mode ------------------------------------------------
    # Triggered when both endpoints of a meaningful sub-range are provided.
    # We build two per-rep envelopes (one with marker, one without) at the
    # preview source rate and slice each touched cycle from the appropriate
    # one. The pre/post zeros are part of the envelope, so the silent gaps
    # appear naturally in the zoom — no more zigzag between consecutive reps.
    if (
        body.t_start_s is not None
        and body.t_end_s is not None
        and body.t_end_s > body.t_start_s
        and cycle_dur > 0
        and p.mode != "fus"
    ):
        t0 = max(0.0, float(body.t_start_s))
        t1 = min(total_dur, float(body.t_end_s))
        if t1 > t0 and (t1 - t0) < total_dur * 0.999:
            t_normal, v1_normal, v2_normal = _one_rep_envelope(
                p_hw, preview_sr, with_marker=False
            )
            if needs_prologue:
                _, v1_marker, v2_marker = _one_rep_envelope(
                    p_hw, preview_sr, with_marker=True
                )
            else:
                v1_marker, v2_marker = v1_normal, v2_normal
            n_per_cycle = len(t_normal)
            sum_normal = (v1_normal + v2_normal) if show_sum else None
            sum_marker = (v1_marker + v2_marker) if show_sum else None

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
                pick1 = v1_marker if k == 0 else v1_normal
                pick2 = v2_marker if k == 0 else v2_normal
                picksum = sum_marker if k == 0 else sum_normal
                v1_parts.append(pick1[i0:i1])
                v2_parts.append(pick2[i0:i1])
                if picksum is not None:
                    sum_parts.append(picksum[i0:i1])
                t_parts.append(t_normal[i0:i1] + k * cycle_dur)

            if not t_parts:
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

    # ---- Overview mode (all modes, including fUS) ---------------------------
    # Build a "normal" repetition envelope (pre_stim zeros + stim + post_stim
    # zeros), decimate it once to ``pts_per_cycle = max_pts // n_cycles``, and
    # tile that decimated envelope across reps 1..N-1. Rep 0 is decimated
    # separately when a session prologue is scheduled, so the marker pulse
    # shows up on the first rep only.
    t_normal, v1_normal, v2_normal = _one_rep_envelope(
        p_hw, preview_sr, with_marker=False
    )
    sum_normal = (v1_normal + v2_normal) if show_sum else None

    n_cycles = max(1, n_cycles_requested)
    pts_per_cycle = max(2, body.preview_max_points // n_cycles)

    to_dec_normal = [v1_normal, v2_normal]
    if sum_normal is not None:
        to_dec_normal.append(sum_normal)
    dec_normal, dec_t = _decimate_minmax(to_dec_normal, t_normal, pts_per_cycle)
    n_pts = len(dec_t)

    if needs_prologue:
        _, v1_marker, v2_marker = _one_rep_envelope(
            p_hw, preview_sr, with_marker=True
        )
        sum_marker = (v1_marker + v2_marker) if show_sum else None
        to_dec_marker = [v1_marker, v2_marker]
        if sum_marker is not None:
            to_dec_marker.append(sum_marker)
        dec_marker, _ = _decimate_minmax(to_dec_marker, t_normal, pts_per_cycle)
    else:
        dec_marker = dec_normal

    big_t = np.empty(n_pts * n_cycles, dtype=np.float64)
    for i in range(n_cycles):
        big_t[i * n_pts : (i + 1) * n_pts] = dec_t + i * cycle_dur
    big_v1 = np.concatenate(
        [dec_marker[0]] + [dec_normal[0]] * (n_cycles - 1)
    )
    big_v2 = np.concatenate(
        [dec_marker[1]] + [dec_normal[1]] * (n_cycles - 1)
    )
    if sum_normal is not None:
        big_sum = np.concatenate(
            [dec_marker[2]] + [dec_normal[2]] * (n_cycles - 1)
        )
    else:
        big_sum = None

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
    rate next to the total-time display, without building a waveform.

    Reports buffer math relative to what the AWG will actually hold: the
    stim block (Control/TI/TBS) or one ISI cycle (fUS).
    """
    from tiestim.waveform import _buffer_duration_s

    try:
        sr, note = choose_hardware_sample_rate(params)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    buf_dur = _buffer_duration_s(params)
    resp = {
        "ok": True,
        "sample_rate_hz": sr,
        "sample_rate_note": note,
        "total_time_s": params.total_time_s,
        "stim_time_s": params.stim_time_s,
        "buffer_duration_s": buf_dur,
        "buffer_samples": int(round(buf_dur * sr)),
    }
    if params.mode == "fus" and params.fus is not None:
        isi = params.fus.sonication_duration_s + params.fus.isi_off_s
        resp["fus"] = {
            "channel": params.fus.channel,
            "carrier_hz": params.fus.carrier_hz,
            "prf_hz": params.fus.prf_hz,
            "prf_duty": params.fus.prf_duty,
            "tone_burst_s": params.fus.tone_burst_s,
            "sonication_duration_s": params.fus.sonication_duration_s,
            "isi_off_s": params.fus.isi_off_s,
            "isi_s": isi,
            "n_pulses": params.fus.n_pulses,
            "train_duration_s": params.fus.n_pulses * isi,
            "amplitude_mv_pp": params.fus.amplitude_mv_pp,
            "expected_amplifier_output_v_pp": params.fus.amplitude_mv_pp * 316 / 1000.0,
        }
    return resp


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
