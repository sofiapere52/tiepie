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
      amplitude_a: num("ti_amplitude_a", 0.002),
      carrier_hz: carrier,
      delta_f_hz: isTbs ? 50 : num("delta_f_hz", 10),
      amplitude_ratio: (byId("amplitude_ratio") || {}).value || "1:1",
      frequency_hz: carrier,
    };
    if (isTbs) params.tbs_freq_hz = num("tbs_freq_hz", 5);
    return { params, preview_max_points: 8000 };
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
      amplitude_a: num(prefix + "_amplitude_a", 0.001),
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
    preview_max_points: 8000,
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
  const ts = new Date().toISOString().slice(11, 23);
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

  const opts = (title, stroke) => ({
    width: w,
    height: 320,
    title,
    cursor: { drag: { x: true, y: false } },
    scales: {
      x: { time: false },
      y: { range: [-yMax * 1.08, yMax * 1.08] },
    },
    series: [{}, { stroke, width: 1, label: "A" }],
    axes: [
      { stroke: "#8b949e", grid: { stroke: "#30363d22" }, label: "Time (s)", size: 32 },
      { stroke: "#8b949e", grid: { stroke: "#30363d22" }, label: "Amplitude (A)", size: 52 },
    ],
    legend: { show: false },
  });

  const u1 = byId("u-ch1");
  const u2 = byId("u-ch2");
  const us = byId("u-sum");
  if (!u1 || !u2 || !us) return;
  u1.innerHTML = "";
  u2.innerHTML = "";
  us.innerHTML = "";
  us.classList.toggle("hidden", !out.show_sum);

  previewCharts.push(new uPlot(opts("Channel 1 (A)", "#58a6ff"), [t, out.ch1], u1));
  previewCharts.push(new uPlot(opts("Channel 2 (A)", "#f0883e"), [t, out.ch2], u2));
  if (out.show_sum && out.sum_v) {
    previewCharts.push(new uPlot(opts("TI sum Ch1+Ch2 (A)", "#a371f7"), [t, out.sum_v], us));
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
  ["pre_stim_s", "stim_time_s", "post_stim_s"].forEach((id) => {
    const n = byId(id);
    if (n) n.addEventListener("input", updateTotalTimeDisplay);
  });
  updateTotalTimeDisplay();
  const modeEl = byId("mode");
  if (modeEl) modeEl.addEventListener("change", toggleModePanels);
  const tiShapeEl = byId("ti_shape");
  if (tiShapeEl) tiShapeEl.addEventListener("change", toggleTbsFields);
  toggleModePanels();
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
      const body = collectParams();
      const out = await api("/waveform/preview", { method: "POST", body: JSON.stringify(body) });
      if (!out || !out.t_seconds || !out.ch1) {
        appendLogLine("Preview failed: server returned incomplete data");
        return;
      }
      drawPreviewUplot(out);
    } catch (e) { alert(e.message); }
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
