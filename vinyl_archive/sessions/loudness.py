"""How much playback gain an entry needs, from levels measured at write time.

A phono stage into line-in lands 15-20 dB below full scale, so *everything*
here needs a boost — but the amount differs per pressing, which is why a fixed
slider is the wrong control. Two measurements per entry decide the gain:

- ``mean_sq``: energy over the whole entry, i.e. its RMS. The gain aims this
  at ``TARGET_RMS_DBFS`` so two records cut at different levels play back
  equally loud.
- ``short_peak``: the level of the loudest ``WINDOW_MS`` window. The gain is
  held back if it would push *that* past ``CEILING_DBFS``, which is what stops
  a genuinely loud passage from clipping.

The ceiling deliberately measures a short window rather than the loudest
single sample. Vinyl is full of clicks: one scratch a hair under full scale
would leave no headroom at all, and peak normalisation would hand back ~0 dB
for a side sitting at -30 dBFS — the exact failure this is built to avoid. A
click *is* allowed to clip after boosting; it was already a click. Sustained
audio, which spreads across a whole window, is not.

Both figures are accumulated while the audio is already in RAM — in the
segment writer during capture, in the exporter while a keeper is written — so
asking for the gain of a 25-minute side reads two DB columns instead of
decoding half a gigabyte of FLAC.

The gain is a single scalar per entry, which is what makes it free to apply:
nothing in the byte-offset-to-sample arithmetic of ``streamer.py`` moves, and
the archived file itself is never touched. Only what leaves the speakers
changes; a download is always the untouched transfer.
"""

from __future__ import annotations

import math

import numpy as np

TARGET_RMS_DBFS = -18.0
# Headroom for the loudest window. Music runs ~9-12 dB of crest above its
# short-term RMS, so a window landing here peaks around full scale.
CEILING_DBFS = -9.0
WINDOW_MS = 20
# A near-silent entry would otherwise ask for 40 dB and hand back nothing but
# hiss; a hot one does not need dragging all the way down to the target.
MAX_GAIN_DB = 24.0
MIN_GAIN_DB = -12.0


def window_frames(sample_rate: int) -> int:
    return max(1, round(sample_rate * WINDOW_MS / 1000))


class LevelMeter:
    """Running RMS energy and short-term peak over the blocks of one segment
    or export.

    Blocks arrive in whatever size the caller happens to have, so energy is
    summed rather than averaged per block, and the window straddling a block
    boundary is carried over instead of being measured twice.
    """

    def __init__(self, window: int) -> None:
        self._window = max(1, window)
        self._peak_sq = 0.0   # loudest window, mean square
        self._sq = 0.0        # total energy
        self._n = 0           # frames seen
        self._carry = np.zeros(0)

    def add(self, frames: np.ndarray) -> None:
        if len(frames) == 0:
            return
        # Full scale from the dtype, so int16 and MSB-justified int32 (24-bit
        # capture) measure identically — same convention as detector.rms_dbfs.
        x = frames.astype(np.float64) / -float(np.iinfo(frames.dtype).min)
        per_frame = (x * x).mean(axis=1) if x.ndim > 1 else x * x
        self._sq += float(per_frame.sum())
        self._n += len(per_frame)

        pending = (np.concatenate([self._carry, per_frame])
                   if len(self._carry) else per_frame)
        whole = len(pending) - len(pending) % self._window
        if whole:
            windows = pending[:whole].reshape(-1, self._window).mean(axis=1)
            self._peak_sq = max(self._peak_sq, float(windows.max()))
        self._carry = pending[whole:]

    def levels(self) -> tuple[float | None, float | None]:
        """(short_peak, mean_sq) for the DB, or (None, None) if nothing was
        seen. ``short_peak`` is an amplitude, 0..1 of full scale."""
        if self._n == 0:
            return None, None
        peak_sq = self._peak_sq
        if len(self._carry):  # ragged tail, shorter than one window
            peak_sq = max(peak_sq, float(self._carry.mean()))
        return math.sqrt(peak_sq), self._sq / self._n


def auto_gain_db(short_peak: float | None, mean_sq: float | None) -> float:
    """Playback gain in dB for an entry with these measured levels.

    0.0 when the levels are unknown — segments re-registered by ``reconcile``
    after a crash, or rows written before this was recorded — so an entry
    without measurements plays at unity rather than at a guess.
    """
    if not short_peak or not mean_sq or short_peak <= 0.0 or mean_sq <= 0.0:
        return 0.0
    rms_dbfs = 10.0 * math.log10(mean_sq)
    peak_dbfs = 20.0 * math.log10(min(short_peak, 1.0))
    gain = min(TARGET_RMS_DBFS - rms_dbfs, CEILING_DBFS - peak_dbfs)
    return round(max(MIN_GAIN_DB, min(MAX_GAIN_DB, gain)), 1)
