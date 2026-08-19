"""Per-run capture loop: read PCM blocks from the source, feed the segment
writer and the silence detector.

With silence gating (``gate_frames`` set), blocks outside an active session
are held in a small in-memory delay line and eventually skipped in the writer
instead of hitting disk. The delay line covers the detector's preroll + start
hold, so when a session begins its preroll audio is still in RAM and is
written retroactively from the exact session start sample. The absolute
sample counter advances over skipped audio, keeping coordinates continuous.
Gating follows the detector's session state, not instantaneous silence —
track gaps within a session are written like any other audio.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Callable

import numpy as np

from ..config import AudioConfig
from ..ring.writer import SegmentWriter
from ..sessions.detector import SilenceDetector, rms_dbfs
from .source import SubprocessSource

log = logging.getLogger(__name__)


def decode_pcm(data: bytes, channels: int, sample_bytes: int) -> np.ndarray:
    """Raw little-endian PCM bytes -> (n, channels) integer frames.

    24-bit (S24_3LE) samples are unpacked MSB-justified into int32: the top
    byte carries the sign, and libsndfile's int I/O on PCM_24 files uses the
    same justification, so the values pass through writer/exporter unscaled.
    """
    if sample_bytes == 2:
        return np.frombuffer(data, dtype="<i2").reshape(-1, channels)
    b = np.frombuffer(data, dtype=np.uint8).astype(np.uint32).reshape(-1, 3)
    samples = (b[:, 0] << 8) | (b[:, 1] << 16) | (b[:, 2] << 24)
    return samples.view(np.int32).reshape(-1, channels)


class CaptureRecorder:
    def __init__(self, source: SubprocessSource, writer: SegmentWriter,
                 detector: SilenceDetector, block_frames: int,
                 audio: AudioConfig,
                 on_level: Callable[[float], None],
                 should_stop: Callable[[], bool],
                 gate_frames: Callable[[], int | None] = lambda: None):
        self._source = source
        self._writer = writer
        self._detector = detector
        self._block_frames = block_frames
        self._audio = audio
        self._on_level = on_level
        self._should_stop = should_stop
        self._gate_frames = gate_frames  # callable; None result: write all
        self._pending: deque[tuple[int, np.ndarray]] = deque()
        self._pending_frames = 0

    def run(self) -> None:
        """Read until EOF (source died) or should_stop() turns true."""
        frame_bytes = self._audio.bytes_per_frame
        block_bytes = self._block_frames * frame_bytes
        pos = self._writer.position
        while not self._should_stop():
            data = self._source.read_exact(block_bytes)
            if len(data) < block_bytes:
                # Partial tail before EOF: keep whole frames, then stop.
                data = data[: len(data) - len(data) % frame_bytes]
                if not data:
                    break
            frames = decode_pcm(data, self._audio.channels,
                                self._audio.sample_bytes)
            level = rms_dbfs(frames)
            self._on_level(level)
            self._detector.process_block(pos, len(frames), level)
            self._dispatch(pos, frames)
            pos += len(frames)
            if len(data) < block_bytes:
                break

    def _dispatch(self, block_start: int, frames: np.ndarray) -> None:
        gate = self._gate_frames()  # re-read: editable at runtime
        if gate is None or self._detector.active:
            self._flush_pending(self._detector.session_start)
            self._writer.append(frames)
            return
        self._pending.append((block_start, frames))
        self._pending_frames += len(frames)
        while self._pending_frames > gate:
            _, old = self._pending.popleft()
            self._pending_frames -= len(old)
            self._writer.skip(len(old))

    def _flush_pending(self, boundary: int) -> None:
        """Drain the delay line: skip samples before ``boundary`` (the
        session start), write everything from there on."""
        while self._pending:
            block_start, frames = self._pending.popleft()
            cut = min(max(boundary - block_start, 0), len(frames))
            if cut:
                self._writer.skip(cut)
            if cut < len(frames):
                self._writer.append(frames[cut:])
        self._pending_frames = 0
