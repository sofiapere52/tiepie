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

## Trigger I/O

- **Trigger Out** (checkbox) — LVTTL pulse on **EXT 1** when stimulation starts.
- **Trigger In** (checkbox) — waits for LVTTL rising edge on **EXT 2** before starting.

Both are on the HS5 extension connector (26-pin IDC header, 3.3 V LVTTL).

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
