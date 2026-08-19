"""Capture recorder gating tests: idle silence must never reach disk, while
sessions — including track gaps inside them — are written contiguously from
the exact preroll sample."""

import numpy as np
import soundfile as sf

from vinyl_archive.capture.recorder import CaptureRecorder, decode_pcm
from vinyl_archive.config import AudioConfig, DetectorConfig
from vinyl_archive.ring.writer import SegmentWriter
from vinyl_archive.sessions.detector import SilenceDetector

RATE = 8000
BLOCK = 800  # 100 ms
SEG = 8000

DET = DetectorConfig(start_hold_seconds=0.2, end_silence_seconds=1.0,
                     preroll_seconds=0.5, postroll_seconds=0.2,
                     min_session_seconds=0.5)

GATE = BLOCK + int((DET.preroll_seconds + DET.start_hold_seconds) * RATE)

AUDIO = [(3.0, 0),     # idle silence      [0, 24000)
         (1.5, 8000),  # music             [24000, 36000)
         (0.5, 0),     # track gap         [36000, 40000)
         (1.5, 8000),  # music             [40000, 52000)
         (3.0, 0)]     # idle silence      [52000, 76000)
TOTAL = 76000

SESSION_START = 24000 - int(DET.preroll_seconds * RATE)
END_SAMPLE = 52000 + int(DET.postroll_seconds * RATE)
# The detector needs end_silence_seconds past the last loud block to confirm
# the end, so writing continues (at most) until that confirmation.
END_CONFIRMED = 52000 + int(DET.end_silence_seconds * RATE)


class FakeSource:
    def __init__(self, data: bytes):
        self._data, self._off = data, 0

    def read_exact(self, nbytes: int) -> bytes:
        chunk = self._data[self._off:self._off + nbytes]
        self._off += len(chunk)
        return chunk


def pcm(seconds: float, amplitude: int) -> np.ndarray:
    return np.full((int(seconds * RATE), 2), amplitude, dtype=np.int16)


def run_recorder(config, db, gate_frames, raw=None,
                 audio=AudioConfig(sample_rate=RATE, channels=2)):
    writer = SegmentWriter(db, config.buffer_dir, RATE, 2, segment_frames=SEG,
                           subtype=audio.flac_subtype)
    events = {"starts": [], "ends": []}
    detector = SilenceDetector(DET, RATE,
                               on_start=events["starts"].append,
                               on_end=events["ends"].append,
                               on_discard=lambda: None)
    if raw is None:
        raw = np.concatenate([pcm(s, a) for s, a in AUDIO]).tobytes()
    CaptureRecorder(FakeSource(raw), writer, detector, BLOCK, audio,
                    on_level=lambda level: None,
                    should_stop=lambda: False,
                    gate_frames=lambda: gate_frames).run()
    writer.close()
    detector.force_close(writer.position)
    return writer, events


def coverage(db) -> tuple[int, int]:
    segs = db.list_segments()
    for a, b in zip(segs, segs[1:]):
        assert a["start_sample"] + a["n_frames"] == b["start_sample"], "coverage gap"
    return segs[0]["start_sample"], segs[-1]["start_sample"] + segs[-1]["n_frames"]


def test_gating_skips_idle_silence_but_keeps_track_gaps(config, db):
    _, events = run_recorder(config, db, gate_frames=GATE)
    assert events["starts"] == [SESSION_START]
    assert events["ends"] == [END_SAMPLE]

    lo, hi = coverage(db)  # contiguous: the track gap did not stop writing
    assert lo == SESSION_START          # idle head skipped, preroll preserved
    assert END_SAMPLE <= hi <= END_CONFIRMED


def test_gating_disabled_writes_everything(config, db):
    writer, _ = run_recorder(config, db, gate_frames=None)
    assert coverage(db) == (0, TOTAL)
    assert writer.position == TOTAL


# -- 24-bit capture -------------------------------------------------------------


def test_decode_pcm_24bit_values_and_sign():
    raw = bytes([0x01, 0x00, 0x00,   # +1        (24-bit)
                 0xff, 0xff, 0xff,   # -1
                 0x00, 0x00, 0x80])  # -2^23 (24-bit full scale negative)
    frames = decode_pcm(raw, channels=1, sample_bytes=3)
    assert frames.dtype == np.int32
    # MSB-justified: 24-bit values are carried shifted up by 8 bits.
    assert frames[:, 0].tolist() == [1 << 8, -(1 << 8), -(1 << 31)]


def test_24bit_capture_roundtrip(config, db):
    audio = AudioConfig(sample_rate=RATE, channels=2, bit_depth=24)
    tone = np.full((RATE, 2), 1 << 28, dtype=np.int32)  # ~ -18 dBFS
    raw = tone.view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()  # pack S24_3LE

    writer, _ = run_recorder(config, db, gate_frames=None, raw=raw, audio=audio)
    assert writer.position == RATE

    seg = db.list_segments()[0]
    path = config.buffer_dir / seg["filename"]
    assert sf.info(path).subtype == "PCM_24"
    out, _ = sf.read(path, dtype="int32", always_2d=True)
    assert np.array_equal(out, tone)
