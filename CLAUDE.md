# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Raspberry Pi audio capture server for archiving vinyl records: continuous line-in recording into a ring buffer of FLAC segments, silence-based session detection, and a web UI to save any detected session as a FLAC file. Python 3.11+, FastAPI, numpy/soundfile. See README.md for deployment and detector-tuning details.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # setup
.venv/bin/pytest                                             # all tests (no audio hardware needed)
.venv/bin/pytest tests/test_detector.py -k test_name         # single test
VINYL_ARCHIVE_CONFIG=dev-config.toml .venv/bin/uvicorn vinyl_archive.main:app   # dev server
```

No linter/formatter is configured. Tests run at 8 kHz sample rate (conftest.py) to stay fast.

For end-to-end testing without ALSA hardware, override `[capture] command` in config with any command that writes raw S16_LE PCM to stdout — `tools/make_test_audio.sh` (requires ffmpeg) generates a silence/tone/gap pattern that exercises the detector.

## Architecture

Everything is positioned by one **absolute sample counter** (frames since first capture, persisted across restarts via `db.max_end_sample()`). Segment filenames encode their start sample (`seg_<16-digit sample>_<ts>.flac`), sessions store start/end samples, and exports extract exact sample ranges. Wall-clock time is derived only for display, via an anchor in CaptureManager.

Data flow: subprocess source (`arecord` or config override) → `capture/recorder.py` reads PCM blocks → feeds both `ring/writer.py` (rotating 60 s FLAC segments + SQLite rows) and `sessions/detector.py` (hysteresis state machine; a session ends only after 25 s of continuous silence so track gaps never split a record side). With silence gating (`[capture] silence_gating`, default on) the recorder writes to disk only while a session is active: idle blocks sit in a RAM delay line sized to preroll+hold and are `skip()`ed in the writer — the sample counter advances over skipped audio, so gaps in segment coverage are normal and only ever lie outside sessions. Saving a session (`sessions/exporter.py`) streams the sample range out of buffer segments into `recordings/*.flac` — capture never pauses; output goes to a `.part` file renamed atomically.

Threading model:
- **capture-supervisor thread** (`capture/manager.py`): owns the writer, detector, and ring GC; restarts the source subprocess with backoff on EOF (device unplug/crash surfaces as pipe EOF — that's the whole failure-detection model, see `capture/source.py`). Most `SegmentWriter` methods are capture-thread-only; cross-thread requests go through `request_rotate()`/`flushed_end()`.
- **export pool** (single worker, `main.py`): runs exports; `Exporter` also holds its own lock.
- **FastAPI event loop**: API reads shared state; all handles live on `app.state`.
- `db.py` wraps one SQLite connection in an RLock (WAL mode); safe from all threads.

Key invariants:
- **Files are the source of truth.** `db.reconcile()` runs at startup and rebuilds/repairs DB rows from what's on disk (re-registers orphan segments, drops rows for missing files, force-closes sessions that were active at crash time — a restart implies a gap, so continuing them would be wrong).
- Session state machine: `active → ended → saving → saved`, with `expired` when the audio has left the buffer; only `ended` sessions are exportable, and a failed export reverts to `ended`.
- Ring GC (`ring/gc.py`, runs on every segment rotation, must never throw into the capture thread): deletes released segments after a grace period, enforces the segment cap, and protects segments overlapping unsaved sessions — evicting into one marks it `truncated_head`/`expired` rather than losing track silently. Saved recordings are never deleted automatically.
- Capture gaps are tracked explicitly (`discontinuity` flag on segments, `has_gaps` on recordings) rather than papered over.

Config (`config.py`) is frozen dataclasses loaded from TOML via `VINYL_ARCHIVE_CONFIG`; tests construct `Config(...)` directly with `tmp_path`. Audio format follows `[audio] bit_depth` (16 → int16/S16_LE, 24 → MSB-justified int32 unpacked from S24_3LE — the justification libsndfile uses for PCM_24 int I/O); `rms_dbfs` scales by dtype. Detector tuning and `silence_gating` are runtime-editable via `GET/PATCH /api/settings` (whitelist + bounds in `config.EDITABLE_SETTINGS`): changes persist in the DB `settings` table, which overrides the TOML at startup, and apply live via `CaptureManager.apply_config` / `SilenceDetector.reconfigure`. Structural settings (device, sample_rate, bit_depth, paths) are file-only because changing them would mix formats inside the buffer.
