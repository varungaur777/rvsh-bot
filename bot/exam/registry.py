"""Exam bank: strict reuse + variants (spec §12.6). Every exam whose sheets pass through the bot ends up as one
bundle (``exam.bundle``: JSON + images) in the dump channel, registered in ``exams``.

``await ensure(app, task) -> dict``
1. pool job (``render`` kind — background work is governed): signature of the task's sheets + the candidate's choices
   (``task.data["responses"]``, memory only);
2. under a per-exam ``asyncio.Lock`` (two users with the same new exam → the second waits and reuses), compare with
   the base ``exam_key`` and every variant (``schema.compare`` — all checks must pass; nothing is downloaded);
3. exact match → **reuse** (``reuses`` counter; zero parsing for outputs, zero uploads); the same questions and
   answers in **another language** (an English-medium and a Hindi-medium sheet of one shift) → **merge**: the language
   is added to the stored bundle (next ``version`` of the same key, cached outputs reset, dump note) — owner 2026-10-12;
4. otherwise build the bundle (pool), upload it to dump (uploader bot, reply to the task's anchor) and register it —
   the base key when the exam is new, else ``exam_key#2``, ``#3`` … with ``variant_of`` + ``diff`` and a dump alert
   ``⚠️ Exam variant``.

Returns ``{"key", "reused", "ref", "path"?, "diff"}`` (``path`` = the local bundle when just built). Exceptions
propagate (callers decide: background bank → logged; custom PDF → task error).

The bank is filled **when an output needs the exam JSON** (custom PDF now, quiz cards in P5), never for Marks alone
(owner 2026-10-12: one JSON with processed images serves PDFs and quiz cards alike).

Depends on: ``exam.bundle``, ``exam.schema``, ``exam.jobs``, ``compute``, uploader pool, ``core.tgref``, ``db.repo``.
Used by: exam pipeline (custom PDF; quiz in P5), batch (P7).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from bot.compute import run_cpu
from bot.core.tgref import from_message, private_link
from bot.exam import bundle
from bot.exam.keys import meta_line
from bot.core.errors import UserFacingError
from bot.exam.schema import decide, medium
from bot.parsers.models import ExamMeta
from bot.ui.progress import human_bytes

log = logging.getLogger(__name__)
_locks: dict[str, asyncio.Lock] = {}


class _KeyLock:
    """Per-exam lock, dropped from the table when nobody holds or waits for it."""

    def __init__(self, key: str) -> None:
        self.key = key

    async def __aenter__(self):
        self.lock = _locks.setdefault(self.key, asyncio.Lock())
        await self.lock.acquire()

    async def __aexit__(self, *exc):
        self.lock.release()
        if not self.lock.locked() and not getattr(self.lock, "_waiters", None):
            _locks.pop(self.key, None)


def bundle_name(exam: dict, variant: int = 1) -> str:
    d = "-".join(reversed((exam.get("date") or "").split("-"))) if exam.get("date") else ""
    parts = [f"({d})" if d else "", exam.get("exam_name") or "Exam", f"(Shift {exam['shift']})" if exam.get("shift") else "",
             f"v{variant}" if variant > 1 else ""]
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", " ".join(p for p in parts if p))
    return re.sub(r"\s+", " ", name).strip()[:140] + ".zip"


def _meta(exam: dict) -> ExamMeta:
    return ExamMeta(exam.get("exam_name"), level=exam.get("level"), date=exam.get("date"), time=exam.get("time"),
                    start_hhmm=exam.get("start_hhmm"), shift=exam.get("shift"), source=exam.get("source") or "")


async def ensure(app, task) -> dict:
    from bot.exam.inputs import anchor_id

    folder, hints = str(task.workdir / "in"), task.data.get("hints") or {}
    sig, resp = await run_cpu(app, "render", task.task_id, bundle.sheet_signature, folder, hints, est_mb=60)
    task.data["responses"] = resp
    other = medium(sig.get("census") or {})["other"]
    if other:  # a Bengali / Tamil … sheet never enters the bank (owner 2026-10-19: "Result do, data mat lo")
        raise UserFacingError("other_language", f"{sig['exam']['exam_key']}: {other}")
    base = sig["exam"]["exam_key"]
    repo = app.repo
    async with _KeyLock(base):
        base_doc = await repo.exams.get(base) or {}
        variants = list(base_doc.get("variants") or [])
        merge_into = None
        for k in [base, *variants]:
            d = base_doc if k == base else (await repo.exams.get(k) or {})
            if not (d.get("fingerprint") and d.get("json_ref") and d.get("qs")):
                continue
            action, _failed, added = decide(sig, d)
            if action == "reuse":
                await repo.exams.update(k, {"$inc": {"reuses": 1}})
                task.data["exam_key"] = k
                return {"key": k, "reused": True, "ref": d["json_ref"], "diff": []}
            if action == "merge" and merge_into is None:
                merge_into = (k, d, added)
        if merge_into:
            return await _merge(app, task, sig, *merge_into)

        is_variant = bool(base_doc.get("fingerprint"))
        n = len(variants) + 2 if is_variant else 1
        key = f"{base}#{n}" if is_variant else base
        diff = (decide(sig, base_doc)[1] or ["questions"]) if is_variant else []
        out = task.workdir / "bank" / bundle_name(sig["exam"], n)
        out.parent.mkdir(parents=True, exist_ok=True)
        info = await run_cpu(app, "render", task.task_id, bundle.write_bundle, folder, hints, str(out),
                             app.settings.get("ocr.mode") or "lazy", est_mb=80)
        e = sig["exam"]
        caption = (f"📦 <b>Exam data</b>{f' · variant {n}' if is_variant else ''}\n<b>{html.escape(e.get('exam_name') or 'Exam')}</b>\n"
                   f"<i>{html.escape(meta_line(_meta(e)))}</i>\n"
                   f"🧩 {info['questions']} questions · 🖼 {info['images']} images · 🌐 {', '.join(sig['languages'])} · "
                   f"{human_bytes(info['bytes'])}\n"
                   f"🔑 <code>{html.escape(key)}</code>" + (f"\n⚠️ differs from v1: {html.escape(', '.join(diff))}" if diff else ""))
        _bot, msg = await app.uploads.send_document(app.config.dump_channel_id, str(out), caption,
                                                    reply_to=await anchor_id(task, timeout=30))
        ref = from_message(msg)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec = {k: sig[k] for k in ("exam", "sections", "languages", "qs", "fingerprint", "questions", "sig_version")}
        rec.update(json_ref=ref, version=1, images=info["images"], bundle_bytes=info["bytes"], created_at=now,
                   created_by=task.user_id, ocr_mode_used=app.settings.get("ocr.mode") or "lazy")
        if is_variant:
            rec.update(variant_of=base, diff=diff)
        await repo.exams.update(key, {"$set": rec})
        if is_variant:
            await repo.exams.update(base, {"$addToSet": {"variants": key}})
            app.dump.notify(f"⚠️ <b>Exam variant</b> <code>{html.escape(key)}</code>\n"
                            f"Differs from <code>{html.escape(base)}</code>: {html.escape(', '.join(diff))}\n"
                            f"📦 {private_link(msg.chat.id, msg.id)}")
        log.info("exam bank: %s registered (%d q, %d images, %s, %d ms)", key, info["questions"], info["images"],
                 human_bytes(info["bytes"]), info["ms"])
        task.data["exam_key"] = key
        return {"key": key, "reused": False, "ref": ref, "path": str(out), "diff": diff}


async def _merge(app, task, sig: dict, key: str, rec: dict, added: list[str]) -> dict:
    """Same exam, another language (owner 2026-10-12): build this sheet's bundle, add its new language(s) to the
    stored one (questions paired by ``qref``), upload the merged bundle as the next version of the same key."""
    from bot.core.tgref import download
    from bot.exam.inputs import anchor_id

    folder, hints = str(task.workdir / "in"), task.data.get("hints") or {}
    work = task.workdir / "bank"
    work.mkdir(parents=True, exist_ok=True)
    new_zip = work / "new.zip"
    await run_cpu(app, "render", task.task_id, bundle.write_bundle, folder, hints, str(new_zip),
                  app.settings.get("ocr.mode") or "lazy", est_mb=80)
    stored = await download(app, rec["json_ref"], work / "stored")
    version = int(rec.get("version") or 1) + 1
    out = work / bundle_name(sig["exam"], 1).replace(".zip", f" (v{version}).zip")
    info = await run_cpu(app, "render", task.task_id, bundle.merge_bundles, str(stored), str(new_zip), str(out), added,
                         est_mb=80)
    e = sig["exam"]
    names = ", ".join(LANG_NAMES.get(x, x) for x in added)
    caption = (f"📦 <b>Exam data</b> · v{version} · 🌐 + {html.escape(names)}\n<b>{html.escape(e.get('exam_name') or 'Exam')}</b>\n"
               f"<i>{html.escape(meta_line(_meta(e)))}</i>\n"
               f"🧩 {info['questions']} questions · 🖼 {info['images']} images · 🌐 {', '.join(info['languages'])} · "
               f"{human_bytes(info['bytes'])}\n🔑 <code>{html.escape(key)}</code>")
    _bot, msg = await app.uploads.send_document(app.config.dump_channel_id, str(out), caption,
                                                reply_to=await anchor_id(task, timeout=30))
    ref = from_message(msg)
    new_qs = {x["k"]: x for x in sig["qs"]}
    qs = []
    for x in rec["qs"]:
        y = new_qs.get(x["k"]) or {}
        qs.append({**x, "lh": {**(y.get("lh") or {}), **x["lh"]}, "ah": {**(y.get("ah") or {}), **x["ah"]}})
    await app.repo.exams.update(key, {"$set": {
        "json_ref": ref, "version": version, "languages": info["languages"], "qs": qs, "images": info["images"],
        "bundle_bytes": info["bytes"], "outputs": {}, "merged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        "$addToSet": {"merged_languages": {"$each": added}}})
    app.dump.notify(f"🌐 <b>Exam languages merged</b> <code>{html.escape(key)}</code> + {html.escape(names)} → "
                    f"v{version}\n📦 {private_link(msg.chat.id, msg.id)}")
    log.info("exam bank: %s + %s → v%d", key, names, version)
    task.data["exam_key"] = key
    return {"key": key, "reused": False, "merged": True, "ref": ref, "path": str(out), "diff": []}


LANG_NAMES = {"en": "English", "hi": "Hindi"}
