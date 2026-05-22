from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_dir() -> Path:
    return Path(os.environ.get("TIESTIM_LOG_DIR", "log")).resolve()


@dataclass
class StimLogRow:
    timestamp_utc: str
    timestamp_local: str
    mode: str
    shape: str
    frequency_hz: float | None
    carrier_hz: float | None
    delta_f_hz: float | None
    amplitude_ma: float
    amplitude_ratio: str | None
    pulse_width_s: float | None
    sample_rate_hz: float
    stim_time_s: float
    total_time_s: float
    pre_stim_s: float
    post_stim_s: float
    ramp_s: float
    repetitions: int
    trigger_out: bool
    trigger_in: bool
    trigger_stimulation: bool
    session_marker_emitted: bool
    # fUS-specific columns. Populated only when mode == "fus"; left as None
    # (= blank cell in CSV) for Control/TI runs so the daily file stays
    # readable for non-fUS workflows.
    fus_channel: int | None
    fus_carrier_hz: float | None
    fus_prf_hz: float | None
    fus_prf_duty: float | None
    fus_tone_burst_s: float | None
    fus_sonication_duration_s: float | None
    fus_isi_off_s: float | None
    fus_n_pulses: int | None
    fus_amplitude_mv_pp: float | None
    device_1_serial: str
    device_2_serial: str
    outcome: str
    error_message: str
    duration_actual_s: float | None

    @staticmethod
    def headers() -> list[str]:
        return [
            "timestamp_utc",
            "timestamp_local",
            "mode",
            "shape",
            "frequency_hz",
            "carrier_hz",
            "delta_f_hz",
            "amplitude_ma",
            "amplitude_ratio",
            "pulse_width_s",
            "sample_rate_hz",
            "stim_time_s",
            "total_time_s",
            "pre_stim_s",
            "post_stim_s",
            "ramp_s",
            "repetitions",
            "trigger_out",
            "trigger_in",
            "trigger_stimulation",
            "session_marker_emitted",
            "fus_channel",
            "fus_carrier_hz",
            "fus_prf_hz",
            "fus_prf_duty",
            "fus_tone_burst_s",
            "fus_sonication_duration_s",
            "fus_isi_off_s",
            "fus_n_pulses",
            "fus_amplitude_mv_pp",
            "device_1_serial",
            "device_2_serial",
            "outcome",
            "error_message",
            "duration_actual_s",
        ]

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.headers()}


def append_stim_row(row: StimLogRow) -> Path:
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    # Use local wall-clock date for the daily file so a stim at 23:30 local
    # time stays in the same file as the rest of that evening's runs.
    day = datetime.now().astimezone().strftime("%Y%m%d")
    path = d / f"stim_{day}.csv"
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=StimLogRow.headers())
        if new_file:
            w.writeheader()
        w.writerow(row.as_dict())
    return path


def row_from_params(
    params: Any,
    serial1: str,
    serial2: str,
    outcome: str,
    error_message: str = "",
    duration_actual_s: float | None = None,
) -> StimLogRow:
    now = datetime.now(timezone.utc)
    local = datetime.now().astimezone()
    if params.mode == "ti":
        freq = None
        shape = params.shape
        amp = float(params.amplitude_ma) if params.amplitude_ma is not None else 0.0
        pulse = params.pulse_width_s
    elif params.mode == "fus":
        # fUS reports its carrier in the dedicated fUS columns; the legacy
        # frequency/shape fields are kept generic so the file remains parseable
        # by tooling expecting the original schema.
        shape = "fus-sine"
        freq = None
        amp = 0.0
        pulse = None
    else:
        assert params.ch1 is not None and params.ch2 is not None
        freq = params.ch1.frequency_hz
        shape = f"{params.ch1.shape}/{params.ch2.shape}"
        amp = max(
            params.ch1.amplitude_ma if params.ch1.enabled else 0.0,
            params.ch2.amplitude_ma if params.ch2.enabled else 0.0,
        )
        pulse = params.ch1.pulse_width_s or params.ch2.pulse_width_s

    fus = params.fus if params.mode == "fus" else None
    return StimLogRow(
        timestamp_utc=now.isoformat(),
        timestamp_local=local.isoformat(),
        mode=params.mode,
        shape=shape,
        frequency_hz=freq if params.mode == "control" else None,
        carrier_hz=params.carrier_hz if params.mode == "ti" else None,
        delta_f_hz=params.delta_f_hz if params.mode == "ti" else None,
        amplitude_ma=amp,
        amplitude_ratio=params.amplitude_ratio,
        pulse_width_s=pulse,
        sample_rate_hz=params.sample_rate_hz,
        stim_time_s=params.stim_time_s,
        total_time_s=params.total_time_s,
        pre_stim_s=params.pre_stim_s,
        post_stim_s=params.post_stim_s,
        ramp_s=params.ramp_s,
        repetitions=params.repetitions,
        trigger_out=bool(params.trigger_out),
        trigger_in=bool(params.trigger_in),
        trigger_stimulation=bool(getattr(params, "trigger_stimulation", False)),
        session_marker_emitted=bool(params.trigger_in or params.trigger_out),
        fus_channel=(fus.channel if fus else None),
        fus_carrier_hz=(fus.carrier_hz if fus else None),
        fus_prf_hz=(fus.prf_hz if fus else None),
        fus_prf_duty=(fus.prf_duty if fus else None),
        fus_tone_burst_s=(fus.tone_burst_s if fus else None),
        fus_sonication_duration_s=(fus.sonication_duration_s if fus else None),
        fus_isi_off_s=(fus.isi_off_s if fus else None),
        fus_n_pulses=(fus.n_pulses if fus else None),
        fus_amplitude_mv_pp=(fus.amplitude_mv_pp if fus else None),
        device_1_serial=serial1,
        device_2_serial=serial2,
        outcome=outcome,
        error_message=error_message,
        duration_actual_s=duration_actual_s,
    )
