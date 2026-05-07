const byId = (id) => document.getElementById(id);

let previewCharts = [];

function stateFromPayload(p) {
  if (p && p.type === "snapshot") {
    const { type, ...rest } = p;
    return rest;
  }
  return p;
}

function collectParams() {
  const modeEl = byId("mode");
  if (!modeEl) throw new Error("UI not ready");
  const mode = modeEl.value;
  const num = (id, def) => {
    const el = byId(id);
    if (!el) return def;
    const v = parseFloat(el.value);
    return Number.isFinite(v) ? v : def;
  };
  const intv = (id, def) => {
    const el = byId(id);
    if (!el) return def;
    const v = parseInt(el.value, 10);
    return Number.isFinite(v) ? v : def;
  };
  const common = {
    mode,
    stim_time_s: num("stim_time_s", 0.1),
    pre_stim_s: num("pre_stim_s", 0),
    post_stim_s: num("post_stim_s", 0),
    ramp_s: num("ramp_s", 0),
    sample_rate_hz: 500000,
    repetitions: intv("repetitions", 1),
    trigger_out: byId("trigger_out") ? byId("trigger_out").checked : false,
    trigger_in: byId("trigger_in") ? byId("trigger_in").checked : false,
  };
  if (mode === "ti") {
    const carrier = num("carrier_hz", 2000);
    const tiShape = (byId("ti_shape") || {}).value || "sine";
    const isTbs = tiShape === "tbs";
    const params = {
      ...common,
      shape: tiShape,
      amplitude_ma: num("ti_amplitude_ma", 2),
      carrier_hz: carrier,
      delta_f_hz: isTbs ? 50 : num("delta_f_hz", 10),
      amplitude_ratio: (byId("amplitude_ratio") || {}).value || "1:1",
      frequency_hz: carrier,
    };
    if (isTbs) params.tbs_freq_hz = num("tbs_freq_hz", 5);
    return { params, preview_max_points: 50000 };
  }
  const ch = (prefix) => {
    const enabledEl = byId(prefix + "_enabled");
    const enabled = enabledEl ? enabledEl.checked : true;
    const pulseEl = byId(prefix + "_pulse_width_s");
    const pulse = pulseEl && pulseEl.value.trim() !== "" ? parseFloat(pulseEl.value) : null;
    return {
      enabled,
      shape: (byId(prefix + "_shape") || {}).value || "sine",
      frequency_hz: num(prefix + "_frequency_hz", 100),
      amplitude_ma: num(prefix + "_amplitude_ma", 1),
      pulse_width_s: pulse,
    };
  };
  return {
    params: {
      ...common,
      shape: "sine",
      amplitude_a: null,
      frequency_hz: null,
      carrier_hz: null,
      delta_f_hz: null,
      amplitude_ratio: null,
      ch1: ch("ch1"),
      ch2: ch("ch2"),
    },
    preview_max_points: 50000,
  };
}

function updateTotalTimeDisplay() {
  const preEl = byId("pre_stim_s");
  const stimEl = byId("stim_time_s");
  const postEl = byId("post_stim_s");
  const outEl = byId("total_time_display");
  if (!preEl || !stimEl || !postEl || !outEl) return;
  const pre = parseFloat(preEl.value) || 0;
  const stim = parseFloat(stimEl.value) || 0;
  const post = parseFloat(postEl.value) || 0;
  outEl.textContent = Number((pre + stim + post).toFixed(9)).toString();
}

// Mirror of `choose_hardware_sample_rate` (waveform.py) for live UI feedback.
// Keep these constants in sync with the Python module.
const HS5_BUFFER_MAX = 67108864;
const HS5_MAX_SR = 240_000_000;
const HS5_TARGET_SAMPLES_PER_CYCLE = 50;
const HS5_MIN_SAMPLES_PER_CYCLE = 10;

function maxSignalFrequencyFromUI(params) {
  const freqs = [];
  if (params.mode === "ti") {
    if (params.carrier_hz && params.carrier_hz > 0) {
      freqs.push(params.carrier_hz);
      if (params.delta_f_hz != null) {
        freqs.push(Math.abs(params.carrier_hz + params.delta_f_hz));
      }
    }
  } else {
    if (params.ch1 && params.ch1.enabled && params.ch1.frequency_hz > 0) {
      freqs.push(params.ch1.frequency_hz);
    }
    if (params.ch2 && params.ch2.enabled && params.ch2.frequency_hz > 0) {
      freqs.push(params.ch2.frequency_hz);
    }
  }
  return freqs.length ? Math.max.apply(null, freqs) : 1.0;
}

