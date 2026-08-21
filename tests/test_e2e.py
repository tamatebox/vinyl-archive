"""One pass through the whole thing, with a real capture subprocess.

Everything else here tests a layer. This walks the path a record actually
takes — audio arriving on a pipe, the detector finding a session in it, the
export, and the two steps that eventually let go of the file — so that the
pieces are checked against each other and not only against their own mocks.

The source is a `[capture] command` override writing raw PCM, which is the
same hook README documents for testing without an ADC.
"""

import io
import sys
import time

import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from vinyl_archive.config import (AudioConfig, CaptureConfig, Config,
                                  DetectorConfig, RingConfig)
from vinyl_archive.main import create_app

RATE = 8000

# Silence, a tone, then silence forever: enough for the detector to open a
# session on the tone and close it on the silence that follows.
#
# It never exits, because exiting is EOF, and EOF means the supervisor
# restarts the source and a second session turns up mid-test. It also never
# stops writing, the way an ADC does not: a source that goes quiet without
# closing its pipe leaves the capture thread blocked in read(), and shutdown
# then waits out its 10 s join timeout.
GENERATOR = f"""
import array, math, sys, time
R = {RATE}
def block(seconds, amp):
    a = array.array('h')
    for i in range(int(R * seconds)):
        v = int(amp * math.sin(2 * math.pi * 440 * i / R))
        a.append(v)
        a.append(v)
    return a.tobytes()
out = sys.stdout.buffer
out.write(block(0.5, 0))
out.write(block(2.0, 1200))
out.flush()
while True:
    out.write(block(0.1, 0))
    out.flush()
    time.sleep(0.05)
"""


@pytest.fixture
def e2e_config(tmp_path) -> Config:
    cfg = Config(
        data_dir=tmp_path,
        audio=AudioConfig(sample_rate=RATE, channels=2),
        capture=CaptureConfig(auto_start=True,
                              command=(sys.executable, "-c", GENERATOR)),
        # 1 s segments so the session's audio is flushed to disk quickly, and
        # a floor of 0 so the GC never decides to reclaim anything under us.
        ring=RingConfig(segment_seconds=1, max_segments=50,
                        released_grace_seconds=0.0, min_free_mb=0),
        # Scaled down from the real thing: a record side's worth of patience
        # would make this a minute-long test.
        detector=DetectorConfig(block_ms=100, start_hold_seconds=0.1,
                                end_silence_seconds=0.5, preroll_seconds=0.1,
                                postroll_seconds=0.1, min_session_seconds=0.5),
    )
    cfg.ensure_dirs()
    return cfg


def wait_for(predicate, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("condition not met within timeout")


def test_a_record_from_the_pipe_to_the_last_byte(e2e_config):
    app = create_app(e2e_config)
    with TestClient(app) as client:
        # 1. The detector finds the tone on its own -- nothing asked it to.
        entry = wait_for(lambda: next(
            (i for i in client.get("/api/history").json()
             if i["status"] == "buffered"), None))
        assert entry["kind"] == "auto"
        assert entry["duration_s"] == pytest.approx(2.2, abs=0.6)
        # Quiet the way a phono stage is, so auto level asks for a boost.
        assert entry["gain_db"] > 0

        # 2. It is playable and downloadable before anything is saved.
        wav = client.get(entry["audio_url"])
        assert wav.status_code == 200
        assert wav.headers["content-type"] == "audio/wav"
        flac = client.get(entry["download_url"])
        assert flac.status_code == 200
        info = sf.info(io.BytesIO(flac.content))
        assert info.samplerate == RATE and info.channels == 2

        # 3. Keeping it writes a real file, and the session says so.
        assert client.post(f"/api/sessions/{entry['id']}/save",
                           json={"label": "E2E Side"}).status_code == 202
        rec = wait_for(lambda: next(
            (r for r in client.get("/api/recordings").json()
             if r["label"] == "E2E Side"), None))
        path = e2e_config.recordings_dir / rec["filename"]
        assert path.exists() and path.stat().st_size == rec["size_bytes"]
        assert sf.info(path).frames == pytest.approx(
            entry["duration_s"] * RATE, rel=0.05)
        assert client.app.state.db.get_session(entry["id"])["state"] == "saved"

        # The saved entry replaces the buffered one rather than doubling it.
        timeline = client.get("/api/history").json()
        assert [i["type"] for i in timeline if i["status"] == "saved"] == ["recording"]
        assert not [i for i in timeline if i["id"] == entry["id"]
                    and i["type"] == "session"]

        front = "/api/history?buffered_limit=5&include_archived=false"

        # 4. Archiving takes it off the front page and nothing else.
        client.patch(f"/api/recordings/{rec['id']}", json={"archived": True})
        assert not [i for i in client.get(front).json() if i["permanent"]]
        assert [i["id"] for i in client.get("/api/history").json()
                if i["permanent"]] == [rec["id"]]
        assert path.exists()

        # 5. Deleting needs two steps, and the first one is reversible.
        assert client.delete(f"/api/recordings/{rec['id']}").status_code == 409
        client.patch(f"/api/recordings/{rec['id']}", json={"trashed": True})
        assert client.get("/api/trash").json()["total_bytes"] == rec["size_bytes"]
        assert path.exists()
        client.patch(f"/api/recordings/{rec['id']}", json={"trashed": False})
        assert client.get("/api/trash").json()["items"] == []

        # 6. And the second one is not.
        client.patch(f"/api/recordings/{rec['id']}", json={"trashed": True})
        assert client.delete(f"/api/recordings/{rec['id']}").status_code == 204
        assert not path.exists()
        assert client.get("/api/history").json() == [] or all(
            i["id"] != rec["id"] or i["type"] != "recording"
            for i in client.get("/api/history").json())
        # Its session outlived it, and says the audio is gone.
        assert client.app.state.db.get_session(entry["id"])["state"] == "expired"

        # Capture never paused for any of it.
        assert client.get("/api/status").json()["capture"] == "recording"
