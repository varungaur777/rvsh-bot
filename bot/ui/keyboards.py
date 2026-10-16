"""Inline keyboards and compact callback data (spec §6.5, §6.9).

Depends on: Kurigram types/enums, ``ui.texts``. Used by: handlers, access (force-sub), tasks.

Public API
----------
- ``cb(action, user_id, obj="", arg="") -> str`` — ``"<action>:<user_id>:<obj>[:<arg>]"``, ≤ 64 bytes
  (``ValueError`` otherwise — a programming error caught by tests).
- ``parse_cb(data) -> CallbackData`` (``action, user_id, obj, arg``).
- ``btn(text, data=None, url=None, style=None)`` — style: ``"primary" | "success" | "danger" | None``
  (coloured buttons where Kurigram supports them).
- ``kb(*rows)`` → ``InlineKeyboardMarkup``; ``nav_row(user_id, back=…)`` → [⬅ Back, 🏠 Home].
- ``home_keyboard(user_id, is_admin)``.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyrogram import enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.ui.texts import t

_STYLES = {
    "primary": getattr(enums.ButtonStyle, "PRIMARY", None),
    "success": getattr(enums.ButtonStyle, "SUCCESS", None),
    "danger": getattr(enums.ButtonStyle, "DANGER", None),
}


@dataclass(frozen=True, slots=True)
class CallbackData:
    action: str
    user_id: int
    obj: str = ""
    arg: str = ""


def cb(action: str, user_id: int, obj: str = "", arg: str = "") -> str:
    data = f"{action}:{user_id}:{obj}" + (f":{arg}" if arg else "")
    if len(data.encode()) > 64:
        raise ValueError(f"callback data too long ({len(data.encode())} bytes): {data}")
    return data


def parse_cb(data: str | bytes) -> CallbackData | None:
    if isinstance(data, bytes):
        data = data.decode(errors="ignore")
    parts = data.split(":", 3)
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return None
    return CallbackData(parts[0], int(parts[1]), parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")


def btn(text: str, data: str | None = None, *, url: str | None = None, style: str | None = None, **kw) -> InlineKeyboardButton:
    if style and _STYLES.get(style) is not None:
        kw["style"] = _STYLES[style]
    return InlineKeyboardButton(text, callback_data=data, url=url, **kw)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def nav_row(user_id: int, back: str | None = "home") -> list[InlineKeyboardButton]:
    row = []
    if back and back != "home":
        row.append(btn(t("btn_back"), cb("nav", user_id, back)))
    row.append(btn(t("btn_home"), cb("nav", user_id, "home")))
    return row


def home_keyboard(user_id: int, is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn(t("btn_exam"), cb("nav", user_id, "exam"), style="primary"), btn(t("btn_quiz"), cb("nav", user_id, "quiz"), style="primary")],
        [btn(t("btn_pdf"), cb("nav", user_id, "pdf")), btn(t("btn_stats"), cb("nav", user_id, "stats"))],
        [btn(t("btn_help"), cb("nav", user_id, "help"))],
    ]
    if is_admin:
        rows.append([btn(t("btn_admin"), cb("nav", user_id, "admin"), style="success")])
    return kb(*rows)