function chooseHardwareSampleRate(params) {
  const fmax = maxSignalFrequencyFromUI(params);
  const total = (params.pre_stim_s || 0) + (params.stim_time_s || 0) + (params.post_stim_s || 0);
  let ideal = Math.min(HS5_MAX_SR, fmax * HS5_TARGET_SAMPLES_PER_CYCLE);
  let sr = ideal;
  let note = "";
  if (total > 0) {
    const cap = HS5_BUFFER_MAX / total;
    if (cap < ideal) {
      sr = cap;
      const sps = sr / fmax;
      note = `reduced to fit AWG buffer · ${sps.toFixed(1)} samples/period at ${fmax.toLocaleString()} Hz`;
    }
  }
  const sps = fmax > 0 ? sr / fmax : Infinity;
  if (sps < HS5_MIN_SAMPLES_PER_CYCLE) {
    const maxT = HS5_BUFFER_MAX / (HS5_MIN_SAMPLES_PER_CYCLE * fmax);
    return {
      ok: false,
      message: `Total time too long for ${fmax.toLocaleString()} Hz signal — buffer would hold ${sps.toFixed(2)} samples/period (min ${HS5_MIN_SAMPLES_PER_CYCLE}). Reduce total time below ${maxT.toFixed(1)} s, lower the frequency, or split into repetitions.`,
    };
  }
  return { ok: true, sr, note };
}

function updateSampleRateDisplay() {
  const el = byId("sample_rate_display");
  const hintEl = byId("sample_rate_hint");
  if (!el) return;
  let params;
  try {
    params = collectParams().params;
  } catch (_) {
    el.textContent = "—";
    if (hintEl) hintEl.textContent = "";
    return;
  }
  const r = chooseHardwareSampleRate(params);
  if (!r.ok) {
    el.textContent = "—";
    if (hintEl) hintEl.textContent = r.message;
    return;
  }
  el.textContent = Math.round(r.sr).toLocaleString();
  if (hintEl) hintEl.textContent = r.note ? "(" + r.note + ")" : "";
}

function ledLabel(state) {
  const map = {
    disconnected: "Grey — not connected",
    ready: "Green (steady) — ready / idle",
    armed: "Amber — loaded, waiting for start",
    running: "Green (pulsing) — stimulation running",
    done: "Blue — finished",
    error: "Red — fault / error",
  };
  return map[state] || state;
}

function applySingleDeviceRestrictions(payload) {
  const devs = payload.devices || [];
  const allDisconnected = devs.every((d) => d.state === "disconnected");
  if (allDisconnected) return;
  const singleDevice =
    devs.length < 2 || (devs.length >= 2 && devs[1].state === "disconnected");
  const modeEl = byId("mode");
  if (modeEl) {
    const tiOpt = modeEl.querySelector('option[value="ti"]');
    if (tiOpt) tiOpt.disabled = singleDevice;
    if (singleDevice && modeEl.value === "ti") {
      modeEl.value = "control";
      toggleModePanels();
    }
  }
  const ch2Enabled = byId("ch2_enabled");
  if (ch2Enabled) {
    if (singleDevice) {
      ch2Enabled.checked = false;
      ch2Enabled.disabled = true;
    } else {
      ch2Enabled.disabled = false;
    }
  }
}

function renderDevices(payload) {
  payload = payload || {};
  const el = byId("devices");
  if (!el) return;
  el.innerHTML = "";
  (payload.devices || []).forEach((d) => {
    const row = document.createElement("div");
    row.className = "device-row";
    const led = document.createElement("span");
    led.className = "led " + (d.state || "disconnected");
    const lab = document.createElement("span");
    lab.className = "led-label";
    lab.textContent = ledLabel(d.state);
    const sn = document.createElement("span");
    sn.className = "serial";
    sn.textContent = "Ch " + d.slot + (d.serial ? " · " + d.serial : "");
    row.append(led, lab, sn);
    if (d.detail) {
      const det = document.createElement("span");
      det.className = "led-label";
      det.textContent = "(" + d.detail + ")";
      row.append(det);
    }
    el.append(row);
  });
  const errEl = byId("last-error");
  if (errEl) errEl.textContent = payload.last_error || "";
  const mockEl = byId("mock-badge");
  if (mockEl) mockEl.classList.toggle("hidden", !payload.mock);
  applySingleDeviceRestrictions(payload);
}

