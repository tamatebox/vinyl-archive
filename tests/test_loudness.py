"""Playback gain: what a quiet transfer asks for, and what it must not ask for.

The reason the ceiling is measured over a window rather than over the loudest
sample is vinyl clicks: a scratch must not decide the level of a whole side.
"""

import math

import numpy as np
import pytest

from vinyl_archive.db import Database, utcnow_iso
from vinyl_archive.ring.writer import SegmentWriter
from vinyl_archive.sessions.loudness import (CEILING_DBFS, MAX_GAIN_DB,
                                             MIN_GAIN_DB, TARGET_RMS_DBFS,
                                             LevelMeter, auto_gain_db,
                                             window_frames)

RATE = 8000
SEG = 8000
FS = 32768.0
WINDOW = window_frames(RATE)


def tone(n: int, dbfs: float, channels: int = 2) -> np.ndarray:
    """Sine at a given RMS level, as int16 frames."""
    amp = 10 ** (dbfs / 20) * math.sqrt(2) * FS
    x = amp * np.sin(2 * np.pi * 100 * np.arange(n) / RATE)
    return np.repeat(x.astype(np.int16)[:, None], channels, axis=1)


def levels(*blocks: np.ndarray) -> tuple[float, float]:
    meter = LevelMeter(WINDOW)
    for block in blocks:
        meter.add(block)
    return meter.levels()


def gain(*blocks: np.ndarray) -> float:
    return auto_gain_db(*levels(*blocks))


def rms_dbfs(mean_sq: float) -> float:
    return 10.0 * math.log10(mean_sq)


def peak_dbfs(short_peak: float) -> float:
    return 20.0 * math.log10(short_peak)


def test_a_steady_tone_measures_at_its_own_level():
    short_peak, mean_sq = levels(tone(RATE, -20.0))
    assert rms_dbfs(mean_sq) == pytest.approx(-20.0, abs=0.1)
    # A steady signal's loudest window is the signal itself.
    assert peak_dbfs(short_peak) == pytest.approx(-20.0, abs=0.3)


def test_levels_are_identical_for_16_and_24_bit():
    """MSB-justified int32 must measure the same as int16, or a 24-bit install
    would get a wildly different gain from the same audio."""
    as16 = tone(RATE, -20.0)
    as32 = as16.astype(np.int32) << 16
    for a, b in zip(levels(as16), levels(as32)):
        assert b == pytest.approx(a, rel=1e-4)


def test_quiet_transfer_is_lifted_to_the_rms_target():
    short_peak, mean_sq = levels(tone(RATE, -34.0))
    assert auto_gain_db(short_peak, mean_sq) == pytest.approx(16.0, abs=0.2)
    assert rms_dbfs(mean_sq) + 16.0 == pytest.approx(TARGET_RMS_DBFS, abs=0.2)


def test_a_single_click_does_not_hold_a_quiet_side_down():
    """Peak normalisation would hand this side ~0 dB: the scratch alone sits a
    hair under full scale. The windowed ceiling is what keeps the boost."""
    side = tone(RATE, -30.0)
    side[500] = 32000  # scratch

    assert gain(side) == pytest.approx(11.5, abs=1.0)
    # For contrast: what a true-peak ceiling would have allowed.
    true_peak_dbfs = 20 * math.log10(32000 / FS)
    assert CEILING_DBFS - true_peak_dbfs < 1.0


def test_a_loud_passage_does_hold_the_boost_back():
    """The ceiling still binds where it should: mostly-quiet audio with one
    loud stretch is levelled on the loud stretch, not on its average."""
    quiet, loud = tone(RATE * 9, -40.0), tone(RATE, -10.0)

    short_peak, mean_sq = levels(quiet, loud)
    g = gain(quiet, loud)

    assert g < TARGET_RMS_DBFS - rms_dbfs(mean_sq)  # target overruled...
    assert peak_dbfs(short_peak) + g == pytest.approx(CEILING_DBFS, abs=0.3)


def test_hot_transfer_is_pulled_down():
    assert gain(tone(RATE, -10.0)) < 0.0


def test_gain_is_clamped_both_ways():
    assert gain(tone(RATE, -80.0)) == MAX_GAIN_DB
    hot = np.full((RATE, 2), 32000, dtype=np.int16)  # full-scale square
    hot[1::2] = -32000
    assert gain(hot) == MIN_GAIN_DB


def test_unknown_levels_play_at_unity():
    """Rows from before this existed, or segments reconcile re-registered."""
    assert auto_gain_db(None, None) == 0.0
    assert auto_gain_db(0.0, 0.0) == 0.0
    assert auto_gain_db(0.5, None) == 0.0


def test_block_size_does_not_change_the_measurement():
    """Blocks arrive at whatever size the recorder or exporter reads, and a
    window straddling a boundary must not be measured twice or dropped."""
    side = tone(RATE, -25.0)
    side[3000:3010] = 30000  # a burst that lands mid-window
    whole = levels(side)
    split = levels(*[side[i:i + 333] for i in range(0, len(side), 333)])
    for a, b in zip(whole, split):
        assert b == pytest.approx(a, rel=1e-6)


def test_empty_meter_reports_nothing_rather_than_silence():
    assert LevelMeter(WINDOW).levels() == (None, None)


def test_tail_shorter_than_a_window_still_counts():
    meter = LevelMeter(WINDOW)
    meter.add(tone(WINDOW // 2, -20.0))
    short_peak, mean_sq = meter.levels()
    assert peak_dbfs(short_peak) == pytest.approx(-20.0, abs=0.5)
    assert rms_dbfs(mean_sq) == pytest.approx(-20.0, abs=0.5)


# -- through the writer and the DB --------------------------------------------

def test_writer_stores_levels_per_segment(config, db):
    w = SegmentWriter(db, config.buffer_dir, RATE, 2, segment_frames=SEG)
    w.append(tone(SEG, -30.0))
    w.append(tone(SEG, -12.0))
    w.close()

    segs = db.list_segments()
    assert [round(rms_dbfs(s["mean_sq"])) for s in segs] == [-30, -12]
    assert all(0.0 < s["short_peak"] <= 1.0 for s in segs)


def test_level_in_range_averages_energy_over_the_covered_segments(config, db):
    w = SegmentWriter(db, config.buffer_dir, RATE, 2, segment_frames=SEG)
    w.append(tone(SEG, -30.0))
    w.append(tone(SEG, -30.0))
    w.append(tone(SEG, -12.0))
    w.close()

    short_peak, mean_sq = db.level_in_range(0, 2 * SEG)
    assert rms_dbfs(mean_sq) == pytest.approx(-30.0, abs=0.1)

    # The loud third segment only counts once the range reaches it.
    _peak, all_three = db.level_in_range(0, 3 * SEG)
    assert all_three > mean_sq


def test_unmeasured_segments_sit_out_of_the_average(config, db):
    """reconcile re-registers orphans without levels; they must not drag the
    average toward silence and hand back a bogus 24 dB."""
    w = SegmentWriter(db, config.buffer_dir, RATE, 2, segment_frames=SEG)
    w.append(tone(SEG, -30.0))
    w.close()
    db.add_segment("seg_0000000000008000_20260101T000000.flac", SEG, SEG,
                   utcnow_iso())

    _peak, mean_sq = db.level_in_range(0, 2 * SEG)
    assert rms_dbfs(mean_sq) == pytest.approx(-30.0, abs=0.1)


def test_range_with_no_measured_segments_yields_no_gain(db):
    assert db.level_in_range(0, 1000) == (None, None)
    assert auto_gain_db(*db.level_in_range(0, 1000)) == 0.0
