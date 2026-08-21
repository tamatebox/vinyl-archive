"""Segment writer rotation/naming and ring GC policy tests."""

import time

import numpy as np

from vinyl_archive.config import RingConfig
from vinyl_archive.db import Database
from vinyl_archive.ring.gc import RingGC
from vinyl_archive.ring.writer import SegmentWriter

RATE = 8000
SEG = 8000  # 1 s segments


def frames(n: int, offset: int = 0) -> np.ndarray:
    i = np.arange(offset, offset + n, dtype=np.int64)
    left = (i % 30000) - 15000
    right = ((i * 7) % 30000) - 15000
    return np.stack([left, right], axis=1).astype(np.int16)


def make_writer(config, db) -> SegmentWriter:
    return SegmentWriter(db, config.buffer_dir, RATE, 2, segment_frames=SEG)


def test_rotation_and_naming(config, db):
    w = make_writer(config, db)
    w.append(frames(20000))

    assert w.position == 20000
    assert w.flushed_end() == 16000  # two full segments closed
    w.close()
    assert w.flushed_end() == 20000

    segs = db.list_segments()
    assert [s["start_sample"] for s in segs] == [0, 8000, 16000]
    assert [s["n_frames"] for s in segs] == [8000, 8000, 4000]
    for s in segs:
        assert s["filename"].startswith(f"seg_{s['start_sample']:016d}_")
        assert (config.buffer_dir / s["filename"]).exists()


def test_request_rotate_closes_partial_segment(config, db):
    w = make_writer(config, db)
    w.append(frames(3000))
    assert w.flushed_end() == 0

    w.request_rotate()
    w.append(frames(100, offset=3000))

    assert w.flushed_end() == 3100
    assert db.list_segments()[0]["n_frames"] == 3100


def test_discontinuity_flag(config, db):
    w = make_writer(config, db)
    w.append(frames(SEG))
    w.mark_discontinuity()
    w.append(frames(SEG, offset=SEG))

    segs = db.list_segments()
    assert segs[0]["discontinuity"] == 0
    assert segs[1]["discontinuity"] == 1


def test_writer_resumes_sample_counter_after_restart(config, db):
    w = make_writer(config, db)
    w.append(frames(SEG + 100))
    w.close()

    w2 = make_writer(config, db)
    assert w2.position == SEG + 100
    w2.append(frames(50))
    w2.close()

    last = db.list_segments()[-1]
    assert last["start_sample"] == SEG + 100
    assert last["discontinuity"] == 1  # restart implies a capture gap


def test_skip_advances_counter_without_writing(config, db):
    w = make_writer(config, db)
    w.append(frames(3000))
    w.skip(5000)
    assert w.position == 8000
    assert w.flushed_end() == 8000  # segment closed, skipped range settled

    w.append(frames(100, offset=8000))
    w.close()
    segs = db.list_segments()
    assert [(s["start_sample"], s["n_frames"]) for s in segs] == [(0, 3000), (8000, 100)]


# -- GC -----------------------------------------------------------------------


def add_seg(db: Database, config, start: int, n: int = SEG) -> dict:
    name = f"seg_{start:016d}_20260101T000000.flac"
    (config.buffer_dir / name).touch()
    seg_id = db.add_segment(name, start, n, "2026-01-01T00:00:00Z")
    return {"id": seg_id, "filename": name}


def test_cap_deletes_oldest(config, db):
    for i in range(7):
        add_seg(db, config, i * SEG)
    RingGC(db, config.buffer_dir, config.ring, config.recordings_dir).collect()  # max_segments=5

    segs = db.list_segments()
    assert [s["start_sample"] for s in segs] == [2 * SEG, 3 * SEG, 4 * SEG, 5 * SEG, 6 * SEG]
    assert not (config.buffer_dir / f"seg_{0:016d}_20260101T000000.flac").exists()


def test_released_segments_deleted_early(config, db):
    for i in range(3):  # well under the cap
        add_seg(db, config, i * SEG)
    db.mark_segments_released([db.list_segments()[0]["id"]], released_at=time.time() - 10)

    RingGC(db, config.buffer_dir, config.ring, config.recordings_dir).collect()  # grace = 0
    assert [s["start_sample"] for s in db.list_segments()] == [SEG, 2 * SEG]


def test_released_segment_kept_during_grace(config, db):
    add_seg(db, config, 0)
    db.mark_segments_released([db.list_segments()[0]["id"]])

    cfg = RingConfig(max_segments=5, released_grace_seconds=3600)
    RingGC(db, config.buffer_dir, cfg, config.recordings_dir).collect()
    assert len(db.list_segments()) == 1


def test_unsaved_session_protects_released_segment(config, db):
    add_seg(db, config, 0)
    db.mark_segments_released([db.list_segments()[0]["id"]], released_at=time.time() - 10)
    sid = db.create_session(100, "2026-01-01T00:00:00Z")
    db.close_session(sid, 7000, "2026-01-01T00:01:00Z")

    RingGC(db, config.buffer_dir, config.ring, config.recordings_dir).collect()
    assert len(db.list_segments()) == 1


