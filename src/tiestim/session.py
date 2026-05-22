from __future__ import annotations

import os
import sys
import threading
import time
from abc import ABC, abstractmethod
from array import array
from dataclasses import dataclass
from typing import Callable

from tiestim.models import StimParams
from tiestim.waveform import (
    SESSION_MARKER_S,
    WaveformPair,
    build_session_primer,
    numpy_to_array_f,
    peak_amplitudes,
    session_prologue_needed,
)


def _set_high_res_timer() -> None:
    """On Windows, raise the OS timer resolution to ~1 ms so software pre/post
    waits in the session worker thread are accurate to sub-ms. No-op on other
    platforms.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def _release_high_res_timer() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


@dataclass
class DeviceState:
    slot: int
    serial: str
    ui_state: str  # disconnected|ready|armed|running|done|error
    detail: str = ""


class BaseSession(ABC):
    @abstractmethod
    def connect(self) -> list[DeviceState]:
        ...

    @abstractmethod
    def arm(self, params: StimParams, wf: WaveformPair) -> None:
        ...

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def status(self) -> list[DeviceState]:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    @abstractmethod
    def on_run_finished(self, cb: Callable[[], None]) -> None:
        """Burst mode: invoked when burst completes. Continuous: not used."""
        ...


@dataclass
class MockSession(BaseSession):
    """In-memory mock that mirrors the real session's per-phase timing.

    The worker thread loops over repetitions and waits for pre_stim →
    stim_time → post_stim per rep, so tests that observe the timing match
    what the real TiePieSession does. Phase markers are exposed via
    ``last_run`` for assertions.
    """

    _armed: bool = False
    _running: bool = False
    _thread: threading.Thread | None = None
    _finished_cb: Callable[[], None] | None = None
    _params: StimParams | None = None
    _user_stop: threading.Event = None  # type: ignore[assignment]
    last_run: dict | None = None

    def __post_init__(self) -> None:
        self._user_stop = threading.Event()

    def connect(self) -> list[DeviceState]:
        return [
            DeviceState(1, "MOCK1", "ready"),
            DeviceState(2, "MOCK2", "ready"),
        ]

    def arm(self, params: StimParams, wf: WaveformPair) -> None:
        self._params = params
        self._armed = True
        self._running = False
        # Per-arm telemetry to support tests without touching hardware.
        self.last_run = {
            "armed_at": time.monotonic(),
            "phases": [],          # list of (phase_name, duration_s)
            "gen_start_calls": 0,  # how many times the worker pretended to start the AWG
            "trigger_in": params.trigger_in,
            "trigger_out": params.trigger_out,
            "trigger_stim": params.trigger_stimulation,
            "mode": params.mode,
            "fus_burst_count": params.fus.n_pulses if (params.mode == "fus" and params.fus) else None,
            "session_prologue": session_prologue_needed(params),
        }

    def start(self) -> None:
        if not self._armed:
            raise RuntimeError("arm before start")
        self._running = True
        self._user_stop.clear()

        def run():
            p = self._params
            if p is None:
                return
            pre = float(p.pre_stim_s)
            stim = float(p.stim_time_s)
            post = float(p.post_stim_s)

            def _wait(name: str, dur: float) -> bool:
                # Return True if interrupted; otherwise record the phase.
                if dur <= 0:
                    return self._user_stop.is_set()
                interrupted = self._user_stop.wait(timeout=dur)
                if self.last_run is not None:
                    self.last_run["phases"].append((name, dur))
                return interrupted

            reps = p.repetitions if p.repetitions > 0 else -1  # -1 = continuous
            rep_count = 0
            need_prologue = session_prologue_needed(p)
            try:
                # ---- session prologue (rep 0 prefix) ----
                # Mirror the real TiePieSession: when trigger_in or trigger_out
                # is enabled, the AWG plays a 5 ms primer at the very start
                # (acts as the first 5 ms of pre_stim); the remainder of
                # pre_stim is software-managed.
                if need_prologue:
                    if _wait("session_marker", SESSION_MARKER_S):
                        return
                    if self.last_run is not None:
                        self.last_run["gen_start_calls"] += 1
                    if _wait("pre_stim_remainder", max(0.0, pre - SESSION_MARKER_S)):
                        return
                while reps == -1 or rep_count < reps:
                    # The very first rep's pre_stim has already elapsed as
                    # the prologue (marker + remainder), so skip it here.
                    skip_pre = need_prologue and rep_count == 0
                    if not skip_pre:
                        if _wait("pre_stim", pre):
                            return
                    if self.last_run is not None:
                        self.last_run["gen_start_calls"] += 1
                    if _wait("stim", stim):
                        return
                    if _wait("post_stim", post):
                        return
                    rep_count += 1
            finally:
                self._running = False
                self._armed = False
                if not self._user_stop.is_set() and self._finished_cb:
                    self._finished_cb()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._user_stop.set()
        self._running = False
        self._armed = False

    def status(self) -> list[DeviceState]:
        st = "running" if self._running else ("armed" if self._armed else "ready")
        return [
            DeviceState(1, "MOCK1", st),
            DeviceState(2, "MOCK2", st),
        ]

    def close(self) -> None:
        self.stop()

    def on_run_finished(self, cb: Callable[[], None]) -> None:
        self._finished_cb = cb


class TiePieSession(BaseSession):
    """Two HS5 AWGs via python-libtiepie (Windows/Linux only).

    Worker-thread model: ``start()`` returns immediately and a daemon thread
    handles per-phase timing (pre_stim software wait → gen.start → stim_time
    wait → gen.stop → post_stim software wait), looped over repetitions.

    Triggers are configured on the **active** generator (slot 1 by default, or
    ``fus.channel`` for fUS). EXT 1 ``trigger_out``, EXT 2 ``trigger_in``,
    EXT 3 ``trigger_stimulation`` are fully independent — any combination
    works because the buffer always holds stim samples only.
    """

    def __init__(self) -> None:
        import libtiepie as lt

        self._lt = lt
        self._gens: list = []
        self._serials: list[str] = []
        self._finished_cb: Callable[[], None] | None = None
        self._params_ref: StimParams | None = None
        self._armed: bool = False
        self._hardware_burst: bool = False
        self._has_prologue: bool = False
        self._ti_sync: bool = False
        self._stim_ch1_data = None
        self._stim_ch2_data = None
        self._stim_sample_rate_hz: float = 0.0
        self._user_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        _set_high_res_timer()

    def _active_slot_index(self, params: StimParams) -> int:
        """0-based index of the active generator. Defaults to slot 1 (index 0)
        except for fUS, where the user picks via ``fus.channel`` (1 or 2).
        """
        if params.mode == "fus" and params.fus is not None:
            return max(0, min(1, params.fus.channel - 1))
        return 0

    def _discover(self) -> None:
        lt = self._lt
        lt.network.auto_detect_enabled = True
        lt.device_list.update()
        found: list[tuple[int, object]] = []
        n = len(lt.device_list)
        s1 = os.environ.get("TIESTIM_SERIAL_1")
        s2 = os.environ.get("TIESTIM_SERIAL_2")

        for i in range(n):
            item = lt.device_list.get_item_by_index(i)
            if not item.can_open(lt.DEVICETYPE_GENERATOR):
                continue
            try:
                gen = item.open_generator()
            except Exception:
                continue
            if not (gen.signal_types & lt.ST_ARBITRARY):
                del gen
                continue
            found.append((item.serial_number, gen))

        found.sort(key=lambda x: x[0])

        def pick() -> list[tuple[int, object]]:
            if s1 and s2:
                by_sn = {str(sn): (sn, g) for sn, g in found}
                if str(s1) in by_sn and str(s2) in by_sn:
                    return [by_sn[str(s1)], by_sn[str(s2)]]
            return found[:2]

        chosen = pick()
        if len(chosen) < 1:
            raise RuntimeError(
                "No generators with ST_ARBITRARY found. "
                "Set TIESTIM_SERIAL_1 / TIESTIM_SERIAL_2 if needed."
            )
        self._serials = [str(c[0]) for c in chosen]
        self._gens = [c[1] for c in chosen]

    def connect(self) -> list[DeviceState]:
        self.close()
        self._armed = False
        self._discover()
        return self.status()

    def _prepare_gen(self, gen, data: array, sr: float, amp_v: float) -> None:
        lt = self._lt
        steps: list[tuple[str, callable]] = [
            ("stop",            lambda: gen.stop()),
            ("output_enable=0", lambda: setattr(gen, "output_enable", False)),
            ("signal_type",     lambda: setattr(gen, "signal_type", lt.ST_ARBITRARY)),
            ("frequency_mode",  lambda: setattr(gen, "frequency_mode", lt.FM_SAMPLERATE)),
            ("frequency",       lambda: setattr(gen, "frequency", float(sr))),
            ("amplitude",       lambda: setattr(gen, "amplitude", float(amp_v))),
            ("offset",          lambda: setattr(gen, "offset", 0.0)),
            ("set_data",        lambda: gen.set_data(data)),
        ]
        errors: list[str] = []
        for label, fn in steps:
            try:
                fn()
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        if errors:
            raise RuntimeError(
                "Generator setup failed — " + "; ".join(errors)
            )
        # Verify the hardware actually accepted the requested amplitude.
        # libtiepie may silently clamp the value to the nearest valid range
        # without raising an exception, so a readback is the only way to
        # detect the discrepancy.
        try:
            actual_amp = float(gen.amplitude)
            tol = max(1e-4, abs(amp_v) * 0.02)  # 2 % relative tolerance
            if abs(actual_amp - amp_v) > tol:
                raise RuntimeError(
                    f"Amplitude readback mismatch: requested {amp_v:.6f} V, "
                    f"hardware returned {actual_amp:.6f} V. "
                    "Check the HS5 output range configuration."
                )
        except RuntimeError:
            raise
        except Exception:
            pass  # attribute not available on this driver version — skip silently

    def _finish_outputs(self, burst: bool, burst_count: int, active_idx: int | None = None) -> None:
        """Configure operating mode on each gen.

        ``burst`` + ``burst_count`` → ``GM_BURST_COUNT`` with the given count.
        Falls back to ``GM_CONTINUOUS`` if the device doesn't support burst.

        ``active_idx`` (when given) is the only gen that gets its output
        enabled — used by fUS where the unused channel must stay idle.
        """
        lt = self._lt
        for i, g in enumerate(self._gens):
            if burst and burst_count > 0:
                try:
                    g.mode = lt.GM_BURST_COUNT
                    g.burst_count = int(burst_count)
                except Exception:
                    g.mode = lt.GM_CONTINUOUS
            else:
                g.mode = lt.GM_CONTINUOUS
            if active_idx is not None and i != active_idx:
                g.output_enable = False
            else:
                g.output_enable = True

    def _gen_diag(self, gen) -> dict:
        lt = self._lt
        info: dict = {}
        for attr in (
            "signal_types", "amplitude_min", "amplitude_max",
            "frequency_min", "frequency_max",
            "data_length_min", "data_length_max",
            "modes_native", "is_controllable",
        ):
            try:
                info[attr] = getattr(gen, attr)
            except Exception as e:
                info[attr] = f"ERR: {e}"
        for flag_name in ("ST_ARBITRARY", "ST_SINE", "FM_SAMPLERATE", "FM_SIGNALFREQUENCY",
                          "GM_CONTINUOUS", "GM_BURST_COUNT"):
            try:
                val = getattr(lt, flag_name)
                info[f"has_{flag_name}"] = bool(gen.signal_types & val) if "ST_" in flag_name else bool(gen.modes_native & val) if "GM_" in flag_name else val
            except Exception:
                info[f"has_{flag_name}"] = "N/A"
        return info

    def diagnostics(self) -> list[dict]:
        out = []
        for i, g in enumerate(self._gens):
            d = {"slot": i + 1, "serial": self._serials[i] if i < len(self._serials) else "?"}
            d.update(self._gen_diag(g))
            out.append(d)
        return out

    def _find_trigger_io(self, gen, direction: str, ext_name: str = "EXT 1"):
        """Find a trigger output or input for the named EXT port.

        direction: 'output' or 'input'
        """
        lt = self._lt
        try:
            collection = getattr(gen, f"trigger_{direction}s")
        except Exception:
            return None
        tiid_key = ext_name.upper().replace(" ", "")
        for attr in (f"TIID_{tiid_key}", tiid_key):
            try:
                tiid = getattr(lt, attr)
                for item in collection:
                    if item.id == tiid:
                        return item
            except Exception:
                continue
        try:
            for item in collection:
                if ext_name.lower() in item.name.lower():
                    return item
        except Exception:
            pass
        return None

    def _ext_trigger_ids(self) -> set:
        """Collect all TIID_EXT* constants so we can exclude them."""
        lt = self._lt
        ids: set = set()
        for n in ("1", "2", "3"):
            for pat in (f"TIID_EXT{n}", f"TIID_EXT_{n}"):
                try:
                    ids.add(getattr(lt, pat))
                except AttributeError:
                    continue
        return ids

    def _find_cmi_trigger_pair(self):
        """Find an internal / CMI trigger pair for cross-device sync.

        Returns (gen1_trigger_output, gen2_trigger_input) whose IDs match
        but are NOT EXT ports, i.e. routed through the CMI cable.
        Returns (None, None) when no such pair exists.
        """
        if len(self._gens) < 2:
            return None, None
        g0, g1 = self._gens[0], self._gens[1]
        ext_ids = self._ext_trigger_ids()

        out_by_id: dict = {}
        try:
            for tout in g0.trigger_outputs:
                if tout.id not in ext_ids:
                    out_by_id[tout.id] = tout
        except Exception:
            return None, None

        try:
            for tin in g1.trigger_inputs:
                if tin.id not in ext_ids and tin.id in out_by_id:
                    return out_by_id[tin.id], tin
        except Exception:
            pass
        return None, None

    def _disable_all_triggers(self) -> None:
        for g in self._gens:
            try:
                for tout in g.trigger_outputs:
                    tout.enabled = False
            except Exception:
                pass
            try:
                for tin in g.trigger_inputs:
                    tin.enabled = False
            except Exception:
                pass

    def _configure_session_triggers(self, params: StimParams) -> None:
        """Disable everything except TI CMI sync.

        Called at arm time as a clean baseline. EXT 1 / EXT 2 / EXT 3 are
        wired in two phases by the worker thread: ``_apply_prologue_triggers``
        before the session marker, ``_apply_stim_triggers`` before the stim
        loop. This split is what keeps the three EXT pins independent:

        - EXT 1 fires HIGH **only during the session marker** (5 ms, once
          per session). After the marker the host disables EXT 1 so it does
          not re-fire on each stim's gen.start.
        - EXT 2 is armed only for the session marker; after the marker the
          host disables it so subsequent reps start without waiting.
        - EXT 3 is enabled only for the stim phase, so it cleanly mirrors
          the per-stim AWG running window — never the prologue.
        """
        self._ti_sync = False
        if not self._gens:
            return
        lt = self._lt
        self._disable_all_triggers()
        if params.mode == "ti" and len(self._gens) >= 2:
            tout, tin = self._find_cmi_trigger_pair()
            if tout and tin:
                try:
                    tout.enabled = True
                    tout.event = lt.TOE_GENERATOR_START
                    tin.enabled = True
                    tin.kind = lt.TK_RISINGEDGE
                    self._ti_sync = True
                except Exception:
                    self._ti_sync = False

    def _apply_prologue_triggers(self, params: StimParams) -> None:
        """Enable EXT 1 and/or EXT 2 for the one-shot session marker.

        EXT 3 stays disabled here — the prologue is allowed to glitch a brief
        HIGH on EXT 3 only because of the underlying ``TOE_GENERATOR_STOP``
        semantics (any AWG run sets the pin HIGH), but we deliberately do
        not wire trigger_stim during the prologue.
        """
        if not self._gens:
            return
        lt = self._lt
        active = self._gens[self._active_slot_index(params)]
        if params.trigger_out:
            tout = self._find_trigger_io(active, "output", "EXT 1")
            if tout:
                try:
                    tout.enabled = True
                    tout.event = lt.TOE_GENERATOR_STOP
                except Exception:
                    pass
        if params.trigger_in:
            tin = self._find_trigger_io(active, "input", "EXT 2")
            if tin:
                try:
                    tin.enabled = True
                    tin.kind = lt.TK_RISINGEDGE
                except Exception:
                    pass

    def _apply_stim_triggers(self, params: StimParams) -> None:
        """Disable EXT 1 / EXT 2 (one-shot done) and enable EXT 3 if requested.

        Run after the session prologue (or at start of stim phase when no
        prologue is needed). Idempotent.
        """
        if not self._gens:
            return
        lt = self._lt
        active = self._gens[self._active_slot_index(params)]
        # EXT 1 OFF — the marker is one-shot per session.
        tout1 = self._find_trigger_io(active, "output", "EXT 1")
        if tout1:
            try:
                tout1.enabled = False
            except Exception:
                pass
        # EXT 2 OFF — once the session has started, no more edge-wait.
        tin2 = self._find_trigger_io(active, "input", "EXT 2")
        if tin2:
            try:
                tin2.enabled = False
            except Exception:
                pass
        # EXT 3 ON — gate signal HIGH only while the AWG runs the stim.
        if params.trigger_stimulation:
            tout3 = self._find_trigger_io(active, "output", "EXT 3")
            if tout3:
                try:
                    tout3.enabled = True
                    tout3.event = lt.TOE_GENERATOR_STOP
                except Exception:
                    pass

    def arm(self, params: StimParams, wf: WaveformPair) -> None:
        """Load the AWG buffer(s), set burst mode, and wire triggers.

        Buffer composition: the stim waveform is stim-only (no pre/post), so
        the AWG runs only during stim. Pre/post are software waits in the
        worker thread.

        Session prologue: when ``trigger_in`` or ``trigger_out`` is enabled,
        the AWG is initially loaded with a 5 ms primer buffer (DC marker if
        ``trigger_out``, silent if only ``trigger_in``). The worker plays the
        primer first, then reloads the stim buffer over USB before the stim
        loop starts. The full stim buffer (and its sample rate / amplitude)
        is stashed on the session so the worker can do the reload.

        Modes:
        - Control / TI / TBS: both gens are loaded with the stim buffer (or
          primer when a prologue is needed). Hardware burst fast path is
          used when ``pre/post = 0`` AND no prologue AND ``repetitions > 0``.
        - fUS: only the chosen channel emits; the other stays disabled.
          ``GM_BURST_COUNT`` with ``burst_count = fus.n_pulses`` so the whole
          ISI train plays in one hardware burst; the worker loops over
          repetitions and handles pre/post in software.
        """
        self._params_ref = params
        self._hardware_burst = False
        self._stim_ch1_data = None
        self._stim_ch2_data = None
        self._stim_sample_rate_hz = float(wf.sample_rate_hz)
        if not self._gens:
            raise RuntimeError("not connected")
        if len(self._gens) < 2 and params.mode == "ti":
            raise RuntimeError("TI mode requires 2 connected devices")

        active_idx = self._active_slot_index(params)
        if params.mode == "fus" and active_idx >= len(self._gens):
            raise RuntimeError(
                f"fUS requested channel {params.fus.channel if params.fus else '?'} "
                f"but only {len(self._gens)} HS5 device(s) connected"
            )

        a1, a2 = peak_amplitudes(params)
        d1_stim = numpy_to_array_f(wf.ch1)
        d2_stim = numpy_to_array_f(wf.ch2) if len(self._gens) >= 2 else None
        g0 = self._gens[0]

        try:
            mx = float(g0.amplitude_max)
            if a1 > mx:
                raise ValueError(
                    f"ch1 amplitude {a1:.4f} V exceeds device max {mx:.4f} V"
                )
        except ValueError:
            raise
        except Exception as exc:
            import warnings
            warnings.warn(f"Could not read ch1 amplitude_max: {exc}")
        try:
            lo, hi = g0.data_length_min, g0.data_length_max
            n = len(d1_stim)
            if n < lo or n > hi:
                raise ValueError(f"buffer length {n} not in [{lo}, {hi}]")
        except ValueError:
            raise
        except Exception:
            pass

        # Build the prologue primer (if needed). Marker is constant +0.1
        # (10 % of gen.amplitude, which is set to the stim peak). Silent
        # primer is all zeros, used only to make is_running observable when
        # only trigger_in is enabled.
        self._has_prologue = session_prologue_needed(params)
        prim_ch1_data = None
        prim_ch2_data = None
        if self._has_prologue:
            marker_np = build_session_primer(
                self._stim_sample_rate_hz,
                with_marker=bool(params.trigger_out),
            )
            prim_ch1_data = numpy_to_array_f(marker_np)
            if len(self._gens) >= 2:
                # TI mode plays the marker on both gens via CMI; for control
                # we also load the second slot (its output_enable is set
                # below as appropriate so it won't emit unless wanted).
                prim_ch2_data = numpy_to_array_f(marker_np)

        # Decide which buffer goes into the AWG slot at arm time. For
        # prologue runs the primer is loaded first; the worker reloads stim
        # later. For non-prologue runs the stim buffer is loaded directly.
        d1_init = prim_ch1_data if self._has_prologue else d1_stim
        d2_init = prim_ch2_data if self._has_prologue and prim_ch2_data is not None else d2_stim

        # gen.amplitude stays at the stim peak in both phases — the marker's
        # 1/10 amplitude comes from the buffer values (constant 0.1) being
        # scaled by amplitude_max=peak. No reconfigure required.
        self._prepare_gen(g0, d1_init, self._stim_sample_rate_hz, a1)

        if len(self._gens) >= 2 and d2_init is not None:
            g1 = self._gens[1]
            try:
                mx = float(g1.amplitude_max)
                if a2 > mx:
                    raise ValueError(
                        f"ch2 amplitude {a2:.4f} V exceeds device max {mx:.4f} V"
                    )
            except ValueError:
                raise
            except Exception as exc:
                import warnings
                warnings.warn(f"Could not read ch2 amplitude_max: {exc}")
            self._prepare_gen(g1, d2_init, self._stim_sample_rate_hz, a2)

        # Stash the real stim buffers for the worker to reload after the
        # prologue.
        self._stim_ch1_data = d1_stim
        self._stim_ch2_data = d2_stim

        lt = self._lt

        if params.mode == "fus":
            assert params.fus is not None
            # Prologue → single-shot 1-burst playback. The worker reconfigures
            # to n_pulses bursts after the prologue.
            burst_count = 1 if self._has_prologue else int(params.fus.n_pulses)
            try:
                self._finish_outputs(burst=True, burst_count=burst_count,
                                     active_idx=active_idx)
            except Exception:
                self._finish_outputs(burst=False, burst_count=0,
                                     active_idx=active_idx)
            self._hardware_burst = not self._has_prologue
        else:
            # Fast path: hardware burst of stim-only buffer × repetitions.
            # Disabled when a prologue is needed (worker has to drive the
            # reps in software so it can handle the prologue → stim swap).
            want_burst = (
                not self._has_prologue
                and params.repetitions > 0
                and not params.trigger_in
                and params.pre_stim_s == 0
                and params.post_stim_s == 0
                and all((g.modes_native & lt.GM_BURST_COUNT) for g in self._gens)
            )
            # During the prologue the gen plays one short primer burst.
            prologue_burst = 1 if self._has_prologue else 0
            try:
                self._finish_outputs(
                    burst=want_burst or self._has_prologue,
                    burst_count=(params.repetitions if want_burst else prologue_burst),
                )
            except Exception:
                self._finish_outputs(burst=False, burst_count=0)
            self._hardware_burst = want_burst

        self._configure_session_triggers(params)
        if self._has_prologue:
            self._apply_prologue_triggers(params)
        else:
            self._apply_stim_triggers(params)
        self._armed = True

    def _do_gen_start(self) -> None:
        """Start the active generator(s). For TI sync, gen2 is armed first
        (waits for CMI), then gen1 fires it. For everything else, the active
        gen alone starts (the inactive gen is output-disabled when fUS is
        active; for Control with one channel disabled, calling start on
        a disabled output is still benign — keep the symmetric call)."""
        if self._ti_sync and len(self._gens) >= 2:
            self._gens[1].start()
            self._gens[0].start()
            return
        params = self._params_ref
        if params is not None and params.mode == "fus":
            active = self._gens[self._active_slot_index(params)]
            active.start()
            return
        for g in self._gens:
            g.start()

    def _do_gen_stop(self) -> None:
        for g in self._gens:
            try:
                g.stop()
            except Exception:
                pass

    def _gen_is_running(self) -> bool:
        for g in self._gens:
            try:
                if g.is_running:
                    return True
            except Exception:
                pass
        return False

    def _gen_burst_active(self) -> bool:
        for g in self._gens:
            try:
                if g.is_burst_active:
                    return True
            except Exception:
                pass
        return False

    def start(self) -> None:
        if not self._gens:
            raise RuntimeError("not connected")
        self._armed = False
        self._user_stop.clear()

        # Fast path for Control/TI: hardware burst already configured to play
        # the full repetition train. One gen.start kicks off everything; the
        # worker just polls for burst-complete. No software pre/post in this
        # branch (those are gated to zero by the fast-path conditions).
        if self._hardware_burst and self._params_ref is not None and self._params_ref.mode != "fus":
            self._do_gen_start()

            def wait_hw_burst():
                try:
                    while self._gen_burst_active():
                        if self._user_stop.wait(0.02):
                            return
                except Exception:
                    pass
                if not self._user_stop.is_set() and self._finished_cb:
                    self._finished_cb()

            self._worker_thread = threading.Thread(target=wait_hw_burst, daemon=True)
            self._worker_thread.start()
            return

        # General path: per-repetition worker with software pre/post.
        self._worker_thread = threading.Thread(target=self._run_worker, daemon=True)
        self._worker_thread.start()

    def _reload_stim_buffers(self) -> None:
        """Replace the AWG buffer(s) with the real stim data after the
        prologue. The amplitude was already set to the stim peak at arm
        time (the marker buffer was just constant 0.1 so we got 0.1 × peak),
        so we only need to push the new sample data.

        This is the slow USB step (~50–100 ms per gen); the deadline-based
        software wait in the worker absorbs the jitter so the user-facing
        pre_stim_s is honoured.
        """
        if self._stim_ch1_data is None:
            return
        try:
            self._gens[0].set_data(self._stim_ch1_data)
        except Exception:
            pass
        if len(self._gens) >= 2 and self._stim_ch2_data is not None:
            try:
                self._gens[1].set_data(self._stim_ch2_data)
            except Exception:
                pass

    def _reconfigure_burst_for_stim(self, params: StimParams) -> None:
        """After the prologue, re-set the gens' burst mode for the stim
        phase. For fUS this becomes ``GM_BURST_COUNT`` with
        ``burst_count = n_pulses``; for Control/TI it stays in burst-of-1
        (single playback per software rep) or continuous, as appropriate.
        """
        lt = self._lt
        active_idx = self._active_slot_index(params)
        if params.mode == "fus":
            assert params.fus is not None
            self._finish_outputs(
                burst=True,
                burst_count=int(params.fus.n_pulses),
                active_idx=active_idx,
            )
        else:
            # Control / TI: single playback per gen.start in the worker
            # loop. GM_CONTINUOUS would loop indefinitely; we want one stim
            # block per rep, gated by gen.stop() in the worker.
            self._finish_outputs(burst=True, burst_count=1)

    def _run_worker(self) -> None:
        """Per-repetition worker. Handles pre_stim → stim → post_stim looped
        over repetitions. The first rep is preceded by a session prologue
        (one-shot marker / detection primer) when ``trigger_in`` or
        ``trigger_out`` is enabled.

        For fUS the stim phase is gated by ``is_burst_active`` (hardware burst
        of ``n_pulses`` ISI cycles). For Control/TI the stim phase is a
        software timer.

        EXT 3 ``trigger_stim`` follows the AWG running state, so it is HIGH
        exactly between ``gen.start()`` and ``gen.stop()`` in every iteration
        (with a brief blip during the prologue marker; documented and
        accepted).
        """
        p = self._params_ref
        if p is None:
            return

        pre = float(p.pre_stim_s)
        stim = float(p.stim_time_s)
        post = float(p.post_stim_s)

        reps = p.repetitions if p.repetitions > 0 else -1
        rep_count = 0

        def _wait(dur: float) -> bool:
            """Return True iff stop was requested during the wait."""
            if dur <= 0:
                return self._user_stop.is_set()
            return self._user_stop.wait(timeout=dur)

        skip_first_pre_stim = False
        try:
            # ---- session prologue ----
            if self._has_prologue:
                t_session = time.monotonic()
                # Start the gen. If trigger_in is enabled the gen sits in
                # hardware-wait until the external EXT 2 rising edge; we
                # poll is_running to detect the arrival of the edge.
                self._do_gen_start()
                if p.trigger_in:
                    while not self._gen_is_running():
                        if self._user_stop.wait(0.001):
                            self._do_gen_stop()
                            return
                t_edge = time.monotonic()
                # Wait for the primer burst to finish (~5 ms).
                while self._gen_burst_active():
                    if self._user_stop.wait(0.001):
                        self._do_gen_stop()
                        return
                # The 5 ms marker has now been emitted on EXT 1 (if
                # configured). Disable EXT 1 / EXT 2 so they stay quiet
                # for the rest of the session, and enable EXT 3 (if the
                # user asked for the per-stim gate).
                self._apply_stim_triggers(p)
                # Reload the real stim waveform over USB (~50–100 ms).
                self._reload_stim_buffers()
                self._reconfigure_burst_for_stim(p)
                # Deadline-based remainder wait. We target
                # t_edge + pre_stim_s so the marker→stim interval is
                # pre_stim_s regardless of USB reload jitter.
                remaining = (t_edge + pre) - time.monotonic()
                if remaining > 0:
                    if _wait(remaining):
                        return
                skip_first_pre_stim = True

            # ---- normal rep loop ----
            while reps == -1 or rep_count < reps:
                if not (skip_first_pre_stim and rep_count == 0):
                    if _wait(pre):
                        return
                self._do_gen_start()
                if p.mode == "fus":
                    while self._gen_burst_active():
                        if self._user_stop.wait(0.001):
                            self._do_gen_stop()
                            return
                else:
                    if _wait(stim):
                        self._do_gen_stop()
                        return
                self._do_gen_stop()
                if _wait(post):
                    return
                rep_count += 1
        finally:
            if not self._user_stop.is_set() and self._finished_cb:
                self._finished_cb()

    def stop(self) -> None:
        self._user_stop.set()
        self._armed = False
        for g in self._gens:
            try:
                g.stop()
                g.output_enable = False
            except Exception:
                pass

    def status(self) -> list[DeviceState]:
        out = []
        for i in range(2):
            if i < len(self._gens):
                g = self._gens[i]
                sn = self._serials[i] if i < len(self._serials) else ""
                det = ""
                try:
                    if g.is_running:
                        st = "running"
                    elif self._armed and g.is_controllable:
                        st = "armed"
                    elif g.is_controllable:
                        st = "ready"
                    else:
                        st = "error"
                        det = "not controllable"
                except Exception as e:
                    st = "error"
                    det = str(e)
                out.append(DeviceState(i + 1, sn, st, det))
            else:
                out.append(DeviceState(i + 1, "", "disconnected"))
        return out

    def close(self) -> None:
        self._user_stop.set()
        self._ti_sync = False
        for g in self._gens:
            try:
                g.stop()
                del g
            except Exception:
                pass
        self._gens = []
        self._serials = []
        self._armed = False
        _release_high_res_timer()

    def on_run_finished(self, cb: Callable[[], None]) -> None:
        self._finished_cb = cb


def create_session() -> BaseSession:
    mock = os.environ.get("TIESTIM_MOCK", "").lower() in ("1", "true", "yes")
    if mock:
        return MockSession()
    try:
        import libtiepie  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "python-libtiepie not available (install on Windows/Linux). "
            "Use TIESTIM_MOCK=1 for UI-only."
        ) from e
    return TiePieSession()
