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

echo "==> installing systemd unit"
cp deploy/vinyl-archive.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable vinyl-archive

# The code sync, pre-flight check and (re)start live in one place, so
# installing and updating cannot drift apart.
echo "==> installing the application"
sh deploy/update.sh

echo "==> done. Later updates: git pull && sudo sh deploy/update.sh"
