"use strict";

const POLL_MS = 2000;

const STATE_LABEL = {
  recording: "Recording",
  waiting_device: "Waiting for device",
  restarting: "Restarting",
  stopped: "Stopped",
};

const SESSION_LABEL = {
  active: "Detecting…",
  ended: "Ready to save",
  saving: "Saving…",
  saved: "Saved",
  expired: "Expired",
};

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
  const d = new Date(iso);
  return d.toLocaleString("en-US", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hour12: false,
  });
}

function fmtDuration(s) {
  if (s == null) return "--";
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}m ${String(sec).padStart(2, "0")}s`;
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
  const pct = Math.max(0, Math.min(100, (level + 60) / 60 * 100));
  $("level-bar").style.width = `${pct}%`;
  $("level-text").textContent =
    level <= -100 ? "silent" : `${level.toFixed(1)} dBFS`;

  const buf = st.buffer;
  $("buffer-text").textContent =
    `Buffer ${Math.round(buf.seconds / 60)} / ${Math.round(buf.capacity_seconds / 60)} min`;
  $("disk-text").textContent =
    `Disk free ${(buf.disk_free_mb / 1024).toFixed(1)} GB · ${buf.segments} segments`;

  const running = st.capture !== "stopped";
  $("btn-start").hidden = running;
  $("btn-stop").hidden = !running;
}

// -- sessions -----------------------------------------------------------------

function renderSessions(sessions) {
  const box = $("sessions");
  box.innerHTML = "";
  const visible = sessions.filter((s) => s.state !== "saved" && s.state !== "expired");
  if (visible.length === 0) {
    box.innerHTML = `<div class="empty">No sessions detected yet. Play a record and it will be picked up automatically.</div>`;
    return;
  }
  for (const s of visible) {
    const item = document.createElement("div");
    item.className = "item";
    const warn = s.truncated_head
      ? `<span class="warn">⚠ head evicted from buffer</span>` : "";
    item.innerHTML = `
      <span class="title">${fmtTime(s.start_utc)}</span>
      <span class="meta">${fmtDuration(s.duration_s)} · ${SESSION_LABEL[s.state] || s.state}</span>
      ${warn}
      <span class="actions"></span>`;
    const actions = item.querySelector(".actions");
    if (s.state === "ended") {
      const btn = document.createElement("button");
      btn.textContent = "Save";
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await api(`/api/sessions/${s.id}/save`, { method: "POST" });
        } catch (e) {
          alert(`Save failed: ${e.message}`);
        }
        refresh();
      };
      actions.appendChild(btn);
    }
    box.appendChild(item);
  }
}

// -- recordings ---------------------------------------------------------------

function renderRecordings(recordings) {
  const box = $("recordings");
  box.innerHTML = "";
  if (recordings.length === 0) {
    box.innerHTML = `<div class="empty">No saved recordings.</div>`;
    return;
  }
  for (const r of recordings) {
    const item = document.createElement("div");
    item.className = "item";
    const warn = r.has_gaps ? `<span class="warn">⚠ has gaps</span>` : "";
    item.innerHTML = `
      <span class="title">${r.label || r.filename}</span>
      <span class="meta">${fmtDuration(r.duration_s)} · ${fmtSize(r.size_bytes)} · ${fmtTime(r.created_utc)}</span>
      ${warn}
      <span class="actions">
        <a class="dl" href="/api/recordings/${r.id}/download">Download</a>
        <button class="secondary btn-rename">Rename</button>
        <button class="danger btn-delete">Delete</button>
      </span>`;
    item.querySelector(".btn-rename").onclick = async () => {
      const label = prompt("Recording name:", r.label || "");
      if (label === null) return;
      try {
        await api(`/api/recordings/${r.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label }),
        });
      } catch (e) {
        alert(`Rename failed: ${e.message}`);
      }
      refresh();
    };
    item.querySelector(".btn-delete").onclick = async () => {
      if (!confirm(`Delete "${r.label || r.filename}" permanently?`)) return;
      try {
        await api(`/api/recordings/${r.id}`, { method: "DELETE" });
      } catch (e) {
        alert(`Delete failed: ${e.message}`);
      }
      refresh();
    };
    box.appendChild(item);
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
}

$("settings-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const patch = {};
  for (const input of ev.target.querySelectorAll("input")) {
    patch[input.name] = input.type === "checkbox"
      ? input.checked : Number(input.value);
  }
  const msg = $("settings-msg");
  try {
    await api("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    msg.textContent = "Saved.";
    await loadSettings();
  } catch (e) {
    msg.textContent = `Error: ${e.message}`;
  }
};

loadSettings().catch(() => {});

// -- polling ------------------------------------------------------------------

async function refresh() {
  try {
    const [status, sessions, recordings] = await Promise.all([
      api("/api/status"), api("/api/sessions"), api("/api/recordings"),
    ]);
    renderStatus(status);
    renderSessions(sessions);
    renderRecordings(recordings);
  } catch (e) {
    $("capture-state").textContent = "Connection error";
    $("capture-state").className = "badge stopped";
  }
}

$("btn-start").onclick = () => api("/api/capture/start", { method: "POST" }).then(refresh);
$("btn-stop").onclick = () => {
  if (confirm("Stop continuous capture?")) {
    api("/api/capture/stop", { method: "POST" }).then(refresh);
  }
};

refresh();
setInterval(refresh, POLL_MS);
