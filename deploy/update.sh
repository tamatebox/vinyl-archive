#!/bin/sh
# Update an existing installation to this checkout, then restart the service.
#   sudo sh deploy/update.sh
#
# Safe to re-run. The database migrates itself on startup and config.toml is
# never touched. Capture pauses for a few seconds during the restart.
set -eu

APP_DIR=/opt/vinyl-archive
CONF=/etc/vinyl-archive/config.toml
UNIT=vinyl-archive
PY="$APP_DIR/venv/bin/python"

[ "$(id -u)" = 0 ] || { echo "run as root: sudo sh deploy/update.sh" >&2; exit 1; }
[ -d vinyl_archive ] || { echo "run from the repository root" >&2; exit 1; }
[ -x "$PY" ] || { echo "nothing installed at $APP_DIR — run deploy/install.sh" >&2; exit 1; }

[ -f "$CONF" ] && export VINYL_ARCHIVE_CONFIG="$CONF"
# PYTHONSAFEPATH keeps the checkout's ./vinyl_archive off sys.path, so every
# check below inspects the *installed* package rather than these sources.
export PYTHONSAFEPATH=1

# Warn if audio is being captured right now: the restart closes any open
# session (a restart implies a gap), though buffered audio is kept.
if systemctl is-active --quiet "$UNIT"; then
    (cd / && "$PY" - <<'EOF'
import json, urllib.request
from vinyl_archive.config import Config
cfg = Config.load()
host = cfg.server.host
host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
try:
    with urllib.request.urlopen(
            f"http://{host}:{cfg.server.port}/api/status", timeout=2) as r:
        st = json.load(r)
except Exception:
    raise SystemExit(0)
if st.get("active_session_id") is not None:
    print("    note: a session is in progress; the restart will close it"
          " (audio already buffered is kept)")
EOF
    ) || true
fi

echo "==> building and installing into the venv"
# Stage outside $APP_DIR: a copy of the sources next to the service's
# WorkingDirectory would shadow the installed package (python -m puts the
# working directory on sys.path first).
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT INT TERM
cp -r vinyl_archive pyproject.toml "$STAGE"/
# The version number does not change between commits, so force the app
# itself; this also clears out files no longer present in the package.
"$APP_DIR/venv/bin/pip" install --quiet --upgrade --force-reinstall --no-deps "$STAGE"
"$APP_DIR/venv/bin/pip" install --quiet "$STAGE"   # new or changed deps
# Remove the staging copy left by older versions of this script.
rm -rf "$APP_DIR/vinyl_archive" "$APP_DIR/pyproject.toml"

echo "==> checking the new build before restarting"
# Import errors, a broken package install and an unparseable config.toml all
# surface here, while the old version is still serving.
(cd / && "$PY" -c "import vinyl_archive.main") >/dev/null

echo "==> restarting $UNIT"
cp deploy/vinyl-archive.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart "$UNIT"

echo "==> waiting for it to answer"
if (cd / && "$PY" - <<'EOF'
import sys, time, urllib.request
from vinyl_archive.config import Config
cfg = Config.load()
host = cfg.server.host
host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
url = f"http://{host}:{cfg.server.port}/api/status"
for _ in range(30):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            if r.status == 200:
                print(f"    {url} is up")
                sys.exit(0)
    except Exception:
        time.sleep(1)
sys.exit(1)
EOF
)
then
    echo "==> done (revision $(git rev-parse --short HEAD 2>/dev/null || echo unknown))"
else
    echo "!! the service did not come up. Recent log:" >&2
    journalctl -u "$UNIT" -n 40 --no-pager >&2
    echo "!! roll back with: git checkout <last good revision> &&" >&2
    echo "!!                 sudo sh deploy/update.sh" >&2
    exit 1
fi
