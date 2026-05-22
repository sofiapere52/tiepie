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
    elif p.mode == "fus":
        assert p.fus is not None
        freqs.append(float(p.fus.carrier_hz))
    else:
        if p.ch1 is not None and p.ch1.enabled and p.ch1.frequency_hz > 0:
            freqs.append(float(p.ch1.frequency_hz))
        if p.ch2 is not None and p.ch2.enabled and p.ch2.frequency_hz > 0:
            freqs.append(float(p.ch2.frequency_hz))
    return max(freqs) if freqs else 1.0


SESSION_MARKER_S = 5e-3
SESSION_MARKER_AMP_FRAC = 0.1
SESSION_MARKER_RELOAD_SAFETY_S = 0.1
SESSION_PROLOGUE_MIN_PRE_STIM_S = SESSION_MARKER_S + SESSION_MARKER_RELOAD_SAFETY_S


def session_prologue_needed(p: StimParams) -> bool:
    """True iff a session-prologue buffer is needed for the run.

    The prologue is a single short playback of either a DC marker (when
    ``trigger_out`` is enabled, so EXT 1 can emit a clean rising edge for the
    downstream system) or a silent buffer (when ``trigger_in`` is enabled but
    ``trigger_out`` is not, so the host can detect the EXT 2 rising edge via
    ``gen.is_running``).
    """
    return bool(p.trigger_out or p.trigger_in)


def build_session_primer(sr_hz: float, *, with_marker: bool) -> np.ndarray:
    """Build the 5-ms session prologue buffer.

    ``with_marker=True``  → constant +SESSION_MARKER_AMP_FRAC of the gen's
    configured amplitude (a benign DC step on the BNC, large enough to be
    visible on a scope but well below the stim amplitude).

    ``with_marker=False`` → all zeros (silent — used purely so the host can
    catch the ``is_running`` rising edge that follows an EXT 2 input edge).
    """
    n = max(8, int(round(SESSION_MARKER_S * sr_hz)))
    if with_marker:
        return np.full(n, SESSION_MARKER_AMP_FRAC, dtype=np.float32)
    return np.zeros(n, dtype=np.float32)


def _buffer_duration_s(p: StimParams) -> float:
    """Duration of the AWG buffer the hardware will actually play.

    Pre/post stim are software-managed waits in the session worker, so the
    AWG buffer holds **only stim samples**:
    - Control/TI/TBS: one stim block of ``stim_time_s``.
    - fUS: one ISI cycle = ``sonication_duration_s + isi_off_s``. The full
      ISI train of ``n_pulses`` cycles is delivered via HS5 hardware burst
      mode (``GM_BURST_COUNT``), so the buffer contains just one cycle.
    """
    if p.mode == "fus":
        assert p.fus is not None
        return float(p.fus.sonication_duration_s + p.fus.isi_off_s)
    return float(p.stim_time_s)


