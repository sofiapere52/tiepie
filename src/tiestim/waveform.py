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


def _active_window(n_total: int, pre: int, post: int) -> tuple[int, int]:
    end = n_total - post
    if pre >= end or pre < 0:
        raise ValueError("pre_stim + post_stim leaves no active samples")
    return pre, end


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
        # Amplitude-normalized triangle [-1,1] at f_hz
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
        # Linear ramp up then down each period
        ph = (f_hz * ta) % 1.0
        saw = np.where(ph < 0.5, 4 * ph - 1, 3 - 4 * ph)
        y[pre:end] = amp * saw
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
        raise ValueError("pre_stim_s + post_stim_s >= total_time_s")
    t = np.arange(n, dtype=np.float64) / sr
    y1 = np.zeros(n, dtype=np.float64)
    y2 = np.zeros(n, dtype=np.float64)
    a, b = _active_window(n, pre, post)

    if p.mode == "standard":
        if abs(p.frequency_hz) >= 0.5 * sr:
            raise ValueError(f"frequency_hz violates Nyquist at sample_rate_hz={sr}")
        _fill_active(y1, a, b, t, p.shape, p.frequency_hz, p.amplitude_v, p.pulse_width_s, sr)
        y2[:] = y1
    else:
        fc, df, r1, r2 = p.ti_parts()
        f2 = fc + df
        nyq = 0.5 * sr
        for name, f in ("carrier", fc), ("carrier+Δf", f2):
            if abs(f) >= nyq:
                raise ValueError(f"{name} frequency {f} Hz violates Nyquist at sr={sr}")
        a1 = p.amplitude_v * r1
        a2 = p.amplitude_v * r2
        ta = t[a:b] - t[a]
        y1[a:b] = a1 * np.cos(2 * np.pi * fc * ta)
        y2[a:b] = -a2 * np.cos(2 * np.pi * f2 * ta)

    # Normalize each channel to ±1 for arbitrary buffer; hardware amplitude set separately
    def norm_unit(z: np.ndarray) -> np.ndarray:
        m = np.max(np.abs(z))
        if m <= 0:
            return z
        return z / m

    # Keep relative TI amplitudes: scale common factor into gen.amplitude on each channel separately
    # Here we encode shape in [-1,1] and set per-gen amplitude to desired Vpk.
    y1n = norm_unit(y1)
    y2n = norm_unit(y2)
    return WaveformPair(ch1=y1n.astype(np.float32), ch2=y2n.astype(np.float32), sample_rate_hz=sr, n_samples=n)


def peak_voltages(p: StimParams) -> tuple[float, float]:
    """Per-channel peak voltage at BNC after normalization (matches build_waveforms scaling)."""
    if p.mode == "standard":
        return p.amplitude_v, p.amplitude_v
    _, _, r1, r2 = p.ti_parts()
    return p.amplitude_v * r1, p.amplitude_v * r2


def validate_against_limits(
    n_samples: int,
    data_length_max: int,
    data_length_min: int,
    amp1: float,
    amp2: float,
    amp_max: float,
) -> None:
    if n_samples < data_length_min or n_samples > data_length_max:
        raise ValueError(
            f"buffer length {n_samples} not in [{data_length_min}, {data_length_max}]"
        )
    if amp1 > amp_max or amp2 > amp_max:
        raise ValueError(f"amplitude exceeds generator max {amp_max} V")


def numpy_to_array_f(arr: np.ndarray) -> array:
    if arr.dtype != np.float32:
        arr = arr.astype(np.float32)
    return array("f", arr.tolist())

