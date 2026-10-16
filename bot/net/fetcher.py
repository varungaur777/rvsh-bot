"""Exam-sheet fetcher: URL → one or more ``.mht`` archives, without a browser (spec §12.1).

**Standalone on purpose**: depends only on ``aiohttp`` + stdlib (``curl_cffi``, ``lxml`` optional), so the same file runs inside
the bot (``bot/net/``) and inside the optional remote fetch server (``tools/fetch_server/``) — no duplicated logic.

Public API
----------
- ``normalize_url(url)`` — trim, drop ``#fragment``, lower-case host, collapse repeated ``/`` in the path.
- ``detect_kind(url) -> "tcs" | "edqt" | None``
- ``await fetch_exam(url, proxy=None, timeout=30, concurrency=48, progress=None, engine="auto", on_assets=None)``
  ``-> list[Page]`` — ``concurrency`` = asset downloads in flight (an EDQT sheet has ~500 tiny images: 16 → 19 s,
  48 → 9 s with aiohttp, measured 2026-10-12). ``progress(text)`` gets status lines, ``on_assets(done, known)``
  asset counts (for the progress bar).
  - **Engines**: ``auto``/``chrome`` (owner 2026-10-14: "request jae to chrome ka use lage python ka nhi") —
    every request (page, EDQT postbacks, CSS, images) goes through ``curl_cffi`` **impersonating Chrome** (Chrome's
    TLS + HTTP/2 fingerprint and headers). ``wget`` (owner 2026-10-12; ``auto`` when ``curl_cffi`` is not
    installed) — the URL is **checked as Chrome first** (page + every EDQT section page), then **GNU wget downloads
    the CSS + images** of each page found (several wget processes in parallel; wget fetches one file at a time),
    and **whatever wget fails on is retried as Chrome**; wget not installed / not runnable → Chrome does it all.
    ``aiohttp`` — plain Python (Chrome User-Agent only), everything in-process; also what "Chrome" means when
    ``curl_cffi`` is missing. wget is found by ``find_wget()``: ``WGET_PATH`` → PATH → WinGet folders.
  - TCS: one page (all sections).
  - EDQT: the **user's own link is fetched first, alone** (the URL check); then ``ViewCandResponse.aspx`` and
    ``ViewCandResponse1…6.aspx`` with the same query are probed ``PROBE_PARALLEL`` (2) at a time; every page that
    really contains questions (``Q.No``) is kept, de-duplicated by section label, in page-number order.
  - EDQT **other language** (``other_language``, owner 2026-10-12): a page shows the candidate's medium only; each
    question's ``Hindi version`` / ``English version`` button is an ASP.NET async postback (``updHindi`` panel →
    popup). The fetcher posts them one at a time per page (``PROBE_PARALLEL`` pages at once, ``OTHER_LANG_GAP_S``
    apart), takes the popup's ``_HI``/``_EN`` images and adds them to the question's cells, so the MHT reads like a
    bilingual sheet. Any failure keeps the page as it was. ``parse_delta(text)`` reads the postback reply.
  - **Politeness / resilience** (owner 2026-10-12, a burst got Cloudflare 521): pages and assets are retried on
    5xx / 52x / 429 / timeouts with back-off (``Retry-After`` honoured, ≤ 10 s); when the server struggles the
    asset concurrency halves (``_Limiter``, floor 4).
  - every page's images (``src``, lazy ``data-src``/``data-original``/``srcset``), stylesheets **and what the
    stylesheets reference** (``url(...)`` fonts/backgrounds, ``@import``) are fetched and packed with the HTML into
    an MHT (same layout Chrome produces). One asset cache per fetch; pages are packed in parallel.
- ``await fetch_remote(api_url, token, url, timeout=120) -> list[Page]`` — the same, done by a remote fetch server.
- ``Page`` — ``url``, ``kind``, ``mht`` (bytes), ``section`` (EDQT label or None), ``assets`` (count).
- ``redact(url)`` — host + path only (the query identifies the candidate; never log it).
- ``build_mht(page_url, html, assets) -> bytes``

Exceptions → outcome
--------------------
``FetchError(reason, status)``: ``http_<code>`` (non-200, e.g. region block), ``not_exam`` (page has no
questions), ``network`` (timeout/DNS/TLS, or every EDQT page failed), ``remote`` (fetch server error),
(the download engine never causes a failure: wget problems fall back to the HTTP client). The bot turns it into the
"save the page as .mhtml and send it" guide.
"""

from __future__ import annotations

import asyncio
import base64
import email.utils
import glob
import hashlib
import json
import logging
import mimetypes
import os
import quopri
import re
import secrets
import shutil
import tempfile
import warnings
from dataclasses import dataclass, field
from html import unescape
from typing import Callable, Mapping, NamedTuple
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import aiohttp

