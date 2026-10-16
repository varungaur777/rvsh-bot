"""Exam identity & display helpers (spec §12.6, D-005).

Depends on: ``parsers.models``. Used by: results, exam JSON registry (P4), URL registry, batch, captions.

Public API
----------
- ``name_slug(name)`` — ``"RRB NTPC GRADUATE CBT 1"`` → ``"rrb-ntpc-graduate-cbt-1"`` (no dots: safe in Mongo paths)
- ``exam_key(meta)`` — ``slug|YYYY-MM-DD|HHMM`` (start time), else ``slug|date|s<shift>``, else ``slug|date|na``
- ``display_date(iso)`` — ``"2025-12-29"`` → ``"29 Dec 2025"``
- ``meta_line(meta)`` — ``"29 Dec 2025 • Shift 3 • 4:30 PM - 6:00 PM"`` (only parts that exist)
"""

from __future__ import annotations

import re
from datetime import date

from bot.parsers.models import ExamMeta


def name_slug(name: str | None) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "unknown").lower()).strip("-")
    return s[:80] or "unknown"


def exam_key(meta: ExamMeta) -> str:
    when = meta.start_hhmm or (f"s{meta.shift}" if meta.shift else "na")
    return f"{name_slug(meta.exam_name)}|{meta.date or 'nodate'}|{when}"


def display_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return date.fromisoformat(iso).strftime("%d %b %Y")
    except ValueError:
        return iso


def meta_line(meta: ExamMeta) -> str:
    parts = [display_date(meta.date), f"Shift {meta.shift}" if meta.shift else None, meta.time]
    return " • ".join(p for p in parts if p)
