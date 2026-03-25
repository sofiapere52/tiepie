const $ = (id) => document.getElementById(id);

function collectParams() {
  const mode = $("mode").value;
  const pulse = $("pulse_width_s").value.trim();
  const base = {
    mode,
    shape: $("shape").value,
    amplitude_v: parseFloat($("amplitude_v").value),
    pulse_width_s: pulse === "" ? null : parseFloat(pulse),
    total_time_s: parseFloat($("total_time_s").value),
    pre_stim_s: parseFloat($("pre_stim_s").value),
    post_stim_s: parseFloat($("post_stim_s").value),
    sample_rate_hz: parseFloat($("sample_rate_hz").value),
    repetitions: parseInt($("repetitions").value, 10),
  };
  if (mode === "standard") {
    base.frequency_hz = parseFloat($("frequency_hz").value);
  } else {
    base.frequency_hz = parseFloat($("carrier_hz").value);
    base.carrier_hz = parseFloat($("carrier_hz").value);
    base.delta_f_hz = parseFloat($("delta_f_hz").value);
    base.amplitude_ratio = $("amplitude_ratio").value.trim();
  }
  return { params: base, preview_max_points: 2000 };
}

function ledLabel(state) {
  const map = {
    disconnected: "Grey — not connected",
    ready: "Green (steady) — ready / idle",
    armed: "Amber — armed, waiting for start",
    running: "Green (pulsing) — stimulation running",
    done: "Blue — finished",
    error: "Red — fault / error",
  };
  return map[state] || state;
}

function renderDevices(payload) {
  const el = $("devices");
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
  $("last-error").textContent = payload.last_error || "";
  $("mock-badge").classList.toggle("hidden", !payload.mock);
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(proto + "//" + location.host + "/ws/status");
  ws.onmessage = (ev) => {
    try {
      renderDevices(JSON.parse(ev.data));
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

function drawPreview(ch1, ch2) {
  const c = $("canvas");
  const ctx = c.getContext("2d");
  const w = c.width;
  const h = c.height;
  ctx.fillStyle = "#0f1419";
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = "#30363d";
  ctx.beginPath();
  ctx.moveTo(0, h / 2);
  ctx.lineTo(w, h / 2);
  ctx.stroke();
  const n = Math.max(ch1.length, ch2.length);
  if (n < 2) return;
  function plot(arr, color) {
    ctx.beginPath();
    ctx.strokeStyle = color;
    for (let i = 0; i < arr.length; i++) {
      const x = (i / (arr.length - 1)) * w;
      const y = h / 2 - (arr[i] * (h * 0.42));
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  plot(ch1, "#58a6ff");
  plot(ch2, "#f0883e");
}

function toggleTiFields() {
  const ti = $("mode").value === "ti";
  ["lbl-carrier", "lbl-df", "lbl-ratio"].forEach((id) => $(id).classList.toggle("hidden", !ti));
  $("lbl-freq").classList.toggle("hidden", ti);
}

$("mode").addEventListener("change", toggleTiFields);
toggleTiFields();

$("btn-connect").onclick = async () => {
  try {
    await api("/connect", { method: "POST" });
  } catch (e) {
    alert(e.message);
  }
};

$("btn-preview").onclick = async () => {
  try {
    const body = collectParams();
    const out = await api("/waveform/preview", { method: "POST", body: JSON.stringify(body) });
    drawPreview(out.ch1, out.ch2);
  } catch (e) {
    alert(e.message);
  }
};

$("btn-arm").onclick = async () => {
  try {
    const { params } = collectParams();
    await api("/arm", { method: "POST", body: JSON.stringify(params) });
  } catch (e) {
    alert(e.message);
  }
};

$("btn-start").onclick = async () => {
  try {
    await api("/start", { method: "POST" });
  } catch (e) {
    alert(e.message);
  }
};

$("btn-stop").onclick = async () => {
  try {
    await api("/stop", { method: "POST" });
  } catch (e) {
    alert(e.message);
  }
};

connectWS();
api("/health").then(renderDevices).catch(() => {});
