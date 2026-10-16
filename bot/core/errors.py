"""Exception hierarchy shared by the whole bot (spec §6.2).

Depends on: nothing. Used by: every package.

Rules:
- Code raises these types; handlers/tasks catch ``BotError`` through ``@safe_handler`` (added in P1),
  which shows the user ``user_message()`` (never the raw exception) and cancels the task.
- ``key`` is a stable machine-readable reason, e.g. ``"no_questions"``; ``detail`` is for logs/dump only.
"""

from __future__ import annotations


class BotError(Exception):
    """Base of every expected error. ``key`` → friendly text in ``ui/texts.py``; ``detail`` → logs only."""

    key: str = "internal"

    def __init__(self, key: str | None = None, detail: str = "") -> None:
        if key:
            self.key = key
        self.detail = detail
        super().__init__(f"{self.key}: {detail}" if detail else self.key)


class UserFacingError(BotError):
    """An error whose ``key`` maps to a message the user should read (bad input, limits…)."""


class TaskCancelled(BotError):
    key = "cancelled"


class InputTimeout(BotError):
    key = "timeout"


class SourceUnavailable(UserFacingError):
    """A URL could not be fetched (non-200, region block, network). ``reason`` is the fetcher's reason
    (``http_404``, ``http_403``, ``network``, ``not_exam``, ``remote`` …) — it picks the user-facing hint."""

    key = "source_unavailable"

    def __init__(self, key: str | None = None, detail: str = "", reason: str = "") -> None:
        super().__init__(key, detail)
        self.reason = reason


class LimitExceeded(UserFacingError):
    key = "limit_exceeded"


class ParseError(UserFacingError):
    """Input is not a supported response sheet or its structure is broken."""

    key = "parse_error"


class OcrError(BotError):
    key = "ocr_error"


class RenderError(BotError):
    key = "render_error"


class UserBlockedBot(BotError):
    key = "user_blocked"
