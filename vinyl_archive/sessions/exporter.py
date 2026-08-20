"""Export a detected session from ring buffer segments into a saved FLAC.

Sample-accurate: segments are opened with libsndfile, seeked to the exact
offset and streamed into the output file. Capture is never paused; writes go
to a .part file that is atomically renamed on success.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import soundfile as sf

from ..config import Config
from ..db import Database

log = logging.getLogger(__name__)

CHUNK_FRAMES = 48000


class ExportError(Exception):
    pass


class Exporter:
    def __init__(self, config: Config, db: Database, manager=None):
        self._config = config
        self._db = db
        self._manager = manager  # optional CaptureManager, for tail flushing
        self._lock = threading.Lock()  # serialize exports (Pi-friendly)

    def export(self, session_id: int, label: str = "") -> dict:
        with self._lock:
            return self._export(session_id, label)

    def _export(self, session_id: int, label: str = "") -> dict:
        sess = self._db.get_session(session_id)
        if sess is None:
            raise ExportError(f"session {session_id} not found")
        if sess["state"] != "ended":
            raise ExportError(f"session {session_id} is not saveable "
                              f"(state={sess['state']})")

        start, end = sess["start_sample"], sess["end_sample"]
        self._db.set_session_state(session_id, "saving")
        part_path = None
        try:
            if self._manager is not None:
                self._manager.request_flush_to(end)

            segments = self._db.segments_overlapping(start, end)
            if not segments:
                raise ExportError("no buffered audio remains for this session")

            filename = self._make_filename(sess)
            out_path = self._config.recordings_dir / filename
            part_path = out_path.with_suffix(".flac.part")

            frames_written, has_gaps = self._copy_range(
                segments, start, end, part_path)
            if frames_written == 0:
                raise ExportError("no audio frames could be extracted")

            part_path.rename(out_path)
            part_path = None

            rate = self._config.audio.sample_rate
            rec_id = self._db.add_recording(
                filename=filename,
                session_id=session_id,
                duration_s=round(frames_written / rate, 2),
                size_bytes=out_path.stat().st_size,
                has_gaps=has_gaps or bool(sess["truncated_head"]),
                label=label,
            )
            self._db.set_session_state(session_id, "saved")
            self._release_covered_segments(start, end)
            log.info("exported session %d -> %s (%.1fs)", session_id, filename,
                     frames_written / rate)
            return self._db.get_recording(rec_id)
        except Exception:
            if part_path is not None:
                part_path.unlink(missing_ok=True)
            self._db.set_session_state(session_id, "ended")
            raise

    def _copy_range(self, segments: list[dict], start: int, end: int,
                    part_path: Path) -> tuple[int, bool]:
        audio = self._config.audio
        frames_written = 0
        has_gaps = False
        expected = max(start, segments[0]["start_sample"])
        if expected > start:
            has_gaps = True  # head already evicted from the buffer

        with sf.SoundFile(str(part_path), "w", samplerate=audio.sample_rate,
                          channels=audio.channels, subtype=audio.flac_subtype,
                          format="FLAC") as out:
            for seg in segments:
                seg_start = seg["start_sample"]
                seg_end = seg_start + seg["n_frames"]
                lo, hi = max(start, seg_start), min(end, seg_end)
                if lo >= hi:
                    continue
                if lo > expected or seg["discontinuity"]:
                    has_gaps = True
                seg_path = self._config.buffer_dir / seg["filename"]
                try:
                    with sf.SoundFile(str(seg_path)) as src:
                        src.seek(lo - seg_start)
                        remaining = hi - lo
                        while remaining > 0:
                            chunk = src.read(min(CHUNK_FRAMES, remaining),
                                             dtype=audio.frame_dtype,
                                             always_2d=True)
                            if len(chunk) == 0:
                                break
                            out.write(chunk)
                            frames_written += len(chunk)
                            remaining -= len(chunk)
                        if remaining > 0:
                            has_gaps = True
                except (OSError, sf.LibsndfileError):
                    log.warning("segment %s unreadable during export, skipping",
                                seg["filename"])
                    has_gaps = True
                expected = hi
        if expected < end:
            has_gaps = True  # tail was not flushed / already gone
        return frames_written, has_gaps

    def _make_filename(self, sess: dict) -> str:
        ts = datetime.strptime(sess["start_utc"], "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc).astimezone()
        base = ts.strftime("rec_%Y%m%d_%H%M%S")
        name = f"{base}.flac"
        n = 1
        while (self._config.recordings_dir / name).exists():
            n += 1
            name = f"{base}_{n}.flac"
        return name

    def _release_covered_segments(self, start: int, end: int) -> None:
        ids = [seg["id"] for seg in self._db.segments_overlapping(start, end)
               if seg["start_sample"] >= start
               and seg["start_sample"] + seg["n_frames"] <= end]
        self._db.mark_segments_released(ids)
