"""Tests for the /api/waveform/preview endpoint.

Covers the two render paths added when the preview was made tile-at-display
(so all repetitions are always shown) and windowed (so deep zoom stays
carrier-resolved). Hardware-free: uses TIESTIM_MOCK so no libtiepie required.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    os.environ["TIESTIM_MOCK"] = "1"
    from tiestim.api.app import app

    with TestClient(app) as c:
        yield c


def _ti_body(reps: int, stim_time_s: float, **overrides):
    body = {
        "params": {
            "mode": "ti",
            "shape": "sine",
            "carrier_hz": 2000,
            "delta_f_hz": 10,
            "amplitude_ratio": "1:1",
            "amplitude_ma": 1.0,
            "stim_time_s": stim_time_s,
            "pre_stim_s": 0,
            "post_stim_s": 0,
            "ramp_s": 0,
            "sample_rate_hz": 500_000,
            "repetitions": reps,
            "frequency_hz": 2000,
        },
        "preview_max_points": 8000,
    }
    body["params"].update(overrides)
    return body


def test_preview_overview_shows_all_reps_short(client):
    """For a small total run, all 5 reps are tiled in the response."""
    body = _ti_body(reps=5, stim_time_s=0.05)
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["n_cycles_requested"] == 5
    assert out["n_cycles_shown"] == 5
    # The time axis must span ~5 × 0.05 s = 0.25 s (min/max decimation can
    # drop the final few source samples of the last bucket, hence the loose
    # tolerance).
    t = out["t_seconds"]
    assert t[-1] == pytest.approx(0.25, rel=0.05)
    assert t[-1] >= 0.20  # at minimum, well past 4 of the 5 cycles


def test_preview_overview_shows_all_reps_long(client):
    """Long stim with many reps: tile-at-display-level keeps every rep
    in the response (the previous code dropped reps when it would not fit
    in the source budget)."""
    body = _ti_body(reps=10, stim_time_s=120.0)
    body["preview_max_points"] = 20_000
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["n_cycles_requested"] == 10
    assert out["n_cycles_shown"] == 10
    # Sanity: time axis spans ~10 × 120 s.
    t = out["t_seconds"]
    assert t[-1] == pytest.approx(1200.0, rel=0.01)


def test_preview_windowed_returns_only_window(client):
    """Zoom mode: response covers exactly [t_start, t_end]."""
    body = _ti_body(reps=3, stim_time_s=1.0)
    body["t_start_s"] = 0.5
    body["t_end_s"] = 1.5  # crosses the boundary between rep 1 and rep 2
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    t = out["t_seconds"]
    assert t[0] >= 0.5 - 1e-3
    assert t[-1] <= 1.5 + 1e-3
    assert out["n_cycles_shown"] == 2  # rep 0 and rep 1 are touched


def test_preview_sample_rate_never_exceeds_hw(client):
    """Preview source rate is always capped by the auto-picked hardware rate."""
    for path, body_extra in (
        ("overview", {}),
        ("window", {"t_start_s": 1.0, "t_end_s": 1.05}),
    ):
        body = _ti_body(reps=2, stim_time_s=0.5)
        body.update(body_extra)
        r = client.post("/api/waveform/preview", json=body)
        assert r.status_code == 200, (path, r.text)
        out = r.json()
        assert out["preview_sample_rate_hz"] <= out["hw_sample_rate_hz"] + 1e-6, path


def test_preview_windowed_matches_hw_sample_rate_when_possible(client):
    """Windowed preview uses the same dense sampling as hardware when the
    fidelity target fits inside the per-cycle budget and the HW cap."""
    body = _ti_body(reps=1, stim_time_s=120.0)
    body["t_start_s"] = 60.0
    body["t_end_s"] = 60.01
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    fmax = 2010.0
    assert out["preview_sample_rate_hz"] <= out["hw_sample_rate_hz"] + 1e-6
    assert out["preview_sample_rate_hz"] == pytest.approx(out["hw_sample_rate_hz"])
    assert out["preview_sample_rate_hz"] >= fmax * 50 * 0.99


def test_preview_overview_below_hw_when_ram_budget_binds(client):
    """Very long single-cycle overview can be subsampled below HW rate when
    one buffer would exceed the preview RAM budget; still never above HW."""
    body = _ti_body(reps=1, stim_time_s=300.0)
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["preview_sample_rate_hz"] <= out["hw_sample_rate_hz"] + 1e-6
    assert out["preview_sample_rate_hz"] < out["hw_sample_rate_hz"]


def test_preview_includes_sample_rate_metadata(client):
    body = _ti_body(reps=1, stim_time_s=0.1)
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    for k in ("hw_sample_rate_hz", "preview_sample_rate_hz", "n_cycles_requested"):
        assert k in out, f"missing field: {k}"


def test_preview_rejects_unrenderable_run(client):
    """A run too long for the AWG even at the minimum sps must return a
    400 with the same explanatory message as /arm."""
    body = _ti_body(reps=1, stim_time_s=10_000.0)  # ~2.8 hours @ 2 kHz
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 400
    assert "Cannot represent" in r.text


# ---- fUS preview tests -----------------------------------------------------

def _fus_body(**fus_over):
    fus = {
        "channel": 1,
        "carrier_hz": 1_000_000,
        "prf_hz": 1000,
        "prf_duty": 0.5,
        "tone_burst_s": 0.5e-3,
        "sonication_duration_s": 0.005,
        "isi_off_s": 0.005,
        "n_pulses": 3,
        "amplitude_mv_pp": 200,
    }
    fus.update(fus_over)
    return {
        "params": {
            "mode": "fus",
            "shape": "sine",
            "stim_time_s": 0.001,  # snapped server-side
            "pre_stim_s": 0.02,
            "post_stim_s": 0.02,
            "ramp_s": 0,
            "sample_rate_hz": 500_000,
            "repetitions": 1,
            "fus": fus,
        },
        "preview_max_points": 20_000,
    }


def test_preview_fus_overview_includes_all_pulses(client):
    """The /preview overview for fUS tiles the ISI cycle n_pulses times and
    pads pre/post."""
    body = _fus_body(n_pulses=3, sonication_duration_s=0.005, isi_off_s=0.005)
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["mode"] == "fus"
    assert out["fus_active_channel"] == 1
    # total = pre + n_pulses * (sd + isi_off) + post = 0.02 + 0.03 + 0.02 = 0.07 s
    t = out["t_seconds"]
    assert t[-1] == pytest.approx(0.07, rel=0.05)


def test_preview_fus_buffer_overflow_returns_400(client):
    """fUS request whose ISI cycle won't fit the buffer returns 400 with the
    fUS-specific hint (or the generic message)."""
    body = _fus_body(
        carrier_hz=2_000_000,
        sonication_duration_s=8.0,
        isi_off_s=2.0,
    )
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 400
    txt = r.text
    assert "ISI" in txt or "Cannot represent" in txt


def test_preview_fus_inactive_channel_is_zero(client):
    """When channel=2, the ch1 preview series must be all zeros."""
    body = _fus_body(channel=2)
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert max(abs(x) for x in out["ch1"]) < 1e-6
    assert max(abs(x) for x in out["ch2"]) > 0.0
    assert out["fus_active_channel"] == 2


def test_preview_fus_no_ti_sum(client):
    """fUS is a single-channel modality — show_sum must be False."""
    body = _fus_body()
    r = client.post("/api/waveform/preview", json=body)
    out = r.json()
    assert out["show_sum"] is False
    assert out["sum_v"] is None


# ---- session-prologue / silent-gap preview tests --------------------------

def test_preview_shows_pre_post_silence(client):
    """Pre/post zeros must appear in the preview so the X-axis (which spans
    pre+stim+post per cycle) matches the trace (which used to span only stim,
    creating an artefact where consecutive reps got connected straight)."""
    body = _ti_body(reps=1, stim_time_s=0.05, pre_stim_s=0.02, post_stim_s=0.02)
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    t = out["t_seconds"]
    ch1 = out["ch1"]
    # The first ~10% of the trace is inside pre_stim → must be ~zero.
    n = len(t)
    head = ch1[: n // 10]
    assert max(abs(x) for x in head) < 1e-6, "pre_stim samples should be zero"
    # The last ~10% of the trace is inside post_stim → must be ~zero.
    tail = ch1[-n // 10 :]
    assert max(abs(x) for x in tail) < 1e-6, "post_stim samples should be zero"
    # The trace overall must reach the stim amplitude in the middle.
    assert max(abs(x) for x in ch1) >= 0.4


def test_preview_marker_only_on_first_rep(client):
    """With trigger_out enabled, rep 0 starts with a 5 ms × 0.1 × peak DC
    marker. Reps 2..N must start with pure silence (no marker)."""
    pre = 0.2  # > 105 ms threshold
    stim = 0.05
    post = 0.0
    body = _ti_body(
        reps=3,
        stim_time_s=stim,
        pre_stim_s=pre,
        post_stim_s=post,
        trigger_out=True,
    )
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    t = out["t_seconds"]
    ch1 = out["ch1"]
    cycle = pre + stim + post
    # Sample 1: inside rep 0's first 5 ms (marker is HIGH).
    # Sample 2: inside rep 1's first 5 ms (no marker → still 0).
    # The decimation makes a perfectly exact index tricky, so we just check
    # the per-cycle MAX of the first 1% of each cycle.
    def cycle_head_max(k: int) -> float:
        head_t_end = k * cycle + 0.01  # 10 ms window covers the 5 ms marker
        head_vals = [abs(v) for ti, v in zip(t, ch1) if k * cycle <= ti < head_t_end]
        return max(head_vals) if head_vals else 0.0

    rep0_head = cycle_head_max(0)
    rep1_head = cycle_head_max(1)
    rep2_head = cycle_head_max(2)
    assert rep0_head > 0.0, "expected non-zero marker in rep 0"
    assert rep1_head < 1e-6, "rep 1 must start silent (no marker)"
    assert rep2_head < 1e-6, "rep 2 must start silent (no marker)"


def test_preview_no_marker_when_only_trigger_in(client):
    """trigger_in alone uses a SILENT 5 ms primer for edge detection — it
    must NOT show a visible marker on the preview."""
    body = _ti_body(
        reps=1,
        stim_time_s=0.05,
        pre_stim_s=0.2,
        post_stim_s=0.0,
        trigger_in=True,
    )
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    t = out["t_seconds"]
    ch1 = out["ch1"]
    # The first 10 ms of the trace (covering the 5 ms primer + slack) must
    # be all zero — the primer is silent in this configuration.
    head_vals = [abs(v) for ti, v in zip(t, ch1) if ti < 0.01]
    assert max(head_vals) < 1e-6


def test_preview_fus_marker_on_active_channel(client):
    """fUS + trigger_out: the marker appears only on the channel selected
    by fus.channel; the inactive channel stays silent."""
    body = _fus_body(channel=1)
    body["params"]["pre_stim_s"] = 0.2
    body["params"]["post_stim_s"] = 0.02
    body["params"]["trigger_out"] = True
    r = client.post("/api/waveform/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    t = out["t_seconds"]
    # Active channel (ch1): marker present in the first ~10 ms.
    head_ch1 = [abs(v) for ti, v in zip(t, out["ch1"]) if ti < 0.01]
    assert max(head_ch1) > 0.0, "marker should appear on the active fUS channel"
    # Inactive channel (ch2): silent throughout.
    assert max(abs(v) for v in out["ch2"]) < 1e-6


def test_plan_returns_fus_metadata(client):
    """The /plan endpoint exposes the derived ISI / train metadata for fUS."""
    body = _fus_body(n_pulses=4, sonication_duration_s=0.010, isi_off_s=0.005)["params"]
    r = client.post("/api/plan", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True
    assert "fus" in out
    assert out["fus"]["n_pulses"] == 4
    assert out["fus"]["isi_s"] == pytest.approx(0.015)
    assert out["fus"]["train_duration_s"] == pytest.approx(4 * 0.015)
    # buffer_duration_s for fUS is one ISI cycle.
    assert out["buffer_duration_s"] == pytest.approx(0.015)
    # stim_time_s gets snapped by the validator.
    assert out["stim_time_s"] == pytest.approx(4 * 0.015)
