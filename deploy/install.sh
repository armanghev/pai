#!/usr/bin/env bash
# First-time setup on a Debian/Ubuntu machine (tested on Ubuntu 24.04). Run as root:
#   sudo bash install.sh
# Safe to re-run; each step skips itself if already done.
set -euo pipefail

REPO="${PAI_REPO:-git@github.com:armanghev/pai.git}"  # or your fork via PAI_REPO
APP=/opt/pai
KEY=/etc/pai/deploy_key

[[ $EUID -eq 0 ]] || { echo "run as root (sudo bash install.sh)"; exit 1; }

echo "== packages"
apt-get update -qq
apt-get install -y -qq git python3.12 curl ca-certificates ffmpeg unzip >/dev/null

echo "== deno (yt-dlp needs a JS runtime for YouTube)"
if ! command -v /usr/local/bin/deno &>/dev/null; then
  curl -fsSL https://deno.land/install.sh | env DENO_INSTALL=/usr/local sh -s -- -y --no-modify-path
fi

echo "== pai user"
id pai &>/dev/null || useradd --system --home-dir "$APP" --no-create-home --shell /usr/sbin/nologin pai

echo "== deploy key"
install -d -m 750 -o root -g pai /etc/pai
if [[ ! -f $KEY ]]; then
  ssh-keygen -q -t ed25519 -N "" -C "pai@$(hostname)" -f "$KEY"
  chown pai:pai "$KEY" "$KEY.pub"; chmod 600 "$KEY"
  echo
  echo "Add this as a READ-ONLY deploy key on the repo ($REPO, Settings > Deploy keys),"
  echo "then run this script again:"
  echo
  cat "$KEY.pub"
  exit 0
fi

echo "== code"
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=/etc/pai/known_hosts -o StrictHostKeyChecking=accept-new"
touch /etc/pai/known_hosts && chown pai:pai /etc/pai/known_hosts
if [[ ! -d $APP/.git ]]; then
  install -d -o pai -g pai "$APP"
  sudo -u pai env GIT_SSH_COMMAND="$SSH" git clone -q "$REPO" "$APP"
fi
sudo -u pai git -C "$APP" config core.sshCommand "$SSH"

echo "== uv"
if ! command -v /usr/local/bin/uv &>/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
fi
sudo -u pai env HOME="$APP" UV_CACHE_DIR="$APP/.uv-cache" UV_PYTHON_DOWNLOADS=never \
  /usr/local/bin/uv sync --frozen --no-dev --project "$APP"

echo "== config"
[[ -f $APP/config.toml ]] || sudo -u pai cp "$APP/config.example.toml" "$APP/config.toml"
if [[ ! -f $APP/.env ]]; then
  sudo -u pai cp "$APP/.env.example" "$APP/.env"
fi
chown pai:pai "$APP/.env" "$APP/config.toml"; chmod 600 "$APP/.env"

echo "== systemd + journald"
install -m 644 "$APP/deploy/pai.service" /etc/systemd/system/pai.service
install -d /etc/systemd/journald.conf.d
install -m 644 "$APP/deploy/journald.conf.d/pai.conf" /etc/systemd/journald.conf.d/pai.conf
systemctl restart systemd-journald
systemctl daemon-reload
systemctl enable pai >/dev/null

if grep -qE '^TELEGRAM_TOKEN=.+' "$APP/.env"; then
  systemctl restart pai
  echo "pai is running. Follow logs with: journalctl -u pai -f"
else
  echo "Fill in $APP/.env (sudo -u pai nano $APP/.env), then: sudo systemctl restart pai"
fi
