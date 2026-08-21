"""SQLite metadata store: segments, sessions, recordings.

A single connection guarded by an RLock is shared across the capture thread,
export workers and the API. Write volume is tiny (one row per segment
rotation), so contention is a non-issue.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SEGMENT_NAME_RE = re.compile(r"^seg_(\d{16})_\d{8}T\d{6}\.flac$")

UNSAVED_STATES = ("active", "ended", "saving")

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    start_sample INTEGER NOT NULL,
    n_frames INTEGER NOT NULL,
    wall_start_utc TEXT NOT NULL,
    discontinuity INTEGER NOT NULL DEFAULT 0,
    released_at REAL,
    short_peak REAL,
    mean_sq REAL
);
CREATE INDEX IF NOT EXISTS idx_segments_start ON segments(start_sample);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    start_sample INTEGER NOT NULL,
    end_sample INTEGER,
    start_utc TEXT NOT NULL,
    end_utc TEXT,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','ended','saving','saved','expired')),
    truncated_head INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'auto'
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    session_id INTEGER,
    duration_s REAL NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_utc TEXT NOT NULL,
    has_gaps INTEGER NOT NULL DEFAULT 0,
    short_peak REAL,
    mean_sq REAL,
    -- Archived: off the front page, still in the full history, file
    -- untouched. Purely about where an entry is listed -- nothing reads this
    -- to decide whether bytes may go.
    archived_at REAL
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")]
            if "kind" not in cols:  # pre-'kind' database
                self._conn.execute("ALTER TABLE sessions ADD COLUMN"
                                   " kind TEXT NOT NULL DEFAULT 'auto'")
            # Playback levels: nullable on purpose. Rows written before this
            # existed keep NULL and play at unity rather than at a guess.
            for table in ("segments", "recordings"):
                have = [r[1] for r in
                        self._conn.execute(f"PRAGMA table_info({table})")]
                for col in ("short_peak", "mean_sq"):
                    if col not in have:
                        self._conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col} REAL")
            have = [r[1] for r in
                    self._conn.execute("PRAGMA table_info(recordings)")]
            if "archived_at" not in have:  # pre-'archive' database
                self._conn.execute(
                    "ALTER TABLE recordings ADD COLUMN archived_at REAL")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # -- segments ----------------------------------------------------------

    def add_segment(self, filename: str, start_sample: int, n_frames: int,
                    wall_start_utc: str, discontinuity: bool = False,
                    short_peak: float | None = None,
                    mean_sq: float | None = None) -> int:
        cur = self._exec(
            "INSERT INTO segments (filename, start_sample, n_frames, wall_start_utc,"
            " discontinuity, short_peak, mean_sq) VALUES (?,?,?,?,?,?,?)",
            (filename, start_sample, n_frames, wall_start_utc, int(discontinuity),
             short_peak, mean_sq),
        )
        return cur.lastrowid

    def list_segments(self) -> list[dict]:
        return self._query("SELECT * FROM segments ORDER BY start_sample")

    def segments_overlapping(self, start_sample: int, end_sample: int) -> list[dict]:
        return self._query(
            "SELECT * FROM segments WHERE start_sample + n_frames > ? AND start_sample < ?"
            " ORDER BY start_sample",
            (start_sample, end_sample),
        )

    def level_in_range(self, start_sample: int,
                       end_sample: int) -> tuple[float | None, float | None]:
        """Playback levels of the buffered audio covering a sample range.

        Whole segments are used even where the range only clips their edges:
        including a little audio from outside can only over-state the level,
        and over-stating it asks for less gain, never more. Segments with no
        measurement (re-registered by ``reconcile``) sit out rather than
        dragging the average toward silence.
        """
        rows = self._query(
            "SELECT MAX(short_peak) AS short_peak,"
            " SUM(mean_sq * n_frames) / SUM(n_frames) AS mean_sq"
            " FROM segments WHERE start_sample + n_frames > ? AND start_sample < ?"
            " AND short_peak IS NOT NULL AND mean_sq IS NOT NULL",
            (start_sample, end_sample),
        )
        row = rows[0] if rows else {}
        return row.get("short_peak"), row.get("mean_sq")

    def delete_segment(self, segment_id: int) -> None:
        self._exec("DELETE FROM segments WHERE id = ?", (segment_id,))

    def mark_segments_released(self, ids: list[int], released_at: float | None = None) -> None:
        if not ids:
            return
        ts = released_at if released_at is not None else time.time()
        qs = ",".join("?" * len(ids))
        self._exec(
            f"UPDATE segments SET released_at = ? WHERE id IN ({qs}) AND released_at IS NULL",
            (ts, *ids),
        )

    def max_end_sample(self) -> int:
        rows = self._query("SELECT MAX(start_sample + n_frames) AS m FROM segments")
        return int(rows[0]["m"] or 0)

    def min_start_sample(self) -> int | None:
        rows = self._query("SELECT MIN(start_sample) AS m FROM segments")
        return None if rows[0]["m"] is None else int(rows[0]["m"])

    def buffered_frames(self) -> int:
        rows = self._query("SELECT COALESCE(SUM(n_frames), 0) AS s FROM segments")
        return int(rows[0]["s"])

    # -- sessions ----------------------------------------------------------

    def create_session(self, start_sample: int, start_utc: str,
                       kind: str = "auto") -> int:
        cur = self._exec(
            "INSERT INTO sessions (start_sample, start_utc, state, kind)"
            " VALUES (?,?, 'active', ?)",
            (start_sample, start_utc, kind),
        )
        return cur.lastrowid

    def close_session(self, session_id: int, end_sample: int, end_utc: str) -> None:
        self._exec(
            "UPDATE sessions SET end_sample = ?, end_utc = ?, state = 'ended'"
            " WHERE id = ? AND state = 'active'",
            (end_sample, end_utc, session_id),
        )

    def delete_session(self, session_id: int) -> None:
        self._exec("DELETE FROM sessions WHERE id = ?", (session_id,))

    def set_session_state(self, session_id: int, state: str) -> None:
        self._exec("UPDATE sessions SET state = ? WHERE id = ?", (state, session_id))

    def set_session_kind(self, session_id: int, kind: str) -> None:
        self._exec("UPDATE sessions SET kind = ? WHERE id = ?", (kind, session_id))

    def mark_truncated_head(self, session_id: int, new_start_sample: int) -> None:
        self._exec(
            "UPDATE sessions SET truncated_head = 1, start_sample = ? WHERE id = ?",
            (new_start_sample, session_id),
        )

    def get_session(self, session_id: int) -> dict | None:
        rows = self._query("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return rows[0] if rows else None

    def list_sessions(self, limit: int = 50) -> list[dict]:
        return self._query(
            "SELECT * FROM sessions ORDER BY start_sample DESC LIMIT ?", (limit,)
        )

    def session_meta(self) -> dict[int, dict]:
        """id -> {kind, start_utc} for every session, for joining onto
        recordings without one query per row."""
        return {r["id"]: r for r in
                self._query("SELECT id, kind, start_utc FROM sessions")}

    def unsaved_sessions(self) -> list[dict]:
        qs = ",".join("?" * len(UNSAVED_STATES))
        return self._query(
            f"SELECT * FROM sessions WHERE state IN ({qs}) ORDER BY start_sample",
            UNSAVED_STATES,
        )

    # -- settings ----------------------------------------------------------

    def get_settings(self) -> dict:
        return {r["key"]: json.loads(r["value"])
                for r in self._query("SELECT key, value FROM settings")}

    def set_settings(self, settings: dict) -> None:
        with self._lock:
            for key, value in settings.items():
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)),
                )
            self._conn.commit()

    # -- recordings --------------------------------------------------------

    def add_recording(self, filename: str, session_id: int | None, duration_s: float,
                      size_bytes: int, has_gaps: bool, label: str = "",
                      short_peak: float | None = None,
                      mean_sq: float | None = None) -> int:
        cur = self._exec(
            "INSERT INTO recordings (filename, label, session_id, duration_s, size_bytes,"
            " created_utc, has_gaps, short_peak, mean_sq) VALUES (?,?,?,?,?,?,?,?,?)",
            (filename, label, session_id, duration_s, size_bytes, utcnow_iso(),
             int(has_gaps), short_peak, mean_sq),
        )
        return cur.lastrowid

    def get_recording(self, recording_id: int) -> dict | None:
        rows = self._query("SELECT * FROM recordings WHERE id = ?", (recording_id,))
        return rows[0] if rows else None

    def list_recordings(self) -> list[dict]:
        """Every recording, archived ones included.

        Deliberately unfiltered: `reconcile` walks this to drop rows whose
        file has gone, so hiding rows here would leak them instead. Callers
        that want a subset filter it themselves.
        """
        return self._query("SELECT * FROM recordings ORDER BY created_utc DESC")

    def delete_recording(self, recording_id: int) -> None:
        self._exec("DELETE FROM recordings WHERE id = ?", (recording_id,))

    def set_recording_label(self, recording_id: int, label: str) -> None:
        self._exec("UPDATE recordings SET label = ? WHERE id = ?", (label, recording_id))

    def set_recording_archived(self, recording_id: int, archived: bool) -> None:
        self._exec("UPDATE recordings SET archived_at = ? WHERE id = ?",
                   (time.time() if archived else None, recording_id))


def reconcile(db: Database, buffer_dir: Path, recordings_dir: Path,
              audio=None) -> None:
    """Bring the DB in line with reality after a restart. Files are the truth.

    When ``audio`` (an AudioConfig) is given, buffer segments recorded in a
    different format are discarded: the audio format is only changeable with
    a restart, and mixing formats inside the buffer would corrupt exports.
    Saved recordings are standalone files and are never touched.
    """
    import soundfile as sf

    on_disk = {p.name: p for p in buffer_dir.glob("seg_*.flac")}

    def format_matches(path: Path) -> bool:
        if audio is None:
            return True
        try:
            info = sf.info(str(path))
        except Exception:
            return False
        return (info.samplerate == audio.sample_rate
                and info.channels == audio.channels
                and info.subtype == audio.flac_subtype)

    for row in db.list_segments():
        path = on_disk.pop(row["filename"], None)
        if path is None:
            log.info("reconcile: dropping DB row for missing segment %s", row["filename"])
            db.delete_segment(row["id"])
        elif not format_matches(path):
            log.warning("reconcile: discarding segment %s (audio format changed)",
                        row["filename"])
            path.unlink(missing_ok=True)
            db.delete_segment(row["id"])

    for name, path in sorted(on_disk.items()):
        m = SEGMENT_NAME_RE.match(name)
        if not m:
            log.warning("reconcile: ignoring unrecognized file %s", name)
            continue
        try:
            info = sf.info(str(path))
        except Exception:
            log.warning("reconcile: removing unreadable segment %s", name)
            path.unlink(missing_ok=True)
            continue
        if not format_matches(path):
            log.warning("reconcile: discarding segment %s (audio format changed)", name)
            path.unlink(missing_ok=True)
            continue
        wall = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        db.add_segment(name, int(m.group(1)), info.frames,
                       wall.strftime("%Y-%m-%dT%H:%M:%SZ"), discontinuity=True)
        log.info("reconcile: re-registered segment %s (%d frames)", name, info.frames)

    for part in recordings_dir.glob("*.part"):
        log.info("reconcile: removing interrupted export %s", part.name)
        part.unlink(missing_ok=True)

    for rec in db.list_recordings():
        if not (recordings_dir / rec["filename"]).exists():
            log.info("reconcile: dropping DB row for missing recording %s", rec["filename"])
            db.delete_recording(rec["id"])

    # Interrupted exports go back to 'ended' (retryable); sessions that were
    # still open when we died are force-closed at the end of buffered data —
    # capture restarts imply a gap, so continuing them would be wrong.
    max_end = db.max_end_sample()
    for sess in db.unsaved_sessions():
        if sess["state"] == "saving":
            db.set_session_state(sess["id"], "ended")
        elif sess["state"] == "active":
            end = min(max_end, max(sess["start_sample"], max_end))
            if end - sess["start_sample"] > 0:
                db.close_session(sess["id"], end, utcnow_iso())
            else:
                db.set_session_state(sess["id"], "expired")
        # Sessions whose audio no longer exists in the buffer are unsaveable.
        min_start = db.min_start_sample()
        cur = db.get_session(sess["id"])
        if cur and cur["state"] == "ended" and cur["end_sample"] is not None:
            if min_start is None or cur["end_sample"] <= min_start:
                db.set_session_state(sess["id"], "expired")
