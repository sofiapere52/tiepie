# Operator reference

## Safety

- Software is **not** a medical device and **does not** enforce DS5, tissue, or amplifier limits. **STOP** requests generator stop on both channels; verify output with a scope when commissioning.
- Close TiePie **Multi Channel** desktop software before using this tool if the instrument is locked.

## Hardware setup

- **Single device** — connect one HS5 via USB. TI mode is disabled; control mode (Channel 1) and fUS mode (Channel 1 auto-selected) are available.
- **Two devices** — connect both HS5 units via USB. For TI mode, also connect the **CMI (Combined Measurement Interface) cable** between the two units; this provides sub-sample hardware synchronisation. For fUS you can pick either unit via the Channel 1 / 2 radio buttons.
- **Trigger I/O** — the HS5 extension connector (26-pin IDC header) carries EXT 1, EXT 2, and EXT 3. Trigger signals are 3.3 V LVTTL. The three pins are fully independent — any combination is allowed.
  - **Trigger Out** (EXT 1, OUT): one-shot **session marker**. When enabled, the AWG emits a 5 ms × 1/10-amplitude DC pulse at the very start of the session — that pulse takes the place of the first 5 ms of `pre_stim`, the rising edge fires EXT 1, then the rest of `pre_stim` is software-managed. EXT 1 does **not** re-fire on each stim. Requires `pre_stim_s ≥ 105 ms`.
  - **Trigger In** (EXT 2, IN): one-shot **session arm**. When enabled, the host waits at session start for an external rising edge on EXT 2 before continuing with `pre_stim → stim → post_stim`. Subsequent repetitions do **not** re-wait. Requires `pre_stim_s ≥ 105 ms`.
  - **Trigger Stim** (EXT 3, OUT): per-stim **gate**. Held HIGH while the AWG is running the stim (= during the stimulation segment), LOW during pre-stim and post-stim. For fUS, HIGH for the entire `n_pulses × ISI` train. *Note:* during the 5 ms session marker / primer (if Trigger In or Trigger Out is enabled) EXT 3 also briefly goes HIGH because the AWG is technically "running" that primer. This is a benign 5 ms blip; downstream systems should treat the first sustained HIGH window of EXT 3 as the real stim.

### fUS-specific hardware setup

The HS5 is a small-signal arbitrary waveform generator (≈12 V pp max, ≥10 Ω output impedance — see HS5 datasheet). It is **not** a power amplifier: to drive an ultrasound transducer at therapeutic levels you need an external RF amplifier. This GUI was developed with the **Vectawave VBA-230-80** in mind (50 dB ≈ ×316 voltage gain, 80 W into 50 Ω, 2–300 MHz). Recommended setup:

1. **Power-on order**: Power on the HS5 first, then the amplifier. Power off in reverse. Powering the amplifier while the HS5 output is floating can latch in a noisy state.
2. **Cabling**: Use 50 Ω coax (RG-58 or better) from the HS5 BNC output to the amplifier RF input. Keep it short (< 1 m) and away from USB / power cables.
3. **Output**: The amplifier output drives the transducer via 50 Ω coax. Maintain a 50 Ω matched load — running the amplifier into an open/short can damage the output stage on every Vectawave datasheet.
4. **Amplitude convention**:
   - You enter amplitude as **mV peak-to-peak** at the HS5 BNC (i.e. at the amplifier *input*).
   - The GUI shows the expected amplifier *output* live: `output = input × 316` (50 dB voltage gain). E.g. 200 mV pp in → ~63.2 V pp out.
   - **Soft limit at 500 mV pp**: the GUI warns above this. The Vectawave VBA-230-80 datasheet rates 500 mV pp as the maximum recommended input; higher inputs may saturate the amplifier or damage the output stage. The GUI does **not** hard-block — you can override if you understand your amplifier's spec.
5. **Acoustic safety**: Ultrasound at MHz frequencies is invisible. Always confirm the transducer is in water (or coupling gel) before energizing; air-loaded transducers can self-destruct.

### Behaviour change vs older versions

Older versions always uploaded the **full** `pre_stim + stim + post_stim` window to the AWG buffer, and trigger_out / trigger_in were per-rep events tied to the buffer's start. This wasted buffer space on silence, limited long runs at high carriers, and entangled the three EXT pins.

Now (v0.2+):

| Mode | Buffer holds |
|------|--------------|
| Control / TI / TBS | **stim_time only**. Pre/post are software waits in the host (`time.sleep`). |
| fUS | **one ISI cycle (SD + isi_off)**. The full `n_pulses` train is delivered by HS5 hardware burst mode. Pre/post are software waits. |

When **Trigger Out** or **Trigger In** is enabled the session is prefixed by a **5 ms session prologue**:

