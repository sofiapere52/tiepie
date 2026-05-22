"""Tests for the session prologue and trigger-pin behaviour.

Uses ``MockSession`` so we don't need libtiepie. The mock records each phase
of the worker thread in ``last_run["phases"]``, which is enough to verify the
high-level timing semantics:

- No prologue when both ``trigger_in`` and ``trigger_out`` are off.
- A ``session_marker`` (5 ms) + ``pre_stim_remainder`` is emitted when either
  trigger is on, exactly once per session.
- Reps 2..N use plain ``pre_stim → stim → post_stim`` regardless.
- ``pre_stim_s`` short enough to violate the prologue minimum is rejected by
  the Pydantic validator.

We deliberately keep the durations small here so the worker thread completes
quickly under pytest; the real session uses the same logic with wall-clock
durations the operator supplied.
"""

from __future__ import annotations

import time

import pytest

from tiestim.models import ChannelParams, FusParams, StimParams
from tiestim.session import MockSession
from tiestim.waveform import (
    SESSION_MARKER_S,
    SESSION_PROLOGUE_MIN_PRE_STIM_S,
    build_waveforms,
)


def _control_params(**over) -> StimParams:
    defaults = dict(
        mode="control",
        stim_time_s=0.02,
        pre_stim_s=0.0,
        post_stim_s=0.0,
        sample_rate_hz=200_000,
        repetitions=1,
        ch1=ChannelParams(shape="sine", frequency_hz=100, amplitude_ma=1.0),
        ch2=ChannelParams(shape="sine", frequency_hz=100, amplitude_ma=1.0),
    )
    defaults.update(over)
    return StimParams(**defaults)


