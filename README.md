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
              ├─ 60 s FLAC segments → buffer/   (ring buffer, ~12 h)
              ├─ silence detector   → sessions  (SQLite)
              └─ level / status     → web UI
keep: session range → sample-accurate concat/trim → recordings/*.flac
play: session range → WAV on the fly (no disk writes, seekable)
```

Everything that plays through the input is captured and listed in the web
UI's history, whether or not you asked for it — the automatic detection is
the safety net for the times you forget to press record. Press **Record** to
mark a take explicitly; that takes precedence over detection and is also the
only way to capture something quieter than the detector's threshold. Every
entry, explicit or automatic, can be played and downloaded straight away.
Entries you **Keep** become permanent FLAC files — one click, no dialog, and
**Rename** gives them a name whenever you get around to it; the rest are
eventually reclaimed as the buffer fills.

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
  stops once the 25 s end-of-session silence is confirmed. An explicit
  recording always writes, however quiet the input. Set
  `[capture] silence_gating = false` to buffer everything continuously.
- **Playback** streams the session's sample range out of the buffer as WAV,
  generated on the fly. Nothing is stored to preview something, and byte
  ranges map onto samples, so the player seeks freely even in a 20-minute
  side. Disk always holds FLAC; WAV exists only on the wire.
- **Auto level** (on by default, in the *Playback* row above the list) gives
  every entry its own gain so records cut at different levels play back
  equally loud. Levels are measured while the audio is written — the RMS of
  the whole entry and the level of its loudest 20 ms window — so the gain for
  a 25-minute side is two database columns, not a re-read of the file. The
  RMS is aimed at −18 dBFS, backed off if that would push the loudest window
  past −9 dBFS. The window matters: vinyl is full of clicks, and normalising
  to the loudest *sample* would let one scratch hold a whole quiet side down.
  A boosted click may clip on output; it was already a click.
- **Playback trim** (−12 to +24 dB) is a manual offset on top of auto level,
  remembered across visits. Both are Web Audio gain nodes in front of the
  browser's output: playback is the only thing that changes, downloads are
  always the untouched transfer. Neither is a fix for a genuinely weak input
  — check the level meter while a record plays and raise the ALSA capture
  gain (`amixer -c <card> sset Capture 80%`) if the RMS sits below −45 dBFS.
- **Keeping** extracts the exact sample range from the buffer segments into
  `recordings/` without pausing capture. Kept files are never deleted
  automatically; buffer segments fully covered by one are removed early to
  free space.

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
3. `sudo systemctl start vinyl-archive` and open `http://<pi>/` — port 80
   by default, so there is no port to type. Change `[server] port` to move
   it (the URL then needs it: `http://<pi>:8000/`); the service, and the
   health check in `update.sh`, both read the port from this file.

The web UI has no authentication — run it on a trusted LAN only.

### Updating a running Pi

```sh
ssh pi@<host>
cd vinyl-archive && git pull
sudo sh deploy/update.sh
```

`update.sh` builds the new version, checks that it imports and that
`config.toml` still parses **before** touching the running service, then
restarts and waits until the API answers again. If it does not come up, the
script prints the log and tells you how to roll back (`git checkout <last
good revision> && sudo sh deploy/update.sh`).

`config.toml` is never modified, and the database migrates itself on startup,
so there is no manual step. Settings changed from the web UI live in the
database and survive updates.

Capture pauses for a few seconds while the service restarts. That is a real
gap: any session in progress is closed at the restart (already-buffered audio
is kept, and the script warns you if a session is open), so update between
records rather than mid-side. Kept recordings are never touched.

If the Pi has no checkout of this repo, push the sources from your machine
instead and run the same script there:

```sh
rsync -a --delete --exclude .venv --exclude dev-data ./ pi@<host>:vinyl-archive/
ssh pi@<host> 'cd vinyl-archive && sudo sh deploy/update.sh'
```

### Reducing SD card wear

With silence gating (the default) the buffer only writes while something is
actually playing (~6 MB/min); idle time costs nothing, and previewing costs
nothing either. If you disable gating, or the input is rarely idle, keep the
data on a USB SSD instead of the SD card.

The two paths can be split: `[paths] data_dir` holds the database and the
ring buffer, `[paths] recordings_dir` the keepers. Putting only
`recordings_dir` on a USB drive keeps the archive off the SD card (and lets
you unplug it to copy files) while the churn-heavy buffer stays local; point
`data_dir` at the drive as well if you want the buffer writes off the card
too. Everything else (FLAC segments instead of WAV, WAL-mode SQLite,
journald-only logging) is already tuned to minimize writes.

## Tuning the silence detector

Watch the level meter in the web UI:

- While the record plays: the level should sit well above −40 dBFS
  (`start_threshold_dbfs`).
- Between tracks / needle in groove: surface noise should stay below
  −48 dBFS (`stop_threshold_dbfs`). If sessions never end, raise this value
  above your noise floor; if sides get split mid-play, lower it or increase
  `end_silence_seconds`.

Nearly all of `config.toml` can be changed from the web UI's **Settings**
panel — `[detector]`, `[ring]`, and the `[capture]`/`[audio]` input settings.
Changes are stored in the database and override the file from then on.

Most apply immediately: detector tuning, silence gating and the ring-buffer
policy (segment length, cap, disk floor). The ALSA device and the analysis
block size cycle the capture process, so they land within a second or two.
Sample rate, channels and bit depth need a service restart, and the UI says
so — the ring buffer cannot mix formats, so its current contents are
discarded on the restart that adopts the new format. Kept recordings are
standalone files and are never affected.

Four settings stay file-only, each for a concrete reason: `[server]`
host/port (a wrong binding would lock the UI out), `[paths] data_dir` (the
settings store itself lives under it), `[paths] recordings_dir` (moving it
would orphan existing recordings, whose rows startup reconciliation then
drops), and `[capture] command` (accepting an arbitrary command line from an
unauthenticated UI would be remote code execution as the service user).

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
VINYL_ARCHIVE_CONFIG=dev-config.toml .venv/bin/python -m vinyl_archive
```

On a Linux box you can test the real ALSA path without an ADC using the
loopback driver: `modprobe snd-aloop`, set `device = "hw:CARD=Loopback,1,0"`,
and `aplay -D hw:CARD=Loopback,0,0 some.wav` to feed it.

## API

| Method & path | Description |
|---|---|
| `GET /api/status` | Capture state, input level, buffer fill, disk free, format |
| `GET /api/history` | Sessions and kept recordings as one timeline, with playback `gain_db` |
| `POST /api/record/start` / `stop` | Explicit recording (wins over detection) |
| `GET /api/sessions/{id}/audio` | Stream a buffered session as WAV (Range OK) |
| `POST /api/sessions/{id}/save` | Keep a session as FLAC (async, 202) |
| `DELETE /api/sessions/{id}` | Drop a buffered session from the history |
| `GET /api/sessions` | Detected sessions (newest first) |
| `GET /api/recordings` | Kept recordings |
| `PATCH /api/recordings/{id}` | Rename (`{"label": "..."}`) |
| `GET /api/recordings/{id}/download` | Download the FLAC |
| `DELETE /api/recordings/{id}` | Delete a kept recording |
| `POST /api/capture/start` / `stop` | Pause/resume continuous capture |
| `GET /api/settings` | Editable settings + whether a restart is pending |
| `PATCH /api/settings` | Update settings; applied live and persisted |
