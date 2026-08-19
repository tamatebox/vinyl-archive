#!/bin/sh
# Install vinyl-archive on a Raspberry Pi (run as root from the repo root).
set -eu

APP_DIR=/opt/vinyl-archive
DATA_DIR=/var/lib/vinyl-archive
CONF_DIR=/etc/vinyl-archive

echo "==> creating service user"
id vinyl >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin vinyl
usermod -aG audio vinyl

echo "==> installing application to $APP_DIR"
mkdir -p "$APP_DIR"
cp -r vinyl_archive pyproject.toml "$APP_DIR"/
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install "$APP_DIR"

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

echo "==> done. start with: systemctl start vinyl-archive"