def _wait_for_run_to_finish(sess: MockSession, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if sess._thread is None:
            return
        if not sess._thread.is_alive():
            return
        time.sleep(0.01)
    raise RuntimeError("mock session did not finish in time")


def test_no_prologue_when_both_triggers_off():
    """Default settings: worker just runs pre_stim → stim → post_stim per rep
    with no session-marker phase."""
    p = _control_params(stim_time_s=0.005, repetitions=2)
    wf = build_waveforms(p)
    s = MockSession()
    s.connect()
    s.arm(p, wf)
    s.start()
    _wait_for_run_to_finish(s)
    phases = [name for name, _ in s.last_run["phases"]]
    assert "session_marker" not in phases
    # Two reps, each contributes one stim phase.
    assert phases.count("stim") == 2


def test_trigger_out_inserts_session_marker_first():
    """trigger_out=True → 5 ms marker + pre_stim_remainder before stim of rep 1."""
    p = _control_params(
        stim_time_s=0.005,
        pre_stim_s=SESSION_PROLOGUE_MIN_PRE_STIM_S + 0.005,
        repetitions=1,
        trigger_out=True,
    )
    wf = build_waveforms(p)
    s = MockSession()
    s.connect()
    s.arm(p, wf)
    s.start()
    _wait_for_run_to_finish(s)
    names = [name for name, _ in s.last_run["phases"]]
    assert names[0] == "session_marker"
    assert names[1] == "pre_stim_remainder"
    # Rep 1's stim follows directly — the first pre_stim is the prologue.
    assert names[2] == "stim"
    # Marker duration matches the canonical constant.
    marker_dur = dict(s.last_run["phases"])["session_marker"]
    assert marker_dur == pytest.approx(SESSION_MARKER_S)


def test_trigger_in_inserts_session_marker_first():
    """trigger_in=True alone also triggers the prologue (silent primer for
    edge detection)."""
    p = _control_params(
        stim_time_s=0.005,
        pre_stim_s=SESSION_PROLOGUE_MIN_PRE_STIM_S + 0.005,
        repetitions=1,
        trigger_in=True,
    )
    wf = build_waveforms(p)
    s = MockSession()
    s.connect()
    s.arm(p, wf)
    s.start()
    _wait_for_run_to_finish(s)
    names = [name for name, _ in s.last_run["phases"]]
    assert names[0] == "session_marker"
    assert s.last_run["session_prologue"] is True


def test_session_marker_emitted_only_once_for_multi_rep():
    """The marker fires exactly once at the start of the session, even when
    repetitions > 1. Reps 2..N use plain pre_stim → stim → post_stim."""
    p = _control_params(
        stim_time_s=0.005,
        pre_stim_s=SESSION_PROLOGUE_MIN_PRE_STIM_S + 0.005,
        post_stim_s=0.005,
        repetitions=3,
        trigger_out=True,
    )
    wf = build_waveforms(p)
    s = MockSession()
    s.connect()
    s.arm(p, wf)
    s.start()
    _wait_for_run_to_finish(s)
    names = [name for name, _ in s.last_run["phases"]]
    assert names.count("session_marker") == 1
    assert names.count("pre_stim_remainder") == 1
    # Reps 2 and 3 each contribute pre_stim + stim + post_stim.
    assert names.count("pre_stim") == 2
    assert names.count("stim") == 3
    assert names.count("post_stim") == 3


def test_pre_stim_too_short_with_trigger_out_rejected():
    """The Pydantic validator hard-rejects pre_stim shorter than the 105 ms
    minimum when a session prologue is requested."""
    with pytest.raises(ValueError, match="pre_stim_s must be ≥"):
        _control_params(
            stim_time_s=0.01, pre_stim_s=0.05, trigger_out=True,
        )


def test_pre_stim_too_short_with_trigger_in_rejected():
    with pytest.raises(ValueError, match="pre_stim_s must be ≥"):
        _control_params(
            stim_time_s=0.01, pre_stim_s=0.05, trigger_in=True,
        )


def test_no_prologue_threshold_when_triggers_off():
    """Without any trigger, the 105 ms threshold does NOT apply — pre_stim
    can be zero."""
    p = _control_params(stim_time_s=0.01, pre_stim_s=0.0)
    assert p.pre_stim_s == 0.0


def test_session_prologue_flag_in_last_run():
    """The MockSession records session_prologue=True iff either trigger is on."""
    p_no = _control_params(stim_time_s=0.005)
    p_yes = _control_params(
        stim_time_s=0.005,
        pre_stim_s=SESSION_PROLOGUE_MIN_PRE_STIM_S + 0.005,
        trigger_out=True,
    )
    wf_no = build_waveforms(p_no)
    wf_yes = build_waveforms(p_yes)
    s = MockSession()
    s.connect()
    s.arm(p_no, wf_no)
    assert s.last_run["session_prologue"] is False
    s.arm(p_yes, wf_yes)
    assert s.last_run["session_prologue"] is True


def test_fus_prologue_also_works():
    """fUS mode with trigger_out: marker phase before the stim window."""
    fus = FusParams(
        channel=1,
        carrier_hz=1_000_000,
        prf_hz=1000, prf_duty=0.5, tone_burst_s=0.5e-3,
        sonication_duration_s=0.005, isi_off_s=0.005,
        n_pulses=2,
        amplitude_mv_pp=200,
    )
    p = StimParams(
        mode="fus", shape="sine",
        stim_time_s=fus.n_pulses * (fus.sonication_duration_s + fus.isi_off_s),
        pre_stim_s=SESSION_PROLOGUE_MIN_PRE_STIM_S + 0.005,
        post_stim_s=0.0,
        sample_rate_hz=10_000_000,
        repetitions=1,
        fus=fus,
        trigger_out=True,
        trigger_stimulation=True,
    )
    wf = build_waveforms(p)
    s = MockSession()
    s.connect()
    s.arm(p, wf)
    s.start()
    _wait_for_run_to_finish(s)
    names = [name for name, _ in s.last_run["phases"]]
    assert names[0] == "session_marker"
    assert s.last_run["mode"] == "fus"
    assert s.last_run["fus_burst_count"] == 2
    assert s.last_run["trigger_out"] is True
    assert s.last_run["trigger_stim"] is True
