#!/usr/bin/env bash
# One-shot setup for the Radxa Cubie A7Z (Debian 11 image).
#
# Differences from raspi/setup.sh, all forced by the board:
#   - SPI is a device-tree overlay managed by rsetup, not a config.txt line
#   - GPIO goes through the character device (python-periphery), because
#     gpiozero only works on a Raspberry Pi
#   - Debian 11 ships Python 3.9, so python3-venv has to be installed
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES=(epaper-demo epaper-ui epaper-runlog)
SPI_OVERLAY=sun60iw2p1-spi1-spidev

echo "== apt packages =="
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip python3-dev gpiod

echo "== python venv =="
cd "$REPO_DIR"
# Test for the interpreter, not the directory: a venv created before
# python3-venv was installed leaves a directory with no pip in it, and
# checking `-d .venv` would silently accept that wreckage.
if [ ! -x .venv/bin/pip ]; then
  rm -rf .venv
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
# --no-cache-dir: this board has 1 GB and may be running a desktop, and
# pip's cache handling was enough to push it into swap.
.venv/bin/pip install --no-cache-dir -r requirements.txt
.venv/bin/pip install --no-cache-dir -r radxa/requirements.txt

echo "== spi overlay (header pins 19/21/23/24 -> /dev/spidev1.0) =="
if [ -f "/boot/dtbo/${SPI_OVERLAY}.dtbo.disabled" ]; then
  sudo mv "/boot/dtbo/${SPI_OVERLAY}.dtbo.disabled" \
          "/boot/dtbo/${SPI_OVERLAY}.dtbo"
  sudo u-boot-update
  echo "enabled ${SPI_OVERLAY} - REBOOT REQUIRED before the LCD will work"
elif [ -f "/boot/dtbo/${SPI_OVERLAY}.dtbo" ]; then
  echo "${SPI_OVERLAY} already enabled"
else
  echo "WARNING: ${SPI_OVERLAY}.dtbo not found; enable SPI1 with rsetup"
fi

echo "== groups =="
# dialout for the panel board's USB CDC; gpio/spidev are this image's
# own groups for the character devices.
# adm: without it journalctl shows this user nothing, which also blinds
# raspi/runlog.py - it reads the service log to find the cycle count.
sudo usermod -aG dialout,gpio,spidev,adm "$USER"

echo "== systemd services =="
for name in "${SERVICES[@]}"; do
  sed "s|@REPO_DIR@|$REPO_DIR|g; s|@RUN_USER@|$USER|g" \
    "$REPO_DIR/raspi/${name}.service.in" \
    | sudo tee "/etc/systemd/system/${name}.service" >/dev/null
  # Not every unit takes arguments, so an .env is optional.
  if [ -f "$REPO_DIR/raspi/${name}.env" ] && [ ! -f "/etc/default/${name}" ]; then
    sudo cp "$REPO_DIR/raspi/${name}.env" "/etc/default/${name}"
  fi
done
sudo systemctl daemon-reload

echo
echo "Setup finished."
echo "  1) Reboot (SPI overlay + group membership both need it)."
echo "  2) Check readiness:  .venv/bin/python -m ui.main --check"
echo "  3) Pick ONE service (they share the panel serial port):"
echo "       sudo systemctl enable --now epaper-ui     # LCD HAT menu"
echo "       sudo systemctl enable --now epaper-demo   # headless auto-demo"
