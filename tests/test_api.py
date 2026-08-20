"""API tests with the full app lifespan (capture disabled via config)."""

import io
import json
import re
import time

import numpy as np
import pytest
import soundfile as sf
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


def test_icons_and_manifest_are_served(client):
    """Every icon the page or the manifest names must actually resolve.

    Bookmarks and the iOS home screen are the only places these are ever
    seen, so a renamed or unshipped file is invisible in normal use.
    """
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/apple-touch-icon.png").status_code == 200

    res = client.get("/manifest.webmanifest")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(res.text)
    assert manifest["short_name"] == "Vinyl"
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]

    html = client.get("/").text
    hrefs = re.findall(r'<link rel="(?:icon|apple-touch-icon|manifest)"[^>]*'
                       r'href="([^"]+)"', html)
    assert len(hrefs) == 4
    for href in hrefs:
        assert client.get(href).status_code == 200, href


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


def test_history_merges_sessions_and_recordings(client, config):
    sid = seed_session(client.app, config)

    items = client.get("/api/history").json()
    assert len(items) == 1
    assert items[0]["type"] == "session"
    assert items[0]["status"] == "buffered"
    assert items[0]["permanent"] is False
    assert items[0]["kind"] == "auto"
    assert items[0]["audio_url"] == f"/api/sessions/{sid}/audio"

    client.post(f"/api/sessions/{sid}/save", json={"label": "Side A"})
    items = wait_for(lambda: [i for i in client.get("/api/history").json()
                             if i["status"] == "saved"])
    # The saved entry replaces the buffered one instead of duplicating it.
    assert len(client.get("/api/history").json()) == 1
    assert items[0]["type"] == "recording"
    assert items[0]["label"] == "Side A"
    assert items[0]["permanent"] is True
    assert items[0]["start_utc"] == client.get("/api/sessions").json()[0]["start_utc"]


def test_history_carries_the_playback_gain(client, config):
    """Every entry arrives with the gain that levels it, so playback needs no
    slider — and keeping it does not change either the gain or the bytes."""
    sid = seed_session(client.app, config)  # constant 1000 -> -30.3 dBFS

    buffered = client.get("/api/history").json()[0]
    assert buffered["gain_db"] == pytest.approx(12.3, abs=0.3)

    client.post(f"/api/sessions/{sid}/save")
    saved = wait_for(lambda: [i for i in client.get("/api/history").json()
                              if i["status"] == "saved"])[0]
    assert saved["gain_db"] == pytest.approx(buffered["gain_db"], abs=0.3)

    # The gain is playback-side only: the kept file is the raw transfer.
    res = client.get(saved["audio_url"])
    out, _rate = sf.read(io.BytesIO(res.content), dtype="int16", always_2d=True)
    assert np.all(out == 1000)


def test_session_audio_streams_wav_without_saving(client, config):
    sid = seed_session(client.app, config)

    res = client.get(f"/api/sessions/{sid}/audio")
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"
    assert res.headers["accept-ranges"] == "bytes"

    body = res.content
    assert body[:4] == b"RIFF" and body[8:12] == b"WAVE"
    frames = 2 * RATE - 1000
    assert len(body) == 44 + frames * 4  # 16-bit stereo
    assert int(res.headers["content-length"]) == len(body)
    # Streaming must not create files.
    assert not list(config.recordings_dir.iterdir())

    # ...and the audio matches what was captured (seeded with constant 1000).
    samples = np.frombuffer(body[44:], dtype="<i2")
    assert np.all(samples == 1000)


def test_session_audio_supports_range_requests(client, config):
    """Range support is what makes the player seekable for a 20-minute side."""
    sid = seed_session(client.app, config)
    full = client.get(f"/api/sessions/{sid}/audio").content

    res = client.get(f"/api/sessions/{sid}/audio",
                     headers={"Range": "bytes=100-199"})
    assert res.status_code == 206
    assert res.headers["content-range"] == f"bytes 100-199/{len(full)}"
    assert res.content == full[100:200]

    res = client.get(f"/api/sessions/{sid}/audio", headers={"Range": "bytes=20-"})
    assert res.status_code == 206
    assert res.content == full[20:]

    assert client.get(f"/api/sessions/{sid}/audio",
                      headers={"Range": f"bytes={len(full)}-"}).status_code == 416


