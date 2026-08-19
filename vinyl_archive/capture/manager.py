"""Capture supervisor: owns the writer/detector/GC, keeps the source process
alive, and exposes status to the API layer."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from ..config import Config
from ..db import Database
from ..ring.gc import RingGC
from ..ring.writer import SegmentWriter
from ..sessions.detector import SILENCE_FLOOR_DBFS, SilenceDetector
from .recorder import CaptureRecorder
from .source import SubprocessSource, alsa_device_present

log = logging.getLogger(__name__)


class CaptureManager:
    """States: stopped / waiting_device / recording / restarting."""

    def __init__(self, config: Config, db: Database):
        self._config = config
        self._db = db
        rate = config.audio.sample_rate

        self._gc = RingGC(db, config.buffer_dir, config.ring)
        self._writer = SegmentWriter(
            db, config.buffer_dir, rate, config.audio.channels,
            segment_frames=config.ring.segment_seconds * rate,
            on_rotate=self._gc.collect,
            subtype=config.audio.flac_subtype,
        )
        self._detector = SilenceDetector(
            config.detector, rate,
            on_start=self._on_session_start,
            on_end=self._on_session_end,
            on_discard=self._on_session_discard,
        )
        self._block_frames = max(1, config.detector.block_ms * rate // 1000)
        self._gate_frames = self._compute_gate_frames()

        self._enabled = threading.Event()
        if config.capture.auto_start:
            self._enabled.set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._state = "stopped"
        self._level_dbfs = SILENCE_FLOOR_DBFS
        self._current_session_id: int | None = None
        self._anchor_wall = time.time()
        self._anchor_sample = self._writer.position
        self._started_at = time.time()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="capture-supervisor",
                                        daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def enable(self) -> None:
        self._enabled.set()

    def disable(self) -> None:
        self._enabled.clear()

    def _compute_gate_frames(self) -> int | None:
        """Size of the recorder's RAM delay line, or None when gating is off.

        Must cover preroll + start hold so a session's preroll audio is
        still unskipped when the detector fires.
        """
        cfg = self._config
        if not cfg.capture.silence_gating:
            return None
        return self._block_frames + int(
            (cfg.detector.preroll_seconds + cfg.detector.start_hold_seconds)
            * cfg.audio.sample_rate)

    # -- API-facing --------------------------------------------------------

    def apply_config(self, config: Config) -> None:
        """Apply runtime-editable settings live (detector tuning, gating)."""
        self._config = config
        self._detector.reconfigure(config.detector)
        self._gate_frames = self._compute_gate_frames()

    def status(self) -> dict:
        buffered = self._db.buffered_frames()
        rate = self._config.audio.sample_rate
        return {
            "capture": self._state,
            "level_dbfs": round(self._level_dbfs, 1),
            "active_session_id": self._current_session_id,
            "buffer": {
                "seconds": round(buffered / rate, 1),
                "capacity_seconds": self._config.ring.max_segments
                                    * self._config.ring.segment_seconds,
                "segments": len(self._db.list_segments()),
                "disk_free_mb": round(self._gc.free_mb(), 1),
            },
            "uptime_s": round(time.time() - self._started_at, 1),
        }

    def request_flush_to(self, sample: int, timeout: float = 5.0) -> bool:
        """Ensure buffered audio up to `sample` is closed into segments.

        Used by the exporter when a session's tail is still in the open
        segment. Returns True once flushed_end covers the requested sample.
        """
        deadline = time.time() + timeout
        while self._writer.flushed_end() < sample:
            if self._state != "recording":
                # No capture loop running to service the request; whatever was
                # buffered has already been closed by writer.close().
                break
            self._writer.request_rotate()
            if time.time() >= deadline:
                break
            time.sleep(0.1)
        return self._writer.flushed_end() >= sample

    # -- session callbacks (capture thread) --------------------------------

    def _wall_at(self, sample: int) -> str:
        ts = self._anchor_wall + (sample - self._anchor_sample) \
            / self._config.audio.sample_rate
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _on_session_start(self, start_sample: int) -> None:
        self._current_session_id = self._db.create_session(
            start_sample, self._wall_at(start_sample))
        log.info("session %d started at sample %d",
                 self._current_session_id, start_sample)

    def _on_session_end(self, end_sample: int) -> None:
        if self._current_session_id is None:
            return
        self._db.close_session(self._current_session_id, end_sample,
                               self._wall_at(end_sample))
        log.info("session %d ended at sample %d",
                 self._current_session_id, end_sample)
        self._current_session_id = None

    def _on_session_discard(self) -> None:
        if self._current_session_id is not None:
            self._db.delete_session(self._current_session_id)
            self._current_session_id = None

    # -- supervisor loop ----------------------------------------------------

    def _set_state(self, state: str) -> None:
        if state != self._state:
            log.info("capture state: %s -> %s", self._state, state)
        self._state = state
        if state != "recording":
            self._level_dbfs = SILENCE_FLOOR_DBFS

    def _set_level(self, level: float) -> None:
        self._level_dbfs = level

    def _run(self) -> None:
        cap = self._config.capture
        backoff = cap.restart_backoff_min_s
        while not self._stop.is_set():
            if not self._enabled.is_set():
                self._set_state("stopped")
                self._stop.wait(0.5)
                continue

            if cap.uses_alsa and not alsa_device_present(cap.device):
                self._set_state("waiting_device")
                self._stop.wait(2.0)
                continue

            source = SubprocessSource(cap.build_command(self._config.audio))
            try:
                source.start()
            except OSError:
                log.exception("failed to start capture command")
                self._set_state("restarting")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, cap.restart_backoff_max_s)
                continue

            if self._writer.position > 0:
                self._writer.mark_discontinuity()
            self._anchor_wall = time.time()
            self._anchor_sample = self._writer.position
            self._set_state("recording")
            run_started = time.time()

            recorder = CaptureRecorder(
                source, self._writer, self._detector,
                self._block_frames, self._config.audio,
                on_level=self._set_level,
                should_stop=lambda: self._stop.is_set() or not self._enabled.is_set(),
                gate_frames=lambda: self._gate_frames,
            )
            try:
                recorder.run()
            except Exception:
                log.exception("capture loop crashed")
            finally:
                source.stop()
                self._writer.close()
                self._detector.force_close(self._writer.position)
                self._gc.collect()

            if self._stop.is_set() or not self._enabled.is_set():
                continue

            if time.time() - run_started > 60:
                backoff = cap.restart_backoff_min_s
            log.warning("capture source exited; restarting in %.0fs", backoff)
            self._set_state("restarting")
            self._stop.wait(backoff)
            backoff = min(backoff * 2, cap.restart_backoff_max_s)

        self._set_state("stopped")
