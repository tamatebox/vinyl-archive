"""App factory and process lifecycle."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .capture.manager import CaptureManager
from .config import Config
from .db import Database, reconcile
from .sessions.exporter import Exporter
from .sessions.streamer import SessionStreamer

STATIC_DIR = Path(__file__).parent / "web" / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config: Config = app.state.config
    config.ensure_dirs()

    db = Database(config.db_path)

    # Settings changed from the web UI live in the DB and win over the file.
    # Resolved before reconcile: it needs the effective audio format to spot
    # buffer segments left over from a format change.
    stored = db.get_settings()
    if stored:
        config = config.with_settings(stored)
        app.state.config = config
        config.ensure_dirs()
    reconcile(db, config.buffer_dir, config.recordings_dir, config.audio)

    manager = CaptureManager(config, db)
    manager.start()

    app.state.db = db
    app.state.manager = manager
    app.state.streamer = SessionStreamer(config, db, manager)
    app.state.exporter = Exporter(config, db, manager)
    app.state.export_pool = ThreadPoolExecutor(max_workers=1,
                                               thread_name_prefix="export")
    try:
        yield
    finally:
        manager.shutdown()
        app.state.export_pool.shutdown(wait=True)
        db.close()


def create_app(config: Config | None = None) -> FastAPI:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = FastAPI(title="vinyl-archive", lifespan=_lifespan)
    app.state.config = config or Config.load()
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