function appendLogLine(message) {
  const pre = byId("log-panel");
  if (!pre) return;
  // Local-time timestamp (HH:MM:SS.mmm), so logs match the wall clock of
  // the operator's locale (e.g. Europe/Zurich) rather than UTC.
  const d = new Date();
  const pad = (n, w) => String(n).padStart(w, "0");
  const ts = `${pad(d.getHours(), 2)}:${pad(d.getMinutes(), 2)}:${pad(d.getSeconds(), 2)}.${pad(d.getMilliseconds(), 3)}`;
  pre.textContent += `[${ts}] ${message}\n`;
  pre.scrollTop = pre.scrollHeight;
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(proto + "//" + location.host + "/ws/status");
  ws.onmessage = (ev) => {
    try {
      const o = JSON.parse(ev.data);
      if (o.type === "log") {
        appendLogLine(o.message);
        return;
      }
      if (o.type === "snapshot") {
        const { type, ...rest } = o;
        renderDevices(rest);
      }
    } catch (_) {}
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
}

async function api(path, opt) {
  const r = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json", ...(opt && opt.headers) },
    ...opt,
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || r.statusText);
  }
  return r.json().catch(() => ({}));
}

function destroyPreviewCharts() {
  previewCharts.forEach((c) => {
    try { c.destroy(); } catch (_) {}
  });
  previewCharts = [];
}

// ---- Preview zoom/pan + on-demand refetch ----------------------------------
//
// The preview chart faces a fundamental tension: showing every requested
// repetition (which may be hundreds of seconds long) while also letting the
// user zoom in to inspect the carrier. We solve it by keeping a small
// "overview" payload on the client (envelope of the full signal, all reps
// tiled) and re-fetching a denser, carrier-resolved payload from the server
// for whatever sub-window the user has zoomed into. The handover is
// debounced so wheel-spam doesn't flood the server, and outstanding fetches
// are aborted when superseded.
//
// `previewState` records the "true" full-extent boundaries so we can detect
// when the user has zoomed back out to overview (and skip a redundant
// refetch in that case).

let previewState = {
  totalT0: 0,
  totalT1: 0,
  inOverview: false,
};
let zoomDebounceTimer = null;
let zoomFetchAbort = null;
let isSyncingZoom = false;

const PREVIEW_DEBOUNCE_MS = 250;
const PREVIEW_ZOOM_FULL_EPS = 0.001; // treat ≥99.9 % of full as "overview"

/** Wheel over a preview plot: handled in **document capture** with
 * `{ passive: false }` so we run *before* the browser applies page scroll or
 * Ctrl+pinch page zoom. uPlot-internal wheel handlers are unreliable because
 * the native target is often a canvas below the cursor overlay.
 */
function onPreviewWheelCapture(e) {
  if (!previewCharts.length) return;
  const wrap =
    e.target && e.target.closest && e.target.closest(".uplot-wrap");
  if (!wrap) return;
  const sec = byId("preview-section");
  if (!sec || !sec.contains(wrap)) return;

  e.preventDefault();
  e.stopPropagation();
  try {
    e.stopImmediatePropagation();
  } catch (_) {}

  const u = previewCharts[0];
  if (!u.scales || !u.scales.x) return;
  const over = u.over;
  const xMin = u.scales.x.min;
  const xMax = u.scales.x.max;
  if (xMin == null || xMax == null) return;
  const xRange = xMax - xMin;
  if (xRange <= 0) return;

  const rect = over.getBoundingClientRect();
  const wpx = rect.width || 1;
  let lx = e.clientX - rect.left;
  lx = Math.min(Math.max(0, lx), wpx);
  const cursorX = xMin + (lx / wpx) * xRange;

  let dy = e.deltaY;
  if (e.deltaMode === 1) dy *= 16;
  if (e.deltaMode === 2) dy *= wpx;
  const step = Math.min(2, Math.abs(dy) / 100 + 0.15);
  const factor = dy < 0 ? Math.pow(0.92, step) : Math.pow(1 / 0.92, step);

  const newRange = xRange * factor;
  const leftPct = (cursorX - xMin) / xRange;
  const newMin = cursorX - leftPct * newRange;
  const newMax = newMin + newRange;
  u.setScale("x", { min: newMin, max: newMax });
}

function wirePreviewWheelCapture() {
  document.addEventListener("wheel", onPreviewWheelCapture, {
    capture: true,
    passive: false,
  });
}

