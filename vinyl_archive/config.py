"""Configuration loading (TOML file + dataclasses)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

ENV_CONFIG = "VINYL_ARCHIVE_CONFIG"


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 48000
    channels: int = 2
    # ADC bit depth: 16 (ALSA S16_LE) or 24 (packed S24_3LE). Internally,
    # 24-bit samples are carried as MSB-justified int32 — the justification
    # libsndfile uses for int I/O on PCM_24 files.
    bit_depth: int = 16

    def __post_init__(self) -> None:
        if self.bit_depth not in (16, 24):
            raise ValueError("audio.bit_depth must be 16 or 24")

    @property
    def sample_bytes(self) -> int:
        return self.bit_depth // 8

    @property
    def bytes_per_frame(self) -> int:
        return self.sample_bytes * self.channels

    @property
    def alsa_format(self) -> str:
        return "S16_LE" if self.bit_depth == 16 else "S24_3LE"

    @property
    def flac_subtype(self) -> str:
        return "PCM_16" if self.bit_depth == 16 else "PCM_24"

    @property
    def frame_dtype(self) -> str:
        return "int16" if self.bit_depth == 16 else "int32"


@dataclass(frozen=True)
class CaptureConfig:
    device: str = "hw:CARD=CODEC"
    auto_start: bool = True
    # Write to disk only while a session is active; idle audio is held in a
    # small RAM delay line and dropped. Cuts flash wear to playback time.
    silence_gating: bool = True
    command: tuple[str, ...] | None = None
    restart_backoff_min_s: float = 2.0
    restart_backoff_max_s: float = 10.0

    def build_command(self, audio: AudioConfig) -> list[str]:
        if self.command:
            return list(self.command)
        return [
            "arecord",
            "-D", self.device,
            "-f", audio.alsa_format,
            "-c", str(audio.channels),
            "-r", str(audio.sample_rate),
            "-t", "raw",
            "--buffer-time=2000000",
            "--period-time=100000",
            "-q",
        ]

    @property
    def uses_alsa(self) -> bool:
        return self.command is None


@dataclass(frozen=True)
class RingConfig:
    segment_seconds: int = 60
    max_segments: int = 200
    released_grace_seconds: float = 300.0
    min_free_mb: int = 1024


@dataclass(frozen=True)
class DetectorConfig:
    block_ms: int = 100
    start_threshold_dbfs: float = -40.0
    stop_threshold_dbfs: float = -48.0
    start_hold_seconds: float = 0.3
    end_silence_seconds: float = 25.0
    preroll_seconds: float = 1.5
    postroll_seconds: float = 2.0
    min_session_seconds: float = 30.0


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("/var/lib/vinyl-archive")
    server: ServerConfig = field(default_factory=ServerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    ring: RingConfig = field(default_factory=RingConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)

    @property
    def buffer_dir(self) -> Path:
        return self.data_dir / "buffer"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "vinyl.sqlite3"

    def ensure_dirs(self) -> None:
        for d in (self.buffer_dir, self.recordings_dir, self.db_path.parent):
            d.mkdir(parents=True, exist_ok=True)

    def with_settings(self, settings: dict) -> "Config":
        """Overlay runtime-editable settings (see EDITABLE_SETTINGS).

        Unknown keys are ignored so stale rows from an older schema can't
        break startup.
        """
        def section(name: str) -> dict:
            return {k: v for k, v in settings.items()
                    if EDITABLE_SETTINGS.get(k, (None,))[0] == name}

        cfg = self
        if det := section("detector"):
            cfg = replace(cfg, detector=replace(cfg.detector, **det))
        if cap := section("capture"):
            cfg = replace(cfg, capture=replace(cfg.capture, **cap))
        if aud := section("audio"):
            cfg = replace(cfg, audio=replace(cfg.audio, **aud))
        return cfg

    def editable_values(self) -> dict:
        return {name: getattr(getattr(self, section), name)
                for name, (section, *_rest) in EDITABLE_SETTINGS.items()}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = path or os.environ.get(ENV_CONFIG)
        if not path:
            return cls()
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        def section(name: str) -> dict:
            return raw.get(name, {})

        cap = dict(section("capture"))
        if "command" in cap:
            cap["command"] = tuple(cap["command"])
        return cls(
            data_dir=Path(section("paths").get("data_dir", cls.data_dir)),
            server=ServerConfig(**section("server")),
            audio=AudioConfig(**section("audio")),
            capture=CaptureConfig(**cap),
            ring=RingConfig(**section("ring")),
            detector=DetectorConfig(**section("detector")),
        )


# Settings editable from the web UI. Live-applied ones take effect without a
# restart; RESTART_SETTINGS change the buffer's audio format and only take
# effect after a service restart (reconcile then drops old-format buffer
# segments). data_dir and [server] stay file-only: the settings store lives
# inside data_dir, and a bad server binding would lock the UI out.
# name -> (section, type, min, max); bounds are None where not applicable.
EDITABLE_SETTINGS: dict[str, tuple] = {
    "start_threshold_dbfs": ("detector", float, -120.0, 0.0),
    "stop_threshold_dbfs": ("detector", float, -120.0, 0.0),
    "start_hold_seconds": ("detector", float, 0.0, 10.0),
    "end_silence_seconds": ("detector", float, 1.0, 600.0),
    "preroll_seconds": ("detector", float, 0.0, 30.0),
    "postroll_seconds": ("detector", float, 0.0, 60.0),
    "min_session_seconds": ("detector", float, 0.0, 3600.0),
    "silence_gating": ("capture", bool, None, None),
    "device": ("capture", str, None, None),
    "sample_rate": ("audio", int, 8000, 192000),
    "channels": ("audio", int, 1, 8),
    "bit_depth": ("audio", int, None, None),  # choices enforced: 16 or 24
}

RESTART_SETTINGS = frozenset({"sample_rate", "channels", "bit_depth"})


def restart_required(running: Config, target: Config) -> bool:
    return any(getattr(running.audio, name) != getattr(target.audio, name)
               for name in RESTART_SETTINGS)


def validate_settings(patch: dict, config: Config) -> dict:
    """Validate a settings patch against EDITABLE_SETTINGS.

    Returns the cleaned patch (numbers coerced to float); raises ValueError
    with a user-facing message on any problem.
    """
    clean = {}
    for name, value in patch.items():
        if name not in EDITABLE_SETTINGS:
            raise ValueError(f"unknown setting: {name}")
        _section, typ, lo, hi = EDITABLE_SETTINGS[name]
        if typ is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
        elif typ is str:
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError(f"{name} must be a non-empty string")
            value = value.strip()
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            if typ is int:
                if float(value) != int(value):
                    raise ValueError(f"{name} must be an integer")
                value = int(value)
            else:
                value = float(value)
            if lo is not None and not lo <= value <= hi:
                raise ValueError(f"{name} must be between {lo} and {hi}")
        if name == "bit_depth" and value not in (16, 24):
            raise ValueError("bit_depth must be 16 or 24")
        clean[name] = value

    merged = {**config.editable_values(), **clean}
    if merged["stop_threshold_dbfs"] > merged["start_threshold_dbfs"]:
        raise ValueError("stop_threshold_dbfs must not exceed start_threshold_dbfs")
    return clean