| Combination | What plays on the AWG during the first 5 ms of `pre_stim` |
|---|---|
| Trigger Out ON, Trigger In OFF | 5 ms DC marker (1/10 of stim amplitude) — EXT 1 goes HIGH, fires its rising edge. |
| Trigger In ON, Trigger Out OFF | 5 ms silent primer — gen sits in HW-wait until the EXT 2 rising edge arrives, then plays the silent primer (no BNC output). |
| Both ON | gen sits in HW-wait until EXT 2 edge, then plays the DC marker (EXT 1 fires HIGH). |
| Both OFF | No prologue — worker goes straight to `pre_stim` software wait. |

After the prologue:

1. EXT 1 and EXT 2 are programmatically **disabled** so they stay quiet for the rest of the session.
2. The host reloads the real stim buffer over USB (~50–100 ms).
3. The remainder of `pre_stim` is waited in software using a **deadline timer** anchored at the prologue's start, so the user-facing `pre_stim_s` is honoured regardless of USB reload jitter.
4. The rep loop runs `[pre_stim → stim → post_stim] × repetitions`, with EXT 3 gating each stim window cleanly.

Practical consequences:

- You can now do much longer high-frequency stimulations without overflowing the 64 Mi-sample buffer (silent pre/post no longer count).
- Pre-stim and post-stim jitter is now bound by the OS scheduler — typically < 1 ms on Windows (1 ms timer resolution is requested at startup). **Stim duration** itself is still hardware-clocked and exact.
- All three EXT triggers are independent. You can enable any combination; behaviour does not change based on which others are enabled.
- During the 5 ms session prologue, EXT 3 (if enabled) briefly goes HIGH because the AWG is technically running the primer. Downstream systems that need to detect *stim* should look for the longer sustained HIGH that follows; the prologue blip is documented and benign.
- `pre_stim_s` must be ≥ 105 ms when Trigger Out or Trigger In is enabled (5 ms marker + 100 ms USB safety). The GUI / API both refuse smaller values with a clear error message.

## Parameters (Control / TI / TBS)

| Field | Unit | Notes |
|-------|------|--------|
| `mode` | — | `control`, `ti`, or `fus` |
| `shape` | — | `sine`, `triangle`, `square`, `ramp` (control path shapes); `tbs` (TI-only theta burst) |
| `frequency_hz` | Hz | Control: per-channel tone frequency. TI: duplicate of carrier for API. |
| `amplitude_a` | A | Control: per-channel peak current. TI: **total** peak current budget split by ratio. |
| `pulse_width_s` | s | Square: high time within one period. Optional. |
| `stim_time_s` | s | Active stimulation duration within one buffer cycle. |
| `pre_stim_s` / `post_stim_s` | s | Leading/trailing silence (software waits unless `trigger_in` is on). |
| `total_time_s` | s | Computed: `pre_stim_s + stim_time_s + post_stim_s`. |
| `ramp_s` | s | Linear ramp up/down at start/end of active segment. Must satisfy `2 × ramp_s ≤ stim_time_s`. |
| `sample_rate_hz` | Hz | Hardware sample rate (`FM_SAMPLERATE`), auto-picked per request. |
| `repetitions` | — | `0` = continuous until STOP; `>0` = N repetitions of the cycle. |
| `carrier_hz` | Hz | TI only: carrier frequency. |
| `delta_f_hz` | Hz | TI only: device 2 runs at `carrier_hz + delta_f_hz`. Fixed at 50 Hz when TBS shape is selected. |
| `amplitude_ratio` | — | TI only, e.g. `2:3` → channel peaks `(2/5)×A_tot`, `(3/5)×A_tot`. |
| `tbs_freq_hz` | Hz | TBS only (TI mode, shape = tbs): burst repetition rate, 2–8 Hz. |
| `trigger_out` | — | Boolean. EXT 1 one-shot session marker (5 ms × 1/10 amp at session start; requires `pre_stim_s ≥ 105 ms`). |
| `trigger_in` | — | Boolean. EXT 2 one-shot session arm (wait for external rising edge at session start; requires `pre_stim_s ≥ 105 ms`). |
| `trigger_stimulation` | — | Boolean. EXT 3 per-stim gate (HIGH while AWG plays the stim). New in v0.2. |

## Parameters (fUS)

