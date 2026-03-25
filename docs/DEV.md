# Developer notes

## Layout

- `src/tiestim/models.py` — Pydantic `StimParams` / `StimRequest`
- `src/tiestim/waveform.py` — `build_waveforms`, Nyquist checks, `numpy` → `array('f')`
- `src/tiestim/session.py` — `MockSession`, `TiePieSession` (lazy `libtiepie`); discovery: two list items with `ST_ARBITRARY` generators, optional `TIESTIM_SERIAL_*`
- `src/tiestim/logger.py` — daily CSV append
- `src/tiestim/api/` — FastAPI, poller-driven WS broadcast, burst-complete callback from session thread

TiePie patterns from official examples: `GeneratorArbitrary.py` (arbitrary + sample rate), `GeneratorBurst.py` (burst), `ListDevices.py`.

## Mock

`TIESTIM_MOCK=1` — no `libtiepie` load; UI and API behave for layout testing.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

## Lint / format

No project-enforced formatter yet; match surrounding style.
