import pytest

from vinyl_archive.config import (AudioConfig, CaptureConfig, Config,
                                  DetectorConfig, RingConfig)
from vinyl_archive.db import Database

RATE = 8000  # small but FLAC-valid rate keeps tests fast


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config(
        data_dir=tmp_path,
        audio=AudioConfig(sample_rate=RATE, channels=2),
        capture=CaptureConfig(auto_start=False,
                              command=("/bin/sh", "-c", "exit 0")),
        ring=RingConfig(segment_seconds=1, max_segments=5,
                        released_grace_seconds=0.0, min_free_mb=1),
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def db(config) -> Database:
    database = Database(config.db_path)
    yield database
    database.close()
