"""HTTP API. All state lives on app.state (db, manager, exporter, config)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..config import restart_required, validate_settings
from ..sessions.loudness import auto_gain_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class RecordingUpdate(BaseModel):
    """Both fields optional: a PATCH may set either without touching the
    other, and `label: ""` clearing a name has to stay distinguishable from
    "leave the name alone"."""
    label: str | None = None
    archived: bool | None = None
    trashed: bool | None = None


class SaveRequest(BaseModel):
    label: str = ""


def _session_json(sess: dict, sample_rate: int) -> dict:
    end = sess["end_sample"]
    duration = None if end is None else round((end - sess["start_sample"]) / sample_rate, 1)
    return {
        "id": sess["id"],
        "start_utc": sess["start_utc"],
        "end_utc": sess["end_utc"],
        "duration_s": duration,
        "state": sess["state"],
        "kind": sess["kind"],
        "truncated_head": bool(sess["truncated_head"]),
    }


@router.get("/status")
def get_status(request: Request) -> dict:
    return request.app.state.manager.status()


@router.get("/sessions")
def list_sessions(request: Request) -> list[dict]:
    rate = request.app.state.config.audio.sample_rate
    return [_session_json(s, rate) for s in request.app.state.db.list_sessions()]


@router.post("/sessions/{session_id}/save", status_code=202)
async def save_session(session_id: int, request: Request,
                       body: SaveRequest | None = None) -> dict:
    db = request.app.state.db
    sess = db.get_session(session_id)
    if sess is None:
        raise HTTPException(404, "session not found")
    if sess["state"] != "ended":
        raise HTTPException(409, f"session is not saveable (state={sess['state']})")

    exporter = request.app.state.exporter
    loop = asyncio.get_running_loop()
    label = (body.label if body else "").strip()
    future = loop.run_in_executor(request.app.state.export_pool,
                                  exporter.export, session_id, label)
    future.add_done_callback(_log_export_result)
    return {"status": "saving", "session_id": session_id}


def _resolve_range(streamer, sess: dict) -> tuple[int, int]:
    """Sample range to serve, or the reason there is nothing to serve.

    "Not yet" and "not any more" are different failures with different fixes:
    an active session becomes readable at the next segment rotation, while an
    ended one with no audio left has been reclaimed by the ring buffer and
    never will be.
    """
    start, end = streamer.resolve_range(sess)
    if end <= start:
        if sess["state"] == "active":
            raise HTTPException(409, "session is still recording; no audio has "
                                     "been closed into the buffer yet")
        raise HTTPException(410, "no buffered audio remains for this session")
    return start, end


@router.get("/sessions/{session_id}/audio")
def stream_session(session_id: int, request: Request):
    """Serve a buffered session as WAV, straight from the ring buffer.

    Nothing is written to disk, and byte ranges map onto samples, so the
    player can seek freely without the session having been saved first.
    """
    db = request.app.state.db
    sess = db.get_session(session_id)
    if sess is None:
        raise HTTPException(404, "session not found")

    streamer = request.app.state.streamer
    start, end = _resolve_range(streamer, sess)
    total = streamer.wav_size(end - start)

    byte_start, byte_end = 0, total
    status_code = 200
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        spec = range_header.removeprefix("bytes=").split(",")[0]
        first, _, last = spec.partition("-")
        try:
            byte_start = int(first) if first else 0
            byte_end = (int(last) + 1) if last else total
        except ValueError:
            raise HTTPException(416, "malformed range")
        byte_start, byte_end = max(0, byte_start), min(total, byte_end)
        if byte_start >= total or byte_end <= byte_start:
            raise HTTPException(416, "range not satisfiable")
        status_code = 206
        headers["Content-Range"] = f"bytes {byte_start}-{byte_end - 1}/{total}"

    headers["Content-Length"] = str(byte_end - byte_start)
    name = f"session_{session_id}_{sess['start_utc'].replace(':', '')}.wav"
    headers["Content-Disposition"] = f'inline; filename="{name}"'
    return StreamingResponse(
        streamer.iter_range(start, end, byte_start, byte_end),
        status_code=status_code, media_type="audio/wav", headers=headers)


@router.get("/sessions/{session_id}/download")
def download_session(session_id: int, request: Request):
    """Serve a buffered session as FLAC, encoded as the response is written.

    The player uses the WAV route because it can seek there; a download wants
    the archive format instead, so this one re-encodes on the fly rather than
    shipping twice the bytes. Sequential only: a compressed stream has no
    length to declare up front and no byte-to-sample arithmetic to serve a
    Range with.
    """
    db = request.app.state.db
    sess = db.get_session(session_id)
    if sess is None:
        raise HTTPException(404, "session not found")

    streamer = request.app.state.streamer
    start, end = _resolve_range(streamer, sess)

    name = f"session_{session_id}_{sess['start_utc'].replace(':', '')}.flac"
    return StreamingResponse(
        streamer.iter_flac(start, end), media_type="audio/flac",
        headers={"Cache-Control": "no-store",
                 "Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/history")
def list_history(request: Request, buffered_limit: int | None = None,
                 include_archived: bool = True) -> list[dict]:
    """Sessions and saved recordings as one timeline.

    Both are playable and downloadable; the difference is only whether they
    survive — buffered entries are reclaimed by the ring buffer eventually,
    saved ones are kept until deleted by hand.

    `buffered_limit` makes the front page a recency window instead of a list
    that grows until someone prunes it: only the newest N buffered sessions
    come back, and the rest are reached from /history. Capture in progress is
    never dropped however small the window is — a recording or a save is an
    event, not an entry waiting for a decision. Levels are measured only for
    what is returned, so the front page's two-second poll does not pay for
    every session still in the buffer.

    `include_archived=false` drops kept recordings that have been archived,
    which is the other half of the front page's scoping: one list is a window
    over the buffer, the other is what has not been filed away yet. Archiving
    moves nothing and deletes nothing — the file and the row are untouched.
    """
    db = request.app.state.db
    rate = request.app.state.config.audio.sample_rate
    meta = db.session_meta()
    items = []

    for rec in db.list_recordings():
        # Trashed entries are out of the timeline entirely, not scoped out of
        # it: they are waiting to be given up, and GET /api/trash is where
        # they are seen and undone.
        if rec["trashed_at"] is not None:
            continue
        if not include_archived and rec["archived_at"] is not None:
            continue
        sess = meta.get(rec["session_id"]) or {}
        items.append({
            "type": "recording",
            "id": rec["id"],
            "label": rec["label"],
            "start_utc": sess.get("start_utc") or rec["created_utc"],
            "duration_s": rec["duration_s"],
            "kind": sess.get("kind", "auto"),
            "status": "saved",
            "permanent": True,
            "has_gaps": bool(rec["has_gaps"]),
            "archived": rec["archived_at"] is not None,
            "size_bytes": rec["size_bytes"],
            "gain_db": auto_gain_db(rec["short_peak"], rec["mean_sq"]),
            "audio_url": f"/api/recordings/{rec['id']}/download",
            "download_url": f"/api/recordings/{rec['id']}/download",
        })

    sessions = db.unsaved_sessions()  # oldest first
    if buffered_limit is not None:
        buffered = [s["id"] for s in sessions if s["state"] == "ended"]
        # max(0, ...) on the count: a window wider than the buffer holds drops
        # nothing, and a negative slice bound would silently invert that.
        limit = max(0, buffered_limit)
        dropped = set(buffered[:max(0, len(buffered) - limit)])
        sessions = [s for s in sessions if s["id"] not in dropped]

    for sess in sessions:
        end = sess["end_sample"]
        # An active session has no end yet: level it on what is flushed so far.
        short_peak, mean_sq = db.level_in_range(
            sess["start_sample"],
            end if end is not None else db.max_end_sample())
        items.append({
            "type": "session",
            "id": sess["id"],
            "label": "",
            "start_utc": sess["start_utc"],
            "duration_s": (None if end is None else
                           round((end - sess["start_sample"]) / rate, 1)),
            "kind": sess["kind"],
            "status": {"active": "recording",
                       "saving": "saving"}.get(sess["state"], "buffered"),
            "permanent": False,
            "has_gaps": bool(sess["truncated_head"]),
            "archived": False,
            "size_bytes": None,
            "gain_db": auto_gain_db(short_peak, mean_sq),
            "audio_url": f"/api/sessions/{sess['id']}/audio",
            # Play from the seekable WAV stream, download the FLAC: an entry
            # that is only buffered still lands in the archive as FLAC.
            "download_url": f"/api/sessions/{sess['id']}/download",
        })

    items.sort(key=lambda i: i["start_utc"], reverse=True)
    return items


def _log_export_result(future) -> None:
    exc = future.exception()
    if exc:
        log.error("export failed: %s", exc)


@router.get("/recordings")
def list_recordings(request: Request) -> list[dict]:
    return [
        {**rec, "has_gaps": bool(rec["has_gaps"])}
        for rec in request.app.state.db.list_recordings()
    ]


@router.patch("/recordings/{recording_id}")
def update_recording(recording_id: int, body: RecordingUpdate,
                     request: Request) -> dict:
    db = request.app.state.db
    if db.get_recording(recording_id) is None:
        raise HTTPException(404, "recording not found")
    if body.label is not None:
        db.set_recording_label(recording_id, body.label.strip())
    if body.archived is not None:
        db.set_recording_archived(recording_id, body.archived)
    if body.trashed is not None:
        db.set_recording_trashed(recording_id, body.trashed)
    return db.get_recording(recording_id)


@router.get("/recordings/{recording_id}/download")
def download_recording(recording_id: int, request: Request) -> FileResponse:
    rec = request.app.state.db.get_recording(recording_id)
    if rec is None:
        raise HTTPException(404, "recording not found")
    path = request.app.state.config.recordings_dir / rec["filename"]
    if not path.exists():
        raise HTTPException(410, "recording file is missing")
    download_name = (rec["label"] or rec["filename"].removesuffix(".flac")) + ".flac"
    return FileResponse(path, media_type="audio/flac", filename=download_name)


@router.get("/trash")
def list_trash(request: Request) -> dict:
    """What is waiting to be given up, and how much room it is holding.

    The size is the point of showing this at all: the trash costs nothing
    until the volume runs low, and then it is the first thing spent — so it
    should never be invisible.
    """
    rows = request.app.state.db.trashed_recordings()
    return {
        "total_bytes": sum(r["size_bytes"] for r in rows),
        "items": [
            {"id": r["id"], "label": r["label"], "duration_s": r["duration_s"],
             "size_bytes": r["size_bytes"], "created_utc": r["created_utc"],
             "trashed_at": r["trashed_at"], "has_gaps": bool(r["has_gaps"]),
             "download_url": f"/api/recordings/{r['id']}/download"}
            # Newest discard first: reading order is the reverse of the order
            # they are given up in.
            for r in reversed(rows)
        ],
    }


@router.delete("/recordings/{recording_id}", status_code=204)
def delete_recording(recording_id: int, request: Request) -> None:
    """Delete a recording's file for good.

    Only from the trash. The two steps are an API invariant rather than a
    dialog in the UI, so no client can destroy a kept transfer in one call —
    and a kept transfer is the one thing here that cannot be recovered by
    other means: five minutes after Keep, the buffer copy is gone.
    """
    db = request.app.state.db
    rec = db.get_recording(recording_id)
    if rec is None:
        raise HTTPException(404, "recording not found")
    if rec["trashed_at"] is None:
        raise HTTPException(409, "recording is not in the trash; move it there"
                                 " first (PATCH {\"trashed\": true})")
    (request.app.state.config.recordings_dir / rec["filename"]).unlink(missing_ok=True)
    db.delete_recording(recording_id)   # also expires the session it came from


@router.get("/settings")
def get_settings(request: Request) -> dict:
    running = request.app.state.manager.running_config()
    return {
        **request.app.state.config.editable_values(),
        "restart_required": restart_required(running,
                                             request.app.state.config.audio),
    }


@router.post("/record/start")
def record_start(request: Request) -> dict:
    """Start an explicit recording. Takes precedence over auto-detection:
    an auto session in progress is adopted instead of duplicated."""
    manager = request.app.state.manager
    session_id = manager.manual_start()
    if session_id is None:
        raise HTTPException(409, "capture is not running")
    return {"status": "recording", "session_id": session_id}


@router.post("/record/stop")
def record_stop(request: Request) -> dict:
    session_id = request.app.state.manager.manual_stop()
    if session_id is None:
        raise HTTPException(409, "no manual recording in progress")
    return {"status": "stopped", "session_id": session_id}


@router.patch("/settings")
def update_settings(body: dict, request: Request) -> dict:
    try:
        clean = validate_settings(body, request.app.state.config)
    except ValueError as e:
        raise HTTPException(422, str(e))
    running = request.app.state.manager.running_config()
    new_config = request.app.state.config.with_settings(clean)
    request.app.state.db.set_settings(clean)
    request.app.state.config = new_config
    request.app.state.manager.apply_config(new_config)
    log.info("settings updated: %s", clean)
    return {
        **new_config.editable_values(),
        # Audio-format changes only take effect on restart; until then the
        # buffer keeps its current format.
        "restart_required": restart_required(running, new_config.audio),
    }


@router.post("/capture/start")
def capture_start(request: Request) -> dict:
    request.app.state.manager.enable()
    return {"status": "ok"}


@router.post("/capture/stop")
def capture_stop(request: Request) -> dict:
    request.app.state.manager.disable()
    return {"status": "ok"}
