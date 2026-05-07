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
