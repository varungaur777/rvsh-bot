"""Image links of a question JSON → local files (``/json``; spec §16.8).

Safety (the links come from a file a user sent): only ``http`` / ``https``; the host must resolve to **public**
addresses only (no localhost / LAN / link-local — the bot's own Web App port included); redirects are followed by hand
(≤ 3) and checked the same way; ≤ ``PER_IMAGE_MB`` per image and ``TOTAL_MB`` per file; only real image bytes
(PNG / JPEG / GIF / WebP / BMP — sniffed, not trusted from headers). A link that fails is simply missing (the PDF
prints "[image not available]").

Public API: ``await download_images(urls, folder, *, proxy=None, parallel=8, progress=None) -> {url: [path, mime]}``.
Depends on: aiohttp. Used by: ``jsonq.runner``.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

log = logging.getLogger(__name__)
PER_IMAGE_MB = 8
TOTAL_MB = 250
MAX_REDIRECTS = 3
TIMEOUT_S = 25
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp"}


def sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


async def _public(host: str) -> bool:
    """Every address of ``host`` is a public one."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return ip.is_global
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addrs = {i[4][0] for i in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


async def _get(session, url: str, proxy: str | None, cap: int) -> bytes | None:
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or not await _public(parts.hostname):
            return None
        async with session.get(url, proxy=proxy, allow_redirects=False) as r:
            if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            if r.status != 200:
                raise OSError(f"HTTP {r.status}")
            if int(r.headers.get("Content-Length") or 0) > cap:
                return None
            buf = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                buf += chunk
                if len(buf) > cap:
                    return None
            return bytes(buf)
    return None


async def download_images(urls: list[str], folder, *, proxy: str | None = None, parallel: int = 8,
                          progress=None) -> dict[str, list[str]]:
    """``progress(done, total)`` after each link. → ``{url: [path, mime]}`` for the links that worked."""
    import aiohttp

    out: dict[str, list[str]] = {}
    if not urls:
        return out
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, parallel))
    budget = {"left": TOTAL_MB * 2**20}
    done = 0
    cap = PER_IMAGE_MB * 2**20

    async def one(session, url: str) -> None:
        nonlocal done
        try:
            async with sem:
                data = None
                for attempt in range(3):
                    try:
                        data = await _get(session, url, proxy or None, cap)
                        break
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                        if attempt == 2:
                            log.info("json image failed: %r", e)
                        else:
                            await asyncio.sleep(1 + attempt * 2)
                mime = sniff(data or b"")
                if data and mime and len(data) <= budget["left"]:
                    budget["left"] -= len(data)
                    path = folder / f"{hashlib.sha1(url.encode()).hexdigest()[:20]}.{_EXT[mime]}"
                    await asyncio.to_thread(path.write_bytes, data)
                    out[url] = [str(path), mime]
        finally:
            done += 1
            if progress is not None:
                progress(done, len(urls))

    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S, sock_connect=10)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/126.0 Safari/537.36", "Accept": "image/*,*/*;q=0.5"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        await asyncio.gather(*(one(session, u) for u in urls))
    return out
