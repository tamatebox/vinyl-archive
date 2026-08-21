"use strict";

// Shared by the front page (app.js) and the full history (history.js). The two
// are separate documents, so each brings its own row registry and its own idea
// of when to reload; everything that draws an entry or shapes the sound lives
// here so the pages cannot drift apart.

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

// Set by each page: what to reload after an action changed something.
let onMutated = () => {};

// Which actions a kept entry offers. The front page only tidies (Archive is
// reversible and touches no bytes); deleting is offered where the entry
// still is afterwards, which is the full history. Keeping the destructive
// action off the page you look at every day is the point, not an oversight.
let actionScope = "front";   // "front" | "history"

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

// Every player that exists, so starting one can stop the rest. Two sides
// playing at once is never what was meant by pressing Play on the second —
// the transport bars sit one under the other and both keep going.
const players = new Set();

function onPlay(ev) {
  for (const el of players) {
    if (el !== ev.target && !el.paused) el.pause();
  }
  routePlayer(ev.target);
  if (audioCtx) audioCtx.resume().catch(() => {});
}

// The gain settings live in localStorage, so both pages start from whatever
// the other one last set.
function initPlaybackBar() {
  $("gain").oninput = applyGain;
  $("auto-level").onchange = applyGain;
  try {
    const saved = localStorage.getItem(GAIN_KEY);
    if (saved !== null) $("gain").value = saved;  // a range input clamps itself
    $("auto-level").checked = localStorage.getItem(AUTO_KEY) !== "0";
  } catch {}
  applyGain();
}

// -- entries ------------------------------------------------------------------

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
    <div class="player-slot"></div>`;
  const parts = {
    title: el.querySelector(".title"),
    status: el.querySelector(".status"),
    meta: el.querySelector(".meta"),
    warn: el.querySelector(".warn"),
    actions: el.querySelector(".actions"),
    slot: el.querySelector(".player-slot"),
    player: null,   // set by ensurePlayer once there is audio to play
  };
  return { el, parts, status: null, archived: null, gainDb: 0 };
}

// One media element per row, built with the row. Nothing is fetched until
// Play is pressed: `preload="none"` keeps a list of hundreds of rows to
// hundreds of idle elements instead of hundreds of requests, each of which
// would have the Pi assemble a WAV out of buffer segments just to read a
// duration. A row that is never played still costs no audio I/O.
function ensurePlayer(row, item) {
  if (row.parts.player || item.status === "recording") return;
  const el = document.createElement("audio");
  el.className = "player";
  el.controls = true;
  el.preload = "none";
  el.dataset.gainDb = row.gainDb;
  el.addEventListener("play", onPlay);
  el.src = item.audio_url;
  row.parts.slot.append(el);
  row.parts.player = el;
  players.add(el);
}

// A row leaving the list takes its nodes with it: the gain node dies with the
// element, and a paused element left in the registry would keep the whole row
// alive for nothing.
function forgetPlayer(row) {
  const el = row.parts.player;
  if (!el) return;
  gains.delete(el);
  players.delete(el);
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
    onMutated();
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

// No "dismiss" here on purpose. The front page is a recency window, so an
// entry leaves it when newer ones arrive rather than when it is pruned, and
// everything is on /history either way — a button to hide a row could only
// pull an older row up into its place.
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
    );
  } else if (item.status === "saved") {
    const patch = (body) => api(`/api/recordings/${item.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    box.append(
      downloadLink(item),
      button("Rename", "secondary", () => {
        const label = prompt("Recording name:", item.label || "");
        if (label === null) throw new Error("cancelled");
        return patch({ label });
      }),
    );
    if (item.archived) {
      box.append(button("Unarchive", "secondary", () => patch({ archived: false })));
    } else {
      // No confirm: it is reversible, the file is untouched, and the entry is
      // still in the history a click away.
      const b = button("Archive", "secondary", () => patch({ archived: true }));
      b.title = "Off the front page, still here. Nothing is deleted — for "
              + "transfers you have already copied off the Pi.";
      box.append(b);
    }
    if (actionScope === "history") {
      // No confirmation: this moves it to the trash, where it stays visible
      // and restorable until the volume actually needs the room. The dialog
      // belongs on the step that cannot be undone.
      box.append(button("Delete", "danger", () => patch({ trashed: true })));
    }
  }
}

