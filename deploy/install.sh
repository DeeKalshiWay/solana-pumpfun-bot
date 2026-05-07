#!/usr/bin/env bash
# Linux/systemd install for pump_bot.
# Tested on Debian 12 / Ubuntu 22.04+.
#
# Run from a checkout of the repo as root:
#   sudo bash deploy/install.sh
#
# Idempotent. Re-run to update code; preserves logs/ and .env.

set -euo pipefail

INSTALL_DIR="/opt/pump_bot"
SERVICE_USER="pump"
SERVICE_FILE="/etc/systemd/system/pump_bot.service"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (sudo bash deploy/install.sh)" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "==> Source repo: $REPO_DIR"
echo "==> Install dir: $INSTALL_DIR"

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "==> Creating service user '$SERVICE_USER'"
  useradd --system --create-home --home-dir "$INSTALL_DIR" \
          --shell /usr/sbin/nologin "$SERVICE_USER"
else
  echo "==> Service user '$SERVICE_USER' already exists"
fi

mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/logs"

echo "==> Syncing code (preserving .env and logs/)"
rsync -a --delete \
  --exclude='.env' --exclude='.env.*' \
  --exclude='logs/' \
  --exclude='.git/' --exclude='.claude/' \
  --exclude='__pycache__/' --exclude='**/__pycache__/' \
  --exclude='venv/' --exclude='.venv/' \
  --exclude='*.ps1' --exclude='*.bat' \
  --exclude='tools/cloudflared.exe' --exclude='tools/ngrok.exe' --exclude='tools/ngrok.zip' \
  "$REPO_DIR/" "$INSTALL_DIR/"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  if [[ -f "$REPO_DIR/.env" ]]; then
    echo "==> Copying .env from repo"
    install -m 0600 "$REPO_DIR/.env" "$INSTALL_DIR/.env"
  else
    echo "WARN: no .env found. Copy .env.example -> $INSTALL_DIR/.env and fill it in."
    install -m 0600 "$REPO_DIR/.env.example" "$INSTALL_DIR/.env"
  fi
fi

echo "==> Building venv"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
  "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip wheel
# Drop playwright (unused) to keep the install lean.
grep -v '^playwright' "$INSTALL_DIR/requirements.txt" \
  | "$INSTALL_DIR/venv/bin/pip" install -r /dev/stdin

echo "==> Setting ownership"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 0600 "$INSTALL_DIR/.env"

echo "==> Installing systemd unit"
install -m 0644 "$INSTALL_DIR/deploy/pump_bot.service" "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable pump_bot.service
systemctl restart pump_bot.service

echo
echo "==> Installed. Manage with:"
echo "      systemctl status pump_bot"
echo "      journalctl -u pump_bot -f"
echo "      tail -f $INSTALL_DIR/logs/pump_bot.log"
echo
echo "==> Dashboard (localhost only):"
echo "      curl http://127.0.0.1:8765/api/status"