async function fetchPreview(zoomWindow /* [t0,t1] | null */) {
  if (zoomFetchAbort) {
    try { zoomFetchAbort.abort(); } catch (_) {}
  }
  zoomFetchAbort = new AbortController();
  const body = collectParams();
  if (zoomWindow) {
    body.t_start_s = zoomWindow[0];
    body.t_end_s = zoomWindow[1];
  }
  const r = await fetch("/api/waveform/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: zoomFetchAbort.signal,
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(text || r.statusText);
  }
  return await r.json();
}

function isFullRange(xMin, xMax) {
  const total = previewState.totalT1 - previewState.totalT0;
  if (total <= 0) return true;
  const visible = xMax - xMin;
  return (
    visible >= total * (1 - PREVIEW_ZOOM_FULL_EPS) &&
    xMin <= previewState.totalT0 + total * PREVIEW_ZOOM_FULL_EPS &&
    xMax >= previewState.totalT1 - total * PREVIEW_ZOOM_FULL_EPS
  );
}

function scheduleZoomRefetch(xMin, xMax) {
  if (zoomDebounceTimer) clearTimeout(zoomDebounceTimer);
  zoomDebounceTimer = setTimeout(async () => {
    if (!previewCharts.length) return;
    try {
      if (isFullRange(xMin, xMax)) {
        // User zoomed all the way back out: rebuild the tiled overview
        // (rather than asking the server for a window that covers
        // everything, which would be wasteful).
        if (!previewState.inOverview) {
          const out = await fetchPreview(null);
          drawPreviewUplot(out);
        }
        return;
      }
      const out = await fetchPreview([xMin, xMax]);
      // A windowed response carries data ONLY for [xMin, xMax]; update
      // existing charts in-place so the user's zoom level is preserved.
      updateChartsData(out, /*preserveScales=*/ true);
      previewState.inOverview = false;
    } catch (e) {
      if (e && e.name !== "AbortError") {
        appendLogLine("Preview refresh failed: " + (e.message || e));
      }
    }
  }, PREVIEW_DEBOUNCE_MS);
}

function updateChartsData(out, preserveScales) {
  if (!previewCharts.length || !out || !out.t_seconds) return;
  const t = out.t_seconds;
  const reset = !preserveScales;
  // Suppress recursive setScale → broadcast → setScale loops when uPlot
  // auto-adjusts the scale during setData.
  isSyncingZoom = true;
  try {
    if (previewCharts[0]) previewCharts[0].setData([t, out.ch1], reset);
    if (previewCharts[1]) previewCharts[1].setData([t, out.ch2], reset);
    if (previewCharts[2] && out.sum_v) {
      previewCharts[2].setData([t, out.sum_v], reset);
    }
  } finally {
    isSyncingZoom = false;
  }
}

function broadcastZoomToOtherCharts(sourceChart, xMin, xMax) {
  if (isSyncingZoom) return;
  isSyncingZoom = true;
  try {
    previewCharts.forEach((c) => {
      if (c !== sourceChart) c.setScale("x", { min: xMin, max: xMax });
    });
  } finally {
    isSyncingZoom = false;
  }
}

// uPlot plugin: scroll-wheel zoom under cursor, click-drag pan,
// double-click → return to overview. Also wires up the cross-chart sync
// and the debounced refetch.
function makeZoomPanPlugin() {
  return {
    hooks: {
      ready: [(u) => {
        const over = u.over;
        const root = u.root;
        let pan = null;

        const onDown = (e) => {
          if (e.button !== 0) return;
          if (!u.scales || !u.scales.x) return;
          if (!root.contains(e.target)) return;
          pan = {
            startX: e.clientX,
            xMin: u.scales.x.min,
            xMax: u.scales.x.max,
            width: over.clientWidth || root.clientWidth || 1,
          };
          over.style.cursor = "grabbing";
        };

        const onMove = (e) => {
          if (!pan) return;
          const dx = e.clientX - pan.startX;
          const range = pan.xMax - pan.xMin;
          const dxVal = (dx / pan.width) * range;
          u.setScale("x", { min: pan.xMin - dxVal, max: pan.xMax - dxVal });
        };

        const onUp = () => {
          pan = null;
          over.style.cursor = "";
        };

        const onDbl = () => {
          // Reset = re-render the full overview (server-side rebuild so the
          // tiled all-reps view is restored, even if we were last in
          // windowed mode).
          fetchPreview(null)
            .then((out) => { if (out) drawPreviewUplot(out); })
            .catch((e) => {
              if (e && e.name !== "AbortError") {
                appendLogLine("Preview reset failed: " + (e.message || e));
              }
            });
        };

        root.addEventListener("mousedown", onDown);
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onUp);
        root.addEventListener("dblclick", onDbl);

        u._zoomPanCleanup = () => {
          root.removeEventListener("mousedown", onDown);
          window.removeEventListener("mousemove", onMove);
          window.removeEventListener("mouseup", onUp);
          root.removeEventListener("dblclick", onDbl);
        };
      }],
      destroy: [(u) => { if (u._zoomPanCleanup) u._zoomPanCleanup(); }],
      setScale: [(u, key) => {
        if (key !== "x") return;
        if (isSyncingZoom) return; // ignore programmatic broadcasts
        const xMin = u.scales.x.min;
        const xMax = u.scales.x.max;
        if (xMin == null || xMax == null) return;
        broadcastZoomToOtherCharts(u, xMin, xMax);
        scheduleZoomRefetch(xMin, xMax);
      }],
    },
  };
}

