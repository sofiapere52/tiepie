# Tiestim

Source: [github.com/sofiapere52/tiepie](https://github.com/sofiapere52/tiepie).

Local web UI + REST/WebSocket API to drive **two TiePie Handyscope HS5** arbitrary waveform outputs (e.g. Digitimer DS5). **Windows 10/11 x64** + [WinUSB driver](https://download.tiepie.com/Drivers/DriverInstall-WinUSB_v10.0.2.exe) + `pip install python-libtiepie` is the supported hardware path. macOS: use `TIESTIM_MOCK=1` only (no native `libtiepie`).

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

Open `http://127.0.0.1:8000/`. Use **Connect** then **Arm** → **Start**; **STOP** stops output and logs `aborted`.

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
