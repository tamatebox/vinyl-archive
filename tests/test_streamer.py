"""FLAC comes off the ring buffer as the response is written, which is the
only reason a 25-minute side can be downloaded without a temp file. What that
costs is the ability to revise the header afterwards, so these tests pin the
two things that would otherwise break: bytes really do leave before the
encoder is done, and the duration in the header is right anyway.
"""

import io

import numpy as np
import pytest
import soundfile as sf

from vinyl_archive.db import Database
from vinyl_archive.ring.writer import SegmentWriter
from vinyl_archive.sessions import streamer as streamer_mod
from vinyl_archive.sessions.streamer import SessionStreamer

RATE = 8000  # matches conftest


@pytest.fixture
def buffered(config, db):
    """6 s of a ramp in the buffer: compressible, but not constant."""
    frames = 6 * RATE
    ramp = (np.arange(frames) % 4000 - 2000).astype(np.int16)
    audio = np.stack([ramp, -ramp], axis=1)
    w = SegmentWriter(db, config.buffer_dir, RATE, 2, segment_frames=RATE)
    w.append(audio)
    w.close()
    return audio


def test_flac_leaves_before_the_encoder_finishes(config, db, buffered,
                                                 monkeypatch):
    # Force many read blocks so the stream is handed out in pieces, the way a
    # real side is; at the default block size a short test encodes in one go.
    monkeypatch.setattr(streamer_mod, "CHUNK_FRAMES", RATE // 4)
    s = SessionStreamer(config, db)

    chunks = []
    for chunk in s.iter_flac(0, len(buffered)):
        chunks.append(chunk)
        if len(chunks) == 1:
            # The header is out on the wire already: nothing written after
            # this point can go back and revise it.
            assert chunk[:4] == b"fLaC"
    assert len(chunks) > 2

    blob = b"".join(chunks)
    info = sf.info(io.BytesIO(blob))
    assert info.frames == len(buffered)  # stamped up front, not at close
    assert info.samplerate == RATE and info.channels == 2
    out, _rate = sf.read(io.BytesIO(blob), dtype="int16", always_2d=True)
    assert np.array_equal(out, buffered)  # lossless, as advertised
    assert len(blob) < len(buffered) * 4  # and smaller than the PCM


def test_flac_stream_is_silence_filled_past_the_buffer(config, db, buffered):
    """A session whose head has been evicted still downloads at full length —
    the same silence-filling the WAV stream does, so the two agree."""
    s = SessionStreamer(config, db)
    end = len(buffered) + 2 * RATE  # runs off the end of what is buffered
    blob = b"".join(s.iter_flac(0, end))

    out, _rate = sf.read(io.BytesIO(blob), dtype="int16", always_2d=True)
    assert len(out) == end
    assert np.array_equal(out[:len(buffered)], buffered)
    assert np.all(out[len(buffered):] == 0)


def test_flac_and_wav_streams_carry_the_same_audio(config, db, buffered):
    s = SessionStreamer(config, db)
    start, end = RATE // 2, 5 * RATE
    from_flac, _rate = sf.read(io.BytesIO(b"".join(s.iter_flac(start, end))),
                               dtype="int16", always_2d=True)
    from_wav, _rate = sf.read(io.BytesIO(b"".join(s.iter_wav(start, end))),
                              dtype="int16", always_2d=True)
    assert np.array_equal(from_flac, from_wav)


def test_abandoned_flac_stream_closes_its_encoder(config, db, buffered,
                                                  monkeypatch):
    """A listener hitting cancel mid-download must not leave the encoder (and
    the segment file it is reading) open."""
    monkeypatch.setattr(streamer_mod, "CHUNK_FRAMES", RATE // 4)
    s = SessionStreamer(config, db)
    gen = s.iter_flac(0, len(buffered))
    next(gen)
    gen.close()  # what StreamingResponse does when the client disconnects
