"""Exporter tests: sample-accurate extraction across segment boundaries."""

import dataclasses

import numpy as np
import pytest
import soundfile as sf

from vinyl_archive.config import AudioConfig
from vinyl_archive.db import utcnow_iso
from vinyl_archive.ring.writer import SegmentWriter
from vinyl_archive.sessions.exporter import Exporter, ExportError

RATE = 8000
SEG = 8000


def build_buffer(config, db, total: int = 24000) -> np.ndarray:
    i = np.arange(total, dtype=np.int64)
    left = (i % 30000) - 15000
    right = ((i * 7) % 30000) - 15000
    data = np.stack([left, right], axis=1).astype(np.int16)

    w = SegmentWriter(db, config.buffer_dir, RATE, 2, segment_frames=SEG)
    w.append(data)
    w.close()
    return data


def make_session(db, start: int, end: int) -> int:
    sid = db.create_session(start, utcnow_iso())
    db.close_session(sid, end, utcnow_iso())
    return sid


def test_export_extracts_exact_range(config, db):
    data = build_buffer(config, db)
    sid = make_session(db, 5000, 21000)  # crosses all three segments

    rec = Exporter(config, db).export(sid)

    out, rate = sf.read(config.recordings_dir / rec["filename"],
                        dtype="int16", always_2d=True)
    assert rate == RATE
    assert np.array_equal(out, data[5000:21000])
    assert rec["duration_s"] == pytest.approx(16000 / RATE)
    assert rec["has_gaps"] == 0
    assert db.get_session(sid)["state"] == "saved"


def test_export_measures_the_range_it_wrote(config, db):
    """A keeper's playback gain comes from its own audio, not from the
    segments it happened to be carved out of."""
    build_buffer(config, db)
    sid = make_session(db, 5000, 21000)

    rec = Exporter(config, db).export(sid)

    out, _rate = sf.read(config.recordings_dir / rec["filename"],
                         dtype="int16", always_2d=True)
    x = out.astype(np.float64) / 32768.0
    assert rec["mean_sq"] == pytest.approx(float((x * x).mean()), rel=1e-6)
    assert 0.0 < rec["short_peak"] <= 1.0


def test_export_releases_fully_covered_segments(config, db):
    build_buffer(config, db)
    sid = make_session(db, 5000, 21000)
    Exporter(config, db).export(sid)

    segs = db.list_segments()
    released = [s["start_sample"] for s in segs if s["released_at"] is not None]
    assert released == [8000]  # only the fully-covered middle segment


def test_export_rejects_non_ended_session(config, db):
    build_buffer(config, db)
    sid = db.create_session(0, utcnow_iso())  # still active

    with pytest.raises(ExportError):
        Exporter(config, db).export(sid)
    assert db.get_session(sid)["state"] == "active"


def test_export_with_missing_head_marks_gaps(config, db):
    data = build_buffer(config, db)
    first = db.list_segments()[0]
    (config.buffer_dir / first["filename"]).unlink()
    db.delete_segment(first["id"])

    sid = make_session(db, 5000, 21000)
    rec = Exporter(config, db).export(sid)

    out, _ = sf.read(config.recordings_dir / rec["filename"],
                     dtype="int16", always_2d=True)
    assert np.array_equal(out, data[8000:21000])
    assert rec["has_gaps"] == 1


def test_export_24bit_roundtrip(config, db):
    config = dataclasses.replace(
        config, audio=AudioConfig(sample_rate=RATE, channels=2, bit_depth=24))
    # MSB-justified 24-bit samples: int32 values in multiples of 256.
    i = np.arange(16000, dtype=np.int64)
    data = (np.stack([i % 60000 - 30000, (i * 7) % 60000 - 30000], axis=1)
            .astype(np.int32) << 8)
    w = SegmentWriter(db, config.buffer_dir, RATE, 2, segment_frames=SEG,
                      subtype=config.audio.flac_subtype)
    w.append(data)
    w.close()
    sid = make_session(db, 3000, 13000)

    rec = Exporter(config, db).export(sid)

    path = config.recordings_dir / rec["filename"]
    assert sf.info(path).subtype == "PCM_24"
    out, _ = sf.read(path, dtype="int32", always_2d=True)
    assert np.array_equal(out, data[3000:13000])


def test_failed_export_leaves_session_retryable(config, db):
    build_buffer(config, db)
    sid = make_session(db, 30000, 40000)  # range with no buffered audio

    with pytest.raises(ExportError):
        Exporter(config, db).export(sid)
    assert db.get_session(sid)["state"] == "ended"
    assert not list(config.recordings_dir.glob("*.part"))
