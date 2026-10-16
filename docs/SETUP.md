# Setup

## Windows (development — owner's PC)
Verified 2026-10-12: Python 3.14.7, Kurigram 2.2.26, TgCrypto 1.2.5, pymongo 4.18, lxml 6.1, PyMuPDF 1.28, ReportLab 5.0.1, uharfbuzz 0.56, WeasyPrint 70, Pillow 12.3, Tesseract 5.5.3 at `C:\Program Files\Tesseract-OCR` (`hin`, `eng` present; not on PATH — auto-detected), cloudflared in `C:\Program Files (x86)\cloudflared`.

```powershell
python -m pip install -r requirements.txt       # the bot
python -m pip install -r requirements-dev.txt   # + pytest / playwright, for the tests
python -m pytest            # offline tests; sample-based tests skip if samples are absent
python -m bot               # run the bot (reads .env)
```

## `.env` keys
`BOT_TOKEN`, `API_ID`, `API_HASH`, `OWNER_ID`, `DUMP_CHANNEL_ID`, `IMAGE_DUMP_CHANNEL_ID`, `BRANDING`, `IMAGE_BOT_TOKENS` (comma-separated), `MONGO_URL` (or `MONGO_URI`). Optional: `QUIZ_DUMP_CHANNEL_ID`, `DOWNLOAD_ROOT`, `TESSERACT_CMD`, `HTTP_PROXY`, `FORCE_SUB_CHANNELS`, `WEBAPP_PORT`, `WEBAPP_HOST`, `WEBAPP_BASE_URL`, `CLOUDFLARED_TOKEN`, `CLOUDFLARED_CMD`, `RUNTIME_PROFILE`, `DATA_PERSISTENT`, `SESSION_STRING`. See `config.env.example`. Real environment variables win over the file. `ENV_FILE` (environment only) says where the file is — default `.env` in the working folder.

## Linux / VPS / Docker
**Only one copy of the bot may run.** Stop it on the PC before starting it on the server (the instance lease in MongoDB makes a second copy wait until the first is gone).

Any server with 2 GB RAM or more works (the bot sizes its worker pool to the RAM / CPUs it finds). MongoDB stays on Atlas — nothing to install for it.

