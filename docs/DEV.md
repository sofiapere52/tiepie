# Developer notes

## Layout

- `src/tiestim/models.py` — Pydantic `StimParams` / `StimRequest` / `FusParams`; shapes: sine, triangle, square, ramp, tbs; modes: control, ti, **fus**
- `src/tiestim/waveform.py` — `build_waveforms`, Nyquist checks, TBS burst gating, `_fill_fus` (PRF-gated carrier), `choose_hardware_sample_rate`, `numpy` → `array('f')`
- `src/tiestim/session.py` — `MockSession`, `TiePieSession` (lazy `libtiepie`); discovery: one or two `ST_ARBITRARY` generators, optional `TIESTIM_SERIAL_*`; TI sync via CMI internal triggers; EXT 1/2/3 trigger I/O; per-phase worker thread; Windows `timeBeginPeriod(1)` on construction
- `src/tiestim/logger.py` — daily CSV append; nine fUS columns + one trigger column added in v0.2
- `src/tiestim/api/` — FastAPI, poller-driven WS broadcast, burst-complete callback from session thread

TiePie patterns from official examples: `GeneratorArbitrary.py` (arbitrary + sample rate), `GeneratorBurst.py` (burst), `ListDevices.py`.

## Buffer composition (v0.2 refactor)

`waveform._buffer_duration_s(p)` returns the buffer duration the AWG sees:

| Mode | Buffer |
|------|--------|
| Control / TI / TBS | `stim_time_s` (pre/post handled in software) |
| fUS | `sonication_duration_s + isi_off_s` (one ISI cycle) |

`build_waveforms` produces stim-only samples (no pre/post zeros). `choose_hardware_sample_rate` uses `_buffer_duration_s()` for the buffer-fit cap. The motivation: silent pre/post no longer count against the 64 Mi-sample buffer, so long high-frequency runs work; and EXT 3 cleanly mirrors the stim window because the AWG is idle during pre/post.

## Per-phase worker thread with session prologue

`TiePieSession.start()` spawns one worker thread (`_run_worker`) that runs the same per-rep schedule for every mode: `pre_stim software wait → gen.start → wait stim → gen.stop → post_stim software wait`, looped over repetitions.

When `trigger_in` or `trigger_out` is enabled, the worker prepends a **session prologue** to the very first repetition (`_has_prologue` flag set during `arm`):

1. The gen has already been loaded by `arm()` with a 5 ms primer buffer (DC marker if `trigger_out`, silent if only `trigger_in`). The gen is configured with `EXT 2 = TK_RISINGEDGE` if `trigger_in`, and `EXT 1 = TOE_GENERATOR_STOP` if `trigger_out`.
2. Worker calls `gen.start()`. If `trigger_in` is on, the gen sits in HW-wait until the EXT 2 edge; the worker polls `is_running` (~1 ms cadence) to detect the arrival. `t_edge` is captured at the transition.
3. Worker polls `is_burst_active` until the 5 ms primer finishes.
4. `_apply_stim_triggers(p)` disables EXT 1 and EXT 2 (one-shot done) and enables EXT 3 if `trigger_stimulation` is on.
5. `_reload_stim_buffers()` pushes the real stim samples over USB (~50–100 ms) into the gen. `_reconfigure_burst_for_stim(p)` updates `GM_BURST_COUNT` for the stim phase (n_pulses for fUS, 1 for Control/TI).
6. **Deadline-based remainder wait**: `target = t_edge + pre_stim_s`; sleep until `target - now()`. This absorbs USB reload jitter so the user-facing `pre_stim_s` is honoured.
7. The rep loop runs as usual; rep 0 skips its `pre_stim` software wait (it was the prologue).

For mode-specific stim timing inside the loop:
- **Control / TI / TBS**: `_user_stop.wait(stim_time_s)` then `gen.stop()`. TI mode uses CMI sync (gen2 armed first, gen1 fires) on every `gen.start`.
- **fUS**: poll `gen.is_burst_active` until the `n_pulses × ISI` burst completes, then `gen.stop()`.

Fast path (Control/TI only): when `pre = post = 0` AND `trigger_in = False` AND `trigger_out = False` AND `repetitions > 0`, `_hardware_burst` is set and a single `gen.start()` runs the full `GM_BURST_COUNT = repetitions` burst with zero host-side rep jitter.

Each phase wait uses `self._user_stop.wait(timeout=…)` so STOP requests abort within ~10 ms.

## Windows timer resolution

