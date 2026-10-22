#!/usr/bin/env bash
# Idempotent setup of the Buyble collector on this EC2 box (Amazon Linux 2023 or Ubuntu).
#   sudo bash install.sh git@github.com:OWNER/buyble-collector.git
# Safe to re-run: it pulls the latest code, refreshes the venv and units, and never touches a filled-in .env.
set -euo pipefail

REPO_URL="${1:-${REPO_URL:-}}"
APP_DIR=/opt/buyble-collector
APP_USER=buyble

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

echo "== packages"
if command -v dnf >/dev/null; then
  dnf install -y -q python3.11 python3.11-pip git
  PY=python3.11
elif command -v apt-get >/dev/null; then
  apt-get update -qq && apt-get install -y -qq python3 python3-venv git
  PY=python3
else
  echo "unsupported OS"; exit 1
fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' || { echo "need Python 3.11+"; exit 1; }

echo "== user"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /sbin/nologin "$APP_USER"
APP_HOME=$(getent passwd "$APP_USER" | cut -d: -f6)

echo "== deploy key (read-only GitHub access for the nightly git pull)"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.ssh"
if [[ ! -f "$APP_HOME/.ssh/id_ed25519" ]]; then
  sudo -u "$APP_USER" ssh-keygen -q -t ed25519 -N "" -C "buyble-collector@$(hostname)" -f "$APP_HOME/.ssh/id_ed25519"
fi
sudo -u "$APP_USER" sh -c "ssh-keygen -F github.com >/dev/null || ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null"

echo "== code"
if [[ -d "$APP_DIR/.git" ]]; then
  sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
else
  [[ -n "$REPO_URL" ]] || { echo "pass the repo URL: sudo bash install.sh git@github.com:OWNER/REPO.git"; exit 1; }
  install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
  if ! sudo -u "$APP_USER" git clone -q "$REPO_URL" "$APP_DIR"; then
    echo
    echo "Clone failed. Add this public key to the GitHub repo (Settings > Deploy keys, read-only), then re-run:"
    cat "$APP_HOME/.ssh/id_ed25519.pub"
    exit 1
  fi
fi

echo "== venv"
[[ -x "$APP_DIR/.venv/bin/python" ]] || sudo -u "$APP_USER" "$PY" -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "== .env"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "created $APP_DIR/.env from .env.example: fill it in with: sudo -u $APP_USER nano $APP_DIR/.env"
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo "== systemd"
install -m 644 "$APP_DIR/deploy/buyble-collect.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/buyble-collect.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now buyble-collect.timer
systemctl list-timers --no-pager | grep buyble || true

echo
echo "Done. Variable status (values never shown):"
sudo -u "$APP_USER" bash -c "cd $APP_DIR && .venv/bin/python -m buyble.config"
