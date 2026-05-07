from __future__ import annotations

from array import array
from dataclasses import dataclass

import numpy as np

from tiestim.models import StimParams


@dataclass
class WaveformPair:
    """Two AWG buffers (unit peak ±1 before hardware amplitude scaling)."""

    ch1: np.ndarray
    ch2: np.ndarray
    sample_rate_hz: float
    n_samples: int


# ---- HS5 hardware constraints ------------------------------------------------
# AWG memory per channel (samples). Exceeding this causes libtiepie to refuse
# the buffer with "buffer length N not in [1, 67108864]".
HS5_BUFFER_MAX = 67_108_864
# Hardware ceiling for the AWG sample rate. The HS5 TG-AWG nominally goes up to
# ~240 MS/s; we use this only as an upper bound — most stimulation frequencies
# pick a much lower rate.
HS5_MAX_SR = 240_000_000
# Target visual / signal fidelity: at least this many samples per period of the
# highest signal frequency. Square waves benefit from more, but 50 keeps edges
# under ~2 % of the period and is plenty for the analog smoothing of the HS5.
TARGET_SAMPLES_PER_CYCLE = 50
# Hard floor — below this the waveform stops resembling the requested shape and
# we refuse to play it at all (the user must shorten the run, lower the
# frequency, or use repetitions).
MIN_SAMPLES_PER_CYCLE = 10


def _max_signal_frequency_hz(p: StimParams) -> float:
    """Highest signal frequency present in the requested stimulation."""
    freqs: list[float] = []
    if p.mode == "ti":
        if p.carrier_hz is not None and p.carrier_hz > 0:
            freqs.append(float(p.carrier_hz))
            if p.delta_f_hz is not None:
                freqs.append(abs(float(p.carrier_hz) + float(p.delta_f_hz)))
    else:
        if p.ch1 is not None and p.ch1.enabled and p.ch1.frequency_hz > 0:
            freqs.append(float(p.ch1.frequency_hz))
        if p.ch2 is not None and p.ch2.enabled and p.ch2.frequency_hz > 0:
            freqs.append(float(p.ch2.frequency_hz))
    return max(freqs) if freqs else 1.0


def choose_hardware_sample_rate(p: StimParams) -> tuple[float, str]:
    """Pick a hardware AWG sample rate that meets two constraints:

    1. Fits the HS5 internal buffer (one buffer holds ``total_time_s * sr``
       samples, must be ≤ ``HS5_BUFFER_MAX``).
    2. Keeps at least ``TARGET_SAMPLES_PER_CYCLE`` samples per period of the
       highest signal frequency for a clean waveform.

    Returns ``(sample_rate_hz, note)``. The ``note`` is empty when the chosen
    rate meets the fidelity target; otherwise it is a short human-readable
    string explaining why a lower rate had to be used (so the GUI / live log
    can surface it).

    Raises ``ValueError`` when the buffer cannot accommodate even
    ``MIN_SAMPLES_PER_CYCLE`` samples per period — in that case there is no
    rate at which the requested run is reproducible, and the message tells
    the user what to change.
    """
    fmax = _max_signal_frequency_hz(p)
    total = p.total_time_s

    ideal = min(HS5_MAX_SR, fmax * TARGET_SAMPLES_PER_CYCLE)
    sr = ideal
    note = ""

    if total > 0:
        buffer_cap_sr = HS5_BUFFER_MAX / total
        if buffer_cap_sr < ideal:
            sr = buffer_cap_sr
            sps = sr / fmax if fmax > 0 else float("inf")
            note = (
                f"reduced from {ideal:,.0f} Hz to fit the {HS5_BUFFER_MAX:,d}-sample "
                f"AWG buffer ({sps:.1f} samples/period at {fmax:g} Hz)"
            )

    sps = sr / fmax if fmax > 0 else float("inf")
    if sps < MIN_SAMPLES_PER_CYCLE:
        max_total_at_min = (
            HS5_BUFFER_MAX / (MIN_SAMPLES_PER_CYCLE * fmax)
            if fmax > 0
            else float("inf")
        )
        raise ValueError(
            f"Cannot represent this stimulation accurately: {total:g} s at "
            f"{fmax:g} Hz would force the AWG to {sps:.2f} samples per period "
            f"(minimum {MIN_SAMPLES_PER_CYCLE}). Reduce the total time below "
            f"{max_total_at_min:.1f} s, lower the highest signal frequency, or "
            f"split the run into hardware repetitions of a shorter buffer."
        )
    return float(sr), note


def _active_window(n_total: int, pre: int, post: int) -> tuple[int, int]:
    end = n_total - post
    if pre >= end or pre < 0:
        raise ValueError("pre_stim + post_stim leaves no active samples")
    return pre, end


def _apply_ramp(y: np.ndarray, pre_i: int, end_i: int, sr: float, ramp_s: float) -> None:
    """Linear 0->1 over first ramp_s and 1->0 over last ramp_s within [pre_i, end_i)."""
    if ramp_s <= 0:
        return
    n = end_i - pre_i
    if n <= 0:
        return
    ramp_samples = int(round(ramp_s * sr))
    if ramp_samples <= 0:
        return
    up = np.linspace(0.0, 1.0, ramp_samples, endpoint=False)
    down = np.linspace(1.0, 0.0, ramp_samples, endpoint=False)
    actual_up = min(ramp_samples, n)
    y[pre_i : pre_i + actual_up] *= up[:actual_up]
    actual_down = min(ramp_samples, n)
    y[end_i - actual_down : end_i] *= down[-actual_down:]


