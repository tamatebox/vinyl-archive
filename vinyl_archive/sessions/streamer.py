"""Serve a session's sample range straight out of the ring buffer.

Used for previewing and downloading sessions that have not been saved yet,
in two shapes, because playing and keeping want opposite things:

* WAV for the player: its size is known from the frame count alone, so byte
  offsets map onto samples arithmetically and the player can seek freely
  without the session having been saved first.
* FLAC for downloads: about half the bytes, and the file that lands in the
  listener's archive is then the same thing "Keep" would have written. A
  compressed stream has no byte-to-sample arithmetic, so this one is
  sequential — no Range, no length known in advance.

Neither writes anything to disk, so previewing and downloading cost the
buffer volume nothing no matter how often you hit play.

Ranges the buffer no longer covers are filled with silence so the stream
always matches the declared length.
"""

from __future__ import annotations

import io
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


# "fLaC" + the STREAMINFO block header + its 34-byte body. Everything
# libsndfile revises after the fact lives inside it.
FLAC_HEAD_BYTES = 42
_TOTAL_SAMPLES_BITS = 36


class _FlacSink:
    """Collects libsndfile's FLAC output for handing out in chunks.

    libsndfile writes the stream strictly forward and then, at close, seeks
    back into STREAMINFO to stamp the frame count, the frame sizes and an
    MD5 of the audio — bytes that a streaming response sent long ago. So the
    header is held until the frame count (known here up front) is patched in
    by hand, and the close-time rewrite of already-sent bytes is dropped:
    unset frame sizes and an all-zero MD5 both read as "unknown" to a
    decoder, while the duration, the one field a player actually needs, is
    the one we can fill correctly ourselves.
    """

    def __init__(self, n_frames: int):
        self._n_frames = n_frames
        self._pending = bytearray()  # bytes [sent, sent + len(pending))
        self._sent = 0
        self._pos = 0

    # -- file-like interface libsndfile drives ----------------------------
    def write(self, data) -> int:
        data = bytes(data)
        written = len(data)
        offset = self._pos - self._sent
        self._pos += written
        if offset < 0:  # revision of bytes already handed out: drop those
            data = data[-offset:]
            offset = 0
        if data:
            grow = offset + len(data) - len(self._pending)
            if grow > 0:
                self._pending.extend(bytes(grow))
            self._pending[offset:offset + len(data)] = data
        return written

    def read(self, count: int = -1) -> bytes:
        return b""

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._sent + len(self._pending) + offset
        else:
            self._pos = offset
        return self._pos

    def tell(self) -> int:
        return self._pos

    # -- streaming side ---------------------------------------------------
    def take(self) -> bytes:
        """Hand out everything encoded so far, header first and once."""
        if self._sent == 0:
            if len(self._pending) < FLAC_HEAD_BYTES:
                return b""  # header not complete yet; nothing is sendable
            self._patch_total_samples()
        out = bytes(self._pending)
        self._sent += len(out)
        self._pending.clear()
        return out

    def _patch_total_samples(self) -> None:
        # Last 36 bits of the 8 bytes at offset 18: sample rate, channel and
        # depth fields share the same word, so read-modify-write it.
        word = int.from_bytes(self._pending[18:26], "big")
        mask = (1 << _TOTAL_SAMPLES_BITS) - 1
        word = (word & ~mask) | (self._n_frames & mask)
        self._pending[18:26] = word.to_bytes(8, "big")


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

    def iter_flac(self, start: int, end: int) -> Iterator[bytes]:
        """Encode frames [start, end) to FLAC as the response is written.

        Same encoder settings the exporter uses, so downloading a buffered
        session and keeping it hand back the same samples — only the header
        differs, since a stream cannot know its own MD5 in advance.
        """
        audio = self._audio
        sink = _FlacSink(max(0, end - start))
        out = sf.SoundFile(sink, "w", samplerate=audio.sample_rate,
                           channels=audio.channels, subtype=audio.flac_subtype,
                           format="FLAC")
        try:
            for block in self.iter_blocks(start, end):
                out.write(block)
                chunk = sink.take()
                if chunk:
                    yield chunk
        finally:
            out.close()  # also runs when the client hangs up mid-download
        tail = sink.take()
        if tail:
            yield tail

    def iter_frames(self, start: int, end: int) -> Iterator[bytes]:
        for block in self.iter_blocks(start, end):
            yield pack_frames(block, self._audio.sample_bytes)

    def iter_blocks(self, start: int, end: int) -> Iterator[np.ndarray]:
        """Yield frames [start, end) as interleaved integer arrays."""
        audio = self._audio
        silence = np.zeros((CHUNK_FRAMES, audio.channels),
                           dtype=audio.frame_dtype)

        def fill(n: int) -> Iterator[np.ndarray]:
            while n > 0:
                take = min(n, CHUNK_FRAMES)
                yield silence[:take]
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
                        yield chunk
                        pos += len(chunk)
                        remaining -= len(chunk)
            except (OSError, sf.LibsndfileError):
                log.warning("segment %s unreadable while streaming", seg["filename"])
            if pos < hi:
                yield from fill(hi - pos)
                pos = hi
        if pos < end:
            yield from fill(end - pos)
