from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from array import array
from dataclasses import dataclass
from typing import Callable

from tiestim.models import StimParams
from tiestim.waveform import WaveformPair, numpy_to_array_f, peak_amplitudes


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
    _armed: bool = False
    _running: bool = False
    _thread: threading.Thread | None = None
    _finished_cb: Callable[[], None] | None = None
    _params: StimParams | None = None

    def connect(self) -> list[DeviceState]:
        return [
            DeviceState(1, "MOCK1", "ready"),
            DeviceState(2, "MOCK2", "ready"),
        ]

    def arm(self, params: StimParams, wf: WaveformPair) -> None:
        self._params = params
        self._armed = True
        self._running = False

    def start(self) -> None:
        if not self._armed:
            raise RuntimeError("arm before start")
        self._running = True

        def run():
            if self._params and self._params.repetitions == 0:
                return
            dur = self._params.total_time_s if self._params else 0.1
            reps = max(1, self._params.repetitions) if self._params else 1
            time.sleep(min(30.0, dur * reps + 0.05))
            self._running = False
            self._armed = False
            if self._finished_cb:
                self._finished_cb()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
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
    """Two HS5 AWGs via python-libtiepie (Windows/Linux only)."""

    def __init__(self) -> None:
        import libtiepie as lt

        self._lt = lt
        self._gens: list = []
        self._serials: list[str] = []
        self._finished_cb: Callable[[], None] | None = None
        self._params_ref: StimParams | None = None
        self._armed: bool = False
        self._hardware_burst: bool = False
        self._ti_sync: bool = False
        self._soft_rep_cancel = threading.Event()

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

    def _finish_outputs(self, burst: bool, repetitions: int) -> None:
        lt = self._lt
        for g in self._gens:
            if burst and repetitions > 0:
                try:
                    g.mode = lt.GM_BURST_COUNT
                    g.burst_count = int(repetitions)
                except Exception:
                    g.mode = lt.GM_CONTINUOUS
            else:
                g.mode = lt.GM_CONTINUOUS
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

    def _configure_triggers(self, params: StimParams) -> None:
        self._ti_sync = False
        if not self._gens:
            return
        lt = self._lt
        g0 = self._gens[0]

        self._disable_all_triggers()

        # TI sync via CMI internal trigger (keeps EXT ports free)
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

        # User trigger-out on Gen1 EXT 1
        if params.trigger_out:
            tout = self._find_trigger_io(g0, "output", "EXT 1")
            if tout:
                try:
                    tout.enabled = True
                    tout.event = lt.TOE_GENERATOR_START
                except Exception:
                    pass

        # User trigger-in on Gen1 EXT 2
        if params.trigger_in:
            tin = self._find_trigger_io(g0, "input", "EXT 2")
            if tin:
                try:
                    tin.enabled = True
                    tin.kind = lt.TK_RISINGEDGE
                except Exception:
                    pass

    def arm(self, params: StimParams, wf: WaveformPair) -> None:
        self._params_ref = params
        self._hardware_burst = False
        if not self._gens:
            raise RuntimeError("not connected")
        if len(self._gens) < 2 and params.mode == "ti":
            raise RuntimeError("TI mode requires 2 connected devices")

        a1, a2 = peak_amplitudes(params)
        d1 = numpy_to_array_f(wf.ch1)
        g0 = self._gens[0]

        try:
            mx = g0.amplitude_max
            if a1 > mx:
                raise ValueError(f"ch1 amplitude exceeds max {mx} A")
        except (AttributeError, ValueError):
            raise
        except Exception:
            pass
        try:
            lo, hi = g0.data_length_min, g0.data_length_max
            n = len(d1)
            if n < lo or n > hi:
                raise ValueError(f"buffer length {n} not in [{lo}, {hi}]")
        except ValueError:
            raise
        except Exception:
            pass

        self._prepare_gen(g0, d1, wf.sample_rate_hz, a1)

        if len(self._gens) >= 2:
            d2 = numpy_to_array_f(wf.ch2)
            g1 = self._gens[1]
            try:
                mx = g1.amplitude_max
                if a2 > mx:
                    raise ValueError(f"ch2 amplitude exceeds max {mx} A")
            except (AttributeError, ValueError):
                raise
            except Exception:
                pass
            self._prepare_gen(g1, d2, wf.sample_rate_hz, a2)

        lt = self._lt
        want_burst = params.repetitions > 0 and all(
            (g.modes_native & lt.GM_BURST_COUNT) for g in self._gens
        )
        try:
            self._finish_outputs(burst=want_burst, repetitions=params.repetitions)
        except Exception:
            self._finish_outputs(burst=False, repetitions=0)

        self._configure_triggers(params)
        self._hardware_burst = want_burst and params.repetitions > 0
        self._armed = True

    def start(self) -> None:
        if not self._gens:
            raise RuntimeError("not connected")
        self._armed = False
        self._soft_rep_cancel.clear()

        if self._ti_sync and len(self._gens) >= 2:
            # Gen2 first: arms and waits for hardware trigger on EXT 1
            self._gens[1].start()
            # Gen1: starts immediately and fires trigger → Gen2 starts on
            # the same hardware edge (sub-sample synchronisation)
            self._gens[0].start()
        else:
            for g in self._gens:
                g.start()

        pr = self._params_ref

        def wait_burst_hw():
            if not pr or pr.repetitions == 0 or not self._hardware_burst:
                return
            try:
                while any(g.is_burst_active for g in self._gens):
                    if self._soft_rep_cancel.is_set():
                        return
                    time.sleep(0.02)
            except Exception:
                pass
            if not self._soft_rep_cancel.is_set() and self._finished_cb:
                self._finished_cb()

        def wait_software_reps():
            if not pr or pr.repetitions == 0 or self._hardware_burst:
                return
            N = int(pr.repetitions)
            T = pr.total_time_s
            for _ in range(N):
                if self._soft_rep_cancel.wait(timeout=T):
                    return
            self.stop()
            if self._finished_cb:
                self._finished_cb()

        if pr and pr.repetitions > 0:
            if self._hardware_burst:
                threading.Thread(target=wait_burst_hw, daemon=True).start()
            else:
                threading.Thread(target=wait_software_reps, daemon=True).start()

    def stop(self) -> None:
        self._soft_rep_cancel.set()
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
        self._soft_rep_cancel.set()
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
