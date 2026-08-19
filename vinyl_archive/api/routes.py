"""HTTP API. All state lives on app.state (db, manager, exporter, config)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import validate_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class LabelUpdate(BaseModel):
    label: str


def _session_json(sess: dict, sample_rate: int) -> dict:
    end = sess["end_sample"]
    duration = None if end is None else round((end - sess["start_sample"]) / sample_rate, 1)
    return {
        "id": sess["id"],
        "start_utc": sess["start_utc"],
        "end_utc": sess["end_utc"],
        "duration_s": duration,
        "state": sess["state"],
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
async def save_session(session_id: int, request: Request) -> dict:
    db = request.app.state.db
    sess = db.get_session(session_id)
    if sess is None:
        raise HTTPException(404, "session not found")
    if sess["state"] != "ended":
        raise HTTPException(409, f"session is not saveable (state={sess['state']})")

    exporter = request.app.state.exporter
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(request.app.state.export_pool,
                                  exporter.export, session_id)
    future.add_done_callback(_log_export_result)
    return {"status": "saving", "session_id": session_id}


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
def update_recording(recording_id: int, body: LabelUpdate, request: Request) -> dict:
    db = request.app.state.db
    if db.get_recording(recording_id) is None:
        raise HTTPException(404, "recording not found")
    db.set_recording_label(recording_id, body.label.strip())
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


@router.delete("/recordings/{recording_id}", status_code=204)
def delete_recording(recording_id: int, request: Request) -> None:
    db = request.app.state.db
    rec = db.get_recording(recording_id)
    if rec is None:
        raise HTTPException(404, "recording not found")
    (request.app.state.config.recordings_dir / rec["filename"]).unlink(missing_ok=True)
    db.delete_recording(recording_id)


@router.get("/settings")
def get_settings(request: Request) -> dict:
    return request.app.state.config.editable_values()


@router.patch("/settings")
def update_settings(body: dict, request: Request) -> dict:
    try:
        clean = validate_settings(body, request.app.state.config)
    except ValueError as e:
        raise HTTPException(422, str(e))
    new_config = request.app.state.config.with_settings(clean)
    request.app.state.db.set_settings(clean)
    request.app.state.config = new_config
    request.app.state.manager.apply_config(new_config)
    log.info("settings updated: %s", clean)
    return new_config.editable_values()


@router.post("/capture/start")
def capture_start(request: Request) -> dict:
    request.app.state.manager.enable()
    return {"status": "ok"}


@router.post("/capture/stop")
def capture_stop(request: Request) -> dict:
    request.app.state.manager.disable()
    return {"status": "ok"}
