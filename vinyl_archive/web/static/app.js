"use strict";

const POLL_MS = 2000;

const STATE_LABEL = {
  recording: "Capturing",
  waiting_device: "Waiting for device",
  restarting: "Restarting",
  stopped: "Paused",
};

const STATUS_LABEL = {
  recording: "Recording now",
  buffered: "Buffered",
  saving: "Keeping…",
  saved: "Kept",
};

const STATUS_HINT = {
  recording: "Still being recorded.",
  buffered: "Temporary: the ring buffer reclaims it as it fills up. "
          + "Press Keep to store it permanently.",
  saving: "Being written to a permanent file.",
  saved: "Stored permanently until you delete it.",
};

const GAPS_HINT = "Some audio was missing when this was written: capture was "
  + "interrupted, or the beginning had already been reclaimed from the buffer. "
  + "The file plays through but has a discontinuity.";

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok && res.status !== 202) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function fmtTime(iso) {
  if (!iso) return "--";
  return new Date(iso).toLocaleString("en-US", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hour12: false,
  });
}

function fmtDuration(s) {
  if (s == null) return "--";
  const m = Math.floor(s / 60);
  return `${m}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
}

function fmtSize(bytes) {
  const mb = bytes / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
}

// -- status -----------------------------------------------------------------

function renderStatus(st) {
  const badge = $("capture-state");
  badge.textContent = STATE_LABEL[st.capture] || st.capture;
  badge.className = `badge ${st.capture}`;

  const level = st.level_dbfs;
  $("level-bar").style.width = `${Math.max(0, Math.min(100, (level + 60) / 60 * 100))}%`;
  $("level-text").textContent =
    level <= -100 ? "silent" : `${level.toFixed(1)} dBFS`;

  const buf = st.buffer;
  const fmt = st.format;
  $("buffer-text").textContent =
    `Buffer ${Math.round(buf.seconds / 60)} / ${Math.round(buf.capacity_seconds / 60)} min`;
  $("disk-text").textContent = `Disk free ${(buf.disk_free_mb / 1024).toFixed(1)} GB`
    + ` · ${buf.segments} segments · ${fmt.sample_rate / 1000} kHz`
    + ` ${fmt.bit_depth}-bit ${fmt.channels}ch · ${st.device}`;

  const capturing = st.capture !== "stopped";
  $("btn-rec").hidden = st.manual_recording || !capturing;
  $("btn-rec-stop").hidden = !st.manual_recording;
  $("btn-start").hidden = capturing;
  $("btn-stop").hidden = !capturing;
}

// -- history ------------------------------------------------------------------

const rows = new Map();  // key -> {el, parts, status}

const keyOf = (item) => `${item.type}:${item.id}`;

function createRow(item) {
  const el = document.createElement("div");
  el.className = "item";
  el.innerHTML = `
    <div class="row-main">
      <span class="title"></span>
      <span class="badge status"></span>
      <span class="meta"></span>
      <span class="warn" hidden title="${GAPS_HINT}">⚠ has gaps</span>
      <span class="actions"></span>
    </div>
    <audio class="player" controls preload="none"></audio>`;
  const parts = {
    title: el.querySelector(".title"),
    status: el.querySelector(".status"),
    meta: el.querySelector(".meta"),
    warn: el.querySelector(".warn"),
    actions: el.querySelector(".actions"),
    player: el.querySelector(".player"),
  };
  parts.player.src = item.audio_url;
  return { el, parts, status: null };
}

function button(label, cls, onClick) {
  const btn = document.createElement("button");
  btn.textContent = label;
  if (cls) btn.className = cls;
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      await onClick();
    } catch (e) {
      alert(e.message);
      btn.disabled = false;
    }
    refresh();
  };
  return btn;
}

function downloadLink(item) {
  const a = document.createElement("a");
  a.className = "dl";
  a.href = item.audio_url;
  a.download = "";
  a.textContent = item.permanent ? "Download FLAC" : "Download WAV";
  a.title = item.permanent
    ? "The stored lossless file."
    : "Streamed from the buffer as WAV. Keep it to get a FLAC file.";
  return a;
}

function renderActions(row, item) {
  const box = row.parts.actions;
  box.innerHTML = "";
  if (item.status === "buffered") {
    box.append(
      button("Keep", null, () => {
        const label = prompt("Name for this recording (optional):", "");
        if (label === null) throw new Error("cancelled");
        return api(`/api/sessions/${item.id}/save`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label }),
        });
      }),
      downloadLink(item),
      button("Dismiss", "secondary", () =>
        api(`/api/sessions/${item.id}`, { method: "DELETE" })),
    );
  } else if (item.status === "saved") {
    box.append(
      downloadLink(item),
      button("Rename", "secondary", () => {
        const label = prompt("Recording name:", item.label || "");
        if (label === null) throw new Error("cancelled");
        return api(`/api/recordings/${item.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label }),
        });
      }),
      button("Delete", "danger", () => {
        if (!confirm(`Delete "${item.label || fmtTime(item.start_utc)}" permanently?`))
          throw new Error("cancelled");
        return api(`/api/recordings/${item.id}`, { method: "DELETE" });
      }),
    );
  } else if (item.status === "recording") {
    box.append(downloadLink(item));
  }
}

