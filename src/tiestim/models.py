from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator


Shape = Literal["sine", "triangle", "square", "ramp"]
StimMode = Literal["standard", "ti"]


def parse_amplitude_ratio(s: str) -> tuple[float, float]:
    s = s.strip().replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)$", s)
    if not m:
        raise ValueError('amplitude_ratio must be like "2:3" (two positive numbers)')
    a, b = float(m.group(1)), float(m.group(2))
    if a <= 0 or b <= 0:
        raise ValueError("ratio parts must be positive")
    return a, b


class StimParams(BaseModel):
    mode: StimMode = "standard"
    shape: Shape = "sine"
    frequency_hz: float = Field(gt=0, description="Carrier / tone frequency (standard mode)")
    amplitude_v: float = Field(gt=0, description="Peak voltage at BNC (per channel or total in TI)")
    pulse_width_s: float | None = Field(
        default=None,
        ge=0,
        description="Square: high-time within one period (s); ignored for sine/triangle/ramp",
    )
    total_time_s: float = Field(gt=0)
    pre_stim_s: float = Field(ge=0, default=0)
    post_stim_s: float = Field(ge=0, default=0)
    sample_rate_hz: float = Field(gt=0)
    repetitions: int = Field(
        ge=0,
        default=1,
        description="0 = continuous until STOP; >0 = burst count (one buffer per burst)",
    )
    carrier_hz: float | None = Field(default=None, gt=0)
    delta_f_hz: float | None = Field(default=None)
    amplitude_ratio: str | None = Field(default=None, description='TI only, e.g. "2:3"')

    @model_validator(mode="after")
    def ti_required_fields(self):
        if self.mode == "ti":
            if self.carrier_hz is None or self.delta_f_hz is None or self.amplitude_ratio is None:
                raise ValueError("TI mode requires carrier_hz, delta_f_hz, amplitude_ratio")
            if self.delta_f_hz == 0:
                raise ValueError("delta_f_hz must be non-zero in TI mode")
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
    preview_max_points: int = Field(default=2000, ge=64, le=50_000)