def test_session_download_is_flac_and_writes_nothing(client, config):
    """A buffered session downloads as FLAC, so an entry that was never kept
    on the Pi still lands in the archive in the archive format."""
    sid = seed_session(client.app, config)

    res = client.get(f"/api/sessions/{sid}/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/flac"
    assert ".flac" in res.headers["content-disposition"]
    assert res.content[:4] == b"fLaC"
    assert not list(config.recordings_dir.iterdir())
    assert not list(config.buffer_dir.glob("*.part"))

    frames = 2 * RATE - 1000
    info = sf.info(io.BytesIO(res.content))
    # The frame count is stamped into the header up front, so a player knows
    # the duration and can seek in the downloaded file.
    assert info.frames == frames
    assert info.samplerate == RATE and info.channels == 2
    out, _rate = sf.read(io.BytesIO(res.content), dtype="int16", always_2d=True)
    assert out.shape == (frames, 2) and np.all(out == 1000)
    # Half the bytes of the WAV the player streams.
    assert len(res.content) < 44 + frames * 4


def test_session_download_matches_what_keeping_would_store(client, config):
    """Download-now and keep-then-download are the same audio: one encode
    path, so a listener never has to pick between them for quality."""
    sid = seed_session(client.app, config)
    streamed, _rate = sf.read(
        io.BytesIO(client.get(f"/api/sessions/{sid}/download").content),
        dtype="int16", always_2d=True)

    client.post(f"/api/sessions/{sid}/save")
    saved = wait_for(lambda: [i for i in client.get("/api/history").json()
                              if i["status"] == "saved"])[0]
    kept, _rate = sf.read(io.BytesIO(client.get(saved["download_url"]).content),
                          dtype="int16", always_2d=True)
    assert np.array_equal(streamed, kept)


def test_history_downloads_are_always_flac(client, config):
    sid = seed_session(client.app, config)
    item = client.get("/api/history").json()[0]
    # Playing seeks through the WAV stream; downloading takes the FLAC.
    assert item["audio_url"] == f"/api/sessions/{sid}/audio"
    assert item["download_url"] == f"/api/sessions/{sid}/download"


def test_download_session_without_buffered_audio_410(client):
    db = client.app.state.db
    sid = db.create_session(0, utcnow_iso())
    db.close_session(sid, 0, utcnow_iso())
    assert client.get(f"/api/sessions/{sid}/download").status_code == 410
    assert client.get("/api/sessions/999/download").status_code == 404


def test_dismiss_buffered_session(client, config):
    sid = seed_session(client.app, config)
    assert client.delete(f"/api/sessions/{sid}").status_code == 204
    assert client.get("/api/history").json() == []
    # Only the history entry goes; the buffer is reclaimed on its own schedule.
    assert client.app.state.db.list_segments()


def test_dismiss_active_session_409(client):
    sid = client.app.state.db.create_session(0, utcnow_iso())
    assert client.delete(f"/api/sessions/{sid}").status_code == 409


def test_manual_record_requires_running_capture(client):
    assert client.post("/api/record/start").status_code == 409
    assert client.post("/api/record/stop").status_code == 409


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
    # data_dir is deliberately not runtime-editable
    assert client.patch("/api/settings",
                        json={"data_dir": "/tmp/x"}).status_code == 422
    assert client.patch("/api/settings",
                        json={"end_silence_seconds": "long"}).status_code == 422
    assert client.patch("/api/settings",
                        json={"preroll_seconds": -1}).status_code == 422
    assert client.patch("/api/settings",
                        json={"bit_depth": 32}).status_code == 422
    assert client.patch("/api/settings",
                        json={"sample_rate": 48000.5}).status_code == 422
    assert client.patch("/api/settings", json={"device": " "}).status_code == 422
    # stop threshold must stay at or below the start threshold
    assert client.patch("/api/settings",
                        json={"stop_threshold_dbfs": -30.0}).status_code == 422
    # nothing was applied
    assert client.get("/api/settings").json()["stop_threshold_dbfs"] == -48.0


def test_audio_format_change_requires_restart(client):
    res = client.patch("/api/settings", json={"bit_depth": 24})
    assert res.status_code == 200
    assert res.json()["restart_required"] is True
    # capture keeps running in the old format until restarted
    assert client.get("/api/status").json()["format"]["bit_depth"] == 16
    assert client.get("/api/settings").json()["restart_required"] is True


def test_format_change_discards_incompatible_buffer(client, config):
    """After a restart in a new format, old-format segments must not linger:
    exports would otherwise mix formats."""
    seed_session(client.app, config)
    assert client.app.state.db.list_segments()
    client.patch("/api/settings", json={"bit_depth": 24})

    with TestClient(create_app(config)) as c2:
        assert c2.app.state.db.list_segments() == []
        assert c2.get("/api/status").json()["format"]["bit_depth"] == 24
        assert not list(config.buffer_dir.glob("*.flac"))


def test_capture_start_stop_endpoints(client):
    assert client.post("/api/capture/stop").status_code == 200
    assert client.get("/api/status").json()["capture"] == "stopped"
    assert client.post("/api/capture/start").status_code == 200
    # config's capture command exits immediately; just verify it settles back.
    assert client.post("/api/capture/stop").status_code == 200


def test_ring_settings_apply_live(client):
    before = client.get("/api/status").json()["buffer"]["capacity_seconds"]
    res = client.patch("/api/settings",
                       json={"max_segments": 10, "segment_seconds": 30})
    assert res.status_code == 200
    assert res.json()["restart_required"] is False  # ring policy needs no restart
    after = client.get("/api/status").json()["buffer"]["capacity_seconds"]
    assert (before, after) != (300, 300) and after == 300


def test_backoff_bounds_are_cross_checked(client):
    assert client.patch("/api/settings",
                        json={"restart_backoff_min_s": 30.0,
                              "restart_backoff_max_s": 5.0}).status_code == 422
    assert client.patch("/api/settings",
                        json={"restart_backoff_min_s": 1.0,
                              "restart_backoff_max_s": 20.0}).status_code == 200


def test_every_editable_setting_round_trips(client):
    """GET then PATCH the same payload back: every advertised setting must be
    accepted by its own validator."""
    current = client.get("/api/settings").json()
    current.pop("restart_required")
    res = client.patch("/api/settings", json=current)
    assert res.status_code == 200, res.json()
    echoed = res.json()
    echoed.pop("restart_required")
    assert echoed == current


def test_config_fields_are_classified():
    """Every config field is either web-editable or deliberately file-only.

    Adding a field to config.py forces a decision here rather than silently
    landing as an unreachable setting.
    """
    import dataclasses

    from vinyl_archive.config import EDITABLE_SETTINGS, Config

    file_only = {
        "host", "port",              # a bad binding locks the UI out
        "command",                   # argv from an unauthenticated UI is RCE
        "data_dir",                  # the settings store lives under it
        "recordings_dir_override",   # moving it orphans existing recordings
    }
    cfg = Config()
    names = set()
    for f in dataclasses.fields(cfg):
        section = getattr(cfg, f.name)
        if dataclasses.is_dataclass(section):
            names |= {sf.name for sf in dataclasses.fields(section)}
        else:
            names.add(f.name)

    unclassified = names - set(EDITABLE_SETTINGS) - file_only
    assert not unclassified, f"classify these in EDITABLE_SETTINGS or file_only: {unclassified}"
    assert file_only.isdisjoint(EDITABLE_SETTINGS)


def test_settings_form_matches_editable_bounds():
    """The settings form must be able to submit anything the API accepts.

    A number input's ``step`` is a *validation* constraint anchored at ``min``
    (valid values are min + k*step), so a coarse step silently forbids legal
    values — ``step="5" min="1"`` made the default 60 s segment length
    unreachable, the browser offering 56 or 61 instead. Hence: step mirrors
    the type only, and min/max mirror the server's bounds exactly, so neither
    a default nor a file-set value can be rejected before it is even sent.
    """
    from html.parser import HTMLParser

    from vinyl_archive.config import EDITABLE_SETTINGS
    from vinyl_archive.main import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text()
    form = html[html.index('<form id="settings-form"'):html.index("</form>")]

    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.controls = {}

        def handle_starttag(self, tag, attrs):
            if tag in ("input", "select"):
                a = dict(attrs)
                if "name" in a:
                    self.controls[a["name"]] = a

    p = Inputs()
    p.feed(form)
    controls = p.controls

    assert set(controls) == set(EDITABLE_SETTINGS), "form and API disagree"

    for name, attrs in controls.items():
        _, typ, lo, hi = EDITABLE_SETTINGS[name]
        if attrs.get("type") != "number":
            continue
        want_step = "1" if typ is int else "any"
        assert attrs.get("step") == want_step, f"{name}: step must be {want_step}"
        for attr, bound in (("min", lo), ("max", hi)):
            if bound is None:
                assert attr not in attrs, f"{name}: {attr} bounds nothing"
            else:
                assert attr in attrs, f"{name}: missing {attr}"
                assert float(attrs[attr]) == float(bound), (
                    f"{name}: {attr}={attrs[attr]} disagrees with the API's {bound}")
