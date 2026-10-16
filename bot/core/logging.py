"""Logging setup with "upload-before-truncate" rotation (spec §6.1).

Depends on: stdlib logging. Used by: ``bot.app`` at boot; ``core.dump`` registers ``on_rotate``.

Public API
----------
- ``setup_logging(log_dir, level="INFO", max_bytes=10 MB) -> UploadingFileHandler``
- ``UploadingFileHandler.on_rotate`` — callback ``(Path) -> None``; receives the finished log file, which
  the dump notifier uploads and then deletes. Rotation never blocks: the callback only queues work.
- ``current_log_path()`` — for ``/log``.

Third-party noise (Kurigram session chatter) is lowered to WARNING.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Callable

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_handler: "UploadingFileHandler | None" = None


class UploadingFileHandler(logging.FileHandler):
    """Writes ``bot.log``; at ``max_bytes`` the file is renamed ``bot-<timestamp>.log`` and handed to ``on_rotate``."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(path, encoding="utf-8", delay=True)
        self.path = path
        self.max_bytes = max_bytes
        self.on_rotate: Callable[[Path], None] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            if self.stream is not None and self.stream.tell() >= self.max_bytes:
                self._rotate()
        except Exception:
            self.handleError(record)

    def _rotate(self) -> None:
        self.acquire()
        try:
            self.close()
            done = self.path.with_name(f"{self.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.log")
            self.path.replace(done)
        finally:
            self.release()
        if self.on_rotate:
            try:
                self.on_rotate(done)
            except Exception:  # never let log shipping break logging
                pass


def setup_logging(log_dir: Path, level: str = "INFO", max_bytes: int = 10 * 1024 * 1024) -> UploadingFileHandler:
    """Configure root logging once (console + rotating file)."""
    global _handler
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(_FMT)
    try:  # a console that can't show a character (Windows cp1252 output to a file: "→", Hindi) gets "?" — never
        sys.stdout.reconfigure(errors="replace")  # a "--- Logging error ---" with the line lost; the file is UTF-8
    except (AttributeError, ValueError):
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    _handler = UploadingFileHandler(Path(log_dir) / "bot.log", max_bytes)
    _handler.setFormatter(fmt)
    root.addHandler(_handler)
    for noisy in ("pyrogram", "pymongo", "asyncio", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("bot.db").setLevel(level)
    return _handler


def current_log_path() -> Path | None:
    return _handler.path if _handler else None