try:  # Chrome's TLS + HTTP/2 fingerprint and headers (optional: without it, aiohttp)
    from curl_cffi import requests as _cffi
except ImportError:  # pragma: no cover
    _cffi = None
# Windows: curl_cffi adds a selector thread to the Proactor loop by itself and says so on every start — log noise
warnings.filterwarnings("ignore", message=r"\s*Proactor event loop does not implement add_reader")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
           "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"}
IMPERSONATE = "chrome"  # curl_cffi: the newest Chrome it knows
NET_ERRORS: tuple[type[BaseException], ...] = (aiohttp.ClientError, asyncio.TimeoutError) + (
    (_cffi.exceptions.RequestException,) if _cffi is not None else ())
MAX_ASSETS = 800
MAX_ASSET_BYTES = 5 * 1024 * 1024
# temporary server trouble -> retry with back-off (Cloudflare 52x = origin refused / timed out)
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
BACKOFF_S = (1.5, 4.0, 8.0)
PAGE_TRIES, ASSET_TRIES = 3, 3
PROBE_PARALLEL = 2  # EDQT section pages probed at a time (after the user's own link)
log = logging.getLogger(__name__)


class FetchError(Exception):
    def __init__(self, reason: str, status: int | None = None, detail: str = "") -> None:
        self.reason, self.status, self.detail = reason, status, detail
        super().__init__(f"{reason} (status={status}) {detail}".strip())


@dataclass
class Page:
    url: str
    kind: str
    mht: bytes
    section: str | None = None
    assets: int = 0
    missing: list[str] = field(default_factory=list)
    engine: str = ""


def normalize_url(url: str) -> str:
    p = urlsplit(url.strip().strip("<>\"'"))
    path = re.sub(r"/{2,}", "/", p.path)
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower(), path, p.query, ""))


def redact(url: str) -> str:
    p = urlsplit(url)
    return f"{p.netloc}{p.path}" + ("?…" if p.query else "")


def detect_kind(url: str) -> str | None:
    u = url.lower()
    if "digialm.com" in u and "assessmentqphtmlmode1" in u:
        return "tcs"
    if "viewcandresponse" in u and "cbexams.com" in u:
        return "edqt"
    return None


def edqt_variants(url: str) -> list[str]:
    """Base page + numbered section pages with the same query string."""
    m = re.search(r"(?i)(ViewCandResponse)(\d*)(\.aspx)", url)
    if not m:
        return [url]
    head, tail = url[:m.start()], url[m.end():]
    return [f"{head}{m.group(1)}{n}{m.group(3)}{tail}" for n in ["", "1", "2", "3", "4", "5", "6"]]


# ------------------------------------------------------------------ fetching
ENGINES = ("auto", "chrome", "wget", "aiohttp")
StatusFn = Callable[[str], None]  # a short status line ("checking section pages")
AssetsFn = Callable[[int, int], None]  # (assets done, assets known so far)


async def fetch_exam(url: str, *, proxy: str | None = None, timeout: float = 30, concurrency: int = 48,
                     progress: StatusFn | None = None, kind: str | None = None, engine: str = "auto",
                     on_assets: AssetsFn | None = None, other_language: bool = True) -> list[Page]:
    """``kind`` overrides URL detection (tests serve pages from 127.0.0.1). ``engine``: ``auto``/``chrome`` —
    everything as Chrome (``curl_cffi``; ``auto`` without it: the wget hybrid); ``wget`` — pages as Chrome, assets
    by wget, wget's failures retried as Chrome; ``aiohttp`` — plain Python, everything in-process."""
    url = normalize_url(url)
    kind = kind or detect_kind(url)
    if kind is None:
        raise FetchError("unsupported_url")
    args = (proxy, timeout, concurrency, on_assets)
    if engine == "aiohttp":
        dl = _HttpDownloader(*args, chrome=False)
    elif engine == "chrome" or (engine == "auto" and _cffi is not None):
        dl = _HttpDownloader(*args)
    else:  # wget, or auto without curl_cffi: the hybrid, which falls back by itself
        dl = _WgetDownloader(*args)
    return await _fetch(url, kind, dl, progress, other_language)


