"use strict";

// Full history: every buffered session and every kept recording, newest first,
// with a month calendar above it that filters the list to one day.
//
// Loaded once instead of polled — nothing here is live, and a page you are
// reading while listening should not reshuffle under you. Actions reload it.
//
// The calendar is an index over the list, not a separate view: the list below
// holds the month on display, grouped by day, and picking a day narrows it to
// that day. Scoping the list to the month is what makes ‹ › mean something —
// and it bounds the list by a unit that is the same size whatever the buffer
// happens to hold.

const rows = new Map();  // key -> {el, parts, status}

let items = [];          // everything the server returned
let counts = new Map();  // local day key -> number of entries
let oldestDay = null;    // how far back the buffer actually reaches
let viewMonth = null;    // first of the month on display
let selectedDay = null;  // day key, or null for "everything"

const WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

const keyOfDate = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
  + `-${String(d.getDate()).padStart(2, "0")}`;

const firstOfMonth = (d) => new Date(d.getFullYear(), d.getMonth(), 1);
const monthKey = (d) => d.getFullYear() * 12 + d.getMonth();
const monthOf = (dayKey) => dayKey.slice(0, 7);          // "2026-08"
const monthLabel = (d) =>
  d.toLocaleDateString("en-US", { month: "long", year: "numeric" });
const dateOfKey = (key) => {
  const [y, m, d] = key.split("-").map(Number);
  return new Date(y, m - 1, d);
};

function index() {
  counts = new Map();
  for (const item of items) {
    const key = localDayKey(item.start_utc);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const days = [...counts.keys()].sort();
  oldestDay = days[0] || null;
}

// -- calendar -----------------------------------------------------------------

function renderCalendar() {
  const box = $("calendar");
  box.innerHTML = "";
  if (!oldestDay) {          // nothing captured: an empty grid says nothing
    box.hidden = true;
    return;
  }
  box.hidden = false;

  const today = new Date();
  const minMonth = monthKey(dateOfKey(oldestDay));
  const maxMonth = monthKey(today);
  const shown = monthKey(viewMonth);

  const nav = document.createElement("div");
  nav.className = "cal-nav";
  const label = document.createElement("span");
  label.className = "cal-month";
  label.textContent = monthLabel(viewMonth);
  const step = (delta) => {
    const btn = document.createElement("button");
    btn.className = "secondary";
    btn.textContent = delta < 0 ? "‹" : "›";
    btn.title = delta < 0 ? "Earlier month" : "Later month";
    // Bounded by the data, not open-ended: there is nothing to page into
    // before the buffer's oldest day or after today.
    btn.disabled = delta < 0 ? shown <= minMonth : shown >= maxMonth;
    btn.onclick = () => {
      viewMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() + delta, 1);
      selectedDay = null;   // the day picked belonged to the month left behind
      render();
    };
    return btn;
  };
  nav.append(step(-1), label, step(1));
  box.append(nav);

  const grid = document.createElement("div");
  grid.className = "cal-grid";
  for (const w of WEEKDAYS) {
    const head = document.createElement("span");
    head.className = "cal-wd";
    head.textContent = w;
    grid.append(head);
  }

  const year = viewMonth.getFullYear();
  const month = viewMonth.getMonth();
  for (let blank = 0; blank < new Date(year, month, 1).getDay(); blank++) {
    grid.append(document.createElement("span"));
  }
  const lastDay = new Date(year, month + 1, 0).getDate();
  const todayKey = keyOfDate(today);
  for (let day = 1; day <= lastDay; day++) {
    const key = keyOfDate(new Date(year, month, day));
    const count = counts.get(key) || 0;
    // Three states, because two would lie: a day the buffer no longer reaches
    // is not the same as a day nothing was played, and only the second one
    // means "you played nothing".
    if (!count) {
      const future = key > todayKey;
      const gone = key < oldestDay;
      const cell = document.createElement("span");
      cell.className = `cal-day${future || gone ? " outside" : " none"}`;
      cell.textContent = day;
      cell.title = future ? "Still to come"
                 : gone ? "Older than the buffer now reaches"
                 : "Nothing captured";
      grid.append(cell);
      continue;
    }
    const cell = document.createElement("button");
    cell.className = `cal-day has${key === selectedDay ? " sel" : ""}`;
    // The count is text, not just shading: colour alone is not a signal
    // everyone can read, and the number is the useful part anyway.
    cell.innerHTML = `<span class="d">${day}</span><span class="n">${count}</span>`;
    cell.title = `${count} ${count === 1 ? "entry" : "entries"}`;
    cell.setAttribute("aria-pressed", key === selectedDay ? "true" : "false");
    cell.onclick = () => select(key === selectedDay ? null : key);
    grid.append(cell);
  }
  box.append(grid);
}

// -- selection ----------------------------------------------------------------

function renderSelection() {
  const box = $("selection");
  box.innerHTML = "";
  if (!selectedDay) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const count = counts.get(selectedDay) || 0;
  const chip = document.createElement("button");
  chip.className = "chip";
  // The selection is stated in words as well as highlighted in the grid: after
  // paging to another month the highlighted cell is off screen.
  chip.textContent = `${dayHeading(selectedDay)} · `
    + `${count} ${count === 1 ? "entry" : "entries"} ✕`;
  chip.title = "Show everything again";
  chip.onclick = () => select(null);
  box.append(chip);
}

function select(day) {
  selectedDay = day;
  if (day) viewMonth = firstOfMonth(dateOfKey(day));
  render();
}

function render() {
  renderCalendar();
  renderSelection();
  renderItems();
}

// -- list ---------------------------------------------------------------------

function renderItems() {
  const month = keyOfDate(viewMonth).slice(0, 7);
  const shown = items.filter((i) => {
    const day = localDayKey(i.start_utc);
    return selectedDay ? day === selectedDay : monthOf(day) === month;
  });
  // Day headings only earn their place when more than one day is on screen.
  renderList($("history"), rows, shown,
             selectedDay
               ? "Nothing on this day any more."
               : `Nothing captured in ${monthLabel(viewMonth)}.`,
             !selectedDay);
  $("count").textContent = items.length
    ? `${shown.length} of ${items.length} ${items.length === 1 ? "entry" : "entries"}`
    : "";
}

async function load() {
  try {
    items = await api("/api/history");
  } catch (e) {
    $("history").innerHTML =
      `<div class="empty">Could not load the history: ${e.message}</div>`;
    return;
  }
  index();
  // A selected day that the ring buffer has since reclaimed would leave the
  // page filtered to nothing with no obvious way back.
  if (selectedDay && !counts.has(selectedDay)) selectedDay = null;
  if (selectedDay) viewMonth = firstOfMonth(dateOfKey(selectedDay));
  else if (!viewMonth) {
    viewMonth = firstOfMonth(items.length
      ? dateOfKey(localDayKey(items[0].start_utc))   // newest first from the API
      : new Date());
  }
  // A reload must not snap the month back: Keep and Rename both reload, and
  // losing your place mid-browse would be worse than the action was useful.
  // The oldest day can move forward though, so the view still has to be
  // pulled back inside the range that exists.
  if (oldestDay) {
    const low = firstOfMonth(dateOfKey(oldestDay));
    const high = firstOfMonth(new Date());
    if (monthKey(viewMonth) < monthKey(low)) viewMonth = low;
    if (monthKey(viewMonth) > monthKey(high)) viewMonth = high;
  }
  render();
}

actionScope = "history";   // deleting is offered here, not on the front page
$("btn-reload").onclick = load;
onMutated = load;
initPlaybackBar();
load();
