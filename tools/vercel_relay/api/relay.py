"""Mumbai relay — a free Vercel function (region ``bom1``, Indian IP) that opens exam links for the bot.

Exam portals answer only Indian IP addresses; the bot runs abroad (GitHub Actions). The bot sends each request
here; this function makes it from Mumbai and returns the answer unchanged. All processing (marks, scorecards,
PDFs) stays on the bot.

Protected by ``RELAY_TOKEN`` (Vercel → Project → Settings → Environment Variables). Standard library only.

Browser checks (GET):
- ``/api/relay?token=…&ip=1``        → this function's public IP + country (should say IN / Mumbai)
- ``/api/relay?token=…&url=<link>``  → opens the link from Mumbai and shows the page (or the error status)

Bot use (POST, header ``X-Token``):
- one request: JSON ``{"method", "url", "headers": {…}, "body": "<base64>"}`` → the target's body as-is;
  ``X-Relay-Status`` = its status, ``X-Relay-Headers`` = base64 JSON list of ``[name, value]`` (all ``Set-Cookie``
  kept), ``X-Relay-Url`` = the URL. Redirects are **not** followed (the bot follows them, keeping its cookies).
  Vercel's response limit is 4.5 MB → a bigger answer gets status 413.
- many GETs at once (a sheet's ~500 images in ~13 calls, not 500 — Vercel's free plan counts calls):
  ``{"batch": [{"url", "headers"}, …]}`` → JSON ``{"items": [{"status", "ctype", "body": "<base64>"} |
  {"skip": true} | {"error": "…"}]}`` in the same order, fetched ``BATCH_THREADS`` at a time, redirects followed;
  items past the size budget come back as ``skip`` (the bot asks for those one by one).
- an ASP.NET async-postback chain (EDQT "Hindi / English version" buttons — each click needs the view state the
  previous reply returned, so they cannot run in parallel; 25 round trips abroad → Mumbai became one call):
  ``{"aspnet_chain": {"url", "headers", "fields", "sm_id", "panel", "targets": […], "gap"}}`` → JSON
  ``{"items": [{"status", "panels": ["<updatePanel html>", …]} | {"skip": true} | {"error": "…"}], "fields": {…},
  "cookies": {…}}`` (``fields`` = the hidden fields after the last click, so the bot can go on with skipped ones).
"""

from __future__ import annotations

import base64
import concurrent.futures
import hmac
from http.cookies import CookieError, SimpleCookie
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

TOKEN = os.environ.get("RELAY_TOKEN", "")
TIMEOUT = 50  # Vercel stops the function at 60 s (vercel.json maxDuration)
MAX_BODY = 4_400_000  # Vercel's response cap is 4.5 MB
BATCH_BUDGET = 3_000_000  # raw bytes per batch answer (base64 + JSON must stay under 4.5 MB)
BATCH_MAX, BATCH_THREADS, BATCH_ITEM_TIMEOUT = 48, 16, 15
CHAIN_MAX, CHAIN_ITEM_TIMEOUT, CHAIN_DEADLINE = 60, 15, 45  # postback chain: clicks · s per click · s in all
BATCH_DEADLINE = 42  # s: items not started by then come back as skip (the function must answer within 60 s)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "host", "content-length",
       "accept-encoding", "te", "trailer"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # hand 3xx back to the bot
        return None


_CTX = ssl.create_default_context()
_PLAIN = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX), _NoRedirect())
_FOLLOW = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX))


def _open(opener, method: str, url: str, headers: dict, body: bytes | None, timeout: float = TIMEOUT):
    """→ (status, [(name, value)], body bytes). HTTP errors are answers too (403, 500…), not failures."""
    h = {k: v for k, v in headers.items() if k.lower() not in HOP}
    h.setdefault("User-Agent", UA)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        r = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        r = e
    with r:
        data = r.read(MAX_BODY + 1)
        return r.status if hasattr(r, "status") else r.code, list(r.headers.items()), data


