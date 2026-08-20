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
        # The format the writer and buffer segments were built with. Format
        # changes need a restart, so this never follows apply_config().
        self._running_audio = config.audio
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
        self._restart_source = threading.Event()
        self._session_lock = threading.Lock()
        self._thread: threading.Thread | None = None

        self._state = "stopped"
        self._level_dbfs = SILENCE_FLOOR_DBFS
        self._current_session_id: int | None = None
        # Set while a session (auto or manual) is open; drives silence gating.
        self._session_start_sample: int | None = None
        self._manual = False
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
            * self._running_audio.sample_rate)

    # -- API-facing --------------------------------------------------------

    def apply_config(self, config: Config) -> None:
        """Apply runtime-editable settings live (detector tuning, gating,
        capture device). Audio-format changes need a restart and are ignored
        here — the API reports that back to the caller."""
        old = self._config
        self._config = config
        self._detector.reconfigure(config.detector)
        self._gc.reconfigure(config.ring)
        # Segment length is derived from the *running* rate: a pending
        # sample-rate change must not resize segments before the restart.
        self._writer.set_segment_frames(
            config.ring.segment_seconds * self._running_audio.sample_rate)
        self._block_frames = max(
            1, config.detector.block_ms * self._running_audio.sample_rate // 1000)
        self._gate_frames = self._compute_gate_frames()
        if (config.capture.device != old.capture.device
                or config.detector.block_ms != old.detector.block_ms):
            # Cycle the source process: the device is opened once per run, and
            # the recorder reads its block size once per run.
            self._restart_source.set()

    def running_config(self):
        """The audio format capture is actually running with."""
        return self._running_audio

    def flushed_end(self) -> int:
        """End sample of audio durably closed into segments."""
        return self._writer.flushed_end()

    def gate_state(self) -> tuple[bool, int]:
        """(session open, its start sample) — what the recorder gates on."""
        start = self._session_start_sample
        return start is not None, start or 0

    # -- manual recording --------------------------------------------------

    def manual_start(self) -> int | None:
        """Begin an explicit recording, taking over from auto-detection.

        An auto session already in progress is promoted (keeping its
        pre-roll) rather than duplicated. Returns the session id, or None if
        capture is not running.
        """
        with self._session_lock:
            if self._state != "recording" or self._manual:
                return self._current_session_id if self._manual else None
            self._manual = True
            if self._current_session_id is not None:
                self._db.set_session_kind(self._current_session_id, "manual")
                log.info("session %d promoted to manual", self._current_session_id)
                return self._current_session_id

            pos = self._writer.position
            preroll = int(self._config.detector.preroll_seconds
                          * self._config.audio.sample_rate)
            start = max(0, pos - preroll)  # lead-in still sits in the RAM delay line
            self._current_session_id = self._db.create_session(
                start, self._wall_at(start), kind="manual")
            self._session_start_sample = start
            log.info("manual session %d started at sample %d",
                     self._current_session_id, start)
            return self._current_session_id

    def manual_stop(self) -> int | None:
        """End the explicit recording; auto-detection resumes afterwards."""
        with self._session_lock:
            if not self._manual:
                return None
            session_id = self._current_session_id
            pos = self._writer.position
            if session_id is not None:
                self._db.close_session(session_id, pos, self._wall_at(pos))
                log.info("manual session %d ended at sample %d", session_id, pos)
            self._current_session_id = None
            self._session_start_sample = None
            # Re-arm the detector so backup coverage resumes immediately even
            # if playback continues (callbacks are still suppressed here).
            self._detector.force_close(pos)
            self._manual = False
            return session_id

    @property
    def manual_recording(self) -> bool:
        return self._manual

    def status(self) -> dict:
        buffered = self._db.buffered_frames()
        # The running format, not the configured one: buffered audio is in the
        # format capture actually started with.
        audio = self._running_audio
        rate = audio.sample_rate
        return {
            "capture": self._state,
            "level_dbfs": round(self._level_dbfs, 1),
            "active_session_id": self._current_session_id,
            "manual_recording": self._manual,
            "device": self._config.capture.device,
            "format": {
                "sample_rate": audio.sample_rate,
                "channels": audio.channels,
                "bit_depth": audio.bit_depth,
            },
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

    # A manual recording owns the session; detection keeps running (to re-arm
    # correctly) but must not open, close or discard sessions behind its back.

    def _on_session_start(self, start_sample: int) -> None:
        if self._manual:
            return
        self._current_session_id = self._db.create_session(
            start_sample, self._wall_at(start_sample))
        self._session_start_sample = start_sample
        log.info("session %d started at sample %d",
                 self._current_session_id, start_sample)

    def _on_session_end(self, end_sample: int) -> None:
        if self._manual or self._current_session_id is None:
            return
        self._db.close_session(self._current_session_id, end_sample,
                               self._wall_at(end_sample))
        log.info("session %d ended at sample %d",
                 self._current_session_id, end_sample)
        self._current_session_id = None
        self._session_start_sample = None

    def _on_session_discard(self) -> None:
        if self._manual:
            return
        if self._current_session_id is not None:
            self._db.delete_session(self._current_session_id)
            self._current_session_id = None
        self._session_start_sample = None

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
        backoff = self._config.capture.restart_backoff_min_s
        while not self._stop.is_set():
            cap = self._config.capture  # re-read: the device is editable live
            if not self._enabled.is_set():
                self._set_state("stopped")
                self._stop.wait(0.5)
                continue

            if cap.uses_alsa and not alsa_device_present(cap.device):
                self._set_state("waiting_device")
                self._stop.wait(2.0)
                continue

            self._restart_source.clear()
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
                should_stop=lambda: (self._stop.is_set()
                                     or not self._enabled.is_set()
                                     or self._restart_source.is_set()),
                gate_frames=lambda: self._gate_frames,
                gate_state=self.gate_state,
            )
            try:
                recorder.run()
            except Exception:
                log.exception("capture loop crashed")
            finally:
                source.stop()
                self._writer.close()
                if self._manual:
                    self.manual_stop()  # a capture gap ends an explicit take
                self._detector.force_close(self._writer.position)
                self._gc.collect()

            if self._stop.is_set() or not self._enabled.is_set():
                continue

            if self._restart_source.is_set():
                log.info("restarting capture source (settings changed)")
                continue

            if time.time() - run_started > 60:
                backoff = cap.restart_backoff_min_s
            log.warning("capture source exited; restarting in %.0fs", backoff)
            self._set_state("restarting")
            self._stop.wait(backoff)
            backoff = min(backoff * 2, cap.restart_backoff_max_s)

        self._set_state("stopped")
