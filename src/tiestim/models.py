from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator


Shape = Literal["sine", "triangle", "square", "ramp", "tbs"]
StimMode = Literal["control", "ti", "fus"]


def parse_amplitude_ratio(s: str) -> tuple[float, float]:
    s = s.strip().replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)$", s)
    if not m:
        raise ValueError('amplitude_ratio must be like "2:3" (two positive numbers)')
    a, b = float(m.group(1)), float(m.group(2))
    if a <= 0 or b <= 0:
        raise ValueError("ratio parts must be positive")
    return a, b


class ChannelParams(BaseModel):
    enabled: bool = True
    shape: Shape = "sine"
    frequency_hz: float = Field(gt=0, default=100)
    amplitude_ma: float = Field(gt=0, le=10, description="Peak amplitude (mA); sets HS5 output voltage = this value in V (DS5: 1 mA/V)", default=1.0)
    pulse_width_s: float | None = Field(
        default=None,
        ge=0,
        description="Square: high-time within one period (s)",
    )


class FusParams(BaseModel):
    """Functional ultrasound (fUS) stimulation parameters.

    Drives a 0.5–2 MHz sinusoidal carrier, internally gated by a kHz-range
    pulse-repetition frequency (PRF) train of a chosen duty cycle. The full
    sonication pattern is one ISI cycle (SD seconds of PRF-gated carrier
    followed by isi_off_s of silence), played `n_pulses` times back-to-back
    via HS5 hardware burst mode so the AWG runs continuously through the
    entire train.

    Designed for downstream amplification through e.g. a Vectawave VBA-230-80
    (50 dB gain, 50 Ω I/O); ``amplitude_mv_pp`` is the HS5 BNC output (=
    amplifier input). The amplifier recommends ≤500 mV pp; the UI warns
    above that threshold but does NOT block submission.
    """

    channel: Literal[1, 2] = Field(default=1, description="Which HS5 slot drives the output (1 or 2).")
    carrier_hz: float = Field(ge=5e5, le=2e6, default=1e6, description="Ultrasound carrier frequency (Hz); 0.5–2 MHz.")
    prf_hz: float = Field(gt=0, default=1000, description="Pulse Repetition Frequency (Hz).")
    prf_duty: float = Field(gt=0, lt=1, default=0.5, description="PRF duty cycle (0–1).")
    tone_burst_s: float = Field(gt=0, default=5e-4,
        description="Tone-burst duration = PRF on-time per cycle (s); must equal prf_duty / prf_hz.")
    sonication_duration_s: float = Field(gt=0, default=0.3,
        description="SD: duration of one sonication pulse, including PRF gating (s).")
    isi_off_s: float = Field(ge=0, default=0.2,
        description="Silent time between consecutive sonications (s). ISI = SD + isi_off_s.")
    n_pulses: int = Field(ge=1, default=1,
        description="Number of sonications in the ISI train (delivered via HS5 burst mode).")
    amplitude_mv_pp: float = Field(gt=0, default=200,
        description="HS5 BNC peak-to-peak voltage (= amplifier input). VBA-230-80 datasheet "
                    "recommends ≤500 mV pp; UI warns above this without blocking.")

    @model_validator(mode="after")
    def fus_cross_field_consistency(self):
        expected_tone = self.prf_duty / self.prf_hz
        if abs(self.tone_burst_s - expected_tone) > 1e-6:
            raise ValueError(
                f"tone_burst_s ({self.tone_burst_s}) must equal prf_duty / prf_hz "
                f"({expected_tone}); send one or the other and let the UI keep them in sync."
            )
        if self.tone_burst_s >= 1.0 / self.prf_hz:
            raise ValueError("tone_burst_s must be shorter than one PRF period (1 / prf_hz).")
        if self.sonication_duration_s < self.tone_burst_s:
            raise ValueError("sonication_duration_s must be ≥ tone_burst_s.")
        # isi_off_s = 0 is allowed (back-to-back sonications); only negative is rejected
        # by the field validator (ge=0).
        return self


