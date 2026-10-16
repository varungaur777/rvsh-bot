# Remote fetch server (optional)

Exam portals (TCS digialm, EDQT cbexams) often open only from Indian IP addresses. If the bot runs elsewhere
(e.g. a foreign VPS or Colab), run this small server on any machine that **can** open the links — your
Windows PC, an Indian VPS, a Raspberry Pi — and point the bot to it.

## Quick start on a Windows PC (bot on Colab / elsewhere)
1. Double-click `tools\fetch_server\start_fetch_server.bat` (or `python tools\fetch_server\server.py --tunnel`).
   Needs `cloudflared` (`winget install Cloudflare.cloudflared`).
2. The window prints **Fetch server URL** (`https://….trycloudflare.com`) and **Fetch server token**
   (made once, kept in `fetch_token.txt`). Wait for `>>> Link is working now` (about 1–2 minutes).
3. In Telegram: `/bot` → 🔑 config.env → ✏️ Fetch server URL, ✏️ Fetch server token → 🧪 Test fetch server.
   Then `/bot` → Network → Fetching links → **Auto**.
4. Keep the window open. Every new start gives a **new** URL — put it in the bot again (no restart needed).

Not the Web App link: the `trycloudflare.com` address in the bot's own log is the Web App, not this server.

## Setup (any OS with Python 3.10+)
1. Copy two files to the machine: `server.py` and `bot/net/fetcher.py` (as `fetcher.py`, same folder).
2. `pip install aiohttp curl_cffi` (`curl_cffi`: exam links are opened as Chrome — Chrome's TLS/HTTP2 fingerprint — not as Python)
3. Set a secret token (16+ characters; or leave it unset — `fetch_token.txt` is made) and start:
   - Windows (PowerShell): `$env:FETCH_TOKEN="your-long-secret"; python server.py`
   - Linux/macOS: `FETCH_TOKEN=your-long-secret python3 server.py`
4. Make it reachable from the bot (open the port, or use `cloudflared tunnel --url http://localhost:8787`).

Optional env vars: `PORT` (8787), `HOST` (0.0.0.0), `MAX_CONCURRENT` (2), `HTTP_PROXY`, `FETCH_ENGINE` (`auto` = every request as Chrome via curl_cffi, else the wget hybrid; or `chrome` / `wget` (wget images + Chrome pages) / `aiohttp` (plain Python)), `WGET_PATH` (wget binary if not on PATH). Install wget: `apt install wget` · Windows `winget install JernejSimoncic.Wget`.

## Bot side (`.env`)
```
FETCH_API_URL=https://your-server:8787
FETCH_API_TOKEN=your-long-secret
```
Or from Telegram, no file editing: `/bot` → **🔑 config.env** → ✏️ Fetch server URL / ✏️ Fetch server token
(in use at once, no restart; your token message is deleted) → **🧪 Test fetch server** (reachable? token accepted?).

Then in the bot (`/bot` → Network → Fetching links): `direct` (default — the server is not used), `remote`
(always use the server) or `auto` (try direct first, use the server when the portal refuses).

## Keep it running
- Windows: Task Scheduler → "At startup" → `python C:\path\server.py` (set `FETCH_TOKEN` as a user env var).
- Linux: a systemd service with `Environment=FETCH_TOKEN=...` and `Restart=always`.