### A. Docker (any Linux — recommended)
Files: `Dockerfile` (Python 3.13 on Debian 12, Tesseract `hin` + `eng`, WeasyPrint's Pango, cloudflared, tini), `docker-compose.yml`, `.dockerignore` (no secrets, samples or data go into the image).
```bash
# on the server (Docker + the compose plugin installed: https://docs.docker.com/engine/install/)
git clone <your repo> rvsh && cd rvsh          # or copy the project folder (without JSONs/, samples, downloads/)
mkdir -p data && cp /path/to/your/.env data/.env
docker compose up -d --build
docker compose logs -f                           # "bot @… ready" = running
```
- Everything the bot writes is in `./data`: `data/.env` (edited by `/bot` → config.env), `data/downloads/` (tasks, `logs/bot.log`, sessions, the offline DB fallback `_db/`, the Web App tunnel state). Back it up; a rebuilt image keeps it.
- Update: `git pull && docker compose up -d --build`. Stop: `docker compose down` (graceful: running tasks get 90 s). `/restart` restarts inside the container.
- Web App: the free cloudflared tunnel runs inside the container — nothing to open. With your own domain + HTTPS proxy instead: `WEBAPP_HOST=0.0.0.0`, `WEBAPP_BASE_URL=https://…`, uncomment `ports:` in `docker-compose.yml`, `/qz` → tunnel off.

### B. Without Docker (Ubuntu 24.04+ / Debian 13+, Python ≥ 3.12)
```bash
cd rvsh && cp /path/to/your/.env .env
bash deploy/install_ubuntu.sh                    # apt packages, cloudflared, .venv, systemd service "rvsh-bot"
sudo systemctl start rvsh-bot
journalctl -u rvsh-bot -f                        # logs (also downloads/logs/bot.log)
```
`deploy/rvsh-bot.service` restarts the bot if it crashes (not on a bad `.env`: exit code 2), stops it gracefully with SIGTERM. Older systems (Ubuntu 22.04 = Python 3.10, Debian 12 = 3.11): use Docker.

### Updating the code
- **From Telegram (owner):** send the code zip (e.g. the deploy zip `dist/rvsh-bot-<date>.zip`) and reply `/update` → check card → ✅. The bot backs up its code (`<DOWNLOAD_ROOT>/_backup/`), writes the new files, installs new packages when `requirements.txt` changed, tests that the new code imports, and restarts as a new process (systemd / Docker restart it — exit code 3; a bot started by hand starts its own successor). If anything fails the old code is put back. Roll back: `/update` with a backup zip.
- Docker: an `/update` lives until the container is re-created; to keep it, rebuild the image from the new code (`docker compose up -d --build`).
- By hand: `git pull` (or unzip over the folder), then restart.

## Google Colab / GitHub Actions (for trying the bot — keep production on a server)
Both wipe their disk when they end, so **`MONGO_URL` is required** there. Only one copy runs: a new copy asks the running one (PC / server) to hand over and that one stops.

### Google Colab — simplest: `deploy/colab.sh` (no git, no notebook)
1. Colab → New notebook. Files panel (left bar) → upload the deploy zip (`dist/rvsh-bot-<date>.zip`) and your `.env` (the same file as on the PC; `MONGO_URL` must be filled in) to `/content`.
2. In a code cell:
   ```
   !unzip -q -o /content/rvsh-bot-*.zip -d /content
   !bash /content/rvsh-bot/deploy/colab.sh
   ```
3. The first run installs everything (a few minutes), then the bot's output shows in the cell. The cell's stop button stops the bot gracefully; running the cell again skips the installs. Logs: `/content/rvsh-data/downloads/logs/bot.log`. To keep downloads / logs in Google Drive: mount Drive, then `!RVSH_DATA=/content/drive/MyDrive/rvsh-data bash …/colab.sh`.

### Google Colab — `colab_launcher.ipynb` (settings from Colab 🔑 Secrets instead of a `.env` file)
1. Open the notebook in Colab (File → Upload notebook, or from GitHub).
2. 🔑 Secrets (left bar): `BOT_TOKEN`, `API_ID`, `API_HASH`, `OWNER_ID`, `DUMP_CHANNEL_ID`, `IMAGE_DUMP_CHANNEL_ID`, `BRANDING`, `IMAGE_BOT_TOKENS`, `MONGO_URL` (+ optional `QUIZ_DUMP_CHANNEL_ID`, `FORCE_SUB_CHANNELS`, `HTTP_PROXY`, `CLOUDFLARED_TOKEN`, `WEBAPP_BASE_URL`, `GITHUB_TOKEN` for a private repo) — switch on *Notebook access*.
3. First code cell: `REPO_URL` (git) or `ZIP_PATH` (upload the deploy zip `dist/rvsh-bot-<date>.zip`); `USE_DRIVE = True` keeps downloads / logs in Google Drive.
4. Runtime → Run all. The last cell shows the bot's output; ⏹ stops it gracefully. Colab ends free sessions after some hours / when idle.

### GitHub Actions — `.github/workflows/bot.yml`
GitHub's terms allow Actions only for the repository's own build / test / deploy work — **use it for testing, not as production hosting**. A private repository has limited free minutes.
1. Repository → Settings → Secrets and variables → Actions → add the same secrets as above (`MONGO_URL` required).
2. Actions → **bot** → Run workflow. A job lasts up to ~5 h 40 min; at ~5 h 30 min it starts the next run, which takes over (the old bot stops gracefully). A schedule every 6 h starts a run only when none is going.
3. Stop: cancel the run and disable the workflow. The log of each run is kept as an artifact (7 days). Code changes: push them (an `/update` lasts only until the job ends).

## Web App as a Telegram Mini App (fixed link)
A Mini App link `t.me/<bot>/<name>?startapp=<quiz>` opens a quiz inside Telegram from any chat (groups, channels). It needs a **fixed** HTTPS address — the free quick tunnel changes its address, so use a Cloudflare named tunnel (free, your domain on Cloudflare):
1. Cloudflare Zero Trust → Networks → Tunnels → Create a tunnel (Cloudflared) → copy the token. Public hostname: e.g. `quiz.your-domain` → service `HTTP` `127.0.0.1:8080` (`WEBAPP_PORT`).
2. `/bot → 🔑 config.env`: Cloudflare tunnel token = the token, Web App URL = `https://quiz.your-domain`. `/qz → Web App → Tunnel → Named`. `/restart`.
3. @BotFather → `/newapp` → your bot → title, description, a 640×360 photo → Web App URL `https://quiz.your-domain/app` → short name (e.g. `quiz`).
4. `/qz → Web App → Mini App short name` → `quiz`. `/qz → Web App → 📱 Mini App setup` shows what is still missing and checks the address.

## Moving to a new database
Every new quiz's file (`qz_….json`) is in the main dump channel. On a new database: forward / download them (or zip many) and reply `/load_quiz` — the quizzes come back with the same IDs; their cards stay in the image dump (the bot must still be admin there). `/db → 📦 Export everything` makes a full backup of the database.
