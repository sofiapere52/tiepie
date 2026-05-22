# TiePie HS5 GUI

Source: [github.com/sofiapere52/tiepie](https://github.com/sofiapere52/tiepie).

Local web UI + REST/WebSocket API to drive **one or two TiePie Handyscope HS5** arbitrary waveform outputs (e.g. Digitimer DS5 current stimulators). Supports independent control mode and temporal-interference (TI) mode with sub-sample hardware sync. **Windows 10/11 x64** + [WinUSB driver](https://download.tiepie.com/Drivers/DriverInstall-WinUSB_v10.0.2.exe) + `pip install python-libtiepie` is the supported hardware path. macOS: use `TIESTIM_MOCK=1` only (no native `libtiepie`).

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -e .
pip install python-libtiepie   # Windows/Linux hardware machine
```

## Run

```bash
set TIESTIM_MOCK=1          # optional: no USB
python -m uvicorn tiestim.api.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`. Use **Connect** then **Load** → **Start**; **STOP** stops output and logs `aborted`.

Alternatively, double-click `Start_Tiestim.vbs` or run `run_local.bat`.

## Modes

- **Control** — two independent channels, each with its own shape/frequency/amplitude. Works with 1 or 2 connected HS5 units.
- **TI (temporal interference)** — two carriers at f_c and f_c + Δf. Requires 2 HS5 units connected via **CMI cable** for sub-sample synchronisation.
- **TBS (theta burst)** — TI variant: 3-cycle bursts at fixed Δf = 50 Hz, repeated at a configurable theta frequency (2–8 Hz).
- **fUS (functional ultrasound)** — single-channel PRF-gated sinusoidal carrier at 0.5–2 MHz, designed to drive an external RF amplifier (e.g. Vectawave VBA-230-80). Configurable PRF, duty / tone-burst, sonication duration, ISI off-time / ISI frequency, number of pulses, and amplitude in mV peak-to-peak. See [docs/OPERATOR.md](docs/OPERATOR.md#fUS-specific-hardware-setup) for cabling and amplifier safety.

## Trigger I/O

All three EXT pins are on the HS5 extension connector (26-pin IDC header, 3.3 V LVTTL) and are **fully independent** — any combination of the three can be enabled together.

- **Trigger Out** (EXT 1, OUT) — one-shot session marker. When enabled, the AWG plays a 5 ms × 1/10-amplitude DC pulse at the very start of the session; EXT 1's rising edge fires there. Does **not** re-fire on each stim — use Trigger Stim for that. Requires `pre_stim_s ≥ 105 ms`.
- **Trigger In** (EXT 2, IN) — one-shot session arm. When enabled, the host waits at session start for an external rising edge on EXT 2 before continuing with `pre_stim → stim → post_stim`. Subsequent reps do **not** re-wait. Requires `pre_stim_s ≥ 105 ms`.
- **Trigger Stim** (EXT 3, OUT) — per-stim gate. HIGH while the AWG is running the stim segment, LOW during pre/post. For fUS, HIGH for the entire `n_pulses × ISI` train.

See [docs/OPERATOR.md](docs/OPERATOR.md) for the underlying session-prologue mechanics and how they keep the three pins independent on the single-gen HS5.

## Env

| Variable | Meaning |
|----------|---------|
| `TIESTIM_MOCK` | `1` / `true`: fake devices |
| `TIESTIM_LOG_DIR` | CSV log directory (default `./log`) |
| `TIESTIM_SERIAL_1` / `TIESTIM_SERIAL_2` | Optional generator serials (order) |

## Logs

Append-only `stim_YYYYMMDD.csv` under `TIESTIM_LOG_DIR`. Columns: see [docs/OPERATOR.md](docs/OPERATOR.md).

## Docs

- [docs/OPERATOR.md](docs/OPERATOR.md) — parameters, TI math, safety, CSV.
- [docs/DEV.md](docs/DEV.md) — layout, mock mode, tests.

## GitHub

After clone on another PC: same venv + `pip install -e .` + optional `python-libtiepie`.
