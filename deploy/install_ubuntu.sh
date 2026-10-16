#!/usr/bin/env bash
# Install the RVSH bot as a systemd service on Ubuntu 24.04+ / Debian 13+ (needs Python 3.12 or newer).
# Older systems (Ubuntu 22.04, Debian 12): use Docker instead (docker compose up -d --build).
#
# Run from the project folder, as the user who will run the bot (sudo is asked for the system parts):
#     bash deploy/install_ubuntu.sh
# Then: put your .env in the project folder (cp config.env.example .env and fill it in), and
#     sudo systemctl start rvsh-bot && journalctl -u rvsh-bot -f
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
cd "$DIR"

echo "==> system packages"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev build-essential \
  tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
  libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b fontconfig fonts-noto-core \
  wget curl ca-certificates

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "!! Python $(python3 -V 2>&1) is too old — the bot needs 3.12 or newer. Use Docker on this system." >&2
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "==> cloudflared (Web App tunnel)"
  ARCH="$(dpkg --print-architecture)"
  sudo curl -fsSL -o /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}"
  sudo chmod +x /usr/local/bin/cloudflared
fi

echo "==> Python packages (.venv)"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> systemd service rvsh-bot (user ${RUN_USER}, folder ${DIR})"
sed -e "s|@USER@|${RUN_USER}|g" -e "s|@DIR@|${DIR}|g" deploy/rvsh-bot.service | sudo tee /etc/systemd/system/rvsh-bot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable rvsh-bot

if [ ! -f .env ]; then
  echo "!! No .env yet: cp config.env.example .env, fill it in, then: sudo systemctl start rvsh-bot"
else
  echo "==> done. Start: sudo systemctl start rvsh-bot · logs: journalctl -u rvsh-bot -f"
fi
echo "   Stop the bot on your PC first — only one copy may run."
