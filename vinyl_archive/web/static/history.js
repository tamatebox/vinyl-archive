"use strict";

// Full history: every buffered session and every kept recording, newest first.
// Loaded once instead of polled — nothing here is live, and a page you are
// reading while listening should not reshuffle under you. Actions reload it.

const rows = new Map();  // key -> {el, parts, status}

async function load() {
  const box = $("history");
  try {
    const items = await api("/api/history");
    renderList(box, rows, items,
               `Nothing captured yet. Play a record and it shows up here on
                its own.`);
    $("count").textContent = items.length
      ? `${items.length} ${items.length === 1 ? "entry" : "entries"}`
      : "";
  } catch (e) {
    box.innerHTML = `<div class="empty">Could not load the history: ${e.message}</div>`;
  }
}

$("btn-reload").onclick = load;
onMutated = load;
initPlaybackBar();
load();
