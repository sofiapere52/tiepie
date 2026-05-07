import numpy as np
import pytest

from tiestim.models import ChannelParams, StimParams
from tiestim.waveform import (
    HS5_BUFFER_MAX,
    MIN_SAMPLES_PER_CYCLE,
    TARGET_SAMPLES_PER_CYCLE,
    build_waveforms,
    choose_hardware_sample_rate,
    peak_amplitudes,
)


def _ti_params(**kwargs):
    defaults = dict(
        mode="ti",
        shape="sine",
        frequency_hz=2000,
        carrier_hz=2000,
        delta_f_hz=10,
        amplitude_ratio="1:1",
        amplitude_ma=2.0,
        stim_time_s=0.001,
        pre_stim_s=0,
        post_stim_s=0,
        ramp_s=0,
        sample_rate_hz=200_000,
        repetitions=1,
    )
    defaults.update(kwargs)
    return StimParams(**defaults)


def test_ti_uses_cos_at_t0():
    p = _ti_params()
    wf = build_waveforms(p)
    pre = int(round(p.pre_stim_s * p.sample_rate_hz))
    assert wf.ch1[pre] > 0.99
    a1, a2 = peak_amplitudes(p)
    assert abs(a1 - 1.0) < 1e-6 and abs(a2 - 1.0) < 1e-6


def test_ti_delta_f_zero_anti_phase():
    """Δf = 0 is allowed: both channels at carrier, in anti-phase."""
    p = _ti_params(delta_f_hz=0, stim_time_s=0.005, sample_rate_hz=100_000)
    wf = build_waveforms(p)
    pre = int(round(p.pre_stim_s * p.sample_rate_hz))
    assert wf.ch1[pre] > 0.99
    assert wf.ch2[pre] < -0.99


def test_ti_delta_f_zero_tbs_rejected():
    """TBS still requires Δf != 0 because burst_dur = 3/|Δf|."""
    with pytest.raises(ValueError, match="non-zero delta_f_hz"):
        StimParams(
            mode="ti",
            shape="tbs",
            carrier_hz=2000,
            delta_f_hz=0,
            tbs_freq_hz=5,
            amplitude_ratio="1:1",
            amplitude_ma=1.0,
            stim_time_s=0.5,
            sample_rate_hz=200_000,
        )


def test_ti_second_channel_negative_cos_at_t0():
    p = _ti_params(
        carrier_hz=100,
        delta_f_hz=5,
        amplitude_ma=1.0,
        stim_time_s=0.01,
        sample_rate_hz=50_000,
        frequency_hz=100,
    )
    wf = build_waveforms(p)
    assert wf.ch2[0] < -0.95


def test_ramp_exceeds_stim_time():
    with pytest.raises(ValueError, match="ramp_s cannot exceed"):
        StimParams(
            mode="ti",
            shape="sine",
            carrier_hz=1000,
            delta_f_hz=10,
            amplitude_ratio="1:1",
            amplitude_ma=1.0,
            stim_time_s=0.01,
            ramp_s=0.02,
            sample_rate_hz=100_000,
            frequency_hz=1000,
        )


def test_ramp_validation_overlap():
    with pytest.raises(ValueError, match="overlap"):
        StimParams(
            mode="ti",
            shape="sine",
            carrier_hz=1000,
            delta_f_hz=10,
            amplitude_ratio="1:1",
            amplitude_ma=1.0,
            stim_time_s=0.01,
            ramp_s=0.006,
            sample_rate_hz=100_000,
            frequency_hz=1000,
        )


def test_control_independent_channels():
    p = StimParams(
        mode="control",
        stim_time_s=0.002,
        sample_rate_hz=100_000,
        ch1=ChannelParams(shape="sine", frequency_hz=1000, amplitude_ma=1.0),
        ch2=ChannelParams(shape="sine", frequency_hz=500, amplitude_ma=0.5),
    )
    wf = build_waveforms(p)
    a1, a2 = peak_amplitudes(p)
    assert abs(a1 - 1.0) < 1e-6 and abs(a2 - 0.5) < 1e-6
    assert wf.n_samples == int(round(p.total_time_s * p.sample_rate_hz))


