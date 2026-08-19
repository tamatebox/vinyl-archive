"""API tests with the full app lifespan (capture disabled via config)."""

import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from vinyl_archive.db import utcnow_iso
from vinyl_archive.main import create_app
from vinyl_archive.ring.writer import SegmentWriter

RATE = 8000


@pytest.fixture
def client(config):
    app = create_app(config)
    with TestClient(app) as c:
        c.app = app
        yield c


def seed_session(app, config) -> int:
    """Write 3 s of audio into the buffer and register an ended session."""
    db = app.state.db
    w = SegmentWriter(db, config.buffer_dir, RATE, 2, segment_frames=RATE)
    w.append(np.ones((3 * RATE, 2), dtype=np.int16) * 1000)
    w.close()
    sid = db.create_session(1000, utcnow_iso())
    db.close_session(sid, 2 * RATE, utcnow_iso())
    return sid


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("condition not met within timeout")


def test_status_reports_stopped_capture(client):
    st = client.get("/api/status").json()
    assert st["capture"] == "stopped"
    assert "buffer" in st


def test_index_serves_ui(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "vinyl-archive" in res.text


def test_save_download_rename_delete_flow(client, config):
    sid = seed_session(client.app, config)

    res = client.post(f"/api/sessions/{sid}/save")
    assert res.status_code == 202

    recs = wait_for(lambda: client.get("/api/recordings").json())
    assert len(recs) == 1
    rec = recs[0]
    assert rec["duration_s"] == pytest.approx((2 * RATE - 1000) / RATE, abs=0.01)

    sessions = {s["id"]: s for s in client.get("/api/sessions").json()}
    assert sessions[sid]["state"] == "saved"

    res = client.get(f"/api/recordings/{rec['id']}/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/flac"
    assert len(res.content) == rec["size_bytes"]

    res = client.patch(f"/api/recordings/{rec['id']}", json={"label": "My Record"})
    assert res.status_code == 200
    assert res.json()["label"] == "My Record"

    res = client.delete(f"/api/recordings/{rec['id']}")
    assert res.status_code == 204
    assert client.get("/api/recordings").json() == []
    assert not (config.recordings_dir / rec["filename"]).exists()


def test_save_unknown_session_404(client):
    assert client.post("/api/sessions/999/save").status_code == 404


def test_save_active_session_409(client, config):
    db = client.app.state.db
    sid = db.create_session(0, utcnow_iso())
    assert client.post(f"/api/sessions/{sid}/save").status_code == 409


def test_settings_get_returns_defaults(client):
    s = client.get("/api/settings").json()
    assert s["start_threshold_dbfs"] == -40.0
    assert s["silence_gating"] is True


def test_settings_patch_applies_and_persists(client, config):
    res = client.patch("/api/settings", json={
        "stop_threshold_dbfs": -50.0, "silence_gating": False,
    })
    assert res.status_code == 200
    assert res.json()["stop_threshold_dbfs"] == -50.0

    assert client.get("/api/settings").json()["silence_gating"] is False
    assert client.app.state.config.detector.stop_threshold_dbfs == -50.0
    assert client.app.state.manager._gate_frames is None

    # Persisted: a fresh app over the same data_dir starts with the change.
    with TestClient(create_app(config)) as c2:
        assert c2.get("/api/settings").json()["stop_threshold_dbfs"] == -50.0


def test_settings_patch_rejects_bad_values(client):
    assert client.patch("/api/settings",
                        json={"bit_depth": 24}).status_code == 422
    assert client.patch("/api/settings",
                        json={"end_silence_seconds": "long"}).status_code == 422
    assert client.patch("/api/settings",
                        json={"preroll_seconds": -1}).status_code == 422
    # stop threshold must stay at or below the start threshold
    assert client.patch("/api/settings",
                        json={"stop_threshold_dbfs": -30.0}).status_code == 422
    # nothing was applied
    assert client.get("/api/settings").json()["stop_threshold_dbfs"] == -48.0


def test_capture_start_stop_endpoints(client):
    assert client.post("/api/capture/stop").status_code == 200
    assert client.get("/api/status").json()["capture"] == "stopped"
    assert client.post("/api/capture/start").status_code == 200
    # config's capture command exits immediately; just verify it settles back.
    assert client.post("/api/capture/stop").status_code == 200
