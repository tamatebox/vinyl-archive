"""Ring buffer segment writer: fixed-length FLAC segments named by the
absolute sample counter."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

from ..db import Database, utcnow_iso

log = logging.getLogger(__name__)


class SegmentWriter:
    """Writes interleaved integer frames into rotating FLAC segments.

    All methods except ``flushed_end``, ``request_rotate``,
    ``set_segment_frames`` and ``mark_discontinuity`` must be called from the
    capture thread only.
    """

    def __init__(self, db: Database, buffer_dir: Path, sample_rate: int,
                 channels: int, segment_frames: int,
                 on_rotate: Callable[[], None] | None = None,
                 subtype: str = "PCM_16"):
        self._db = db
        self._dir = buffer_dir
        self._rate = sample_rate
        self._channels = channels
        self._subtype = subtype
        self._segment_frames = segment_frames
        self.on_rotate = on_rotate

        self._pos = db.max_end_sample()  # absolute sample counter
        self._flushed_end = self._pos
        self._flush_lock = threading.Lock()
        self._rotate_requested = threading.Event()
        self._pending_discontinuity = self._pos > 0

        self._sf: sf.SoundFile | None = None
        self._seg_path: Path | None = None
        self._seg_start = 0
        self._seg_wall = ""
        self._seg_disc = False

    @property
    def position(self) -> int:
        """Absolute sample counter (end of all appended audio)."""
        return self._pos

    def flushed_end(self) -> int:
        """End sample of data durably closed into registered segments."""
        with self._flush_lock:
            return self._flushed_end

    def request_rotate(self) -> None:
        """Ask the capture thread to close the current segment early."""
        self._rotate_requested.set()

    def set_segment_frames(self, segment_frames: int) -> None:
        """Change the rotation length (safe from any thread).

        Each segment records its own length in the DB, so segments written
        before and after the change coexist without special handling; the
        open segment keeps its original target.
        """
        self._segment_frames = max(1, segment_frames)

    def mark_discontinuity(self) -> None:
        """Flag the next segment as following a capture gap."""
        self._pending_discontinuity = True

    def skip(self, n_frames: int) -> None:
        """Advance the sample counter without writing (silence gating).

        Closes any open segment first — a segment's content must be
        contiguous samples. The skipped range will never be written, so it
        counts as flushed.
        """
        if n_frames <= 0:
            return
        self._rotate()
        self._pos += n_frames
        with self._flush_lock:
            self._flushed_end = self._pos

    def append(self, frames: np.ndarray) -> None:
        offset = 0
        while offset < len(frames):
            if self._sf is None:
                self._open()
            space = self._seg_start + self._segment_frames - self._pos
            take = min(space, len(frames) - offset)
            self._sf.write(frames[offset:offset + take])
            self._pos += take
            offset += take
            if self._pos >= self._seg_start + self._segment_frames:
                self._rotate()
        if self._rotate_requested.is_set():
            self._rotate()

    def close(self) -> None:
        """Finalize and register the current partial segment (end of run)."""
        self._rotate()

    def _open(self) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        name = f"seg_{self._pos:016d}_{ts}.flac"
        self._seg_path = self._dir / name
        self._seg_start = self._pos
        self._seg_wall = utcnow_iso()
        self._seg_disc = self._pending_discontinuity
        self._pending_discontinuity = False
        self._sf = sf.SoundFile(str(self._seg_path), "w", samplerate=self._rate,
                                channels=self._channels, subtype=self._subtype,
                                format="FLAC")

    def _rotate(self) -> None:
        self._rotate_requested.clear()
        if self._sf is None:
            return
        n_frames = self._pos - self._seg_start
        self._sf.close()
        self._sf = None
        if n_frames <= 0:
            self._seg_path.unlink(missing_ok=True)
            return
        self._db.add_segment(self._seg_path.name, self._seg_start, n_frames,
                             self._seg_wall, self._seg_disc)
        with self._flush_lock:
            self._flushed_end = self._pos
        log.debug("segment closed: %s (%d frames)", self._seg_path.name, n_frames)
        if self.on_rotate:
            self.on_rotate()