| Field | Unit | Notes |
|-------|------|--------|
| `fus.channel` | — | 1 or 2: which HS5 drives the transducer. Auto-locked to 1 if only one HS5 is connected. |
| `fus.carrier_hz` | Hz | Acoustic carrier (central) frequency. **Range: 0.5 – 2 MHz**. |
| `fus.prf_hz` | Hz | Pulse Repetition Frequency: rate at which the carrier is gated within one sonication. |
| `fus.prf_duty` | — | PRF duty cycle (0 – 1). Linked to `tone_burst_s` (`duty = tone_burst × PRF`). |
| `fus.tone_burst_s` | s | Tone-burst duration (TBD): carrier-on time per PRF period. Linked to `prf_duty`. |
| `fus.sonication_duration_s` | s | SD: duration of one sonication pulse (PRF-gated carrier window). |
| `fus.isi_off_s` | s | Silent time between sonications. ISI = SD + isi_off (start-to-start). |
| `fus.isi_freq_hz` | Hz | Computed: `1 / ISI`. UI lets you edit this and adjusts `isi_off_s`. |
| `fus.n_pulses` | — | Number of sonications in the train. Delivered by HS5 hardware burst mode. |
| `fus.amplitude_mv_pp` | mV pp | HS5 BNC voltage (peak-to-peak) — the input to your amplifier. |
| `pre_stim_s` / `post_stim_s` | s | Silent waits before/after the entire `n_pulses × ISI` train. |
| `ramp_s` | s | Optional linear ramp at the start/end of each sonication's SD window. Defaults to 0. |
| `repetitions` | — | Number of `(pre + train + post)` cycles to play. |
| `trigger_stimulation` | — | EXT 3 HIGH for the entire `n_pulses × ISI` train (LOW during pre/post). |

### fUS waveform nomenclature

- **Carrier (or central) frequency**: the sinusoidal acoustic frequency, usually 0.5–2 MHz.
- **PRF (Pulse Repetition Frequency)**: the rate at which the carrier is gated on/off *within* one sonication.
- **TBD (Tone-Burst Duration) / tone_burst_s**: how long the carrier is on within each PRF period. TBD = duty / PRF.
- **SD (Sonication Duration)**: how long one PRF-gated sonication lasts (typically 100 ms – 1 s).
- **ISI (Inter-Stimulus Interval)**: start-to-start time between sonications = SD + isi_off.
- **ISI frequency**: 1 / ISI.
- **Train**: `n_pulses` sonications played back-to-back via hardware burst.

### fUS UI interlocks

The GUI keeps these triples consistent automatically:

- **PRF ↔ duty ↔ tone_burst_s**: PRF stays fixed; editing duty recomputes tone_burst_s and vice versa.
- **SD ↔ isi_off ↔ isi_freq**: SD stays fixed; editing isi_off recomputes isi_freq and vice versa. Setting an isi_freq that is too high for the current SD (would require negative isi_off) is clamped to isi_off = 0, with the buffer-status indicator surfacing the issue.
- **n_pulses ↔ stim_time**: stim_time is read-only and equals `n_pulses × ISI`.

### fUS buffer-status indicator

Located inside the fUS panel. Shows:

- ✓ ok (green): the chosen carrier and ISI fit comfortably (≥ 50 samples / period).
- ⚠ amber: the sample rate had to be reduced from the fidelity target to fit the buffer. Still produces a correct waveform but with fewer samples per carrier period.
- ✗ error (red): the ISI cycle cannot fit in the 64 Mi-sample buffer even at the minimum fidelity floor (10 samples / carrier period). Reduce ISI (SD + isi_off) or lower the carrier. The Load button refuses such requests too.

The indicator also shows the maximum ISI cycle at the current carrier, for both the target (50 sps) and floor (10 sps) thresholds. n_pulses itself is unlimited by the buffer because the train is delivered by hardware burst.

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

The CSV log gained nine fUS-specific columns and one trigger column. Existing columns are unchanged.

Common: `timestamp_utc`, `timestamp_local`, `mode`, `shape`, `frequency_hz`, `carrier_hz`, `delta_f_hz`, `amplitude_ma`, `amplitude_ratio`, `pulse_width_s`, `sample_rate_hz`, `stim_time_s`, `total_time_s`, `pre_stim_s`, `post_stim_s`, `ramp_s`, `repetitions`, `device_1_serial`, `device_2_serial`, `outcome`, `error_message`, `duration_actual_s`.

fUS (blank in non-fUS rows): `fus_channel`, `fus_carrier_hz`, `fus_prf_hz`, `fus_prf_duty`, `fus_tone_burst_s`, `fus_sonication_duration_s`, `fus_isi_off_s`, `fus_n_pulses`, `fus_amplitude_mv_pp`.

Triggers: `trigger_out`, `trigger_in`, `trigger_stimulation`, `session_marker_emitted` (= true iff a session-prologue marker / primer was scheduled for that run).

## LED labels (UI)

Inline text next to each LED: grey disconnected; green steady ready; amber loaded, waiting for start; **green pulsing** running; blue done; red error.