function chartWidth() {
  const wrap = byId("u-ch1");
  if (wrap) return Math.max(300, wrap.clientWidth - 2);
  return Math.max(300, Math.min(920, window.innerWidth - 48));
}

function drawPreviewUplot(out) {
  destroyPreviewCharts();
  if (typeof uPlot === "undefined") {
    appendLogLine("uPlot not loaded; hard-refresh (Ctrl+F5) or check network.");
    return;
  }
  const yMax = Math.max(out.y_max, 1e-9);
  const t = out.t_seconds;
  const w = chartWidth();
  // Remember the full extent so the zoom-refetch logic can detect when the
  // user is back at the overview and avoid pointless server round-trips.
  previewState.totalT0 = t && t.length ? t[0] : 0;
  previewState.totalT1 = t && t.length ? t[t.length - 1] : 0;
  previewState.inOverview = true;

  const opts = (title, stroke) => ({
    width: w,
    height: 320,
    title,
    // Disable uPlot's built-in selection-drag-to-zoom; the zoom/pan plugin
    // below provides scroll-zoom (under cursor), click-drag pan, and
    // double-click reset, which is the interaction the user expects.
    cursor: { drag: { x: false, y: false } },
    scales: {
      x: { time: false },
      y: { range: [-yMax * 1.08, yMax * 1.08] },
    },
    series: [{}, { stroke, width: 1, label: "mA" }],
    axes: [
      { stroke: "#8b949e", grid: { stroke: "#30363d22" }, label: "Time (s)", size: 32 },
      { stroke: "#8b949e", grid: { stroke: "#30363d22" }, label: "Amplitude (mA)", size: 60 },
    ],
    legend: { show: false },
    plugins: [makeZoomPanPlugin()],
  });

  const u1 = byId("u-ch1");
  const u2 = byId("u-ch2");
  const us = byId("u-sum");
  if (!u1 || !u2 || !us) return;
  u1.innerHTML = "";
  u2.innerHTML = "";
  us.innerHTML = "";
  us.classList.toggle("hidden", !out.show_sum);

  previewCharts.push(new uPlot(opts("Channel 1 (mA)", "#58a6ff"), [t, out.ch1], u1));
  previewCharts.push(new uPlot(opts("Channel 2 (mA)", "#f0883e"), [t, out.ch2], u2));
  if (out.show_sum && out.sum_v) {
    previewCharts.push(new uPlot(opts("TI sum Ch1+Ch2 (mA)", "#a371f7"), [t, out.sum_v], us));
  }
}

function toggleModePanels() {
  const ti = byId("mode") && byId("mode").value === "ti";
  const pTi = byId("panel-ti");
  const pCtrl = byId("panel-control");
  if (pTi) pTi.classList.toggle("hidden", !ti);
  if (pCtrl) pCtrl.classList.toggle("hidden", ti);
  if (ti) toggleTbsFields();
}

function toggleTbsFields() {
  const tiShape = byId("ti_shape");
  const isTbs = tiShape && tiShape.value === "tbs";
  const dfLabel = byId("lbl-delta-f");
  const tbsLabel = byId("lbl-tbs-freq");
  if (dfLabel) dfLabel.classList.toggle("hidden", isTbs);
  if (tbsLabel) tbsLabel.classList.toggle("hidden", !isTbs);
}

