#!/usr/bin/env bash
# One-shot setup for Raspberry Pi Zero 2 W (Raspberry Pi OS Lite).
# Creates the venv, installs dependencies, grants serial permissions and
# installs (but does not enable) the systemd service.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES=(epaper-demo epaper-ui)

echo "== apt packages =="
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip

echo "== python venv =="
cd "$REPO_DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "== serial permission (dialout group) =="
sudo usermod -aG dialout "$USER"

echo "== spi (needed by the LCD HAT) =="
if ! grep -q '^dtparam=spi=on' /boot/firmware/config.txt 2>/dev/null; then
  echo "dtparam=spi=on" | sudo tee -a /boot/firmware/config.txt >/dev/null
  echo "enabled SPI - reboot required before the LCD HAT will work"
fi
sudo usermod -aG spi,gpio "$USER" 2>/dev/null || true

echo "== systemd services =="
for name in "${SERVICES[@]}"; do
  sed "s|@REPO_DIR@|$REPO_DIR|g; s|@RUN_USER@|$USER|g" \
    "$REPO_DIR/raspi/${name}.service.in" \
    | sudo tee "/etc/systemd/system/${name}.service" >/dev/null
  if [ ! -f "/etc/default/${name}" ]; then
    sudo cp "$REPO_DIR/raspi/${name}.env" "/etc/default/${name}"
  fi
done
sudo systemctl daemon-reload

echo
echo "Setup finished."
echo "  1) Re-login (or reboot) so the dialout/spi/gpio groups take effect."
echo "  2) Test manually:   .venv/bin/python host/stop.py --addr 1"
echo "  3) Pick ONE service (they share the serial port):"
echo "       sudo systemctl enable --now epaper-demo   # headless auto-demo"
echo "       sudo systemctl enable --now epaper-ui     # LCD HAT menu"
