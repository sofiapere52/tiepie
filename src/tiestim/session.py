from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from array import array
from dataclasses import dataclass
from typing import Callable

from tiestim.models import StimParams
from tiestim.waveform import WaveformPair, numpy_to_array_f, peak_voltages


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
            time.sleep(min(5.0, dur * reps + 0.05))
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
        if len(chosen) < 2:
            for _, g in chosen:
                del g
            raise RuntimeError(
                "Need 2 generators with ST_ARBITRARY. Found %d. "
                "Set TIESTIM_SERIAL_1 / TIESTIM_SERIAL_2 if needed."
                % len(chosen)
            )
        self._serials = [str(chosen[0][0]), str(chosen[1][0])]
        self._gens = [chosen[0][1], chosen[1][1]]

    def connect(self) -> list[DeviceState]:
        self.close()
        self._discover()
        return self.status()

    def _configure_one(self, gen, data: array, sr: float, amp_v: float, repetitions: int) -> None:
        lt = self._lt
        gen.stop()
        gen.output_enable = False
        gen.signal_type = lt.ST_ARBITRARY
        gen.frequency_mode = lt.FM_SAMPLERATE
        gen.frequency = float(sr)
        gen.amplitude = float(amp_v)
        gen.offset = 0.0
        gen.verify_data_length(len(data))
        gen.set_data(data)
        if repetitions > 0 and (gen.modes_native & lt.GM_BURST_COUNT):
            gen.mode = lt.GM_BURST_COUNT
            gen.burst_count = int(repetitions)
        else:
            gen.mode = lt.GM_CONTINUOUS
        gen.output_enable = True

    def arm(self, params: StimParams, wf: WaveformPair) -> None:
        self._params_ref = params
        if len(self._gens) != 2:
            raise RuntimeError("not connected")
        a1, a2 = peak_voltages(params)
        d1 = numpy_to_array_f(wf.ch1)
        d2 = numpy_to_array_f(wf.ch2)
        g0, g1 = self._gens[0], self._gens[1]
        for g in (g0, g1):
            mx = g.amplitude_max
            lo, hi = g.data_length_min, g.data_length_max
            n = len(d1)
            if n < lo or n > hi:
                raise ValueError(f"buffer length {n} not in [{lo}, {hi}]")
            if a1 > mx or a2 > mx:
                raise ValueError(f"amplitude exceeds max {mx} V")
        self._configure_one(g0, d1, wf.sample_rate_hz, a1, params.repetitions)
        self._configure_one(g1, d2, wf.sample_rate_hz, a2, params.repetitions)

    def start(self) -> None:
        if len(self._gens) != 2:
            raise RuntimeError("not connected")
        g0, g1 = self._gens[0], self._gens[1]
        g0.start()
        g1.start()

        def wait_burst():
            pr = self._params_ref
            if not pr or pr.repetitions == 0:
                return
            try:
                while g0.is_burst_active or g1.is_burst_active:
                    time.sleep(0.02)
            except Exception:
                pass
            if self._finished_cb:
                self._finished_cb()

        threading.Thread(target=wait_burst, daemon=True).start()

    def stop(self) -> None:
        for g in self._gens:
            try:
                g.stop()
                g.output_enable = False
            except Exception:
                pass

    def status(self) -> list[DeviceState]:
        if len(self._gens) != 2:
            return [
                DeviceState(1, "", "disconnected"),
                DeviceState(2, "", "disconnected"),
            ]
        out = []
        for i, g in enumerate(self._gens):
            sn = self._serials[i] if i < len(self._serials) else ""
            det = ""
            try:
                if g.is_running:
                    st = "running"
                elif g.is_controllable:
                    st = "ready"
                else:
                    st = "error"
                    det = "not controllable"
            except Exception as e:
                st = "error"
                det = str(e)
            out.append(DeviceState(i + 1, sn, st, det))
        return out

    def close(self) -> None:
        for g in self._gens:
            try:
                g.stop()
                del g
            except Exception:
                pass
        self._gens = []
        self._serials = []

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