def choose_hardware_sample_rate(p: StimParams) -> tuple[float, str]:
    """Pick a hardware AWG sample rate that meets two constraints:

    1. Fits the HS5 internal buffer (one buffer holds ``buffer_duration * sr``
       samples, must be ≤ ``HS5_BUFFER_MAX``). The buffer duration is
       ``stim_time_s`` for Control/TI/TBS and one ISI cycle for fUS — pre/post
       are software-managed waits in the session worker, so they do not
       consume buffer.
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
    buf_dur = _buffer_duration_s(p)

    ideal = min(HS5_MAX_SR, fmax * TARGET_SAMPLES_PER_CYCLE)
    sr = ideal
    note = ""

    if buf_dur > 0:
        buffer_cap_sr = HS5_BUFFER_MAX / buf_dur
        if buffer_cap_sr < ideal:
            sr = buffer_cap_sr
            sps = sr / fmax if fmax > 0 else float("inf")
            note = (
                f"reduced from {ideal:,.0f} Hz to fit the {HS5_BUFFER_MAX:,d}-sample "
                f"AWG buffer ({sps:.1f} samples/period at {fmax:g} Hz)"
            )

    sps = sr / fmax if fmax > 0 else float("inf")
    if sps < MIN_SAMPLES_PER_CYCLE:
        max_dur_at_min = (
            HS5_BUFFER_MAX / (MIN_SAMPLES_PER_CYCLE * fmax)
            if fmax > 0
            else float("inf")
        )
        # Build a mode-aware message so fUS users see "ISI cycle" instead of
        # "stim time" — the actionable advice differs slightly per mode.
        if p.mode == "fus":
            assert p.fus is not None
            hint = (
                f"Reduce the ISI cycle (SD + isi_off) below {max_dur_at_min:.3f} s "
                f"(currently {buf_dur:g} s), lower the carrier ({fmax:g} Hz), "
                f"or shorten the sonication."
            )
        else:
            hint = (
                f"Reduce the stim time below {max_dur_at_min:.1f} s "
                f"(currently {buf_dur:g} s), lower the highest signal frequency, "
                f"or split the run into hardware repetitions of a shorter buffer."
            )
        raise ValueError(
            f"Cannot represent this stimulation accurately: {buf_dur:g} s at "
            f"{fmax:g} Hz would force the AWG to {sps:.2f} samples per period "
            f"(minimum {MIN_SAMPLES_PER_CYCLE}). {hint}"
        )
    return float(sr), note


def _apply_ramp(y: np.ndarray, sr: float, ramp_s: float) -> None:
    """Linear 0->1 over first ramp_s and 1->0 over last ramp_s of ``y``."""
    if ramp_s <= 0:
        return
    n = len(y)
    if n <= 0:
        return
    ramp_samples = int(round(ramp_s * sr))
    if ramp_samples <= 0:
        return
    up = np.linspace(0.0, 1.0, ramp_samples, endpoint=False)
    down = np.linspace(1.0, 0.0, ramp_samples, endpoint=False)
    actual_up = min(ramp_samples, n)
    y[:actual_up] *= up[:actual_up]
    actual_down = min(ramp_samples, n)
    y[n - actual_down : n] *= down[-actual_down:]


def _fill_active(
    y: np.ndarray,
    t_active: np.ndarray,
    shape: str,
    f_hz: float,
    amp: float,
    pulse_width_s: float | None,
) -> None:
    """Fill ``y`` (which is the entire stim-only buffer) with the requested shape.

    ``t_active`` is the per-sample time relative to the start of stim.
    """
    if len(y) <= 0:
        return
    if shape == "sine":
        y[:] = amp * np.sin(2 * np.pi * f_hz * t_active)
    elif shape == "triangle":
        ph = (f_hz * t_active) % 1.0
        tri = np.where(ph < 0.5, 4 * ph - 1, 3 - 4 * ph)
        y[:] = amp * tri
    elif shape == "square":
        period = 1.0 / f_hz
        pw = pulse_width_s if pulse_width_s is not None else period / 2
        pw = min(max(pw, 0.0), period)
        ph = (t_active % period) / period
        high = ph < (pw / period)
        y[:] = np.where(high, amp, -amp)
    elif shape == "ramp":
        ph = (f_hz * t_active) % 1.0
        saw = np.where(ph < 0.5, 4 * ph - 1, 3 - 4 * ph)
        y[:] = amp * saw
    elif shape == "tbs":
        y[:] = amp * np.sin(2 * np.pi * f_hz * t_active)
    else:
        raise ValueError(f"unknown shape {shape}")


def _fill_fus(
    y: np.ndarray,
    sr: float,
    fus,
    amp: float,
) -> None:
    """Build one ISI cycle into ``y``: SD seconds of PRF-gated sinusoidal carrier
    followed by ``isi_off_s`` of silence. ``y`` has length
    ``(sonication_duration_s + isi_off_s) * sr``.

    PRF gating: each PRF period of length ``1/prf_hz`` consists of a
    ``tone_burst_s`` ON segment (carrier present) followed by an OFF segment
    (zero). Repeating this pattern across the SD window yields the standard
    fUS waveform.
    """
    n = len(y)
    if n <= 0:
        return
    if fus.carrier_hz >= 0.5 * sr:
        raise ValueError(
            f"carrier_hz {fus.carrier_hz} Hz violates Nyquist at sr={sr} Hz"
        )
    t = np.arange(n, dtype=np.float64) / sr
    n_sd = int(round(fus.sonication_duration_s * sr))
    n_sd = min(n_sd, n)
    if n_sd <= 0:
        return
    t_sd = t[:n_sd]
    period = 1.0 / fus.prf_hz
    # Mask is 1 during the tone-burst portion of each PRF period.
    mask = (t_sd % period) < fus.tone_burst_s
    carrier = np.sin(2 * np.pi * fus.carrier_hz * t_sd)
    y[:n_sd] = amp * mask.astype(np.float64) * carrier
    # The trailing isi_off_s portion stays zero (np.zeros default).


def build_waveforms(p: StimParams) -> WaveformPair:
    """Build the AWG buffer(s) for one stim block.

    The buffer holds **only stim samples** (no leading pre_stim silence or
    trailing post_stim silence): the session worker handles pre/post as
    software-managed waits so EXT 3 ``trigger_stimulation`` can cleanly gate
    the AWG-running window.

    Buffer length:
    - Control / TI / TBS: ``stim_time_s * sr`` samples.
    - fUS: one ISI cycle ``(sonication_duration_s + isi_off_s) * sr`` samples;
      the full ``n_pulses`` train is delivered via HS5 hardware burst mode in
      the session layer.
    """
    sr = p.sample_rate_hz
    buf_dur = _buffer_duration_s(p)
    n = int(round(buf_dur * sr))
    if n < 8:
        raise ValueError("buffer duration × sample_rate is too small")
    t_active = np.arange(n, dtype=np.float64) / sr
    y1 = np.zeros(n, dtype=np.float64)
    y2 = np.zeros(n, dtype=np.float64)

    if p.mode == "control":
        assert p.ch1 is not None and p.ch2 is not None
        for ch, y in [(p.ch1, y1), (p.ch2, y2)]:
            if not ch.enabled:
                continue
            if abs(ch.frequency_hz) >= 0.5 * sr:
                raise ValueError(f"frequency_hz violates Nyquist at sample_rate_hz={sr}")
            _fill_active(y, t_active, ch.shape, ch.frequency_hz, ch.amplitude_ma, ch.pulse_width_s)
        _apply_ramp(y1, sr, p.ramp_s)
        _apply_ramp(y2, sr, p.ramp_s)
    elif p.mode == "fus":
        assert p.fus is not None
        peak_v = p.fus.amplitude_mv_pp / 2.0 / 1000.0  # peak from pp, mV → V
        # Choose which numpy array carries the signal based on the chosen channel;
        # the other channel stays zero so it's safe to load to the idle slot too.
        target = y1 if p.fus.channel == 1 else y2
        _fill_fus(target, sr, p.fus, peak_v)
        # Ramp applies only inside the active SD window for fUS — apply to the
        # whole buffer is fine because the isi_off region is already zero.
        _apply_ramp(target, sr, p.ramp_s)
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
        y1[:] = a1 * np.cos(2 * np.pi * fc * t_active)
        y2[:] = -a2 * np.cos(2 * np.pi * f2 * t_active)

        if p.tbs_freq_hz is not None and p.tbs_freq_hz > 0:
            burst_dur = 3.0 / abs(df)
            tbs_period = 1.0 / p.tbs_freq_hz
            gate = np.zeros(n, dtype=np.float64)
            pos = 0.0
            t_end = t_active[-1] if n else 0.0
            while pos < t_end:
                gate[(t_active >= pos) & (t_active < pos + burst_dur)] = 1.0
                pos += tbs_period
            y1 *= gate
            y2 *= gate

        _apply_ramp(y1, sr, p.ramp_s)
        _apply_ramp(y2, sr, p.ramp_s)

    def norm_unit(z: np.ndarray) -> np.ndarray:
        m = np.max(np.abs(z))
        if m <= 0:
            return z
        return z / m

    y1n = norm_unit(y1)
    y2n = norm_unit(y2)
    return WaveformPair(ch1=y1n.astype(np.float32), ch2=y2n.astype(np.float32), sample_rate_hz=sr, n_samples=n)


def peak_amplitudes(p: StimParams) -> tuple[float, float]:
    """Per-channel peak amplitude (V) after normalization.

    For Control/TI, this is also numerically equal to the per-channel current
    in mA when driving a Digitimer DS5 (1 mA/V) — see legacy ``amplitude_ma``
    field naming. For fUS the value is the peak voltage in volts at the HS5
    BNC, computed from ``amplitude_mv_pp`` (peak = pp / 2 / 1000).
    """
    if p.mode == "control":
        assert p.ch1 is not None and p.ch2 is not None
        return (p.ch1.amplitude_ma if p.ch1.enabled else 0.0,
                p.ch2.amplitude_ma if p.ch2.enabled else 0.0)
    if p.mode == "fus":
        assert p.fus is not None
        peak_v = p.fus.amplitude_mv_pp / 2.0 / 1000.0
        return (peak_v if p.fus.channel == 1 else 0.0,
                peak_v if p.fus.channel == 2 else 0.0)
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
