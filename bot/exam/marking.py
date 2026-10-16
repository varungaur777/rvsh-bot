"""Marking-scheme suggestion for the preference card (spec §13.2: file → ``marking_presets`` → defaults).

EDQT sheets never print a marking scheme (only TCS info panels do), so the suggestion has to come from somewhere
else. Order — the first that exists wins:

1. ``file``   — detected in the sheet itself (TCS info panel).
2. ``admin``  — ``marking_presets/<slug>.admin`` (set by an admin for that exam name).
3. ``rule``   — setting ``marks.rules``: ``[{"match": "<regex on exam name>", "plus": 2, "neg": 0.25}, …]``
                (``neg`` = fraction of the question's marks, owner's rule).
4. ``preset`` — learned per exam name: the scheme found in a TCS file of that exam, else the scheme most users
                confirmed for it (``votes``).
5. ``default`` — +1, no negative.

Depends on: ``exam.keys``, ``db.repo``. Used by: ``handlers/exam.py`` (preference card, confirm).

Public API
----------
- ``await suggest(app, summary) -> {"plus", "neg", "source"}`` — ``summary`` from ``pipeline.inspect``.
- ``await remember(app, summary, prefs)`` — store a detected scheme / count a user's confirmed choice.
  Never raises (a preset problem must not block the user).
"""

from __future__ import annotations

import logging
import re

from bot.exam.keys import name_slug

log = logging.getLogger(__name__)


def _fraction(plus: float, minus: float) -> float:
    return round(minus / plus, 4) if plus else 0.0


def _vote_key(plus: float, neg: float) -> str:
    return f"{plus:g}_{neg:g}".replace(".", "p")  # no dots: safe as a Mongo field name


def _from_vote_key(k: str) -> tuple[float, float]:
    a, b = k.replace("p", ".").split("_")
    return float(a), float(b)


async def suggest(app, summary: dict) -> dict:
    detected = summary.get("marking")
    if detected:
        plus, minus = detected
        return {"plus": plus, "neg": _fraction(plus, minus), "source": "file"}
    name = summary.get("exam_name") or ""
    preset = {}
    try:
        preset = await app.repo.marking_presets.get(name_slug(name), copy=False) or {}
    except Exception as e:
        log.warning("marking preset read failed: %r", e)
    if a := preset.get("admin"):
        return {"plus": a["plus"], "neg": a["neg"], "source": "admin"}
    for rule in app.settings.get("marks.rules") or []:
        try:
            if re.search(rule["match"], name, re.I):
                return {"plus": float(rule["plus"]), "neg": float(rule["neg"]), "source": "rule"}
        except (KeyError, TypeError, ValueError, re.error):
            continue
    if d := preset.get("detected"):
        return {"plus": d["plus"], "neg": d["neg"], "source": "preset"}
    votes = preset.get("votes") or {}
    if votes:
        plus, neg = _from_vote_key(max(votes, key=votes.get))
        return {"plus": plus, "neg": neg, "source": "preset", "votes": int(votes[max(votes, key=votes.get)])}
    return {"plus": 1.0, "neg": 0.0, "source": "default"}


async def remember(app, summary: dict, prefs: dict) -> None:
    name = summary.get("exam_name")
    if not name:
        return
    try:
        detected = summary.get("marking")
        if detected:  # a TCS file of this exam: its scheme is the best guess for the EDQT sheets of the same exam
            plus, minus = detected
            await app.repo.marking_presets.update(name_slug(name), {
                "$set": {"exam_name": name, "detected": {"plus": plus, "neg": _fraction(plus, minus)}}})
            return
        if prefs.get("per_section"):  # an admin's one-off per-section scheme is not a vote for the exam's scheme
            return
        await app.repo.marking_presets.update(name_slug(name), {
            "$set": {"exam_name": name, "last": {"plus": prefs["plus"], "neg": prefs["neg"]}},
            "$inc": {f"votes.{_vote_key(prefs['plus'], prefs['neg'])}": 1}})
    except Exception as e:
        log.warning("marking preset write failed: %r", e)