async def _fetch(url: str, kind: str, dl: "_Downloader", progress: StatusFn | None,
                 other_language: bool = True) -> list[Page]:
    async with dl:
        if kind == "tcs":
            html, final, status = await dl.page(url)
            if status != 200:
                raise FetchError(f"http_{status}", status)
            if b"question-pnl" not in html:
                raise FetchError("not_exam", status)
            return [await _pack(dl, final, html, "tcs", None)]
        # 1) the link the user sent, alone (with retries): the URL check
        html, final, status = await dl.page(url)
        if status != 200:
            raise FetchError(f"http_{status}", status)
        if b"Q.No" not in html:
            raise FetchError("not_exam", status)
        variants = edqt_variants(url)
        own = variants.index(url) if url in variants else 0
        found = {own: (final, html, _edqt_label(html) or hashlib.sha1(html).hexdigest())}
        # 2) the other section pages, gently (EDQT servers are slow: a burst gets Cloudflare 521)
        if progress:
            progress("checking the other section pages")
        gate = asyncio.Semaphore(PROBE_PARALLEL)

        async def probe(i: int, v: str):
            async with gate:
                try:
                    return i, await dl.page(v)
                except FetchError:
                    return i, None

        seen = {found[own][2]}
        for i, got in await asyncio.gather(*(probe(i, v) for i, v in enumerate(variants) if i != own)):
            if got is None:
                continue
            h, f, st = got
            if st != 200 or b"Q.No" not in h:
                continue
            label = _edqt_label(h) or hashlib.sha1(h).hexdigest()
            if label not in seen:
                seen.add(label)
                found[i] = (f, h, label)
        pages = [found[i] for i in sorted(found)]  # section order = page number order
        if progress:
            progress(f"found {len(pages)} section page{'s' if len(pages) != 1 else ''}")
        if other_language:  # the other medium, behind each question's "Hindi version" / "English version" button
            gate = asyncio.Semaphore(PROBE_PARALLEL)
            done = await asyncio.gather(*(_other_language(dl, f, h, progress, gate) for f, h, _lb in pages))
            pages = [(f, h2, lb) for (f, _h, lb), h2 in zip(pages, done)]
        return list(await asyncio.gather(*(_pack(dl, f, h, "edqt", lb) for f, h, lb in pages)))


def _edqt_label(html: bytes) -> str | None:
    m = re.search(rb'id="lblsubject"[^>]*>([^<]*)<', html)
    return unescape(m.group(1).decode("utf-8", "replace")).strip() if m else None


