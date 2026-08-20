"""Manual recording against a live capture loop.

The source is a real-time silent PCM generator, so these also prove manual
recording overrides silence gating: without an explicit take, silence never
reaches the disk.
"""

import dataclasses
import sys
import time

import pytest
from fastapi.testclient import TestClient

from vinyl_archive.config import CaptureConfig
from vinyl_archive.main import create_app

RATE = 8000
# 800 frames (0.1 s at 8 kHz stereo, 16-bit) per tick, paced in real time.
SILENT_SOURCE = (
    sys.executable, "-c",
    "import sys, time\n"
    "block = bytes(3200)\n"
    "while True:\n"
    "    sys.stdout.buffer.write(block)\n"
    "    sys.stdout.buffer.flush()\n"
    "    time.sleep(0.1)\n",
)


def wait_for(predicate, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


@pytest.fixture
def client(config):
    config = dataclasses.replace(
        config, capture=CaptureConfig(auto_start=True, command=SILENT_SOURCE))
    app = create_app(config)
    with TestClient(app) as c:
        c.app = app
        wait_for(lambda: c.get("/api/status").json()["capture"] == "recording")
        yield c


def test_idle_silence_is_not_written(client):
    """Nothing plays, so gating keeps the buffer empty."""
    time.sleep(0.5)
    assert client.get("/api/status").json()["buffer"]["seconds"] == 0.0
    assert client.get("/api/history").json() == []


def test_manual_recording_captures_silence_and_appears_in_history(client):
    res = client.post("/api/record/start")
    assert res.status_code == 200
    session_id = res.json()["session_id"]
    assert client.get("/api/status").json()["manual_recording"] is True

    entry = wait_for(lambda: next(
        (i for i in client.get("/api/history").json() if i["id"] == session_id), None))
    assert entry["kind"] == "manual"
    assert entry["status"] == "recording"

    time.sleep(0.4)
    assert client.post("/api/record/stop").json()["session_id"] == session_id
    assert client.get("/api/status").json()["manual_recording"] is False

    entry = next(i for i in client.get("/api/history").json()
                 if i["id"] == session_id)
    assert entry["status"] == "buffered"
    assert entry["duration_s"] > 0

    # Silence was written because the take was explicit — it is playable.
    res = client.get(f"/api/sessions/{session_id}/audio")
    assert res.status_code == 200
    assert len(res.content) > 44


def test_second_start_is_idempotent_and_stop_needs_a_take(client):
    first = client.post("/api/record/start").json()["session_id"]
    assert client.post("/api/record/start").json()["session_id"] == first
    client.post("/api/record/stop")
    assert client.post("/api/record/stop").status_code == 409


def test_settings_change_cycles_the_source_and_records_the_gap(client):
    """A device/block_ms change restarts capture, which is a real gap in the
    audio — the next segment must be flagged rather than silently stitched."""
    db = client.app.state.db
    client.patch("/api/settings", json={"silence_gating": False})
    wait_for(lambda: db.list_segments())
    assert not any(s["discontinuity"] for s in db.list_segments()[1:])

    client.patch("/api/settings", json={"block_ms": 200})

    wait_for(lambda: any(s["discontinuity"] for s in db.list_segments()[1:]))
    assert client.get("/api/status").json()["capture"] == "recording"


def test_settings_restart_ends_an_open_manual_take(client):
    session_id = client.post("/api/record/start").json()["session_id"]
    client.patch("/api/settings", json={"block_ms": 150})

    # The take is closed rather than left spanning the gap.
    wait_for(lambda: db_state(client, session_id) == "ended")
    assert client.get("/api/status").json()["manual_recording"] is False


def db_state(client, session_id):
    sess = client.app.state.db.get_session(session_id)
    return sess and sess["state"]
