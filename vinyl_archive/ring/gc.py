"""Ring buffer garbage collection.

Policy, applied on every segment rotation:
1. Segments released by a completed export are deleted after a grace period.
2. Over the segment cap, oldest segments are deleted; if that eats into an
   unsaved session, the session is marked truncated (or expired).
3. If the recordings volume drops below the free-space floor, discarded
   recordings are given up, oldest discard first.
4. If the buffer volume drops below the floor, oldest segments go regardless.

Free space is the only thing that ever makes bytes go, and 3 comes before 4
on purpose: the trash holds what has already been declared unwanted, while a
buffer segment may still be wanted. Where both live on one volume that
ordering is what spends the trash first; where recordings sit on their own
drive the two floors are simply independent. A kept recording that is not in
the trash is never touched.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from ..config import RingConfig
from ..db import Database

log = logging.getLogger(__name__)


class RingGC:
    def __init__(self, db: Database, buffer_dir: Path, cfg: RingConfig,
                 recordings_dir: Path):
        self._db = db
        self._dir = buffer_dir
        self._recordings_dir = recordings_dir
        self._cfg = cfg

    def reconfigure(self, cfg: RingConfig) -> None:
        """Apply new ring policy; takes effect on the next collect()."""
        self._cfg = cfg

    def collect(self) -> None:
        try:
            self._collect()
        except Exception:
            # GC must never take down the capture thread.
            log.exception("ring GC failed")

    def free_mb(self) -> float:
        """Free space on the buffer's volume — what status() reports."""
        return self._free_mb(self._dir)

    @staticmethod
    def _free_mb(path: Path) -> float:
        return shutil.disk_usage(path).free / (1024 * 1024)

    def _collect(self) -> None:
        now = time.time()

        # 1. Early deletion of segments fully covered by saved recordings.
        for seg in self._db.list_segments():
            if (seg["released_at"] is not None
                    and now - seg["released_at"] >= self._cfg.released_grace_seconds
                    and not self._is_protected(seg)):
                self._delete(seg, "released")

        # 2. Enforce the segment cap, oldest first.
        segs = self._db.list_segments()
        excess = len(segs) - self._cfg.max_segments
        for seg in segs:
            if excess <= 0:
                break
            if self._is_protected(seg):
                self._truncate_sessions(seg)
            self._delete(seg, "over cap")
            excess -= 1

        # 3. Spend the trash before the buffer, on whichever volume it lives.
        self._purge_trash()

        # 4. Emergency: keep the buffer's disk from filling up.
        segs = self._db.list_segments()
        while segs and self.free_mb() < self._cfg.min_free_mb:
            seg = segs.pop(0)
            if self._is_protected(seg):
                self._truncate_sessions(seg)
            self._delete(seg, "low disk")

        # 5. Sessions whose audio is entirely gone can never be saved.
        min_start = self._db.min_start_sample()
        for sess in self._db.unsaved_sessions():
            if sess["state"] == "ended" and (
                    min_start is None or sess["end_sample"] <= min_start):
                log.info("session %d expired (audio left the buffer)", sess["id"])
                self._db.set_session_state(sess["id"], "expired")

    def _purge_trash(self) -> None:
        """Give up discarded recordings while their volume is below the floor.

        Need-driven and so self-limiting: the loop stops the moment there is
        room again, which is usually one file — a side is a few hundred MB.
        That is why there is no separate cap on how many go per pass; the
        segment eviction below has the same shape for the same reason.
        """
        rows = self._db.trashed_recordings()   # oldest discard first
        while rows and self._free_mb(self._recordings_dir) < self._cfg.min_free_mb:
            rec = rows.pop(0)
            (self._recordings_dir / rec["filename"]).unlink(missing_ok=True)
            self._db.delete_recording(rec["id"])
            log.info("purged trashed recording %s (low disk)", rec["filename"])

    def _is_protected(self, seg: dict) -> bool:
        """A segment overlapping any unsaved session must survive if possible."""
        seg_start = seg["start_sample"]
        seg_end = seg_start + seg["n_frames"]
        for sess in self._db.unsaved_sessions():
            end = sess["end_sample"]  # None = still active (open-ended)
            if seg_end > sess["start_sample"] and (end is None or seg_start < end):
                return True
        return False

    def _truncate_sessions(self, seg: dict) -> None:
        seg_end = seg["start_sample"] + seg["n_frames"]
        for sess in self._db.unsaved_sessions():
            if sess["start_sample"] >= seg_end:
                continue
            if sess["end_sample"] is not None and sess["end_sample"] <= seg_end:
                log.warning("session %d expired (fully evicted from buffer)", sess["id"])
                self._db.set_session_state(sess["id"], "expired")
            else:
                log.warning("session %d head truncated by ring buffer", sess["id"])
                self._db.mark_truncated_head(sess["id"], seg_end)

    def _delete(self, seg: dict, reason: str) -> None:
        (self._dir / seg["filename"]).unlink(missing_ok=True)
        self._db.delete_segment(seg["id"])
        log.info("deleted segment %s (%s)", seg["filename"], reason)
