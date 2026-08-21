"use strict";

// Front page: live status, the controls, and a recency window over the
// history. Shared helpers are in common.js, which must load first.

const POLL_MS = 2000;

// How many merely-buffered entries the front page shows. A window, not a
// budget to be pruned: older ones are not gone, they are on /history. Small
// enough that one sitting's worth of sides fits, which is all the page is for
// — deciding about what was just played.
const BUFFERED_LIMIT = 5;

const STATE_LABEL = {
  recording: "Capturing",
  waiting_device: "Waiting for device",
  restarting: "Restarting",
  stopped: "Paused",
};

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

// One registry per list: rows are matched by key within their own list, so
// an entry moving from the buffer to the archive is a new row in the second
// list rather than a row that changed section under the diff.
const bufferedRows = new Map();
const keptRows = new Map();

async function refresh() {
  try {
    const [status, history] = await Promise.all([
      api("/api/status"),
      api(`/api/history?buffered_limit=${BUFFERED_LIMIT}`
          + "&include_archived=false"),
    ]);
    renderStatus(status);
    // Split on permanence, not on type: a session being written out is still
    // the thing you were deciding about, so "Keeping…" belongs above the line
    // until the file exists.
    renderList($("buffered"), bufferedRows, history.filter((i) => !i.permanent),
               `Nothing in the buffer. Play a record and it shows up here on
                its own.`);
    // No link in here: the "Full history" link sits directly below this
    // list, so an inline one would be the same destination offered twice in
    // two adjacent lines.
    renderList($("kept"), keptRows, history.filter((i) => i.permanent),
               `Nothing kept yet — or everything kept has been archived, in
                which case it is in the full history below.`);
  } catch (e) {
    $("capture-state").textContent = "Connection error";
    $("capture-state").className = "badge stopped";
  }
}

onMutated = refresh;
initPlaybackBar();
loadSettings().catch(() => {});
refresh();
setInterval(refresh, POLL_MS);