_IMG_ATTRS = re.compile(r'<img\b[^>]*>', re.I)
_ATTR = re.compile(r'(?<![\w-])(src|data-src|data-original|data-lazy-src|srcset)\s*=\s*("([^"]*)"|\'([^\']*)\')', re.I)
_SRC_ATTR = re.compile(r"""(?<![\w-])src\s*=\s*("[^"]*"|'[^']*')""", re.I)
_CSS = re.compile(r'<link\b[^>]*rel=["\']?stylesheet["\']?[^>]*>', re.I)
_HREF = re.compile(r'\bhref\s*=\s*("([^"]*)"|\'([^\']*)\')', re.I)
_PLACEHOLDER = re.compile(r"(spacer|blank|placeholder|loading)\.(gif|png|svg)|^data:", re.I)
_CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)|@import\s+(['"])([^'"]+)\3""", re.I)


def _asset_urls(page_url: str, html_text: str) -> tuple[str, list[str]]:
    """Resolve asset URLs; lazy images get their real URL copied into ``src`` so the MHT is self-consistent."""
    urls: list[str] = []

    def fix_img(m: re.Match) -> str:
        tag = m.group(0)
        attrs = {a.group(1).lower(): (a.group(3) if a.group(3) is not None else a.group(4)) for a in _ATTR.finditer(tag)}
        lazy = attrs.get("data-src") or attrs.get("data-original") or attrs.get("data-lazy-src")
        if not lazy and attrs.get("srcset"):
            lazy = attrs["srcset"].split(",")[0].strip().split(" ")[0]
        src = attrs.get("src")
        if lazy and (not src or _PLACEHOLDER.search(src)):
            src = lazy
            tag = re.sub(r'(?<![\w-])src\s*=\s*("[^"]*"|\'[^\']*\')', f'src="{lazy}"', tag, count=1, flags=re.I) if "src" in attrs \
                else tag[:-1].rstrip("/") + f' src="{lazy}">'
        if src and not src.startswith("data:"):
            absolute = urljoin(page_url, unescape(src))
            urls.append(absolute)
            if absolute != src:  # absolute src like Chrome's MHT, so any reader finds the part
                tag = _SRC_ATTR.sub(lambda _m: f'src="{absolute}"', tag, count=1)
        return tag

    html_text = _IMG_ATTRS.sub(fix_img, html_text)
    for m in _CSS.finditer(html_text):
        h = _HREF.search(m.group(0))
        if h:
            urls.append(urljoin(page_url, unescape(h.group(2) or h.group(3))))
    return html_text, _web_only(urls)


def _css_urls(css_url: str, css: bytes) -> list[str]:
    """Fonts / background images / @imports referenced by a stylesheet (resolved against the stylesheet)."""
    text = css.decode("utf-8", "replace")
    return _web_only(urljoin(css_url, (m.group(2) or m.group(4)).strip()) for m in _CSS_URL.finditer(text))


def _web_only(urls) -> list[str]:
    out = [u for u in urls if u.lower().startswith(("http://", "https://"))]  # no cid:/data:/javascript:
    return list(dict.fromkeys(out))[:MAX_ASSETS]


async def _pack(dl: "_Downloader", page_url: str, html: bytes, kind: str, section: str | None) -> Page:
    charset = (re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', html[:4096]) or [None, b"utf-8"])[1].decode()
    text = html.decode(charset, "replace")
    text, urls = _asset_urls(page_url, text)
    got = dict(zip(urls, await dl.get_many(urls, page_url)))
    css = [(u, r[1]) for u, r in got.items() if r and (r[0] == "text/css" or urlsplit(u).path.lower().endswith(".css"))]
    missing = [u for u, r in got.items() if not r]  # only what the page itself uses counts as missing
    for css_url, body in css:  # second round: what the stylesheets reference (fonts, backgrounds) — optional,
        extra = [u for u in _css_urls(css_url, body) if u not in got]  # Chrome also saves only what is used
        got.update(zip(extra, await dl.get_many(extra, css_url)))
    results = [(u, r[0], r[1]) for u, r in got.items() if r]
    mht = build_mht(page_url, text.encode(charset, "replace"), results, charset)
    return Page(url=page_url, kind=kind, mht=mht, section=section, assets=len(results), missing=missing, engine=dl.name)


# ------------------------------------------------------------------ EDQT: the other language (owner 2026-10-12)
_POSTBACK = re.compile(r"__doPostBack\((?:&#39;|')([\w$]+\$btn(Hindi|English))(?:&#39;|')")
_HIDDEN_INPUT = re.compile(r"<input\b[^>]*\btype=[\"']hidden[\"'][^>]*>", re.I)
_HIDDEN_ATTR = re.compile(r"""\b(name|value|id)=(?:"([^"]*)"|'([^']*)')""", re.I)
_SM_INIT = re.compile(r"PageRequestManager\._initialize\('([^']+)',\s*'([^']+)',\s*\[([^\]]*)\]")
_FORM_ACTION = re.compile(r"<form\b[^>]*\baction=[\"']([^\"']*)[\"']", re.I)
_IMG_SRC = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.I)
_SRC_LANG = re.compile(r"_(EN|HI)\.(?:jpe?g|png|gif)", re.I)
OTHER_LANG_GAP_S = 0.25  # pause between two postbacks of one page (politeness)
OTHER_LANG_MAX = 60      # postbacks per page at most


def _hidden_fields(text: str) -> dict[str, str]:
    out = {}
    for tag in _HIDDEN_INPUT.findall(text):
        attrs = {m.group(1).lower(): unescape(m.group(2) if m.group(2) is not None else m.group(3)) for m in _HIDDEN_ATTR.finditer(tag)}
        if attrs.get("name"):
            out[attrs["name"]] = attrs.get("value", "")
    return out


def parse_delta(text: str) -> list[tuple[str, str, str]]:
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


def _inject_other_language(text: str, per_question: list[list[str]]) -> tuple[str, int]:
    """Add each question's other-language images to its cells after the original ones (question cell, then one per
    option row) — the page then reads like a bilingual sheet (``parsers.edqt`` splits cells by ``_EN``/``_HI``).
    Needs lxml (the bot always has it; a bare fetch server without it keeps one language)."""
    try:
        import lxml.html
    except ImportError:
        return text, 0
    root = lxml.html.document_fromstring(text)
    tables = []
    for qfont in root.xpath('//font[contains(@class,"tdtext")]'):
        if re.match(r"\s*Q\.?\s*No\s*[:.]?\s*\d+", qfont.text_content() or "", re.I):
            table = next(iter(qfont.iterancestors("table")), None)
            if table is not None:
                tables.append((table, qfont))
    if len(tables) != len(per_question):
        return text, 0
    done = 0
    for (table, qfont), imgs in zip(tables, per_question):
        if not imgs:
            continue
        rows = table.xpath("./tr | ./tbody/tr")
        qrow = next((r for r in rows if qfont in r.iter()), None)
        if qrow is None:
            continue
        qtds = qrow.xpath("./td")[1:]
        n_q = sum(len(td.xpath(".//img")) for td in qtds)
        opt_cells = [r.xpath("./td")[1] for r in rows[rows.index(qrow) + 1:]
                     if len(r.xpath("./td")) >= 2 and r.xpath("./td")[1].xpath(".//img")]
        if not qtds or n_q < 1 or len(imgs) != n_q + len(opt_cells):
            continue
        cells = [qtds[-1]] * n_q + opt_cells
        for cell, src in zip(cells, imgs):
            for el in (lxml.html.Element("br"), lxml.html.Element("br"), lxml.html.Element("img", src=src)):
                cell.append(el)
        done += 1
    if not done:
        return text, 0
    return lxml.html.tostring(root, encoding="unicode", method="html"), done


async def _other_language(dl: "_Downloader", page_url: str, html: bytes, progress: StatusFn | None,
                          gate: asyncio.Semaphore) -> bytes:
    """An EDQT page shows the candidate's medium only; the other language sits behind one ``Hindi version`` /
    ``English version`` button per question (ASP.NET async postback into the ``updHindi`` panel; popup ``qDivH`` /
    ``qDivE``). Click each one — one at a time per page, ``gate`` across pages — take the popup's images and add them
    to that question. Any problem → the page as it was (the user still gets their sheet)."""
    charset = (re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', html[:4096]) or [None, b"utf-8"])[1].decode()
    text = html.decode(charset, "replace")
    targets = list(dict.fromkeys(m.group(1) for m in _POSTBACK.finditer(text)))[:OTHER_LANG_MAX]
    fields = _hidden_fields(text)
    sm = _SM_INIT.search(text)
    if not targets or "__VIEWSTATE" not in fields or not sm:
        return html
    sm_id = sm.group(1)
    panels = [p.strip().strip("'\"") for p in sm.group(3).split(",") if p.strip()]
    panel = (panels[0][1:] if panels and panels[0].startswith("t") else (panels[0] if panels else "updHindi")) or "updHindi"
    action = _FORM_ACTION.search(text)
    post_url = urljoin(page_url, unescape(action.group(1))) if action else page_url
    own = {m.group(1).upper() for m in _SRC_LANG.finditer(text)}
    per_q: list[list[str]] = []
    headers = {"X-MicrosoftAjax": "Delta=true", "X-Requested-With": "XMLHttpRequest", "Referer": page_url,
               "Cache-Control": "no-cache"}
    for n, target in enumerate(targets, 1):
        form = {**fields, sm_id: f"{panel}|{target}", "__EVENTTARGET": target, "__EVENTARGUMENT": "", "__ASYNCPOST": "true"}
        imgs: list[str] = []
        try:
            async with gate:
                body, status = await dl.post(post_url, form, headers)
            if status == 200:
                for typ, ident, content in parse_delta(body.decode("utf-8", "replace")):
                    if typ == "hiddenField":
                        fields[ident] = content  # the next postback carries the fresh view state
                    elif typ == "updatePanel":
                        imgs += [urljoin(page_url, unescape(s)) for s in _IMG_SRC.findall(content)
                                 if (m := _SRC_LANG.search(s)) and m.group(1).upper() not in own]
        except FetchError:
            pass
        per_q.append(list(dict.fromkeys(imgs)))
        if progress and (n % 5 == 0 or n == len(targets)):
            progress(f"other language: {n}/{len(targets)} questions")
        await asyncio.sleep(OTHER_LANG_GAP_S)
    if not any(per_q):
        return html
    new_text, done = _inject_other_language(text, per_q)
    log.info("edqt other language: %d/%d questions added for %s", done, len(targets), redact(page_url))
    return new_text.encode(charset, "replace") if done else html


# ------------------------------------------------------------------ download engines
_MAGIC = ((b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"), (b"GIF8", "image/gif"), (b"RIFF", "image/webp"),
          (b"wOF2", "font/woff2"), (b"wOFF", "font/woff"), (b"<svg", "image/svg+xml"))


def _sniff(url: str, data: bytes) -> str:
    """Content type for a file wget saved (it does not record headers in -nv mode)."""
    for magic, ctype in _MAGIC:
        if data[:16].lstrip().startswith(magic):
            return ctype
    return mimetypes.guess_type(urlsplit(url).path)[0] or "application/octet-stream"


class _Downloader:
    """One fetch: page requests + asset downloads, each asset URL fetched once (shared by all pages)."""

    name = "?"

    def __init__(self, proxy: str | None, timeout: float, concurrency: int, on_assets: AssetsFn | None) -> None:
        self.proxy, self.timeout, self.concurrency, self.on_assets = proxy, timeout, max(1, concurrency), on_assets
        self.cache: dict[str, asyncio.Future] = {}
        self.done = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        for f in self.cache.values():
            if not f.done():
                f.cancel()

    async def page(self, url: str) -> tuple[bytes, str, int]:
        raise NotImplementedError

    async def post(self, url: str, form: dict, headers: dict) -> tuple[bytes, int]:
        raise NotImplementedError

    async def get_many(self, urls: list[str], referer: str) -> list[tuple[str, bytes] | None]:
        loop = asyncio.get_running_loop()
        new = [u for u in dict.fromkeys(urls) if u not in self.cache]
        for u in new:
            self.cache[u] = loop.create_future()
        if new:
            self._report()
            await self._download(new, referer)
        return list(await asyncio.gather(*(self.cache[u] for u in urls)))

    async def _download(self, urls: list[str], referer: str) -> None:
        """Resolve ``self.cache[url]`` for every url with ``(ctype, data)`` or ``None`` (must resolve them all)."""
        raise NotImplementedError

    def _resolve(self, url: str, value) -> None:
        f = self.cache.get(url)
        if f is not None and not f.done():
            f.set_result(value)
            self.done += 1
            self._report()

    def _report(self) -> None:
        if self.on_assets:
            self.on_assets(self.done, len(self.cache))


class _Limiter:
    """Concurrency limit that **halves** when the server struggles (5xx / Cloudflare 52x / timeouts), never below
    ``floor`` — so a slow exam server is not pushed into refusing connections (owner 2026-10-12: 521)."""

    def __init__(self, limit: int, floor: int = 4) -> None:
        self.limit, self.floor, self.active = max(1, limit), min(floor, max(1, limit)), 0
        self.cond = asyncio.Condition()

    async def __aenter__(self):
        async with self.cond:
            await self.cond.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def __aexit__(self, *exc) -> None:
        async with self.cond:
            self.active -= 1
            self.cond.notify_all()

    def shrink(self) -> None:
        if self.limit > self.floor:
            self.limit = max(self.floor, self.limit // 2)
            log.info("exam server struggling: %d downloads at a time now", self.limit)


def _retry_after(r, attempt: int) -> float:
    try:
        return min(float(r.headers.get("Retry-After", "")), 10.0)
    except (TypeError, ValueError, AttributeError):
        return BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]


class _Resp(NamedTuple):
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes | None  # None: over the size cap


class _HttpDownloader(_Downloader):
    """Pages, form POSTs and assets in-process. ``chrome`` (owner 2026-10-14: "request jae to chrome ka use lage
    python ka nhi"): through ``curl_cffi`` impersonating Chrome — Chrome's TLS + HTTP/2 fingerprint and its own
    headers, so the exam site sees a Chrome browser, not Python. Without ``curl_cffi`` (or ``chrome=False``):
    aiohttp with a Chrome User-Agent."""

    def __init__(self, *args, chrome: bool = True) -> None:
        super().__init__(*args)
        self.chrome = chrome and _cffi is not None
        self.http = "chrome" if self.chrome else "aiohttp"

    @property
    def name(self) -> str:
        return self.http

    async def __aenter__(self):
        if self.chrome:
            self.cs = _cffi.AsyncSession(impersonate=IMPERSONATE, timeout=(self.timeout, 3 * self.timeout),
                                         max_clients=self.concurrency + 4, proxy=self.proxy,
                                         headers={"Accept-Language": HEADERS["Accept-Language"]})
        else:
            tmo = aiohttp.ClientTimeout(total=None, sock_connect=self.timeout, sock_read=self.timeout)
            self.s = aiohttp.ClientSession(headers=HEADERS, timeout=tmo, cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.sem = _Limiter(self.concurrency)
        return self

    async def __aexit__(self, *exc) -> None:
        await super().__aexit__(*exc)
        await (self.cs.close() if self.chrome else self.s.close())

    async def _send(self, method: str, url: str, *, headers: dict | None = None, data: dict | None = None,
                    cap: int | None = None) -> _Resp:
        """One request; network trouble raises one of ``NET_ERRORS``."""
        if self.chrome:
            r = await self.cs.request(method, url, headers=headers, data=data, allow_redirects=True)
            body = r.content
            return _Resp(r.status_code, str(r.url), r.headers, None if cap and len(body) > cap else body)
        async with self.s.request(method, url, headers=headers, data=data, proxy=self.proxy) as r:
            body = await _read_capped(r, cap) if cap else await r.read()
            return _Resp(r.status, str(r.url), r.headers, body)

    async def _retrying(self, method: str, url: str, **kw) -> _Resp:
        """Temporary failures (5xx, Cloudflare 52x, 429, timeouts) are retried with back-off."""
        for attempt in range(PAGE_TRIES):
            last = attempt == PAGE_TRIES - 1
            try:
                r = await self._send(method, url, **kw)
            except NET_ERRORS as e:
                if last:
                    raise FetchError("network", None, repr(e)) from e
                await asyncio.sleep(BACKOFF_S[attempt])
                continue
            if r.status in RETRY_STATUS and not last:
                await asyncio.sleep(_retry_after(r, attempt))
                continue
            return r
        raise FetchError("network")

    async def page(self, url: str) -> tuple[bytes, str, int]:
        r = await self._retrying("GET", url)
        return r.body or b"", r.url, r.status

    async def post(self, url: str, form: dict, headers: dict) -> tuple[bytes, int]:
        """Form POST (ASP.NET partial postback); retried like ``page``."""
        r = await self._retrying("POST", url, data=form, headers=headers)
        return r.body or b"", r.status

    async def _download(self, urls: list[str], referer: str) -> None:
        await asyncio.gather(*(self._one(u, referer) for u in urls))

    async def _one(self, url: str, referer: str) -> None:
        for attempt in range(ASSET_TRIES):
            retry, wait = False, BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            async with self.sem:
                try:
                    r = await self._send("GET", url, headers={"Referer": referer}, cap=MAX_ASSET_BYTES)
                    ok = r.status == 200 and r.body is not None
                    data = r.body if ok else b""
                    ctype = (r.headers.get("Content-Type") or "application/octet-stream").split(";")[0]
                    retry = r.status in RETRY_STATUS
                    wait = _retry_after(r, attempt) if retry else wait
                except NET_ERRORS:
                    ok, data, ctype, retry = False, b"", "", True
            if ok or not retry or attempt == ASSET_TRIES - 1:
                break
            self.sem.shrink()  # the server is struggling: fewer at a time, then try again
            await asyncio.sleep(wait)
        self._resolve(url, (ctype, data) if ok else None)


async def _read_capped(r, cap: int) -> bytes | None:
    """The whole body, or None when it is over ``cap``. (``StreamReader.read(n)`` returns only the bytes that have
    arrived so far — on a slow server that cut images in half, which then showed as grey boxes in the PDF.)"""
    buf = bytearray()
    async for chunk in r.content.iter_chunked(64 * 1024):
        buf += chunk
        if len(buf) > cap:
            return None
    return bytes(buf)


def find_wget() -> str | None:
    """``WGET_PATH`` → ``PATH`` → WinGet's folders (a running process does not see a PATH change made by an
    install, so the bot finds a freshly installed wget without a restart)."""
    env = os.environ.get("WGET_PATH")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("wget")
    if found:
        return found
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "")
        for pattern in ("Microsoft/WinGet/Links/wget.exe", "Microsoft/WinGet/Packages/*Wget*/wget.exe"):
            hits = glob.glob(os.path.join(base, pattern))
            if hits:
                return hits[0]
    return None


_WGET_SAVED = re.compile(r'URL:\s*(\S+)\s+\[[^\]]*\]\s+->\s+"([^"]+)"')


class _WgetDownloader(_HttpDownloader):
    """Hybrid engine (owner 2026-10-12; engine ``wget``): **Chrome (``curl_cffi``; else aiohttp) checks/fetches the
    pages** (status codes, EDQT section probing), **GNU wget downloads the CSS + images** of every page found, and
    **every asset wget could not get is retried the same way as the pages**. wget fetches one file at a time, so the asset list is spread over several wget processes
    (``concurrency // 2``, ≤ 32) of ≤ 6 URLs each. If wget is missing or cannot start, the fetch simply continues
    the same way — nothing fails because of the engine."""

    async def __aenter__(self):
        await super().__aenter__()
        self.bin = find_wget()
        self.tmp = tempfile.mkdtemp(prefix="rvsh-wget-")
        self.nprocs = max(1, min(self.concurrency // 2, 16))
        self.procs = asyncio.Semaphore(self.nprocs)
        self.loads: set[asyncio.Task] = set()
        self.via = {"wget": 0, self.http: 0}
        self.env = dict(os.environ)
        if self.proxy:
            self.env.update(http_proxy=self.proxy, https_proxy=self.proxy)
        self.seq = 0
        return self

    async def __aexit__(self, *exc) -> None:
        await super().__aexit__(*exc)
        await asyncio.to_thread(shutil.rmtree, self.tmp, True)

    @property
    def name(self) -> str:  # e.g. "wget 503 + chrome 2"
        v = getattr(self, "via", None)
        if not v:
            return "wget"
        return " + ".join(f"{k} {n}" for k, n in v.items() if n) or "wget"

    async def _download(self, urls: list[str], referer: str) -> None:
        if self.bin:
            per = max(1, min(6, -(-len(urls) // self.nprocs)))  # spread over the processes, ≤ 6 URLs each
            await asyncio.gather(*(self._batch(urls[i:i + per], referer) for i in range(0, len(urls), per)))
        leftovers = [u for u in urls if not self.cache[u].done()]
        if self.loads:
            await asyncio.gather(*self.loads, return_exceptions=True)
            leftovers = [u for u in urls if not self.cache[u].done()]
        if leftovers:  # wget missing / failed for these → Chrome (or aiohttp)
            self.via[self.http] += len(leftovers)
            await asyncio.gather(*(self._one(u, referer) for u in leftovers))

    def _args(self) -> list[str]:
        return [self.bin, "-e", "robots=off", f"--timeout={int(self.timeout)}", "--tries=1", "--no-hsts",
                f"--user-agent={UA}", f"--header=Accept-Language: {HEADERS['Accept-Language']}",
                f"--header=Accept: {HEADERS['Accept']}", "--max-redirect=5"]

    async def _batch(self, urls: list[str], referer: str) -> None:
        self.seq += 1
        folder = os.path.join(self.tmp, f"a{self.seq}")
        listing = folder + ".txt"
        await asyncio.to_thread(_write, listing, "\n".join(urls).encode())
        by_key = {unquote(u): u for u in urls}

        def on_line(line: str) -> None:
            m = _WGET_SAVED.search(line)
            url = by_key.pop(unquote(m.group(1)), None) if m else None
            if url is None:
                return
            path = m.group(2) if os.path.isabs(m.group(2)) else os.path.join(os.getcwd(), m.group(2))
            job = asyncio.get_running_loop().create_task(self._load(url, path))
            self.loads.add(job)
            job.add_done_callback(self.loads.discard)

        args = self._args() + ["-nv", "-x", "-P", folder, "--restrict-file-names=windows", f"--referer={referer}",
                               "-i", listing]
        async with self.procs:
            try:
                proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.PIPE, env=self.env)
            except OSError as e:  # wget not runnable: stop using it for this fetch, the HTTP client takes over
                self.bin = None
                log.warning("wget cannot run (%r) — %s takes over", e, self.http)
                return
            try:
                async for raw in proc.stderr:
                    on_line(raw.decode("utf-8", "replace"))
                await proc.wait()
            except asyncio.CancelledError:
                if proc.returncode is None:
                    proc.kill()
                raise

    async def _load(self, url: str, path: str) -> None:
        try:
            data = await asyncio.to_thread(_read, path)
        except OSError:
            return  # left for the HTTP client
        if len(data) <= MAX_ASSET_BYTES:
            self.via["wget"] += 1
            self._resolve(url, (_sniff(url, data), data))


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


# ------------------------------------------------------------------ MHT writer
def build_mht(page_url: str, html: bytes, assets: list[tuple[str, str, bytes]], charset: str = "utf-8") -> bytes:
    """MIME multipart/related archive (Chrome layout): HTML quoted-printable, assets base64."""
    boundary = "----MultipartBoundary--" + secrets.token_urlsafe(30)
    head = (f"From: <Saved by RVSH bot>\r\nSnapshot-Content-Location: {page_url}\r\nSubject: \r\n"
            f"Date: {email.utils.formatdate(usegmt=True)}\r\nMIME-Version: 1.0\r\n"
            f'Content-Type: multipart/related;\r\n\ttype="text/html";\r\n\tboundary="{boundary}"\r\n\r\n')
    out = [head.encode()]
    out.append(f"\r\n--{boundary}\r\nContent-Type: text/html; charset={charset}\r\nContent-ID: <frame-0@mhtml.blink>\r\n"
               f"Content-Transfer-Encoding: quoted-printable\r\nContent-Location: {page_url}\r\n\r\n".encode())
    out.append(quopri.encodestring(html).replace(b"\n", b"\r\n").replace(b"\r\r\n", b"\r\n"))
    for u, ctype, data in assets:
        b64 = base64.encodebytes(data).replace(b"\n", b"\r\n")
        out.append(f"\r\n--{boundary}\r\nContent-Type: {ctype}\r\nContent-Transfer-Encoding: base64\r\n"
                   f"Content-Location: {u}\r\n\r\n".encode() + b64)
    out.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(out)


# ------------------------------------------------------------------ remote mode
async def fetch_remote(api_url: str, token: str, url: str, *, timeout: float = 180) -> list[Page]:
    """Ask a remote fetch server (``tools/fetch_server``) to fetch ``url`` and return the MHT pages."""
    tmo = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=tmo) as s:
            async with s.post(api_url.rstrip("/") + "/fetch", json={"url": url}, headers={"X-Token": token}) as r:
                body = await r.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise FetchError("remote", None, repr(e)) from e
    try:
        data = json.loads(body)
    except ValueError as e:
        raise FetchError("remote", r.status, "bad response") from e
    if r.status != 200 or "pages" not in data:
        raise FetchError(data.get("error", "remote"), data.get("status") or r.status, data.get("detail", ""))
    return [Page(url=p["url"], kind=p["kind"], mht=base64.b64decode(p["mht_b64"]), section=p.get("section"),
                 assets=p.get("assets", 0), missing=p.get("missing", []), engine="remote:" + p.get("engine", ""))
            for p in data["pages"]]


def pages_to_json(pages: list[Page]) -> dict:
    """Wire format used by the fetch server."""
    return {"pages": [{"url": p.url, "kind": p.kind, "section": p.section, "assets": p.assets, "missing": p.missing[:20],
                       "engine": p.engine,
                       "mht_b64": base64.b64encode(p.mht).decode()} for p in pages]}
