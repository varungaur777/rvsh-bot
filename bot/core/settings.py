"""Live settings (spec §7, layer 2): typed defaults in code + the ``settings/global`` document, cached in memory.

Depends on: ``db.repo``, ``db.updates``, ``core.runtime``. Used by: almost everything (lanes, sender, access,
quiz, pdf, governor) and the admin panels that edit settings (``/bot /es /qz /wm /ps /groups /admin``).

Public API
----------
- ``DEFAULTS`` — nested dict; dotted keys address it (``"queue.heavy_concurrent"``).
- ``Settings(repo, runtime)`` · ``await load()`` · ``get(key)`` · ``value(key)`` (resolves ``"auto"``) ·
  ``await set(key, value, by=None)`` · ``subscribe(prefix, callback)`` · ``describe(prefix)``

Validation: a new value must have the default's type (``int`` accepts ``"auto"`` where the default is
``"auto"``; floats accept ints). Raises ``SettingError`` (shown to the admin as a friendly message).
Subscribers are called after a successful change; a failing subscriber is logged, never propagated.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any, Callable

from bot.core.runtime import RuntimeInfo
from bot.db.updates import get_path

log = logging.getLogger(__name__)

GB = 1024**3

DEFAULTS: dict[str, Any] = {
    "public_enabled": True,
    "force_sub": {"channels": [], "cache_ttl_s": 600},
    "admins": [],
    "channel_link": "",
    "admin_contact": "",
    "help_video": "",
    # prepare_concurrent: users whose files are downloaded + parsed at once after "Done" (tasks.gate; auto = cores)
    "queue": {"light_concurrent": "auto", "heavy_concurrent": "auto", "background_concurrent": 1,
              "prepare_concurrent": "auto"},
    "input_timeout_s": 60,
    "collect_idle_s": 8,
    "edit_interval_s": 3,
    "ocr": {"mode": "lazy", "public_mode": "lazy", "image_slots": "auto", "pdf_page_slots": 1},
    "render": {"slots": "auto"},
    "merge": {"slots": 1},
    "pdf": {
        "edit_batch_pages": 10, "columns": 2, "font": "poppins",
        "colors": {"question_no": "#000000", "question": "#1a237e", "options": "#000000", "label": "#000000",
                   "correct": "#2e7d32",
                   "chosen_right": "#2e7d32", "chosen_wrong": "#c62828", "banner_border": "#000000",
                   "banner_fill": "#eeeeee", "header": "#1a237e", "footer": "#1a237e",
                   "explain_fill": "#e3f2fd", "explain_head": "#1a237e"},
        "header_text": "", "header_url": "", "footer_text": "", "footer_url": "",
    },
    # quiz card colours (/ps → 🃏 Quiz cards; "" = the card's own default)
    "card": {"colors": {"title": "", "accent": "", "header_fill": "", "pill": "", "answer": "", "explain_fill": ""}},
    "watermark": {"enabled": True, "text": "", "image_url": "", "position": "diagonal", "opacity": 0.08, "color": "#9e9e9e"},
    # warm_up: one worker started in the background after boot (first sheet is read without the spawn delay) ·
    # idle_trim_s: a pool idle this long is retired and one fresh worker warmed (0 = never)
    "pool": {"workers": "auto", "reserve_mb": 512, "per_worker_mb": 300, "max_workers": 8, "max_tasks_per_child": 50,
             "warm_up": True, "idle_trim_s": 300},
    # fast_download_*: parallel connections per file (core.fastdl; 0 = Kurigram's normal download) · prewarm_dcs: data
    # centres connected at boot (users' files live on their DC) · direct_upload_slots: main-bot uploads when dump copy fails
    "tg": {"max_concurrent_transmissions": 4, "fast_download_connections": 4, "fast_download_part_kb": 512,
           "prewarm_dcs": [1, 5], "direct_upload_slots": 2},
    # forward_window_s: files sent together are forwarded in one call · forward_max_wait_s: longer → uploader re-upload
    "dump": {"forward_window_s": 1.0, "forward_max_wait_s": 10.0},
    "governor": {"free_soft_mb": 800, "free_hard_mb": 300, "cpu_soft_pct": 80, "cpu_hard_pct": 95,
                 "worker_rss_limit_mb": 300, "job_timeout_s": 60, "disk_min_free_mb": 1024},
    "db": {"lru_size": 2000, "json_flush_ms": 1000},
    "uploader": {"per_bot_concurrency": 4},
    "marks": {"per_question": [1, 2, 3], "negative": [0, 0.25, 0.3333, 0.5],
              # suggested scheme by exam name when the sheet prints none (EDQT); neg = fraction of the question's marks
              "rules": [{"match": r"selection\s*post", "plus": 2, "neg": 0.25},
                        {"match": r"stenographer", "plus": 1, "neg": 0.25}]},
    # display masks on scorecards / result PDFs (stores always keep real values) · mask_name: light (ARU* KUMA*) /
    # strong (A*** K****) / off
    "privacy": {"mask_name": "light", "mask_roll": True},
    # quiz_public: everyone may make a quiz from their sheet (owner 2026-10-13: "Sab users")
    # json_public: /json (PDF / quiz from a question JSON file) for everyone, not only admins
    "feature": {"custom_pdf_public": False, "quiz_public": True, "json_public": False},
    "results": {"show_name": True, "rows_per_chunk": 500},
    "pdf_tools": {"public": True, "merge_limit_public": 10, "merge_max_total_bytes": int(1.25 * GB),
                  "max_file_bytes_public": 200 * 1024**2, "clean_max_pages_public": 300},
    "quiz": {"default_question_time_s": 30, "default_language": "ask", "card_max_height_px": 1800,
             "branding_probability": 0.15, "pause_timeout_min": 30, "per_page": 8},
    "quiz_stats": {"max_players": 50000},
    "autoquiz": {"enabled": False, "interval_min": 15, "open_period_s": 0},
    "broadcast": {"concurrency": 20},
    "batch": {"fetch_concurrency": 2},
    "webapp": {"enabled": True, "tunnel": "quick", "miniapp": "", "img_cache_mb": 500, "session_ttl_min": 240,
               "init_data_max_age_s": 86400, "rate_limit_per_min": 60},
    "tasklog": {"enabled": True},
    "snapshot": {"interval_min": 30},
    "runtime": {"max_uptime_min": 0},
    "shutdown": {"grace_s": 60},
    "http": {"proxy": ""},
    # mode: direct | remote | auto (tools/fetch_server) · engine: auto (wget if installed) | wget | aiohttp · concurrency: assets in flight
    # parallel_links: pasted links opened at once (tasks.gate "fetch"); more wait in a visible queue
    # other_language: EDQT pages — also fetch each question's other medium (Hindi/English version button)
    "fetch": {"mode": "direct", "engine": "auto", "concurrency": 48, "parallel_links": 3, "other_language": True},
}


class SettingError(ValueError):
    pass


CHOICES = {"pdf.columns": (2, 1), "pdf.font": ("poppins", "noto"), "fetch.mode": ("direct", "remote", "auto"), "fetch.engine": ("auto", "chrome", "wget", "aiohttp"), "privacy.mask_name": ("light", "strong", "off"), "ocr.mode": ("lazy", "full"), "ocr.public_mode": ("lazy", "full"),
           "webapp.tunnel": ("quick", "named", "off"), "quiz.default_language": ("ask", "en", "hi", "both")}


class Settings:
    def __init__(self, repo, runtime: RuntimeInfo | None = None) -> None:
        self.repo = repo
        self.runtime = runtime
        self._doc: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._subs: list[tuple[str, Callable[[str, Any], Any]]] = []

    async def load(self) -> None:
        """Merge the stored document over the defaults (unknown stored keys are kept but unused)."""
        stored = await self.repo.settings.get("global") or {}
        stored.pop("_id", None)
        stored.pop("updated_at", None)
        self._doc = _deep_merge(copy.deepcopy(DEFAULTS), stored)

    def get(self, key: str, default: Any = None) -> Any:
        """Raw value (may be ``"auto"``)."""
        return get_path(self._doc, key, default)

    def value(self, key: str) -> Any:
        """Effective value: ``"auto"`` resolved from the runtime (hardware) info."""
        v = self.get(key)
        if v != "auto":
            return v
        rt = self.runtime
        workers = self.workers()
        auto = {
            "pool.workers": workers,
            "queue.light_concurrent": rt.auto_light() if rt else 4,
            "queue.heavy_concurrent": rt.auto_heavy(workers) if rt else 2,
            "queue.prepare_concurrent": max(2, rt.cpus) if rt else 2,
            "ocr.image_slots": max(2, workers),
            "render.slots": max(1, workers // 2),
        }
        if key not in auto:
            raise SettingError(f"{key} has no auto rule")
        return auto[key]

    def workers(self) -> int:
        """Effective pool worker count (fixed override or auto from CPU + RAM)."""
        raw = self.get("pool.workers")
        if raw != "auto":
            return int(raw)
        if not self.runtime:
            return 1
        return self.runtime.auto_workers(self.get("pool.reserve_mb"), self.get("pool.per_worker_mb"), self.get("pool.max_workers"))

    async def set(self, key: str, value: Any, by: int | None = None) -> Any:
        """Validate, persist (Mongo + JSON) and publish a change. Returns the stored value."""
        default = get_path(DEFAULTS, key, _MISSING)
        if default is _MISSING:
            raise SettingError(f"unknown setting {key}")
        value = _coerce(key, default, value)
        if key in CHOICES and value not in CHOICES[key]:
            raise SettingError(f"{key} must be one of {', '.join(map(str, CHOICES[key]))}")
        await self.repo.settings.update("global", {"$set": {key: value, "_last_change": {"key": key, "by": by}}})
        _set_path(self._doc, key, value)
        for prefix, cb in list(self._subs):
            if key == prefix or key.startswith(prefix + ".") or prefix.startswith(key + "."):
                try:
                    res = cb(key, value)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    log.exception("settings subscriber for %s failed", prefix)
        return value

    def subscribe(self, prefix: str, callback: Callable[[str, Any], Any]) -> None:
        """Call ``callback(key, value)`` whenever a key at/under ``prefix`` changes."""
        self._subs.append((prefix, callback))

    def describe(self, prefix: str = "") -> dict[str, Any]:
        """Flat ``{dotted_key: value}`` view of a subtree — used by the admin panels."""
        node = get_path(self._doc, prefix) if prefix else self._doc
        out: dict[str, Any] = {}
        _flatten(node, prefix, out)
        return out


_MISSING = object()


def _coerce(key: str, default: Any, value: Any) -> Any:
    if key.startswith(("pdf.colors.", "card.colors.")) or key == "watermark.color":  # "" = the default colour
        v = str(value or "").strip().lower().lstrip("#")
        if len(v) == 3:
            v = "".join(ch * 2 for ch in v)
        if v and (len(v) != 6 or any(ch not in "0123456789abcdef" for ch in v)):
            raise SettingError("send a colour like #1a237e")
        return f"#{v}" if v else ""
    if value == "auto" and default == "auto":
        return value
    if default == "auto":
        try:
            v = int(value)
        except (TypeError, ValueError) as e:
            raise SettingError(f"{key} expects a whole number or 'auto'") from e
        if v < 1:
            raise SettingError(f"{key} must be ≥ 1")
        return v
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "on", "yes", "1"):
            return True
        if str(value).lower() in ("false", "off", "no", "0"):
            return False
        raise SettingError(f"{key} expects on/off")
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError) as e:
            raise SettingError(f"{key} expects a whole number") from e
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError) as e:
            raise SettingError(f"{key} expects a number") from e
    if isinstance(default, list) and not isinstance(value, list):
        raise SettingError(f"{key} expects a list")
    if isinstance(default, dict) and not isinstance(value, dict):
        raise SettingError(f"{key} expects a group of values")
    if isinstance(default, str) and not isinstance(value, str):
        return str(value)
    return value


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _set_path(doc: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = copy.deepcopy(value)


def _flatten(node: Any, prefix: str, out: dict) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten(v, f"{prefix}.{k}" if prefix else k, out)
    else:
        out[prefix] = node
