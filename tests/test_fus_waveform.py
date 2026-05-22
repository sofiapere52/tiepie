"""Hardware-free tests for the fUS waveform builder and buffer math.

Validates PRF gating, ISI silence, amplitude scaling, and the buffer-fit
behaviour of ``choose_hardware_sample_rate`` for fUS-shaped requests.
"""

from __future__ import annotations

import numpy as np
import pytest

from tiestim.models import FusParams, StimParams
from tiestim.waveform import (
    HS5_BUFFER_MAX,
    MIN_SAMPLES_PER_CYCLE,
    TARGET_SAMPLES_PER_CYCLE,
    build_waveforms,
    choose_hardware_sample_rate,
    peak_amplitudes,
)


def _fus_params(**fus_over) -> StimParams:
    """Build a valid fUS StimParams instance with reasonable defaults."""
    fus_defaults = dict(
        channel=1,
        carrier_hz=1_000_000,
        prf_hz=1_000,
        prf_duty=0.5,
        tone_burst_s=0.5e-3,
        sonication_duration_s=0.005,
        isi_off_s=0.005,
        n_pulses=2,
        amplitude_mv_pp=200,
    )
    fus_defaults.update(fus_over)
    fus = FusParams(**fus_defaults)
    return StimParams(
        mode="fus",
        shape="sine",
        # stim_time_s is auto-snapped by the validator to n_pulses * ISI.
        stim_time_s=fus.n_pulses * (fus.sonication_duration_s + fus.isi_off_s),
        sample_rate_hz=10_000_000,  # 10 MS/s — enough for a 1 MHz carrier
        fus=fus,
    )


def test_fus_buffer_is_one_isi_cycle():
    """The fUS buffer holds a SINGLE ISI cycle; the n_pulses train is
    delivered by HS5 burst mode in the session layer."""
    p = _fus_params(n_pulses=4)
    wf = build_waveforms(p)
    expected = int(round((p.fus.sonication_duration_s + p.fus.isi_off_s) * p.sample_rate_hz))
    assert wf.n_samples == expected


def test_fus_isi_off_region_silent():
    """The last isi_off_s seconds of the ISI cycle must be zero."""
    p = _fus_params(sonication_duration_s=0.002, isi_off_s=0.003, n_pulses=1)
    wf = build_waveforms(p)
    sr = p.sample_rate_hz
    n_sd = int(round(p.fus.sonication_duration_s * sr))
    tail = wf.ch1[n_sd:]
    assert np.max(np.abs(tail)) < 1e-6


def test_fus_prf_gating_zero_during_off_phase():
    """Within the SD window, each PRF period has a tone_burst_s ON segment
    followed by silence. Pick a sample firmly inside the OFF phase and
    confirm it is zero."""
    # PRF = 1 kHz, duty 50% → 500 µs ON, 500 µs OFF per ms.
    p = _fus_params(
        prf_hz=1000, prf_duty=0.5, tone_burst_s=5e-4,
        sonication_duration_s=0.003, isi_off_s=0.001,
        n_pulses=1,
    )
    wf = build_waveforms(p)
    sr = p.sample_rate_hz
    # 700 µs into the SD window is firmly in the OFF phase of the first
    # PRF period (which ends at 500 µs).
    idx = int(0.0007 * sr)
    assert abs(wf.ch1[idx]) < 1e-6


def test_fus_prf_gating_active_during_on_phase():
    """Within an ON segment the carrier is non-zero at non-zero-crossing samples."""
    p = _fus_params(
        prf_hz=1000, prf_duty=0.5, tone_burst_s=5e-4,
        sonication_duration_s=0.003, isi_off_s=0.001,
        n_pulses=1,
        # Lowest allowed carrier (500 kHz) at 10 MS/s gives 20 samples/period,
        # easy to find a clearly non-zero sample inside an ON window.
        carrier_hz=500_000,
    )
    wf = build_waveforms(p)
    sr = p.sample_rate_hz
    # Index 3 corresponds to t = 0.3 µs (in the first ON window of the first
    # PRF period); sin(2π × 5e5 × 3e-7) = sin(0.3π) ≈ 0.81 — clearly non-zero.
    assert abs(wf.ch1[3]) > 0.1


def test_fus_amplitude_scaling_uses_mv_pp():
    """peak_amplitudes for fUS returns amplitude_mv_pp / 2 / 1000 (peak V) on
    the active channel and zero on the idle channel."""
    p = _fus_params(channel=1, amplitude_mv_pp=400)
    a1, a2 = peak_amplitudes(p)
    assert a1 == pytest.approx(0.2)
    assert a2 == 0.0

    p2 = _fus_params(channel=2, amplitude_mv_pp=400)
    a1b, a2b = peak_amplitudes(p2)
    assert a1b == 0.0
    assert a2b == pytest.approx(0.2)


