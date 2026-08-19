# vinyl-archive

A Raspberry Pi audio capture server for archiving vinyl records. It records
the analog line input continuously into a ring buffer, detects play sessions
(e.g. one side of a record) by silence analysis, and lets you save any
detected session as a FLAC file from a web UI — so you never miss the
beginning of playback.

The input source doesn't matter: vinyl, CD, tuner — anything coming through
the line input is treated identically.

## How it works

```
[USB ADC] → arecord (raw PCM) → capture thread
              ├─ 60 s FLAC segments → buffer/   (ring buffer, ~3 h)
              ├─ silence detector   → sessions  (SQLite)
              └─ level / status     → web UI
save: session range → sample-accurate concat/trim → recordings/*.flac
```

- **Continuous capture** starts at boot (systemd) and survives USB device
  unplug/replug and process crashes; the supervisor restarts `arecord`
  within seconds.
- **Session detection**: playback louder than −40 dBFS starts a session
  (with 1.5 s pre-roll); a session only ends after 25 s of continuous
  silence, so 2–5 s track gaps never split a side. Leading/trailing silence
  is trimmed automatically.
- **Silence gating** (default on): while no session is active, nothing is
  written to disk — the pre-roll window is held in RAM, so the start of
  playback is still captured. Gating follows the session state, not raw
  silence: track gaps inside a side are recorded normally, and writing only
  stops once the 25 s end-of-session silence is confirmed. Set
  `[capture] silence_gating = false` to buffer everything continuously.
- **Saving** extracts the exact sample range from the buffer segments into
  `recordings/` without pausing capture. Saved files are never deleted
  automatically; buffer segments fully covered by a saved recording are
  removed early to free space.

## Installation (Raspberry Pi)

```sh
sudo apt install python3-venv libsndfile1 alsa-utils
git clone <this repo> && cd vinyl-archive
sudo sh deploy/install.sh
```

Then edit `/etc/vinyl-archive/config.toml`:

1. Find your ADC's card name: `cat /proc/asound/cards`
2. Set `[capture] device = "hw:CARD=<name>"` (names are stable across
   reboots; indexes are not).
3. `sudo systemctl start vinyl-archive` and open `http://<pi>:8000/`.

The web UI has no authentication — run it on a trusted LAN only.

### Reducing SD card wear

With silence gating (the default) the buffer only writes while something is
actually playing (~6 MB/min); idle time costs nothing. If you disable gating,
or the input is rarely idle, keep `data_dir` on a USB SSD instead of the SD
card: mount the drive and set `[paths] data_dir` accordingly. Everything else
(FLAC segments instead of WAV, WAL-mode SQLite, journald-only logging) is
already tuned to minimize writes.

## Tuning the silence detector

Watch the level meter in the web UI:

- While the record plays: the level should sit well above −40 dBFS
  (`start_threshold_dbfs`).
- Between tracks / needle in groove: surface noise should stay below
  −48 dBFS (`stop_threshold_dbfs`). If sessions never end, raise this value
  above your noise floor; if sides get split mid-play, lower it or increase
  `end_silence_seconds`.

All detector parameters (and the silence-gating switch) can be changed live
from the web UI's **Settings** panel — changes apply immediately, are stored
in the database, and override `config.toml` across restarts. Structural
settings (device, sample rate, bit depth, paths) stay in `config.toml` and
need a service restart, since changing them alters the buffer's audio format.

## Development without a Pi

The capture command is fully replaceable in config, so any command that
writes raw PCM to stdout (S16_LE, or packed S24_3LE when
`[audio] bit_depth = 24`) can act as the audio source:

```toml
[capture]
command = ["/bin/sh", "tools/make_test_audio.sh"]
```

`tools/make_test_audio.sh` (requires ffmpeg) generates silence → tone →
track gap → tone → silence, exercising the detector end-to-end. It runs
accelerated by default; set `REALTIME=-re` for real-time pacing.

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                      # unit + API tests, no audio hardware
VINYL_ARCHIVE_CONFIG=dev-config.toml .venv/bin/uvicorn vinyl_archive.main:app
```

On a Linux box you can test the real ALSA path without an ADC using the
loopback driver: `modprobe snd-aloop`, set `device = "hw:CARD=Loopback,1,0"`,
and `aplay -D hw:CARD=Loopback,0,0 some.wav` to feed it.

## API

| Method & path | Description |
|---|---|
| `GET /api/status` | Capture state, input level, buffer fill, disk free |
| `GET /api/sessions` | Detected sessions (newest first) |
| `POST /api/sessions/{id}/save` | Export a session to FLAC (async, 202) |
| `GET /api/recordings` | Saved recordings |
| `PATCH /api/recordings/{id}` | Rename (`{"label": "..."}`) |
| `GET /api/recordings/{id}/download` | Download FLAC |
| `DELETE /api/recordings/{id}` | Delete a saved recording |
| `POST /api/capture/start` / `stop` | Manual capture control |
| `GET /api/settings` | Runtime-editable settings (detector tuning, gating) |
| `PATCH /api/settings` | Update settings; applied live and persisted |
