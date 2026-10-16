# RVSH bot — Linux image (Debian 12 "bookworm", Python 3.13).
# Build:  docker compose build        Run:  docker compose up -d        Logs:  docker compose logs -f
# Everything the bot writes (tasks, logs, sessions, the JSON fallback DB, the Web App tunnel state) and its .env live
# in the /data volume, so a new image keeps them. See docs/SETUP.md → "Linux / VPS / Docker".

# ---- build: wheels for the packages that compile (TgCrypto, rl_accel, …)
FROM python:3.13-slim-bookworm AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

# ---- run
FROM python:3.13-slim-bookworm
ARG TARGETARCH=amd64
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    DOWNLOAD_ROOT=/data/downloads \
    ENV_FILE=/data/.env \
    RVSH_SUPERVISED=1

# OCR (Hindi + English), WeasyPrint's Pango, fonts (the PDF fonts are bundled in templates/fonts), wget (sheet
# downloads), tini (signals → graceful stop)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 fontconfig fonts-noto-core \
      wget curl ca-certificates tini tzdata \
 && rm -rf /var/lib/apt/lists/*

# cloudflared: the Web App's free HTTPS tunnel (/qz → Web App)
RUN curl -fsSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${TARGETARCH}" \
 && chmod +x /usr/local/bin/cloudflared

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

WORKDIR /app
COPY pyproject.toml config.env.example ./
COPY templates ./templates
COPY tools ./tools
COPY bot ./bot

VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "bot"]
