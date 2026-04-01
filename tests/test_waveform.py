import numpy as np
import pytest

from tiestim.models import ChannelParams, StimParams
from tiestim.waveform import build_waveforms, peak_amplitudes


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