function updateRow(row, item) {
  const p = row.parts;
  // Re-read every poll: an active session's level firms up as it grows.
  row.gainDb = item.gain_db ?? 0;
  ensurePlayer(row, item);
  if (p.player) {
    p.player.dataset.gainDb = row.gainDb;
    applyPlayerGain(p.player);
  }
  p.title.textContent = item.label || fmtTime(item.start_utc);
  p.status.textContent = STATUS_LABEL[item.status] || item.status;
  p.status.className = `badge status ${item.status}`;
  p.status.title = STATUS_HINT[item.status] || "";
  const bits = [item.status === "recording"
    ? `${fmtDuration(elapsedSince(item.start_utc))} so far`
    : fmtDuration(item.duration_s)];
  if (item.kind === "manual") bits.push("manual");
  else bits.push("auto backup");
  if (item.archived) bits.push("archived");
  if (item.label) bits.push(fmtTime(item.start_utc));
  if (item.size_bytes) bits.push(fmtSize(item.size_bytes));
  if (item.gain_db) bits.push(`${item.gain_db > 0 ? "+" : ""}${item.gain_db} dB`);
  p.meta.title = item.gain_db
    ? "Auto level: this transfer needs "
      + `${item.gain_db > 0 ? "+" : ""}${item.gain_db} dB on playback.`
    : "";
  p.meta.textContent = bits.join(" · ");
  p.warn.hidden = !item.has_gaps;
  if (row.status !== item.status || row.archived !== item.archived) {
    renderActions(row, item);
    row.status = item.status;
    row.archived = item.archived;
  }
}

// Local calendar day of an entry, as YYYY-MM-DD. The server stores UTC and
// never guesses a timezone; the browser is the only party that knows which
// day 23:40Z belongs to, so every day boundary in the UI is computed here.
function localDayKey(iso) {
  const d = new Date(iso);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
       + `-${String(d.getDate()).padStart(2, "0")}`;
}

function dayHeading(dayKey) {
  const [y, m, d] = dayKey.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric",
  });
}

// `rows` is the caller's registry (key -> row), so each page owns its own.
// With groupByDay, a heading is inserted whenever the local date changes:
// headings hold no state, so they are rebuilt every render while the rows
// themselves are moved, never recreated.
function renderList(box, rows, items, emptyHtml, groupByDay = false) {
  for (const head of box.querySelectorAll(".day-head")) head.remove();

  if (items.length === 0) {
    if (!box.querySelector(".empty")) {
      for (const [, row] of rows) forgetPlayer(row);
      rows.clear();
      box.innerHTML = `<div class="empty">${emptyHtml}</div>`;
    }
    return;
  }
  const empty = box.querySelector(".empty");
  if (empty) empty.remove();

  const seen = new Set();
  const order = [];
  let day = null;
  for (const item of items) {
    if (groupByDay) {
      const key = localDayKey(item.start_utc);
      if (key !== day) {
        day = key;
        const head = document.createElement("div");
        head.className = "day-head";
        head.textContent = dayHeading(key);
        order.push(head);
      }
    }
    const key = keyOf(item);
    seen.add(key);
    let row = rows.get(key);
    if (!row) {
      row = createRow(item);
      rows.set(key, row);
    }
    updateRow(row, item);
    order.push(row.el);
  }

  for (const [key, row] of rows) {
    if (!seen.has(key)) {
      forgetPlayer(row);
      row.el.remove();
      rows.delete(key);
    }
  }
  // Rows are moved rather than rebuilt, so neither polling nor a change of
  // filter interrupts a player that is mid-playback: insertBefore relocates
  // the existing element instead of replacing it.
  order.forEach((node, index) => {
    if (box.children[index] !== node) {
      box.insertBefore(node, box.children[index] || null);
    }
  });
}