function updateRow(row, item) {
  const p = row.parts;
  p.title.textContent = item.label || fmtTime(item.start_utc);
  p.status.textContent = STATUS_LABEL[item.status] || item.status;
  p.status.className = `badge status ${item.status}`;
  p.status.title = STATUS_HINT[item.status] || "";
  const bits = [fmtDuration(item.duration_s)];
  if (item.kind === "manual") bits.push("manual");
  else bits.push("auto backup");
  if (item.label) bits.push(fmtTime(item.start_utc));
  if (item.size_bytes) bits.push(fmtSize(item.size_bytes));
  p.meta.textContent = bits.join(" · ");
  p.warn.hidden = !item.has_gaps;
  if (row.status !== item.status) {
    renderActions(row, item);
    row.status = item.status;
  }
}

function renderHistory(items) {
  const box = $("history");
  if (items.length === 0) {
    if (!box.querySelector(".empty")) {
      rows.clear();
      box.innerHTML = `<div class="empty">Nothing captured yet. Play a record
        and it shows up here on its own.</div>`;
    }
    return;
  }
  const empty = box.querySelector(".empty");
  if (empty) empty.remove();

  const seen = new Set();
  // Rows are updated in place rather than rebuilt, so polling never
  // interrupts a player that is mid-playback.
  items.forEach((item, index) => {
    const key = keyOf(item);
    seen.add(key);
    let row = rows.get(key);
    if (!row) {
      row = createRow(item);
      rows.set(key, row);
    }
    updateRow(row, item);
    if (box.children[index] !== row.el) {
      box.insertBefore(row.el, box.children[index] || null);
    }
  });
  for (const [key, row] of rows) {
    if (!seen.has(key)) {
      row.el.remove();
      rows.delete(key);
    }
  }
}

// -- settings -----------------------------------------------------------------

async function loadSettings() {
  const settings = await api("/api/settings");
  const form = $("settings-form");
  for (const [key, value] of Object.entries(settings)) {
    const input = form.elements[key];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = value;
    else input.value = value;
  }
  if (settings.restart_required) {
    $("settings-msg").textContent = "Restart required for the audio format.";
  }
}

$("settings-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const patch = {};
  for (const el of ev.target.querySelectorAll("input, select")) {
    if (el.type === "checkbox") patch[el.name] = el.checked;
    else if (el.type === "text") patch[el.name] = el.value;
    else patch[el.name] = Number(el.value);
  }
  const msg = $("settings-msg");
  try {
    const result = await api("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    msg.textContent = result.restart_required
      ? "Saved. Restart the service to apply the audio format."
      : "Saved.";
  } catch (e) {
    msg.textContent = `Error: ${e.message}`;
  }
};

// -- controls -----------------------------------------------------------------

$("btn-rec").onclick = () =>
  api("/api/record/start", { method: "POST" }).then(refresh, (e) => alert(e.message));
$("btn-rec-stop").onclick = () =>
  api("/api/record/stop", { method: "POST" }).then(refresh, (e) => alert(e.message));
$("btn-start").onclick = () => api("/api/capture/start", { method: "POST" }).then(refresh);
$("btn-stop").onclick = () => {
  if (confirm("Pause continuous capture? Nothing is backed up while paused.")) {
    api("/api/capture/stop", { method: "POST" }).then(refresh);
  }
};

// -- polling ------------------------------------------------------------------

async function refresh() {
  try {
    const [status, history] = await Promise.all([
      api("/api/status"), api("/api/history"),
    ]);
    renderStatus(status);
    renderHistory(history);
  } catch (e) {
    $("capture-state").textContent = "Connection error";
    $("capture-state").className = "badge stopped";
  }
}

loadSettings().catch(() => {});
refresh();
setInterval(refresh, POLL_MS);