def test_cap_eviction_truncates_overlapping_session(config, db):
    for i in range(7):
        add_seg(db, config, i * SEG)
    sid = db.create_session(0, "2026-01-01T00:00:00Z")
    db.close_session(sid, 7 * SEG, "2026-01-01T00:07:00Z")

    RingGC(db, config.buffer_dir, config.ring, config.recordings_dir).collect()

    sess = db.get_session(sid)
    assert sess["truncated_head"] == 1
    assert sess["start_sample"] == 2 * SEG
    assert sess["state"] == "ended"


def test_fully_evicted_session_expires(config, db):
    for i in range(7):
        add_seg(db, config, i * SEG)
    sid = db.create_session(0, "2026-01-01T00:00:00Z")
    db.close_session(sid, SEG, "2026-01-01T00:00:01Z")

    RingGC(db, config.buffer_dir, config.ring, config.recordings_dir).collect()
    assert db.get_session(sid)["state"] == "expired"


def add_recording(db: Database, config, name: str, size: int = 10) -> int:
    (config.recordings_dir / name).write_bytes(b"x" * size)
    sid = db.create_session(0, "2026-01-01T00:00:00Z")
    db.close_session(sid, SEG, "2026-01-01T00:00:01Z")
    db.set_session_state(sid, "saved")
    return db.add_recording(name, sid, 1.0, size, False)


def test_trash_is_given_up_oldest_first_and_only_while_low(config, db, monkeypatch):
    """Free space is the only thing that makes a keeper's bytes go, and the
    loop stops the moment there is room: what is in the trash is given up one
    file at a time, oldest discard first."""
    keep = add_recording(db, config, "keep.flac")
    old = add_recording(db, config, "old.flac")
    new = add_recording(db, config, "new.flac")
    db.set_recording_trashed(old, True, at=1000.0)
    db.set_recording_trashed(new, True, at=2000.0)
    add_seg(db, config, 0)

    # Below the floor for the first look only, so exactly one file should go.
    seen = []

    def fake_free(path):
        seen.append(path)
        return 0.0 if len(seen) == 1 else 10 ** 9

    monkeypatch.setattr(RingGC, "_free_mb", staticmethod(fake_free))
    RingGC(db, config.buffer_dir, config.ring, config.recordings_dir).collect()

    assert not (config.recordings_dir / "old.flac").exists()
    assert db.get_recording(old) is None
    assert (config.recordings_dir / "new.flac").exists()   # room again, stop
    assert db.get_recording(new)["trashed_at"] == 2000.0
    assert (config.recordings_dir / "keep.flac").exists()
    assert db.get_recording(keep) is not None
    assert db.list_segments()                              # buffer untouched


def test_kept_recordings_survive_when_the_trash_cannot_save_the_volume(
        config, db, monkeypatch):
    """The trash empties, then the buffer gives way — but a keeper that was
    never discarded is never touched, however tight the volume gets."""
    keep = add_recording(db, config, "keep.flac")
    doomed = add_recording(db, config, "doomed.flac")
    db.set_recording_trashed(doomed, True)
    for i in range(3):
        add_seg(db, config, i * SEG)

    monkeypatch.setattr(RingGC, "_free_mb", staticmethod(lambda path: 0.0))
    RingGC(db, config.buffer_dir, config.ring, config.recordings_dir).collect()

    assert not (config.recordings_dir / "doomed.flac").exists()
    assert (config.recordings_dir / "keep.flac").exists()
    assert db.get_recording(keep) is not None
    assert db.list_segments() == []      # the buffer is what gave way instead


def test_purging_a_recording_expires_its_session(config, db, monkeypatch):
    """A session left at 'saved' with no recording appears in no list at all,
    so dropping the row has to say the audio is gone."""
    rec = add_recording(db, config, "gone.flac")
    session_id = db.get_recording(rec)["session_id"]
    db.set_recording_trashed(rec, True)

    monkeypatch.setattr(RingGC, "_free_mb", staticmethod(lambda path: 0.0))
    RingGC(db, config.buffer_dir, config.ring, config.recordings_dir).collect()

    assert db.get_session(session_id)["state"] == "expired"


def test_set_segment_frames_changes_rotation_length(config, db):
    w = make_writer(config, db)
    w.append(frames(SEG))          # one 1 s segment
    w.set_segment_frames(SEG // 4)  # shorten to 0.25 s
    w.append(frames(SEG, offset=SEG))
    w.close()

    lengths = [s["n_frames"] for s in db.list_segments()]
    # The first keeps its original length; later ones use the new one.
    assert lengths[0] == SEG
    assert lengths[1:] == [SEG // 4] * 4
