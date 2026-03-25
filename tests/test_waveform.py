import numpy as np

from tiestim.models import StimParams
from tiestim.waveform import build_waveforms, peak_voltages


def test_ti_uses_cos_at_t0():
    p = StimParams(
        mode="ti",
        shape="sine",
        frequency_hz=2000,
        carrier_hz=2000,
        delta_f_hz=10,
        amplitude_ratio="1:1",
        amplitude_v=2.0,
        total_time_s=0.001,
        pre_stim_s=0,
        post_stim_s=0,
        sample_rate_hz=200_000,
        repetitions=1,
    )
    wf = build_waveforms(p)
    pre = int(round(p.pre_stim_s * p.sample_rate_hz))
    # After normalization to unit peak, first active sample ~ cos(0)=+1
    assert wf.ch1[pre] > 0.99
    a1, a2 = peak_voltages(p)
    assert abs(a1 - 1.0) < 1e-6 and abs(a2 - 1.0) < 1e-6


def test_ti_second_channel_negative_cos_at_t0():
    p = StimParams(
        mode="ti",
        shape="sine",
        frequency_hz=100,
        carrier_hz=100,
        delta_f_hz=5,
        amplitude_ratio="1:1",
        amplitude_v=1.0,
        total_time_s=0.01,
        pre_stim_s=0,
        post_stim_s=0,
        sample_rate_hz=50_000,
        repetitions=1,
    )
    wf = build_waveforms(p)
    assert wf.ch2[0] < -0.95
