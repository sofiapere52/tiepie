# Operator reference

## Safety

- Software is **not** a medical device and **does not** enforce DS5 or tissue limits. **STOP** requests generator stop on both channels; verify output with a scope when commissioning.
- Close TiePie **Multi Channel** desktop software before using this tool if the instrument is locked.

## Parameters

| Field | Unit | Notes |
|-------|------|--------|
| `mode` | — | `standard` or `ti` |
| `shape` | — | `sine`, `triangle`, `square`, `ramp` (standard path; TI uses cosines only) |
| `frequency_hz` | Hz | Standard: tone frequency. TI: duplicate of carrier for API; TI uses `carrier_hz`. |
| `amplitude_v` | V | Peak at AWG output after arbitrary scaling (buffers are ±1; amplitude scales in hardware). TI: **total** peak budget split by ratio. |
| `pulse_width_s` | s | Square: high time within one period. Optional. |
| `total_time_s` | s | Full buffer length including pre/post silence. |
| `pre_stim_s` / `post_stim_s` | s | Leading/trailing zeros. |
| `sample_rate_hz` | Hz | Arbitrary buffer clock (`FM_SAMPLERATE`). |
| `repetitions` | — | `0` = continuous until STOP; `>0` = burst count if hardware supports `GM_BURST_COUNT` with arbitrary. |
| `carrier_hz` | Hz | TI only. |
| `delta_f_hz` | Hz | TI only; device 2 runs at `carrier_hz + delta_f_hz`. |
| `amplitude_ratio` | — | TI only, e.g. `2:3` → channel peaks `(2/5)*A_tot`, `(3/5)*A_tot`. |

## TI waveforms

Per channel (active window), with \(r_1+r_2\) normalized from the ratio string:

- Ch1: \(x_1(t) = A_1 \cos(2\pi f_c t)\)
- Ch2: \(x_2(t) = -A_2 \cos(2\pi (f_c+\Delta f) t)\) (anti-phase)

Buffers are normalized to ±1 before download; \(A_1,A_2\) are applied as separate generator amplitudes. Envelope at \(\Delta f\) appears in the **medium** after superposition, not in these traces alone.

## CSV columns

`timestamp_utc`, `timestamp_local`, `mode`, `shape`, `frequency_hz`, `carrier_hz`, `delta_f_hz`, `amplitude_v`, `amplitude_ratio`, `pulse_width_s`, `sample_rate_hz`, `total_time_s`, `pre_stim_s`, `post_stim_s`, `repetitions`, `device_1_serial`, `device_2_serial`, `outcome` (`ok` / `aborted`), `error_message`, `duration_actual_s`.

## LED labels (UI)

Inline text next to each LED: grey disconnected; green steady ready; amber armed; **green pulsing** running; blue done; red error.
