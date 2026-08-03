#!/usr/bin/env bash
# One-shot setup for Raspberry Pi Zero 2 W (Raspberry Pi OS Lite).
# Creates the venv, installs dependencies, grants serial permissions and
# installs (but does not enable) the systemd service.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="epaper-demo"

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

echo "== systemd service =="
sed "s|@REPO_DIR@|$REPO_DIR|g; s|@RUN_USER@|$USER|g" \
  "$REPO_DIR/raspi/${SERVICE_NAME}.service.in" \
  | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null
if [ ! -f "/etc/default/${SERVICE_NAME}" ]; then
  sudo cp "$REPO_DIR/raspi/${SERVICE_NAME}.env" "/etc/default/${SERVICE_NAME}"
fi
sudo systemctl daemon-reload

echo
echo "Setup finished."
echo "  1) Re-login (or reboot) so the dialout group takes effect."
echo "  2) Test manually:   .venv/bin/python host/stop.py --addr 1"
echo "  3) Enable service:  sudo systemctl enable --now ${SERVICE_NAME}"
