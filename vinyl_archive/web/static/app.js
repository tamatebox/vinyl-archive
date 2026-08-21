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
  recording: "Still being recorded. It becomes playable and downloadable "
           + "as soon as it ends.",
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

// A session that has not ended has no end sample to measure, so its length
// comes from the clock. Nothing is gated out while a session is open, so
// elapsed wall-clock and recorded audio are the same span.
const elapsedSince = (iso) => Math.max(0, (Date.now() - Date.parse(iso)) / 1000);

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

// -- playback level ----------------------------------------------------------
// Output-side only: the player still fetches the untouched WAV/FLAC bytes, so
// nothing here can affect what gets archived. Line-in from a phono stage peaks
// well below full scale, which is why playback sounds quiet next to
// loudness-normalised sources — and why every entry arrives with the gain that
// fixes it (gain_db, from levels measured while it was captured). That gain is
// per entry, so each player gets its own gain node; the trim slider is a
// manual offset on top of all of them.

// The trim key is renamed on purpose: a boost saved back when it was the only
// control would now stack on top of auto level and double the correction.
const GAIN_KEY = "playbackTrimDb";
const AUTO_KEY = "playbackAutoLevel";

let audioCtx = null;
let webAudioBroken = false;
const gains = new Map();  // player element -> its GainNode

const trimDb = () => Number($("gain").value) || 0;

function playerGainDb(el) {
  const auto = $("auto-level").checked ? Number(el.dataset.gainDb) || 0 : 0;
  return auto + trimDb();
}

function applyPlayerGain(el) {
  const node = gains.get(el);
  if (node) node.gain.value = 10 ** (playerGainDb(el) / 20);
}

function applyGain() {
  const db = trimDb();
  $("gain-text").textContent = `${db > 0 ? "+" : ""}${db} dB`;
  try {
    localStorage.setItem(GAIN_KEY, String(db));
    localStorage.setItem(AUTO_KEY, $("auto-level").checked ? "1" : "0");
  } catch {}
  for (const el of gains.keys()) applyPlayerGain(el);
}

// Built on first play, not up front: a context created without a user gesture
// starts suspended, and an element routed through a suspended context is mute.
// createMediaElementSource is one-shot per element and cannot be undone, so
// every player goes through its own gain node even at 0 dB.
function routePlayer(el) {
  if (webAudioBroken || gains.has(el)) return;
  try {
    if (!audioCtx) audioCtx = new AudioContext();
    const node = audioCtx.createGain();
    node.connect(audioCtx.destination);
    audioCtx.createMediaElementSource(el).connect(node);
    gains.set(el, node);
    applyPlayerGain(el);
  } catch (e) {
    // No Web Audio: leave the element on its native output rather than
    // risking silence. Playback keeps working, the controls do nothing.
    webAudioBroken = true;
    $("playback-bar").hidden = true;
  }
}

function onPlay(ev) {
  routePlayer(ev.target);
  if (audioCtx) audioCtx.resume().catch(() => {});
}

$("gain").oninput = applyGain;
$("auto-level").onchange = applyGain;

try {
  const saved = localStorage.getItem(GAIN_KEY);
  if (saved !== null) $("gain").value = saved;  // a range input clamps itself
  $("auto-level").checked = localStorage.getItem(AUTO_KEY) !== "0";
} catch {}
applyGain();

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
  parts.player.addEventListener("play", onPlay);
  return { el, parts, status: null };
}

// An in-progress session is not an artifact yet: its length is still growing,
// and a media element loaded now would stay pinned to the length it saw, so
// the same row would keep playing a truncated take even after the session
// ended. It gets its src on the way out of "recording" instead — and only
// then, because assigning src reloads the element and would cut off playback
// if it ran on every poll.
function renderPlayer(row, item) {
  const player = row.parts.player;
  if (item.status === "recording") {
    player.hidden = true;
    return;
  }
  if (player.getAttribute("src") !== item.audio_url) {
    player.src = item.audio_url;
  }
  player.hidden = false;
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
  a.href = item.download_url;
  a.download = "";
  a.textContent = "Download FLAC";
  a.title = item.permanent
    ? "The stored lossless file."
    : "Encoded out of the buffer on the fly. Same audio Keep would store, "
      + "without keeping it on the Pi.";
  return a;
}

function renderActions(row, item) {
  const box = row.parts.actions;
  box.innerHTML = "";
  if (item.status === "buffered") {
    box.append(
      // Keep is one click: naming is a separate Rename action on the kept
      // entry, so nothing stands between "I want this" and it being safe.
      button("Keep", null, () =>
        api(`/api/sessions/${item.id}/save`, { method: "POST" })),
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
  }
}

function updateRow(row, item) {
  const p = row.parts;
  // Re-read every poll: an active session's level firms up as it grows.
  p.player.dataset.gainDb = item.gain_db ?? 0;
  applyPlayerGain(p.player);
  p.title.textContent = item.label || fmtTime(item.start_utc);
  p.status.textContent = STATUS_LABEL[item.status] || item.status;
  p.status.className = `badge status ${item.status}`;
  p.status.title = STATUS_HINT[item.status] || "";
  const bits = [item.status === "recording"
    ? `${fmtDuration(elapsedSince(item.start_utc))} so far`
    : fmtDuration(item.duration_s)];
  if (item.kind === "manual") bits.push("manual");
  else bits.push("auto backup");
  if (item.label) bits.push(fmtTime(item.start_utc));
  if (item.size_bytes) bits.push(fmtSize(item.size_bytes));
  if (item.gain_db) bits.push(`${item.gain_db > 0 ? "+" : ""}${item.gain_db} dB`);
  p.meta.title = item.gain_db
    ? "Auto level: this transfer needs "
      + `${item.gain_db > 0 ? "+" : ""}${item.gain_db} dB on playback.`
    : "";
  p.meta.textContent = bits.join(" · ");
  p.warn.hidden = !item.has_gaps;
  if (row.status !== item.status) {
    renderPlayer(row, item);
    renderActions(row, item);
    row.status = item.status;
  }
}

function renderHistory(items) {
  const box = $("history");
  if (items.length === 0) {
    if (!box.querySelector(".empty")) {
      rows.clear();
      gains.clear();  // the players go with the innerHTML below
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
      gains.delete(row.parts.player);  // the node dies with the element
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