`_enable_windows_high_res_timer()` calls `winmm.timeBeginPeriod(1)` at `TiePieSession.__init__`. This raises the Windows scheduler resolution from the default 15.6 ms to ~1 ms for the entire process, dramatically reducing `time.sleep` jitter for the new software pre/post phases. Released in `close()`. No-op on non-Windows.

Practical jitter budget after this change:
- **Stim duration** — hardware-clocked, exact.
- **Pre / post duration** — bounded by Windows scheduler, typically < 1 ms.
- **Pre→stim transition** — same scheduler jitter as pre. If you need zero jitter for downstream alignment, trigger off the EXT 3 (Trigger Stim) rising edge — that's the hardware playback start.

## Trigger I/O (EXT 1/2/3)

Three helpers run in sequence during `arm` + worker thread:

1. `_configure_session_triggers(p)` (in `arm`) — disables all EXT outputs/inputs as a baseline, and re-enables the CMI internal trigger for TI sync (independent of EXT flags).
2. `_apply_prologue_triggers(p)` (in `arm`, only when `_has_prologue`) — enables EXT 1 (`TOE_GENERATOR_STOP`) and/or EXT 2 (`TK_RISINGEDGE`) on the active gen for the one-shot prologue. **Does not enable EXT 3 yet** — the prologue is allowed to have a brief 5 ms blip on EXT 3 but it is not the user-facing stim gate.
3. `_apply_stim_triggers(p)` (in worker, after the prologue) — disables EXT 1 and EXT 2, and enables EXT 3 (`TOE_GENERATOR_STOP`) on the active gen if the user requested `trigger_stimulation`.

The **active** gen is `_active_slot_index(params)`:
- fUS → slot `fus.channel - 1` (1 → index 0, 2 → index 1).
- Control / TI / TBS → slot 0.

All three EXT pins are on the same physical HS5 (the active one). The split-phase trigger configuration is what guarantees the three pins are mutually independent at the user-facing level even though they are all driven by the same underlying `TOE_GENERATOR_STOP` event family on the HS5.

## TI synchronisation

In TI mode, `_find_cmi_trigger_pair()` locates a non-EXT trigger output on Gen1 and a matching trigger input on Gen2 (routed through the CMI cable). Gen2 is started first (armed, waiting for trigger), then Gen1 starts and fires the trigger — both begin on the same hardware clock edge. Requires a physical CMI cable between the two HS5 units. **Unchanged by the v0.2 refactor.**

## fUS implementation notes

`_fill_fus(y, p, sr)` fills one ISI cycle: PRF-gated sinusoidal carrier in `[0, SD * sr)`, silence in `[SD * sr, len(y))`. The inactive channel's buffer is zeroed and its gen is idled (`gen.stop(); gen.output_enable=False`). `peak_amplitudes` returns `(amplitude_mv_pp / 2 / 1000, 0)` for channel 1 (or the swap for channel 2).

The full `n_pulses × ISI` train is delivered by `gen.mode = GM_BURST_COUNT; gen.burst_count = n_pulses`. With burst mode, `gen.is_burst_active` is True from start to end of the whole train, which is what gates EXT 3 HIGH.

## TBS (theta burst)

When `tbs_freq_hz` is set in TI mode, `build_waveforms()` applies a binary gate after generating the TI cosines: 3 beat-cycles on (`3 / |Δf|` seconds), then off until the next TBS period (`1 / tbs_freq_hz`). Unchanged by the v0.2 refactor.

## Mock

`TIESTIM_MOCK=1` — no `libtiepie` load; UI and API behave for layout testing. Mock always returns 2 devices. `MockSession.arm` populates the same `arm_info` struct as the real session, so the same tests exercise both wiring paths.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

Test files:

- `tests/test_waveform.py` — pre-existing waveform / sample-rate tests (unchanged).
- `tests/test_preview.py` — preview API tests (both windowed and overview), now with fUS coverage.
- `tests/test_fus_waveform.py` — fUS buffer shape, PRF gating, ISI silence, amplitude scaling, validator rules, buffer-overflow message.
- `tests/test_session_prologue.py` — session-marker prologue: no-op when both triggers off, marker phase + remainder when on, one-shot per session even with reps > 1, pre_stim minimum enforced, fUS path also prologue-aware.
- `tests/test_logger.py` — CSV schema (new trigger + fUS + session_marker columns) and end-to-end writeback.

## Lint / format

No project-enforced formatter yet; match surrounding style.
