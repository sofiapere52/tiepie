# Developer notes

## Layout

- `src/tiestim/models.py` — Pydantic `StimParams` / `StimRequest`; shapes: sine, triangle, square, ramp, tbs
- `src/tiestim/waveform.py` — `build_waveforms`, Nyquist checks, TBS burst gating, `numpy` → `array('f')`
- `src/tiestim/session.py` — `MockSession`, `TiePieSession` (lazy `libtiepie`); discovery: one or two `ST_ARBITRARY` generators, optional `TIESTIM_SERIAL_*`; TI sync via CMI internal triggers; EXT 1/2 trigger I/O
- `src/tiestim/logger.py` — daily CSV append
- `src/tiestim/api/` — FastAPI, poller-driven WS broadcast, burst-complete callback from session thread

TiePie patterns from official examples: `GeneratorArbitrary.py` (arbitrary + sample rate), `GeneratorBurst.py` (burst), `ListDevices.py`.

## Single-device mode

If only one HS5 is found during `connect()`, the session operates with one generator. TI mode is disabled (requires 2). The UI auto-disables the TI option and Ch2 controls.

## TI synchronisation

In TI mode, `_find_cmi_trigger_pair()` locates a non-EXT trigger output on Gen1 and a matching trigger input on Gen2 (routed through the CMI cable). Gen2 is started first (armed, waiting for trigger), then Gen1 starts and fires the trigger — both begin on the same hardware clock edge. Requires a physical CMI cable between the two HS5 units.

## Trigger I/O

EXT 1 (trigger out) and EXT 2 (trigger in) on the HS5 extension connector are configured independently of TI sync. `_find_trigger_io()` locates EXT ports by TIID constant, then by name, with fallback.

## TBS (theta burst)

When `tbs_freq_hz` is set in TI mode, `build_waveforms()` applies a binary gate after generating the TI cosines: 3 beat-cycles on (`3 / |Δf|` seconds), then off until the next TBS period (`1 / tbs_freq_hz`). Δf is fixed at 50 Hz by the frontend.

## Mock

`TIESTIM_MOCK=1` — no `libtiepie` load; UI and API behave for layout testing. Mock always returns 2 devices.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

## Lint / format

No project-enforced formatter yet; match surrounding style.