function wireInputs() {
  // Re-render the derived displays on any parameter edit. Total-time depends
  // only on the three duration fields; the hardware-sample-rate display also
  // depends on mode/carrier/delta_f/per-channel frequency, so we listen on
  // every form input.
  ["pre_stim_s", "stim_time_s", "post_stim_s"].forEach((id) => {
    const n = byId(id);
    if (n) n.addEventListener("input", updateTotalTimeDisplay);
  });
  updateTotalTimeDisplay();
  const refreshAllDerived = () => {
    updateTotalTimeDisplay();
    updateSampleRateDisplay();
  };
  document
    .querySelectorAll(".form-grid input, .form-grid select")
    .forEach((el) => {
      el.addEventListener("input", refreshAllDerived);
      el.addEventListener("change", refreshAllDerived);
    });
  const modeEl = byId("mode");
  if (modeEl) modeEl.addEventListener("change", toggleModePanels);
  const tiShapeEl = byId("ti_shape");
  if (tiShapeEl) tiShapeEl.addEventListener("change", toggleTbsFields);
  toggleModePanels();
  updateSampleRateDisplay();
}

function wireButtons() {
  const btn = (id, fn) => {
    const el = byId(id);
    if (el) el.onclick = fn;
  };
  btn("btn-connect", async () => {
    try { await api("/connect", { method: "POST" }); }
    catch (e) { alert(e.message); }
  });
  btn("btn-preview", async () => {
    try {
      const out = await fetchPreview(null);
      if (!out || !out.t_seconds || !out.ch1) {
        appendLogLine("Preview failed: server returned incomplete data");
        return;
      }
      drawPreviewUplot(out);
      if (out.hw_sample_rate_hz) {
        const sr = Math.round(out.hw_sample_rate_hz).toLocaleString();
        let msg = `Hardware sample rate: ${sr} Hz`;
        if (out.hw_sample_rate_note) msg += ` (${out.hw_sample_rate_note})`;
        appendLogLine(msg);
        const el = byId("sample_rate_display");
        const hintEl = byId("sample_rate_hint");
        if (el) el.textContent = sr;
        if (hintEl) hintEl.textContent = out.hw_sample_rate_note ? "(" + out.hw_sample_rate_note + ")" : "";
      }
      if (out.preview_sample_rate_hz != null && out.hw_sample_rate_hz != null) {
        const p = Math.round(out.preview_sample_rate_hz).toLocaleString();
        const h = Math.round(out.hw_sample_rate_hz).toLocaleString();
        if (Math.abs(out.preview_sample_rate_hz - out.hw_sample_rate_hz) < 0.5) {
          appendLogLine(`Preview sampled at ${p} Hz (same as hardware — what you see is what will play).`);
        } else {
          appendLogLine(
            `Preview sampled at ${p} Hz (capped below hardware ${h} Hz for this cycle length in the overview; zoom in for the full hardware rate).`
          );
        }
      }
      if (typeof out.n_cycles_shown === "number" && out.n_cycles_shown < out.n_cycles_requested) {
        appendLogLine(
          `Preview shows ${out.n_cycles_shown} of ${out.n_cycles_requested} repetitions ` +
          `(remaining repetitions are identical and were collapsed to keep memory bounded).`
        );
      }
      appendLogLine(
        "Preview ready — scroll to zoom, drag to pan, double-click to reset. " +
        "All three plots stay in sync. After zooming in, give it a second or two: " +
        "the waveform refetches at the hardware sample rate so detail catches up."
      );
    } catch (e) {
      if (e && e.name !== "AbortError") alert(e.message);
    }
  });
  btn("btn-arm", async () => {
    try {
      const { params } = collectParams();
      await api("/arm", { method: "POST", body: JSON.stringify(params) });
    } catch (e) { alert(e.message); }
  });
  btn("btn-start", async () => {
    try { await api("/start", { method: "POST" }); }
    catch (e) { alert(e.message); }
  });
  btn("btn-stop", async () => {
    try { await api("/stop", { method: "POST" }); }
    catch (e) { alert(e.message); }
  });
}

function boot() {
  try {
    wirePreviewWheelCapture();
    wireInputs();
    wireButtons();
    connectWS();
    api("/health")
      .then((p) => renderDevices(stateFromPayload(p)))
      .catch(() => {});
  } catch (e) {
    console.error(e);
    appendLogLine("Startup error: " + (e && e.message ? e.message : String(e)));
  }
}

window.addEventListener("error", (e) => {
  appendLogLine("JS error: " + (e.message || "unknown"));
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
