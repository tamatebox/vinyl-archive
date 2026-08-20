"""Upgrading an existing installation must not need manual DB surgery.

These build a database with the pre-update schema and open it with the
current code, the way a service restart after `deploy/update.sh` does.
"""

import sqlite3

from vinyl_archive.db import Database, reconcile

OLD_SCHEMA = """
CREATE TABLE segments (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    start_sample INTEGER NOT NULL,
    n_frames INTEGER NOT NULL,
    wall_start_utc TEXT NOT NULL,
    discontinuity INTEGER NOT NULL DEFAULT 0,
    released_at REAL
);
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    start_sample INTEGER NOT NULL,
    end_sample INTEGER,
    start_utc TEXT NOT NULL,
    end_utc TEXT,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','ended','saving','saved','expired')),
    truncated_head INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE recordings (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    session_id INTEGER,
    duration_s REAL NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_utc TEXT NOT NULL,
    has_gaps INTEGER NOT NULL DEFAULT 0
);
"""


def make_old_db(path, with_rows=True):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    if with_rows:
        conn.execute("INSERT INTO sessions (start_sample, end_sample, start_utc,"
                     " end_utc, state) VALUES (0, 48000, '2026-01-01T00:00:00Z',"
                     " '2026-01-01T00:00:01Z', 'saved')")
        conn.execute("INSERT INTO recordings (filename, label, session_id,"
                     " duration_s, size_bytes, created_utc)"
                     " VALUES ('rec_old.flac', 'Old Keeper', 1, 1.0, 10,"
                     " '2026-01-01T00:00:02Z')")
    conn.commit()
    conn.close()


def test_pre_kind_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "vinyl.sqlite3"
    make_old_db(path)

    db = Database(path)
    try:
        # The new column appears with a sane default for existing rows...
        sess = db.get_session(1)
        assert sess["kind"] == "auto"
        # ...the settings table is created...
        assert db.get_settings() == {}
        db.set_settings({"silence_gating": False})
        assert db.get_settings() == {"silence_gating": False}
        # ...and existing user data survives untouched.
        assert db.list_recordings()[0]["label"] == "Old Keeper"
        assert db.session_meta()[1]["kind"] == "auto"
    finally:
        db.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "vinyl.sqlite3"
    make_old_db(path)
    for _ in range(3):  # every service restart re-runs it
        db = Database(path)
        db.close()
    db = Database(path)
    try:
        assert db.get_session(1)["kind"] == "auto"
    finally:
        db.close()


def test_reconcile_after_upgrade_drops_rows_for_missing_files(tmp_path, config):
    """The old install's recordings row points at a file that the upgrade
    does not move; reconcile must not delete a file that is actually there."""
    path = tmp_path / "vinyl.sqlite3"
    make_old_db(path)
    (config.recordings_dir / "rec_old.flac").write_bytes(b"x")

    db = Database(path)
    try:
        reconcile(db, config.buffer_dir, config.recordings_dir)
        assert [r["filename"] for r in db.list_recordings()] == ["rec_old.flac"]
        assert (config.recordings_dir / "rec_old.flac").exists()
    finally:
        db.close()
