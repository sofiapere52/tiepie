# Operator reference

## Safety

- Software is **not** a medical device and **does not** enforce DS5 or tissue limits. **STOP** requests generator stop on both channels; verify output with a scope when commissioning.
- Close TiePie **Multi Channel** desktop software before using this tool if the instrument is locked.

## Hardware setup

- **Single device** — connect one HS5 via USB. TI mode is disabled; only control mode (Channel 1) is available.
- **Two devices** — connect both HS5 units via USB. For TI mode, also connect the **CMI (Combined Measurement Interface) cable** between the two units; this provides sub-sample hardware synchronisation.
- **Trigger I/O** — the HS5 extension connector (26-pin IDC header) carries EXT 1 and EXT 2. Trigger signals are 3.3 V LVTTL.
  - **Trigger Out** (EXT 1): emits a pulse when stimulation starts.
  - **Trigger In** (EXT 2): waits for a rising edge before starting stimulation.

## Parameters

| Field | Unit | Notes |
|-------|------|--------|
| `mode` | — | `control` or `ti` |
| `shape` | — | `sine`, `triangle`, `square`, `ramp` (control path shapes); `tbs` (TI-only theta burst) |
| `frequency_hz` | Hz | Control: per-channel tone frequency. TI: duplicate of carrier for API. |
| `amplitude_a` | A | Control: per-channel peak current. TI: **total** peak current budget split by ratio. |
| `pulse_width_s` | s | Square: high time within one period. Optional. |
| `stim_time_s` | s | Active stimulation duration within one buffer cycle. |
| `pre_stim_s` / `post_stim_s` | s | Leading/trailing silence. |
| `total_time_s` | s | Computed: `pre_stim_s + stim_time_s + post_stim_s`. |
| `ramp_s` | s | Linear ramp up/down at start/end of active segment. Must satisfy `2 × ramp_s ≤ stim_time_s`. |
| `sample_rate_hz` | Hz | Hardware sample rate (`FM_SAMPLERATE`), default 500 kHz. |
| `repetitions` | — | `0` = continuous until STOP; `>0` = burst count if hardware supports `GM_BURST_COUNT` with arbitrary. |
| `carrier_hz` | Hz | TI only: carrier frequency. |
| `delta_f_hz` | Hz | TI only: device 2 runs at `carrier_hz + delta_f_hz`. Fixed at 50 Hz when TBS shape is selected. |
| `amplitude_ratio` | — | TI only, e.g. `2:3` → channel peaks `(2/5)×A_tot`, `(3/5)×A_tot`. |
| `tbs_freq_hz` | Hz | TBS only (TI mode, shape = tbs): burst repetition rate, 2–8 Hz. |
| `trigger_out` | — | Boolean. If enabled, EXT 1 emits LVTTL pulse on stimulation start. |
| `trigger_in` | — | Boolean. If enabled, waits for LVTTL rising edge on EXT 2 before starting. |

## TI waveforms

Per channel (active window), with \(r_1+r_2\) normalized from the ratio string:

- Ch1: \(x_1(t) = A_1 \cos(2\pi f_c t)\)
- Ch2: \(x_2(t) = -A_2 \cos(2\pi (f_c+\Delta f) t)\) (anti-phase)

Buffers are normalized to ±1 before download; \(A_1,A_2\) are applied as separate generator amplitudes. Envelope at \(\Delta f\) appears in the **medium** after superposition, not in these traces alone.

### TI synchronisation

Both HS5 units must be connected with a **CMI cable**. The software uses a non-EXT internal trigger routed through the CMI bus: Gen2 is armed first (waiting for trigger), then Gen1 starts and fires — both begin on the same hardware clock edge.

### TBS (theta burst stimulation)

When shape = `tbs` is selected, a gated pattern is applied to the TI cosines:

- **Burst duration**: 3 full beat cycles = `3 / |Δf|` seconds (60 ms at Δf = 50 Hz).
- **TBS period**: `1 / tbs_freq_hz` (e.g. 200 ms at 5 Hz).
- The active segment is multiplied by a binary gate: 1 during bursts, 0 during inter-burst intervals.

## CSV columns

`timestamp_utc`, `timestamp_local`, `mode`, `shape`, `frequency_hz`, `carrier_hz`, `delta_f_hz`, `amplitude_a`, `amplitude_ratio`, `pulse_width_s`, `sample_rate_hz`, `stim_time_s`, `total_time_s`, `pre_stim_s`, `post_stim_s`, `ramp_s`, `repetitions`, `device_1_serial`, `device_2_serial`, `outcome` (`ok` / `aborted`), `error_message`, `duration_actual_s`.

## LED labels (UI)

Inline text next to each LED: grey disconnected; green steady ready; amber loaded, waiting for start; **green pulsing** running; blue done; red error.