class handler(BaseHTTPRequestHandler):
    def _token_ok(self, given: str) -> bool:
        return bool(TOKEN) and hmac.compare_digest(given.encode(), TOKEN.encode())

    def _send(self, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------------- browser test
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if not TOKEN:
            return self._send(500, b"RELAY_TOKEN is not set (Vercel -> Settings -> Environment Variables), then Redeploy")
        if not self._token_ok((q.get("token") or [""])[0]):
            return self._send(401, b"wrong or missing token")
        if q.get("ip"):
            try:
                _s, _h, data = _open(_FOLLOW, "GET", "https://ipinfo.io/json", {}, None)
            except Exception as e:  # noqa: BLE001 — shown to the owner as-is
                return self._send(502, f"could not check the IP: {type(e).__name__}: {e}".encode())
            return self._send(200, data, "application/json")
        url = (q.get("url") or [""])[0]
        if not url.lower().startswith(("http://", "https://")):
            return self._send(400, b"add &url=<exam link>  (or &ip=1)")
        try:
            status, headers, data = _open(_FOLLOW, "GET", url, {}, None)
        except Exception as e:  # noqa: BLE001
            return self._send(502, f"could not open the link from Mumbai: {type(e).__name__}: {e}".encode())
        ctype = next((v for k, v in headers if k.lower() == "content-type"), "text/html")
        if len(data) > MAX_BODY:
            return self._send(413, b"page larger than 4.4 MB")
        # the exam site's own status first, so a 403 is visible at a glance
        note = f"<!-- relay: site answered {status}, {len(data)} bytes -->\n".encode()
        return self._send(200, note + data if "html" in ctype else data, ctype, {"X-Relay-Status": str(status)})

    # ---------------------------------------------------------------- bot
    def do_POST(self):
        if not self._token_ok(self.headers.get("X-Token", "")):
            return self._send(401, b"wrong or missing token")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            job = json.loads(self.rfile.read(n) or b"{}")
            if isinstance(job.get("batch"), list):
                return self._batch(job["batch"])
            if isinstance(job.get("aspnet_chain"), dict):
                return self._chain(job["aspnet_chain"])
            method = str(job.get("method") or "GET").upper()
            url = str(job["url"])
            headers = {str(k): str(v) for k, v in (job.get("headers") or {}).items()}
            body = base64.b64decode(job["body"]) if job.get("body") else None
        except (ValueError, KeyError, TypeError) as e:
            return self._send(400, f"bad request: {e}".encode())
        if not url.lower().startswith(("http://", "https://")):
            return self._send(400, b"url must be http(s)")
        try:
            status, rh, data = _open(_PLAIN, method, url, headers, body)
        except Exception as e:  # noqa: BLE001 — network trouble from Mumbai → the bot retries / reports
            return self._send(502, f"{type(e).__name__}: {e}".encode()[:500], extra={"X-Relay-Error": "network"})
        if len(data) > MAX_BODY:
            return self._send(413, b"answer larger than 4.4 MB", extra={"X-Relay-Error": "too_large"})
        packed = base64.b64encode(json.dumps(rh).encode()).decode()
        ctype = next((v for k, v in rh if k.lower() == "content-type"), "application/octet-stream")
        return self._send(200, data, ctype, {"X-Relay-Status": str(status), "X-Relay-Headers": packed,
                                             "X-Relay-Url": url})

    def _batch(self, items: list) -> None:
        jobs = []
        for it in items[:BATCH_MAX]:
            url = str((it or {}).get("url") or "")
            headers = {str(k): str(v) for k, v in ((it or {}).get("headers") or {}).items()}
            jobs.append((url, headers))

        deadline = time.monotonic() + BATCH_DEADLINE

        def one(job):
            url, headers = job
            if time.monotonic() > deadline:
                return {"skip": True}
            if not url.lower().startswith(("http://", "https://")):
                return {"error": "bad url"}
            try:
                status, rh, data = _open(_FOLLOW, "GET", url, headers, None, BATCH_ITEM_TIMEOUT)
            except Exception as e:  # noqa: BLE001 — per item; the bot retries it alone
                return {"error": type(e).__name__}
            ctype = next((v for k, v in rh if k.lower() == "content-type"), "application/octet-stream")
            return {"status": status, "ctype": ctype, "data": data}

        with concurrent.futures.ThreadPoolExecutor(BATCH_THREADS) as ex:
            done = list(ex.map(one, jobs))
        out, used = [], 0
        for r in done:
            data = r.pop("data", None)
            if data is not None:
                if used + len(data) > BATCH_BUDGET:
                    out.append({"skip": True})
                    continue
                used += len(data)
                r["body"] = base64.b64encode(data).decode()
            out.append(r)
        out += [{"skip": True}] * (len(items) - len(jobs))
        return self._send(200, json.dumps({"items": out}).encode(), "application/json")

    def _chain(self, job: dict) -> None:
        try:
            url = str(job["url"])
            headers = {str(k): str(v) for k, v in (job.get("headers") or {}).items()}
            fields = {str(k): str(v) for k, v in (job.get("fields") or {}).items()}
            sm_id, panel = str(job["sm_id"]), str(job["panel"])
            targets = [str(t) for t in (job.get("targets") or [])][:CHAIN_MAX]
            gap = min(max(float(job.get("gap") or 0), 0), 2)
        except (KeyError, TypeError, ValueError) as e:
            return self._send(400, f"bad chain: {e}".encode())
        if not url.lower().startswith(("http://", "https://")):
            return self._send(400, b"url must be http(s)")
        cookies: dict[str, str] = {}
        for part in headers.pop("Cookie", "").split(";"):
            k, sep, v = part.strip().partition("=")
            if sep:
                cookies[k] = v
        deadline = time.monotonic() + CHAIN_DEADLINE
        items = []
        for target in targets:
            if time.monotonic() > deadline:
                items.append({"skip": True})
                continue
            form = {**fields, sm_id: f"{panel}|{target}", "__EVENTTARGET": target, "__EVENTARGUMENT": "",
                    "__ASYNCPOST": "true"}
            h = dict(headers)
            h["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            if cookies:
                h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
            try:
                status, rh, data = _open(_PLAIN, "POST", url, h, urllib.parse.urlencode(form).encode(), CHAIN_ITEM_TIMEOUT)
            except Exception as e:  # noqa: BLE001 — this click only; the bot retries it alone
                items.append({"error": type(e).__name__})
                continue
            for k, v in rh:
                if k.lower() == "set-cookie":
                    c = SimpleCookie()
                    try:
                        c.load(v)
                    except CookieError:
                        continue
                    cookies.update({name: m.value for name, m in c.items()})
            panels = []
            if status == 200:
                for typ, ident, content in _parse_delta(data.decode("utf-8", "replace")):
                    if typ == "hiddenField":
                        fields[ident] = content  # the next click carries the fresh view state
                    elif typ == "updatePanel":
                        panels.append(content)
            items.append({"status": status, "panels": panels})
            if gap:
                time.sleep(gap)
        items += [{"skip": True}] * (len(job.get("targets") or []) - len(targets))
        return self._send(200, json.dumps({"items": items, "fields": fields, "cookies": cookies}).encode(),
                          "application/json")


def _parse_delta(text: str) -> list[tuple[str, str, str]]:
    """ASP.NET AJAX partial-postback reply ``length|type|id|content|…`` → ``[(type, id, content)]``."""
    out, i = [], 0
    while i < len(text):
        j = text.find("|", i)
        if j < 0 or not text[i:j].isdigit():
            break
        n = int(text[i:j])
        k = text.find("|", j + 1)
        l = text.find("|", k + 1)
        if k < 0 or l < 0:
            break
        out.append((text[j + 1:k], text[k + 1:l], text[l + 1:l + 1 + n]))
        i = l + 1 + n + 1
    return out
