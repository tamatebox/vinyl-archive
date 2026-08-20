"""Stream a session's sample range straight out of the ring buffer as WAV.

Used for previewing and downloading sessions that have not been saved yet.
WAV (rather than FLAC) because its size is known from the frame count alone,
so the response can be generated incrementally with no temporary file — the
buffer volume sees zero extra writes no matter how often you hit play.

Ranges the buffer no longer covers are filled with silence so the stream
always matches the declared length.
"""

from __future__ import annotations

import logging
import struct
from typing import Iterator

import numpy as np
import soundfile as sf

from ..config import Config
from ..db import Database

log = logging.getLogger(__name__)

CHUNK_FRAMES = 48000


HEADER_BYTES = 44


def wav_header(n_frames: int, sample_rate: int, channels: int,
               bit_depth: int) -> bytes:
    block_align = channels * bit_depth // 8
    data_size = n_frames * block_align
    return b"".join([
        b"RIFF", struct.pack("<I", 36 + data_size), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                             sample_rate * block_align, block_align, bit_depth),
        b"data", struct.pack("<I", data_size),
    ])


def pack_frames(frames: np.ndarray, sample_bytes: int) -> bytes:
    """Interleaved integer frames -> little-endian PCM bytes."""
    if sample_bytes == 2:
        return frames.astype("<i2").tobytes()
    # int32 MSB-justified -> 3-byte little-endian samples.
    as32 = (frames.astype(np.int32) >> 8).astype("<i4")
    return as32.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


class SessionStreamer:
    def __init__(self, config: Config, db: Database, manager=None):
        self._config = config
        # Pinned at startup: buffer segments are in the format capture is
        # running with, which a settings change cannot alter without a restart.
        self._audio = config.audio
        self._db = db
        self._manager = manager

    def resolve_range(self, sess: dict) -> tuple[int, int]:
        """Sample range available for streaming, clamped to flushed audio.

        An active session has no end yet, so it streams up to whatever has
        been closed into segments so far.
        """
        start = sess["start_sample"]
        end = sess["end_sample"]
        if self._manager is not None:
            if end is None:
                end = self._manager.flushed_end()
            else:
                self._manager.request_flush_to(end)
        if end is None:
            end = self._db.max_end_sample()
        return start, max(start, end)

    def wav_size(self, n_frames: int) -> int:
        return HEADER_BYTES + n_frames * self._audio.bytes_per_frame

    def iter_range(self, start: int, end: int,
                   byte_start: int, byte_end: int) -> Iterator[bytes]:
        """Yield bytes [byte_start, byte_end) of the virtual WAV file.

        Byte offsets map onto frames arithmetically (uncompressed PCM), so a
        seek in the player reads only the segments it actually needs.
        """
        audio = self._audio
        block = audio.bytes_per_frame
        remaining = max(0, byte_end - byte_start)
        if remaining == 0:
            return

        if byte_start < HEADER_BYTES:
            header = wav_header(end - start, audio.sample_rate, audio.channels,
                                audio.bit_depth)[byte_start:]
            chunk = header[:remaining]
            remaining -= len(chunk)
            yield chunk
            data_offset = 0
        else:
            data_offset = byte_start - HEADER_BYTES

        skip = data_offset % block  # start may land mid-frame
        first = start + data_offset // block
        for chunk in self.iter_frames(min(first, end), end):
            if skip:
                chunk = chunk[skip:]
                skip = 0
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            if chunk:
                remaining -= len(chunk)
                yield chunk
            if remaining == 0:
                return

    def iter_wav(self, start: int, end: int) -> Iterator[bytes]:
        audio = self._audio
        yield wav_header(end - start, audio.sample_rate, audio.channels,
                         audio.bit_depth)
        yield from self.iter_frames(start, end)

    def iter_frames(self, start: int, end: int) -> Iterator[bytes]:
        audio = self._audio
        silence = np.zeros((CHUNK_FRAMES, audio.channels),
                           dtype=audio.frame_dtype)

        def fill(n: int) -> Iterator[bytes]:
            while n > 0:
                take = min(n, CHUNK_FRAMES)
                yield pack_frames(silence[:take], audio.sample_bytes)
                n -= take

        pos = start
        for seg in self._db.segments_overlapping(start, end):
            seg_start = seg["start_sample"]
            lo = max(pos, seg_start)
            hi = min(end, seg_start + seg["n_frames"])
            if lo >= hi:
                continue
            if lo > pos:
                yield from fill(lo - pos)  # segment already evicted
                pos = lo
            try:
                with sf.SoundFile(str(self._config.buffer_dir / seg["filename"])) as src:
                    src.seek(lo - seg_start)
                    remaining = hi - lo
                    while remaining > 0:
                        chunk = src.read(min(CHUNK_FRAMES, remaining),
                                         dtype=audio.frame_dtype, always_2d=True)
                        if len(chunk) == 0:
                            break
                        yield pack_frames(chunk, audio.sample_bytes)
                        pos += len(chunk)
                        remaining -= len(chunk)
            except (OSError, sf.LibsndfileError):
                log.warning("segment %s unreadable while streaming", seg["filename"])
            if pos < hi:
                yield from fill(hi - pos)
                pos = hi
        if pos < end:
            yield from fill(end - pos)
