"""Detector state machine tests: pure logic, fed with synthetic RMS blocks."""

from vinyl_archive.config import DetectorConfig
from vinyl_archive.sessions.detector import SilenceDetector

RATE = 48000
BLOCK = RATE // 10  # 100 ms

CFG = DetectorConfig()  # defaults: -40/-48 dBFS, 25 s end silence, etc.

MUSIC = -20.0
GAP = -70.0


class Events:
    def __init__(self):
        self.starts: list[int] = []
        self.ends: list[int] = []
        self.discards = 0


def make(cfg: DetectorConfig = CFG) -> tuple[SilenceDetector, Events]:
    ev = Events()
    det = SilenceDetector(cfg, RATE, on_start=ev.starts.append,
                          on_end=ev.ends.append,
                          on_discard=lambda: setattr(ev, "discards", ev.discards + 1))
    return det, ev


def feed(det: SilenceDetector, pos: int, seconds: float, dbfs: float) -> int:
    for _ in range(int(seconds * 10)):
        det.process_block(pos, BLOCK, dbfs)
        pos += BLOCK
    return pos


def test_track_gap_does_not_split_session():
    det, ev = make()
    pos = feed(det, 0, 90, MUSIC)     # side A first track
    pos = feed(det, pos, 5, GAP)      # gap between tracks
    pos = feed(det, pos, 60, MUSIC)   # next track
    music_end = pos
    pos = feed(det, pos, 40, GAP)     # needle lifted

    assert len(ev.starts) == 1
    assert len(ev.ends) == 1
    assert ev.discards == 0
    assert ev.ends[0] == music_end + int(CFG.postroll_seconds * RATE)


def test_preroll_is_applied_to_session_start():
    det, ev = make()
    pos = feed(det, 0, 30, GAP)       # silence before playback
    loud_from = pos
    feed(det, pos, 10, MUSIC)

    assert ev.starts == [loud_from - int(CFG.preroll_seconds * RATE)]


def test_preroll_clamped_at_zero():
    det, ev = make()
    feed(det, 0, 5, MUSIC)
    assert ev.starts == [0]


def test_short_noise_is_discarded():
    det, ev = make()
    pos = feed(det, 0, 10, MUSIC)     # below 30 s minimum
    feed(det, pos, 40, GAP)

    assert len(ev.starts) == 1
    assert ev.ends == []
    assert ev.discards == 1


def test_single_block_click_does_not_start_session():
    det, ev = make()
    det.process_block(0, BLOCK, MUSIC)   # 100 ms < 300 ms hold
    feed(det, BLOCK, 5, GAP)
    assert ev.starts == []


def test_force_close_ends_active_session():
    det, ev = make()
    pos = feed(det, 0, 60, MUSIC)
    det.force_close(pos)

    assert len(ev.ends) == 1
    assert ev.ends[0] <= pos


def test_force_close_short_session_discards():
    det, ev = make()
    pos = feed(det, 0, 5, MUSIC)
    det.force_close(pos)

    assert ev.ends == []
    assert ev.discards == 1
    assert not det.active
