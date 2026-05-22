"""Tests for tiestim.logger — schema and CSV writeback.

Hardware-free. Uses an isolated TIESTIM_LOG_DIR via tmp_path so the test
doesn't touch the operator's daily log file.
"""

from __future__ import annotations

import csv

from tiestim.logger import StimLogRow, row_from_params, append_stim_row, log_dir
from tiestim.models import ChannelParams, FusParams, StimParams


def _control(**over) -> StimParams:
    defaults = dict(
        mode="control",
        stim_time_s=0.05,
        sample_rate_hz=100_000,
        ch1=ChannelParams(shape="sine", frequency_hz=100, amplitude_ma=1.0),
        ch2=ChannelParams(shape="sine", frequency_hz=200, amplitude_ma=0.5),
    )
    defaults.update(over)
    return StimParams(**defaults)


def _fus(**over) -> StimParams:
    fus = FusParams(
        channel=1,
        carrier_hz=1_000_000,
        prf_hz=1000, prf_duty=0.5, tone_burst_s=0.5e-3,
        sonication_duration_s=0.3, isi_off_s=0.2,
        n_pulses=3,
        amplitude_mv_pp=250,
    )
    defaults = dict(
        mode="fus",
        shape="sine",
        stim_time_s=3 * 0.5,
        sample_rate_hz=10_000_000,
        fus=fus,
    )
    defaults.update(over)
    return StimParams(**defaults)


def test_headers_include_new_trigger_and_fus_columns():
    headers = StimLogRow.headers()
    for col in (
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
    ):
        assert col in headers, f"missing column: {col}"


def test_row_from_control_params_marks_fus_columns_blank():
    """Control runs leave the fUS columns blank (None) so the CSV stays
    readable for non-fUS workflows."""
    p = _control()
    row = row_from_params(p, "SN1", "SN2", "ok")
    d = row.as_dict()
    assert d["mode"] == "control"
    assert d["fus_channel"] is None
    assert d["fus_carrier_hz"] is None
    assert d["session_marker_emitted"] is False


def test_row_from_fus_params_populates_fus_columns():
    p = _fus()
    row = row_from_params(p, "SN1", "SN2", "ok")
    d = row.as_dict()
    assert d["mode"] == "fus"
    assert d["fus_channel"] == 1
    assert d["fus_carrier_hz"] == 1_000_000
    assert d["fus_prf_hz"] == 1000
    assert d["fus_n_pulses"] == 3
    assert d["fus_amplitude_mv_pp"] == 250


def test_session_marker_emitted_set_when_trigger_enabled():
    """The session_marker_emitted column tracks whether a prologue was
    actually scheduled (= either trigger_in or trigger_out is on)."""
    p_off = _control(pre_stim_s=0.0)
    p_on = _control(pre_stim_s=0.2, trigger_out=True)
    r_off = row_from_params(p_off, "SN1", "SN2", "ok").as_dict()
    r_on = row_from_params(p_on, "SN1", "SN2", "ok").as_dict()
    assert r_off["session_marker_emitted"] is False
    assert r_on["session_marker_emitted"] is True


def test_csv_writeback(tmp_path, monkeypatch):
    """append_stim_row creates a daily CSV with a header row and one data
    row when run on a fresh directory."""
    monkeypatch.setenv("TIESTIM_LOG_DIR", str(tmp_path))
    p = _fus()
    row = row_from_params(p, "SN1", "SN2", "ok", duration_actual_s=1.5)
    out_path = append_stim_row(row)
    assert out_path.exists()
    with out_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["mode"] == "fus"
    assert rows[0]["fus_carrier_hz"] == "1000000.0"
    assert rows[0]["trigger_stimulation"] == "False"