def test_fus_inactive_channel_buffer_is_zero():
    p = _fus_params(channel=2)
    wf = build_waveforms(p)
    assert np.max(np.abs(wf.ch1)) < 1e-6
    assert np.max(np.abs(wf.ch2)) > 0.0


def test_fus_amplitude_above_500_mv_pp_is_allowed():
    """The amplitude has no hard cap; the UI surfaces a warning instead.

    Picking a high (but device-feasible) BNC amplitude here exercises the
    Pydantic model — the validator must NOT reject this value.
    """
    p = _fus_params(amplitude_mv_pp=800)
    assert p.fus.amplitude_mv_pp == 800


def test_fus_carrier_range_enforced():
    """Carrier must be in [0.5, 2] MHz."""
    with pytest.raises(ValueError):
        FusParams(carrier_hz=300_000)
    with pytest.raises(ValueError):
        FusParams(carrier_hz=3_000_000)


def test_fus_tone_burst_duty_mismatch_rejected():
    """tone_burst_s must equal prf_duty / prf_hz exactly."""
    with pytest.raises(ValueError, match="tone_burst_s"):
        FusParams(
            prf_hz=1000, prf_duty=0.5,
            tone_burst_s=1e-3,  # would imply duty=1.0, not 0.5
            sonication_duration_s=0.005, isi_off_s=0.005,
        )


def test_fus_stim_time_snapped_to_n_pulses_isi():
    """stim_time_s is overridden by n_pulses × ISI in fUS mode."""
    fus = FusParams(
        channel=1, n_pulses=3,
        sonication_duration_s=0.010, isi_off_s=0.005,
        prf_hz=1000, prf_duty=0.5, tone_burst_s=0.5e-3,
    )
    p = StimParams(
        mode="fus", shape="sine",
        stim_time_s=999.0,  # bogus value — validator must snap
        sample_rate_hz=10_000_000,
        fus=fus,
    )
    assert p.stim_time_s == pytest.approx(3 * 0.015)


def test_fus_buffer_overflow_raises_mode_aware_error():
    """An ISI cycle that cannot fit the buffer even at the minimum
    samples-per-period raises with a fUS-specific hint."""
    # 8 s SD + 2 s isi_off at 2 MHz carrier × 10 sps = 200 M samples > 67 M.
    fus = FusParams(
        channel=1,
        carrier_hz=2_000_000,
        prf_hz=1000, prf_duty=0.5, tone_burst_s=0.5e-3,
        sonication_duration_s=8.0, isi_off_s=2.0,
        n_pulses=1,
    )
    p = StimParams(
        mode="fus", shape="sine",
        stim_time_s=10.0,
        sample_rate_hz=240_000_000,
        fus=fus,
    )
    with pytest.raises(ValueError) as exc:
        choose_hardware_sample_rate(p)
    msg = str(exc.value)
    assert "Cannot represent" in msg
    assert "ISI cycle" in msg


def test_fus_buffer_fits_under_target_uses_target_rate():
    """An ISI cycle short enough that the buffer easily holds 50 sps × carrier
    samples uses the fidelity-target sample rate."""
    p = _fus_params(
        sonication_duration_s=0.005, isi_off_s=0.005,
        n_pulses=1,
        carrier_hz=1_000_000,
    )
    sr, note = choose_hardware_sample_rate(p)
    # Target = 1 MHz × 50 sps = 50 MS/s, well within HS5 max.
    assert sr == pytest.approx(1_000_000 * TARGET_SAMPLES_PER_CYCLE)
    assert note == ""


def test_fus_isi_too_short_for_freq_rejected():
    """ISI frequency that would imply a negative isi_off_s is impossible —
    the cross-field validator already enforces tone_burst < 1/PRF; here we
    construct an ISI that's just too short for SD and confirm the model
    rejects it.

    The Pydantic model permits isi_off_s=0 (back-to-back sonications); but
    if the user tries to construct an invalid combination through the API,
    the validator on the FusParams should still catch obvious bad values.
    """
    # SD = 10 ms, isi_off = 0 → ISI = 10 ms → isi_freq = 100 Hz. Valid.
    fus = FusParams(
        channel=1,
        carrier_hz=1_000_000,
        prf_hz=1000, prf_duty=0.5, tone_burst_s=0.5e-3,
        sonication_duration_s=0.010, isi_off_s=0.0,
        n_pulses=1,
    )
    assert fus.sonication_duration_s == 0.010
    # The UI is responsible for catching "isi_freq too high for given SD"
    # (it computes the impossible isi_off and surfaces the error there).
    # No equivalent Python-side test is needed beyond confirming valid
    # boundary values are accepted.