class StimParams(BaseModel):
    mode: StimMode = "control"
    shape: Shape = "sine"
    frequency_hz: float | None = Field(
        default=None,
        gt=0,
        description="TI: duplicate of carrier for API",
    )
    amplitude_ma: float | None = Field(
        default=None,
        gt=0,
        le=10,
        description="TI: total peak amplitude (mA); sets HS5 output voltage = this value in V (DS5: 1 mA/V); control: unused (use ch1/ch2)",
    )
    pulse_width_s: float | None = Field(
        default=None,
        ge=0,
    )
    stim_time_s: float = Field(gt=0, description="Active stimulation duration within one cycle (s)")
    pre_stim_s: float = Field(ge=0, default=0)
    post_stim_s: float = Field(ge=0, default=0)
    ramp_s: float = Field(ge=0, default=0, description="Linear ramp up and down at start/end of active segment (s)")
    sample_rate_hz: float = Field(gt=0, default=500_000, description="Hardware sample rate (Hz); fixed for HS5")
    repetitions: int = Field(
        ge=0,
        default=1,
        description="0 = continuous until STOP; >0 = play one buffer this many times then stop",
    )
    carrier_hz: float | None = Field(default=None, gt=0)
    delta_f_hz: float | None = Field(default=None)
    amplitude_ratio: str | None = Field(default=None, description='TI only, e.g. "2:3"')
    ch1: ChannelParams | None = None
    ch2: ChannelParams | None = None
    trigger_out: bool = False
    trigger_in: bool = False
    trigger_stimulation: bool = Field(
        default=False,
        description="EXT 3 gate: HIGH while the AWG is running (= during stim), LOW during "
                    "pre/post and ISI_off gaps. For fUS, HIGH for the whole ISI train.",
    )
    fus: FusParams | None = None
    tbs_freq_hz: float | None = Field(
        default=None,
        ge=2,
        le=8,
        description="TBS burst repetition rate (Hz); TI+TBS shape only",
    )

    @field_validator("mode", mode="before")
    @classmethod
    def coerce_standard(cls, v):
        if v == "standard":
            return "control"
        return v

    @computed_field
    @property
    def total_time_s(self) -> float:
        return self.pre_stim_s + self.stim_time_s + self.post_stim_s

    @model_validator(mode="after")
    def ramp_and_mode_fields(self):
        if self.ramp_s > self.stim_time_s:
            raise ValueError("ramp_s cannot exceed stim_time_s")
        # Session prologue: when trigger_in OR trigger_out is enabled, the
        # session begins with a 5 ms primer playback on the AWG (a DC
        # marker pulse for trigger_out, a silent buffer for trigger_in
        # detection). The host then reloads the stim buffer (~50–100 ms
        # USB latency) before launching the actual stim. pre_stim_s
        # absorbs both the marker and the reload; require enough slack.
        if (self.trigger_in or self.trigger_out):
            from tiestim.waveform import SESSION_PROLOGUE_MIN_PRE_STIM_S
            if self.pre_stim_s < SESSION_PROLOGUE_MIN_PRE_STIM_S:
                raise ValueError(
                    f"pre_stim_s must be ≥ {SESSION_PROLOGUE_MIN_PRE_STIM_S*1000:.0f} ms when "
                    "Trigger Out or Trigger In is enabled: the session emits a 5 ms "
                    "marker / detection primer at the very start of pre_stim, then "
                    "needs ~100 ms to reload the stim buffer over USB before stim. "
                    "Either raise pre_stim_s, or disable the trigger."
                )
        if self.ramp_s > 0 and 2 * self.ramp_s > self.stim_time_s:
            raise ValueError(
                "ramp up and ramp down would overlap: need 2 * ramp_s <= stim_time_s "
                "(or set ramp_s to 0)"
            )
        if self.mode == "ti":
            if self.amplitude_ma is None:
                raise ValueError("TI mode requires amplitude_ma")
            if self.carrier_hz is None or self.delta_f_hz is None or self.amplitude_ratio is None:
                raise ValueError("TI mode requires carrier_hz, delta_f_hz, amplitude_ratio")
            # delta_f_hz == 0 is allowed: both channels run at the carrier in
            # anti-phase, the sum collapses to zero (or to the imbalance set
            # by amplitude_ratio). TBS, however, needs a non-zero beat to
            # define the burst duration (3/|Δf|).
            if self.shape == "tbs" and (self.delta_f_hz is None or self.delta_f_hz == 0):
                raise ValueError("TBS shape requires a non-zero delta_f_hz")
            if self.shape == "tbs" and self.tbs_freq_hz is None:
                raise ValueError("TBS shape requires tbs_freq_hz (2–8 Hz)")
        elif self.mode == "fus":
            if self.fus is None:
                raise ValueError("fUS mode requires a `fus` parameter block")
            # stim_time_s is always derived from fUS params: it is the duration of
            # the ISI train (n_pulses × ISI). Whatever the client sent (the UI
            # keeps them in sync, but a stale value is harmless) is snapped here
            # so downstream logging / CSV / preview all agree.
            isi = self.fus.sonication_duration_s + self.fus.isi_off_s
            object.__setattr__(self, "stim_time_s", self.fus.n_pulses * isi)
            # Ramp must still fit within the (newly-snapped) stim_time. Re-check.
            if self.ramp_s > self.stim_time_s:
                raise ValueError("ramp_s cannot exceed stim_time_s")
        else:
            if self.ch1 is None or self.ch2 is None:
                raise ValueError("control mode requires ch1 and ch2 channel parameters")
            if not self.ch1.enabled and not self.ch2.enabled:
                raise ValueError("at least one channel must be enabled in control mode")
        return self

    def ti_parts(self) -> tuple[float, float, float, float]:
        """Returns (carrier_hz, delta_f_hz, r1, r2) normalized so r1+r2=1."""
        if self.mode != "ti":
            raise ValueError("not TI mode")
        assert self.carrier_hz is not None and self.delta_f_hz is not None and self.amplitude_ratio is not None
        r1, r2 = parse_amplitude_ratio(self.amplitude_ratio)
        s = r1 + r2
        return self.carrier_hz, self.delta_f_hz, r1 / s, r2 / s



class StimRequest(BaseModel):
    params: StimParams
    preview_max_points: int = Field(default=8000, ge=64, le=200_000)
    # Optional zoom window. When BOTH are provided and define a sub-range of
    # the full stimulation, the /waveform/preview endpoint switches to a
    # windowed render that builds carrier-resolved data only for this slice
    # — the same memory budget then yields enough density to show a clean
    # sinusoid when the user zooms in deep on the live preview.
    t_start_s: float | None = Field(default=None, ge=0)
    t_end_s: float | None = Field(default=None, ge=0)
