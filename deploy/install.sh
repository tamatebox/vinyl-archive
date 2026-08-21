#!/bin/sh
# Install vinyl-archive on a Raspberry Pi (run as root from the repo root).
set -eu

APP_DIR=/opt/vinyl-archive
DATA_DIR=/var/lib/vinyl-archive
CONF_DIR=/etc/vinyl-archive

echo "==> creating service user"
id vinyl >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin vinyl
usermod -aG audio vinyl

echo "==> creating the virtualenv in $APP_DIR"
mkdir -p "$APP_DIR"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip

echo "==> creating data and config directories"
mkdir -p "$DATA_DIR" "$CONF_DIR"
chown -R vinyl:audio "$DATA_DIR"
if [ ! -f "$CONF_DIR/config.toml" ]; then
    cp config.example.toml "$CONF_DIR/config.toml"
    echo "    edit $CONF_DIR/config.toml — set [capture] device to your ADC"
    echo "    (list cards with: cat /proc/asound/cards)"
fi

echo "==> periodic TRIM for the card"
# Unlinking a file is not an erase. Without a discard reaching the card, its
# controller never learns those blocks are free, so it keeps relocating them
# and sustained write speed decays -- and a write that stalls here shows up as
# missing audio, because the thread that would block is the one reading PCM
# from the capture device. This server writes roughly 0.5 GB per hour of
# playing time, so it is the main writer on the card.
#
# Weekly batch trim, deliberately not the `discard` mount option: an inline
# discard fires on every unlink, synchronously, on that same path.
TRIM_UNIT=$(ls /lib/systemd/system/fstrim.timer /usr/lib/systemd/system/fstrim.timer 2>/dev/null | head -n1 || true)
if ! fstrim "$DATA_DIR" >/dev/null 2>&1; then
    echo "    this volume reports no discard support, so there is nothing to"
    echo "    schedule. Check with: lsblk -D. Leaving free space for the"
    echo "    card's own spare area is then the only lever left."
elif [ -n "$TRIM_UNIT" ]; then
    systemctl enable --now fstrim.timer
    echo "    enabled fstrim.timer (weekly, system-wide -- util-linux's own)"
else
    echo "    fstrim works here but fstrim.timer is not installed. Add a"
    echo "    weekly job for:  fstrim $DATA_DIR"
fi

echo "==> installing systemd unit"
cp deploy/vinyl-archive.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable vinyl-archive

# The code sync, pre-flight check and (re)start live in one place, so
# installing and updating cannot drift apart.
echo "==> installing the application"
sh deploy/update.sh

echo "==> done. Later updates: git pull && sudo sh deploy/update.sh"
