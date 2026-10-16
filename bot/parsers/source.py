"""Load response-sheet inputs into an ``ExamSource`` (spec §12.1).

Depends on: stdlib ``email``; ``core.errors``.
Used by: ``parsers/__init__.py``, ``tcs.py``, ``edqt.py``, ``net/`` (P2, URL fetch → MHT), tests.

Public API
----------
- ``Hints`` — extra facts from outside the sheet (batch line, caption, file path).
- ``ExamSource`` — main HTML bytes + embedded assets by URL + origin URL + hints.
- ``load_bytes(data, filename=None, hints=None)`` — sniff MHT vs HTML.
- ``load_mht(data, hints=None)`` / ``load_html(data, url=None, hints=None)`` / ``load_path(path, hints=None)``.

Exceptions → outcome
--------------------
- ``ParseError("not_an_exam_file")``: no HTML part / not HTML → user is told the file is not a response sheet.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import re
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bot.core.errors import ParseError


@dataclass(slots=True)
class Hints:
    """Facts supplied from outside the sheet; used only when the sheet itself lacks them."""

    exam_name: str | None = None
    date: str | None = None  # ISO YYYY-MM-DD
    shift: int | None = None
    section: str | None = None
    order: int | None = None  # position of this file among several parts


@dataclass(slots=True)
class ExamSource:
    html: bytes
    encoding: str = "utf-8"
    assets: dict[str, bytes] = field(default_factory=dict)  # normalised URL → decoded bytes (filled lazily)
    mimes: dict[str, str] = field(default_factory=dict)  # normalised URL → mime
    url: str | None = None  # page URL (MHT Snapshot-Content-Location)
    saved_at: str | None = None  # ISO date the page was saved (MHT Date header)
    origin: str = "html"  # mht | html | url
    hints: Hints = field(default_factory=Hints)
    raw_parts: dict[str, tuple[str, bytes]] = field(default_factory=dict)  # URL → (transfer encoding, raw body)

    def asset(self, src: str | None) -> tuple[bytes, str] | None:
        """Bytes + mime of an image referenced by ``src`` (data: URI, exact URL, or URL without query).
        MHT parts are decoded on first use only, so unreferenced images cost no CPU."""
        if not src:
            return None
        src = unescape(src.strip())
        if src.startswith("data:"):
            return _decode_data_uri(src)
        if self.url and not re.match(r"(?i)^[a-z][a-z0-9+.-]*:", src):  # relative src → page URL (B-004)
            src = urljoin(self.url, src)
        key = asset_key(src)
        for k in (key, key.split("?", 1)[0]):
            if k not in self.assets and k in self.raw_parts:
                cte, raw = self.raw_parts.pop(k)
                self.assets[k] = _decode_body(cte, raw)
            if k in self.assets:
                return self.assets[k], self.mimes.get(k, "application/octet-stream")
        return None


def asset_key(url: str) -> str:
    """Normalise a URL for asset lookup: lower-case host, no fragment, single slashes in the path."""
    p = urlsplit(unescape(url.strip()))
    path = re.sub(r"/{2,}", "/", unquote(p.path))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


def _decode_data_uri(uri: str) -> tuple[bytes, str] | None:
    m = re.match(r"data:([^;,]*)(;base64)?,(.*)", uri, re.S)
    if not m:
        return None
    mime, b64, payload = m.group(1) or "application/octet-stream", m.group(2), m.group(3)
    try:
        return (base64.b64decode(payload) if b64 else unquote(payload).encode()), mime
    except (ValueError, base64.binascii.Error):
        return None


def load_mht(data: bytes, hints: Hints | None = None) -> ExamSource:
    """Parse an MHT/MHTML archive (as saved by Chrome) into an ``ExamSource``.

    Uses a fast dedicated MIME splitter (the stdlib ``email`` parser spent ~85 % of total parse time
    on header objects); falls back to ``email`` for archives the splitter does not understand."""
    fast = _split_mht(data)
    if fast is not None:
        top, parts = fast
        html_part = None
        raw_parts: dict[str, tuple[str, bytes]] = {}
        mimes: dict[str, str] = {}
        for hdr, body in parts:
            ctype = hdr.get("content-type", "").split(";", 1)[0].strip().lower()
            cte = hdr.get("content-transfer-encoding", "").strip().lower()
            if ctype == "text/html" and html_part is None:
                m = re.search(r'charset="?([A-Za-z0-9_\-]+)', hdr.get("content-type", ""), re.I)
                html_part = (_decode_body(cte, body), m.group(1).lower() if m else None)
            elif loc := hdr.get("content-location"):
                key = asset_key(loc)
                raw_parts[key] = (cte, body)
                mimes[key] = ctype
        if html_part is not None:
            payload, charset = html_part
            return ExamSource(
                html=payload, encoding=charset or _sniff_charset(payload), mimes=mimes, raw_parts=raw_parts,
                url=top.get("snapshot-content-location") or None, saved_at=_iso_date(top.get("date")),
                origin="mht", hints=hints or Hints(),
            )
    return _load_mht_email(data, hints)


_BOUNDARY = re.compile(rb'boundary="?([^";\r\n]+)"?', re.I)


def _split_mht(data: bytes) -> tuple[dict[str, str], list[tuple[dict[str, str], bytes]]] | None:
    """Split a multipart MHT into (top headers, [(part headers, raw body)]); ``None`` if not multipart."""
    head_end, sep = _header_end(data)
    if head_end < 0:
        return None
    top = _parse_headers(data[:head_end])
    m = _BOUNDARY.search(top.get("content-type", "").encode("latin-1", "replace"))
    if not m:
        return None
    boundary = b"--" + m.group(1)
    chunks = data[head_end + sep:].split(boundary)
    parts = []
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk.lstrip(b"\r\n")  # line break after the boundary marker
        he, s = _header_end(chunk)
        if he < 0:
            continue
        body = chunk[he + s:]
        if body.endswith(b"\r\n"):
            body = body[:-2]
        elif body.endswith(b"\n"):
            body = body[:-1]
        parts.append((_parse_headers(chunk[:he]), body))
    return top, parts


def _header_end(buf: bytes) -> tuple[int, int]:
    """Index of the blank line ending a header block and the separator length (CRLF or LF style)."""
    i, j = buf.find(b"\r\n\r\n"), buf.find(b"\n\n")
    if i >= 0 and (j < 0 or i <= j):
        return i, 4
    return (j, 2) if j >= 0 else (-1, 0)


def _parse_headers(block: bytes) -> dict[str, str]:
    """RFC 822 headers → {lower-case name: value}, folded lines joined."""
    text = block.decode("latin-1")
    text = re.sub(r"\r?\n[ \t]+", " ", text)
    out: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition(":")
        if sep and name and " " not in name.strip():
            out.setdefault(name.strip().lower(), value.strip())
    return out


def _decode_body(cte: str, body: bytes) -> bytes:
    if cte == "base64":
        return binascii.a2b_base64(body)
    if cte == "quoted-printable":
        return binascii.a2b_qp(body)
    return body


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _load_mht_email(data: bytes, hints: Hints | None) -> ExamSource:
    """Fallback MHT reader using the stdlib ``email`` package (slower, very tolerant)."""
    msg = email.message_from_bytes(data, policy=email.policy.compat32)
    html_part = None
    assets: dict[str, bytes] = {}
    mimes: dict[str, str] = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True) or b""
        if ctype == "text/html" and html_part is None:
            html_part = (payload, part.get_content_charset())
        elif loc := part.get("Content-Location"):
            key = asset_key(loc)
            assets[key] = payload
            mimes[key] = ctype
    if html_part is None:
        raise ParseError("not_an_exam_file", "MHT without text/html part")
    payload, charset = html_part
    return ExamSource(
        html=payload,
        encoding=charset or _sniff_charset(payload),
        assets=assets,
        mimes=mimes,
        url=msg.get("Snapshot-Content-Location") or None,
        saved_at=_iso_date(msg.get("Date")),
        origin="mht",
        hints=hints or Hints(),
    )


def load_html(data: bytes, url: str | None = None, hints: Hints | None = None) -> ExamSource:
    """Wrap a plain HTML page (no embedded assets)."""
    if b"<" not in data[:4096]:
        raise ParseError("not_an_exam_file", "not HTML")
    return ExamSource(html=data, encoding=_sniff_charset(data), url=url, origin="html", hints=hints or Hints())


def load_bytes(data: bytes, filename: str | None = None, hints: Hints | None = None) -> ExamSource:
    """Detect MHT vs HTML from the content (the extension is only a hint)."""
    head = data[:4096].lower()
    if b"multipart/related" in head or b"mime-version" in head or (filename or "").lower().endswith((".mht", ".mhtml")):
        return load_mht(data, hints)
    return load_html(data, hints=hints)


def load_path(path: str | Path, hints: Hints | None = None) -> ExamSource:
    """Read a file from disk (tests, batch) — see ``load_bytes``."""
    p = Path(path)
    return load_bytes(p.read_bytes(), p.name, hints)


def _sniff_charset(data: bytes) -> str:
    m = re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', data[:4096])
    return m.group(1).decode("ascii").lower() if m else "utf-8"
