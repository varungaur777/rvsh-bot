#!/usr/bin/env bash
# Run the RVSH bot on Google Colab (for trying the bot - keep production on a server).
#
# In a Colab code cell:
#     !unzip -q -o /content/rvsh-bot-*.zip -d /content      # the deploy zip, uploaded with the Files panel
#     !bash /content/rvsh-bot/deploy/colab.sh
#
# Settings: upload your .env (the same file as on the PC) to /content/.env - or keep it in the project folder as
# .env, or point ENV_FILE at it. MONGO_URL is required here: Colab wipes its disk when the session ends.
# Data (downloads, logs) goes to /content/rvsh-data; RVSH_DATA=/content/drive/MyDrive/rvsh-data keeps it in Drive
# (mount Drive first). Stop: the cell's stop button (the bot stops gracefully). Running it again skips the installs.
# Only one copy of the bot may run: a new one asks the running one (PC / server) to hand over.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${RVSH_DATA:-/content/rvsh-data}"
cd "$DIR"

# ---- settings file
ENV_PATH="${ENV_FILE:-}"
if [ -z "$ENV_PATH" ]; then
  for f in /content/.env "$DIR/.env" /content/config.env; do
    if [ -f "$f" ]; then ENV_PATH="$f"; break; fi
  done
fi
if [ -z "$ENV_PATH" ] || [ ! -f "$ENV_PATH" ]; then
  echo "!! No .env found. Upload it to /content/.env (Files panel), then run this cell again." >&2
  exit 1
fi
if [ -z "${MONGO_URL:-}${MONGO_URI:-}" ] && \
   ! grep -Eq "^[[:space:]]*(MONGO_URL|MONGO_URI)[[:space:]]*=[[:space:]]*[^[:space:]#\"']" "$ENV_PATH"; then
  echo "!! MONGO_URL is empty in $ENV_PATH - it is required on Colab (the disk is wiped when the session ends)." >&2
  exit 1
fi
echo "==> settings: $ENV_PATH"

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "!! Python $(python3 -V 2>&1) is too old - the bot needs 3.12 or newer." >&2
  exit 1
fi

# ---- installs (once per Colab session, again when requirements.txt changes)
mkdir -p "$DATA"
STAMP="/tmp/rvsh-installed-$(md5sum requirements.txt | cut -c1-12)"
if [ ! -f "$STAMP" ]; then
  echo "==> system packages (Tesseract Hindi + English, Pango, fonts)"
  apt-get -qq update > /dev/null
  apt-get -qq install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-noto-core wget > /dev/null || exit 1
  if ! command -v cloudflared > /dev/null 2>&1; then
    echo "==> cloudflared (Web App tunnel)"
    curl -fsSL -o /usr/local/bin/cloudflared \
      https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
      && chmod +x /usr/local/bin/cloudflared
  fi
  echo "==> Python packages"
  python3 -m pip install -q -r requirements.txt || exit 1
  touch "$STAMP"
fi

# ---- run (/restart restarts inside the bot; /update exits with 3 = start the new code; a crash: 3 tries)
export ENV_FILE="$ENV_PATH" DOWNLOAD_ROOT="$DATA/downloads" RUNTIME_PROFILE=colab RVSH_SUPERVISED=1 \
       PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
stop=0
trap 'stop=1' INT TERM
crashes=0
echo "==> starting the bot (logs: $DATA/downloads/logs/bot.log)"
while true; do
  code=0
  python3 -m bot || code=$?
  if [ "$stop" = 1 ]; then echo "==> stopped"; break; fi
  echo "==> bot exited with $code"
  if [ "$code" = 3 ]; then continue; fi
  if [ "$code" = 1 ] && [ "$crashes" -lt 3 ]; then
    crashes=$((crashes + 1)); echo "==> starting again in 20 s ($crashes/3)"; sleep 20; continue
  fi
  break  # 0 = stopped / handed over, 2 = a setting is wrong (see above)
done