def _fill_active(
    y: np.ndarray,
    pre: int,
    end: int,
    t: np.ndarray,
    shape: str,
    f_hz: float,
    amp: float,
    pulse_width_s: float | None,
    sr: float,
) -> None:
    ta = t[pre:end] - t[pre]
    dur = end - pre
    if dur <= 0:
        return
    if shape == "sine":
        y[pre:end] = amp * np.sin(2 * np.pi * f_hz * ta)
    elif shape == "triangle":
        ph = (f_hz * ta) % 1.0
        tri = np.where(ph < 0.5, 4 * ph - 1, 3 - 4 * ph)
        y[pre:end] = amp * tri
    elif shape == "square":
        period = 1.0 / f_hz
        pw = pulse_width_s if pulse_width_s is not None else period / 2
        pw = min(max(pw, 0.0), period)
        ph = (ta % period) / period
        high = ph < (pw / period)
        y[pre:end] = np.where(high, amp, -amp)
    elif shape == "ramp":
        ph = (f_hz * ta) % 1.0
        saw = np.where(ph < 0.5, 4 * ph - 1, 3 - 4 * ph)
        y[pre:end] = amp * saw
    elif shape == "tbs":
        y[pre:end] = amp * np.sin(2 * np.pi * f_hz * ta)
    else:
        raise ValueError(f"unknown shape {shape}")


def build_waveforms(p: StimParams) -> WaveformPair:
    sr = p.sample_rate_hz
    n = int(round(p.total_time_s * sr))
    if n < 8:
        raise ValueError("total_time_s * sample_rate too small")
    pre = int(round(p.pre_stim_s * sr))
    post = int(round(p.post_stim_s * sr))
    if pre + post >= n:
        raise ValueError("pre_stim_s + post_stim_s leaves no active segment")
    t = np.arange(n, dtype=np.float64) / sr
    y1 = np.zeros(n, dtype=np.float64)
    y2 = np.zeros(n, dtype=np.float64)
    a, b = _active_window(n, pre, post)

    if p.mode == "control":
        assert p.ch1 is not None and p.ch2 is not None
        for ch, y in [(p.ch1, y1), (p.ch2, y2)]:
            if not ch.enabled:
                continue
            if abs(ch.frequency_hz) >= 0.5 * sr:
                raise ValueError(f"frequency_hz violates Nyquist at sample_rate_hz={sr}")
            _fill_active(y, a, b, t, ch.shape, ch.frequency_hz, ch.amplitude_ma, ch.pulse_width_s, sr)
        _apply_ramp(y1, a, b, sr, p.ramp_s)
        _apply_ramp(y2, a, b, sr, p.ramp_s)
    else:
        fc, df, r1, r2 = p.ti_parts()
        f2 = fc + df
        nyq = 0.5 * sr
        for name, f in [("carrier", fc), ("carrier+df", f2)]:
            if abs(f) >= nyq:
                raise ValueError(f"{name} frequency {f} Hz violates Nyquist at sr={sr}")
        assert p.amplitude_ma is not None
        a1 = p.amplitude_ma * r1
        a2 = p.amplitude_ma * r2
        ta = t[a:b] - t[a]
        y1[a:b] = a1 * np.cos(2 * np.pi * fc * ta)
        y2[a:b] = -a2 * np.cos(2 * np.pi * f2 * ta)

        if p.tbs_freq_hz is not None and p.tbs_freq_hz > 0:
            burst_dur = 3.0 / abs(df)
            tbs_period = 1.0 / p.tbs_freq_hz
            gate = np.zeros(len(ta), dtype=np.float64)
            pos = 0.0
            t_end = ta[-1] if len(ta) else 0.0
            while pos < t_end:
                gate[(ta >= pos) & (ta < pos + burst_dur)] = 1.0
                pos += tbs_period
            y1[a:b] *= gate
            y2[a:b] *= gate

        _apply_ramp(y1, a, b, sr, p.ramp_s)
        _apply_ramp(y2, a, b, sr, p.ramp_s)

    def norm_unit(z: np.ndarray) -> np.ndarray:
        m = np.max(np.abs(z))
        if m <= 0:
            return z
        return z / m

    y1n = norm_unit(y1)
    y2n = norm_unit(y2)
    return WaveformPair(ch1=y1n.astype(np.float32), ch2=y2n.astype(np.float32), sample_rate_hz=sr, n_samples=n)


def peak_amplitudes(p: StimParams) -> tuple[float, float]:
    """Per-channel peak amplitude (mA) after normalization; numerically equals HS5 voltage (V)."""
    if p.mode == "control":
        assert p.ch1 is not None and p.ch2 is not None
        return (p.ch1.amplitude_ma if p.ch1.enabled else 0.0,
                p.ch2.amplitude_ma if p.ch2.enabled else 0.0)
    assert p.amplitude_ma is not None
    _, _, r1, r2 = p.ti_parts()
    return p.amplitude_ma * r1, p.amplitude_ma * r2


def numpy_to_array_f(arr: np.ndarray) -> array:
    if arr.dtype != np.float32:
        arr = arr.astype(np.float32)
    return array("f", arr.tolist())


def waveform_to_amps(wf: WaveformPair, peak1: float, peak2: float) -> tuple[np.ndarray, np.ndarray]:
    """Scale normalized buffers to amps at output."""
    return wf.ch1.astype(np.float64) * peak1, wf.ch2.astype(np.float64) * peak2