def test_control_channel_disabled():
    p = StimParams(
        mode="control",
        stim_time_s=0.01,
        sample_rate_hz=10_000,
        ch1=ChannelParams(enabled=True, shape="sine", frequency_hz=100, amplitude_ma=1.0),
        ch2=ChannelParams(enabled=False, shape="sine", frequency_hz=100, amplitude_ma=1.0),
    )
    wf = build_waveforms(p)
    assert np.max(np.abs(wf.ch1)) > 0.5
    assert np.max(np.abs(wf.ch2)) < 1e-9


def test_hw_sr_short_run_targets_fidelity():
    """Short stimulation: rate should hit the fidelity target (50 sps)."""
    p = _ti_params(carrier_hz=2000, delta_f_hz=10, stim_time_s=0.1)
    sr, note = choose_hardware_sample_rate(p)
    fmax = 2010.0
    assert sr == pytest.approx(fmax * TARGET_SAMPLES_PER_CYCLE)
    assert note == ""


def test_hw_sr_long_run_within_buffer_no_reduction():
    """120 s @ 2 kHz carrier fits in the buffer at the fidelity target
    (~12 M samples ≤ 67 M), so no reduction note is needed and the rate
    matches the fidelity target."""
    p = _ti_params(carrier_hz=2000, delta_f_hz=10, stim_time_s=120.0)
    sr, note = choose_hardware_sample_rate(p)
    fmax = 2010.0
    assert sr == pytest.approx(fmax * TARGET_SAMPLES_PER_CYCLE)
    assert sr * p.total_time_s <= HS5_BUFFER_MAX
    assert note == ""


def test_hw_sr_long_run_reduces_to_fit_buffer():
    """A run long enough that even the fidelity-target rate would overflow
    the AWG buffer must be reduced; the rate should land exactly at the
    buffer limit and produce an explanatory note."""
    # 1000 s × 2010 Hz × 50 sps = 100.5 M > 67 M  → must reduce.
    p = _ti_params(carrier_hz=2000, delta_f_hz=10, stim_time_s=1000.0)
    sr, note = choose_hardware_sample_rate(p)
    assert sr * p.total_time_s <= HS5_BUFFER_MAX
    assert sr == pytest.approx(HS5_BUFFER_MAX / p.total_time_s)
    fmax = 2010.0
    sps = sr / fmax
    assert sps >= MIN_SAMPLES_PER_CYCLE
    assert "AWG buffer" in note


def test_hw_sr_extremely_long_run_raises():
    """A multi-hour run at 2 kHz cannot meet even the minimum fidelity floor."""
    p = _ti_params(
        carrier_hz=2000,
        delta_f_hz=10,
        stim_time_s=10_000.0,  # ~2.8 hours
    )
    with pytest.raises(ValueError, match="Cannot represent"):
        choose_hardware_sample_rate(p)


def test_hw_sr_control_mode_uses_max_channel_freq():
    p = StimParams(
        mode="control",
        stim_time_s=1.0,
        sample_rate_hz=500_000,
        ch1=ChannelParams(shape="sine", frequency_hz=100, amplitude_ma=1.0),
        ch2=ChannelParams(shape="sine", frequency_hz=2000, amplitude_ma=1.0),
    )
    sr, note = choose_hardware_sample_rate(p)
    assert sr == pytest.approx(2000 * TARGET_SAMPLES_PER_CYCLE)
    assert note == ""


def test_hw_sr_control_disabled_channel_ignored():
    p = StimParams(
        mode="control",
        stim_time_s=1.0,
        sample_rate_hz=500_000,
        ch1=ChannelParams(enabled=True, shape="sine", frequency_hz=100, amplitude_ma=1.0),
        ch2=ChannelParams(enabled=False, shape="sine", frequency_hz=20_000, amplitude_ma=1.0),
    )
    sr, _ = choose_hardware_sample_rate(p)
    # Disabled channel must not raise the requested rate.
    assert sr == pytest.approx(100 * TARGET_SAMPLES_PER_CYCLE)


def test_total_time_computed():
    p = StimParams(
        mode="control",
        stim_time_s=0.1,
        pre_stim_s=0.02,
        post_stim_s=0.03,
        sample_rate_hz=1000,
        ch1=ChannelParams(shape="sine", frequency_hz=10, amplitude_ma=1),
        ch2=ChannelParams(shape="sine", frequency_hz=10, amplitude_ma=1),
    )
    assert abs(p.total_time_s - 0.15) < 1e-9
