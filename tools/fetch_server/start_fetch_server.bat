@echo off
rem Fetch server + public link (Cloudflare quick tunnel). Double-click; keep the window open.
cd /d "%~dp0"
python server.py --tunnel
pause
