#!/bin/bash
#
# scripts/setup_pi.sh
#
# One-time Raspberry Pi provisioning for the Smart Toll orchestrator (plan.md Phase 8).
# Installs system + Python deps, enables SPI for the RC522, installs a systemd service
# for core/main.py with auto-restart, and caps journald's disk usage so logs can't fill
# the SD card the way we already saw corrupt this box once (see plan.md Phase 0).
#
# No app-level log files are created on purpose: core/main.py only ever print()s, systemd
# captures that into journald, and journald's own size cap (set below) does the rotation —
# one less thing writing to the SD card, one less rotation policy to hand-roll.
#
# Usage: sudo ./scripts/setup_pi.sh
# Idempotent: safe to re-run (e.g. after a `git pull` that changed requirements.txt).

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(logname)}"

echo "==> Installing system packages"
apt-get update
# cmake + build-essential: libsql (requirements.txt) has no prebuilt wheel for Linux
# aarch64 and builds from source on first `pip install` — see requirements.txt's comment
# for what that involves.
apt-get install -y --no-install-recommends \
  python3-venv python3-pip python3-picamera2 cmake build-essential git

if command -v raspi-config >/dev/null 2>&1; then
  echo "==> Enabling SPI (required by the RC522 reader)"
  raspi-config nonint do_spi 0
else
  echo "==> raspi-config not found, skipping SPI enable (not Raspberry Pi OS?) — enable it manually" >&2
fi

echo "==> Adding $RUN_USER to gpio/spi groups"
usermod -aG gpio,spi "$RUN_USER"

echo "==> Creating virtualenv and installing Python dependencies"
# --system-site-packages so the venv can see apt's python3-picamera2 (it binds against
# system libcamera and isn't meant to be pip-installed on a Pi — see requirements.txt).
sudo -u "$RUN_USER" python3 -m venv --system-site-packages "$REPO_DIR/venv"
sudo -u "$RUN_USER" "$REPO_DIR/venv/bin/pip" install --upgrade pip
# PyTorch CPU wheels FIRST, explicitly. ultralytics (requirements.txt) depends on torch,
# and resolving that unaided on this box pulls the full CUDA build (torch +
# nvidia_cudnn_cu13 + cuda_toolkit, ~900MB) — useless on a Pi with no NVIDIA GPU, and a
# large pile of pointless SD-card writes. Installing the CPU wheel up front means the
# requirement is already satisfied by the time ultralytics is resolved below.
sudo -u "$RUN_USER" "$REPO_DIR/venv/bin/pip" install \
  --index-url https://download.pytorch.org/whl/cpu torch torchvision
sudo -u "$RUN_USER" "$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

echo "==> Installing systemd service"
cat > /etc/systemd/system/smart-toll.service <<SERVICE
[Unit]
Description=Smart Toll orchestrator (RFID -> charge -> log)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/venv/bin/python3 -m core.main
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable smart-toll.service

echo "==> Capping journald disk usage"
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/smart-toll.conf <<'JOURNALD'
[Journal]
SystemMaxUse=50M
SystemMaxFileSize=10M
JOURNALD
systemctl restart systemd-journald

cat <<NEXT

Done. Next steps:
  1. Fill in real secrets in $REPO_DIR/.env (PAYSTACK_SECRET_KEY, ARKESEL_API_KEY,
     TURSO_DATABASE_URL, TURSO_AUTH_TOKEN) - see .env.example. Never committed.
  2. Reboot so the gpio/spi group membership takes effect: sudo reboot
  3. Start the service: sudo systemctl start smart-toll
  4. Watch logs: journalctl -u smart-toll -f
NEXT
