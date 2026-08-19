"""Silence-based session detection: a hysteresis state machine over per-block
RMS levels, working entirely in absolute sample coordinates.

The core trick for vinyl: a session only ends after ``end_silence_seconds``
(default 25 s) of continuous silence, so 2–5 s track gaps never split a side.
"""

from __future__ import annotations

import logging
import math
from typing import Callable

import numpy as np

from ..config import DetectorConfig

log = logging.getLogger(__name__)

SILENCE_FLOOR_DBFS = -120.0


def rms_dbfs(frames: np.ndarray) -> float:
    """RMS level in dBFS of an integer frame block (any channel count).

    Full scale is taken from the dtype, so int16 and MSB-justified int32
    (24-bit capture) blocks measure identically.
    """
    if len(frames) == 0:
        return SILENCE_FLOOR_DBFS
    x = frames.astype(np.float64) / -float(np.iinfo(frames.dtype).min)
    rms = math.sqrt(float(np.mean(x * x)))
    if rms <= 0.0:
        return SILENCE_FLOOR_DBFS
    return max(SILENCE_FLOOR_DBFS, 20.0 * math.log10(rms))


class SilenceDetector:
    """Callbacks:
    - on_start(start_sample): a session began (preroll already applied)
    - on_end(end_sample): the current session ended (postroll applied)
    - on_discard(): the current session was too short and should be dropped
    """

    def __init__(self, cfg: DetectorConfig, sample_rate: int,
                 on_start: Callable[[int], None],
                 on_end: Callable[[int], None],
                 on_discard: Callable[[], None]):
        self._on_start = on_start
        self._on_end = on_end
        self._on_discard = on_discard
        self._rate = sample_rate
        self.reconfigure(cfg)

        self._active = False
        self._run_start: int | None = None  # start of current loud run (IDLE)
        self._session_start = 0
        self._last_loud = 0

    def reconfigure(self, cfg: DetectorConfig) -> None:
        """Apply new tuning live (from any thread); an in-progress session's
        state is untouched — only future decisions use the new values."""
        self._start_threshold = cfg.start_threshold_dbfs
        self._stop_threshold = cfg.stop_threshold_dbfs
        self._hold_frames = int(cfg.start_hold_seconds * self._rate)
        self._end_silence_frames = int(cfg.end_silence_seconds * self._rate)
        self._preroll_frames = int(cfg.preroll_seconds * self._rate)
        self._postroll_frames = int(cfg.postroll_seconds * self._rate)
        self._min_frames = int(cfg.min_session_seconds * self._rate)

    @property
    def active(self) -> bool:
        return self._active

    @property
    def session_start(self) -> int:
        return self._session_start

    def process_block(self, block_start: int, n_frames: int, level_dbfs: float) -> None:
        block_end = block_start + n_frames
        if not self._active:
            if level_dbfs > self._start_threshold:
                if self._run_start is None:
                    self._run_start = block_start
                if block_end - self._run_start >= self._hold_frames:
                    self._session_start = max(0, self._run_start - self._preroll_frames)
                    self._last_loud = block_end
                    self._active = True
                    self._on_start(self._session_start)
            else:
                self._run_start = None
        else:
            if level_dbfs > self._stop_threshold:
                self._last_loud = block_end
            elif block_end - self._last_loud >= self._end_silence_frames:
                self._finish(self._last_loud + self._postroll_frames)

    def force_close(self, at_sample: int) -> None:
        """Close any active session immediately (capture gap / shutdown)."""
        self._run_start = None
        if not self._active:
            return
        self._finish(min(at_sample, self._last_loud + self._postroll_frames))

    def _finish(self, end_sample: int) -> None:
        self._active = False
        self._run_start = None
        if end_sample - self._session_start >= self._min_frames:
            self._on_end(end_sample)
        else:
            log.debug("session discarded (%.1fs, below minimum)",
                      (end_sample - self._session_start))
            self._on_discard()
