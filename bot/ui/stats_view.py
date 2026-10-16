"""``/stats`` and the Admin Panel screen: the bot's live state in plain words (owner 2026-10-13, spec §23).

Two views, switched with a button:

- **simple** (default): one health line on top (``✅ Everything is fine`` or a short ``⚠️ Needs attention`` list),
  then short sections — Server · Users & tasks · Workers · Telegram · Database — with plain labels and sizes in
  MB/GB.
- **details**: the technical numbers for debugging (pool counters, slot limits, governor limits, sender buckets,
  fast-download connections, dump queue, runtime profile).

``collect(app)`` reads every component into one plain dict; ``render(snap, detail)`` is pure (unit-tested with a
hand-made dict). ``keyboard(user_id, detail)`` → ``🔄 Refresh`` · ``🔧 Details`` / ``📈 Simple`` + Back/Home.
Callback: ``sts:<uid>:<simple|detail>`` (handled by ``handlers/admin.stats_button``).

Depends on: psutil, ``core.runtime``, ``ui.keyboards``. Used by: ``handlers/admin.py``, ``handlers/basic.nav``
(Admin Panel), ``bot.app.stats_text``.
"""

from __future__ import annotations

import time

import psutil

from bot.core.runtime import available_ram_mb
from bot.ui.keyboards import btn, cb, kb, nav_row

_LANES = {"light": "light", "heavy": "heavy", "background": "background"}
_GATES = {"prepare": "preparing sheets", "fetch": "opening links"}
_GOV = {"normal": "🟢 normal", "throttled": "🟡 memory low — heavy work slowed", "critical": "🔴 memory very low — heavy work paused"}
_SLOW_LAG_MS = 500


def _size(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def _n(v: int) -> str:
    return f"{v:,}"


def _uptime(s: float) -> str:
    m = int(s) // 60
    d, h, m = m // 1440, m // 60 % 24, m % 60
    return f"{d} d {h} h" if d else (f"{h} h {m} min" if h else f"{m} min")


def collect(app) -> dict:
    proc = psutil.Process()
    vm = psutil.virtual_memory()
    try:
        disk_free_mb = psutil.disk_usage(str(app.root)).free // 2**20
    except Exception:
        disk_free_mb = None
    comp = app.compute
    pool, gov = comp.pool, comp.governor
    soft, hard = gov.limits()
    fd = app.fastdl.stats()
    return {
        "uptime_s": time.time() - app.started_at,
        "public": bool(app.settings.get("public_enabled")),
        "cpu": psutil.cpu_percent(interval=None),
        "ram_free_mb": available_ram_mb(app.runtime), "ram_total_mb": vm.total // 2**20,
        "bot_mb": proc.memory_info().rss // 2**20, "workers_mb": sum(pool.worker_rss_mb()),
        "disk_free_mb": disk_free_mb,
        "lag_ms": app.looplag.last_ms, "lag_peak_ms": app.looplag.max_ms, "slowest_update_s": app.max_update_lag,
        "sessions": len(app.sessions),
        "lanes": {n: (len(l.running), l.limit, len(l.queue)) for n, l in app.tasks.lanes.items()},
        "gates": {g.name: (g.active, g.limit, g.waiting, g.max_wait_s) for g in (app.gates.prepare, app.gates.fetch)},
        "pool": {"up": len(pool.worker_rss_mb()) if pool._ex else 0, "size": pool.workers, "started": pool._ex is not None,
                 **pool.counters},
        "slots": {n: (k.busy, k.effective, k.n_waiting(), k.cap == 0) for n, k in comp.scheduler.kinds.items()},
        "gov": {"state": gov.state, "cap": gov.cap, "free_mb": gov.free_mb, "soft": soft, "hard": hard,
                "admitted_mb": gov.reserved_mb, "disk_low": gov.disk_low, "samples": gov.samples,
                "every_s": gov.interval()},
        "sender": app.sender.stats(),
        "uploaders": app.uploads.stats(), "uploaders_online": len(app.uploaders),
        "uploaders_configured": len(app.config.image_bot_tokens),
        "fastdl": fd,
        "db": {"mongo": app.repo.mongo is not None, "mongo_ok": app.repo.mongo_ok, "outbox": app.repo.outbox.size(),
               "json_pending": app.repo.json.pending_count, "cache": len(app.repo.cache.data)},
        "dump": {"queue": app.dump.queue.qsize(), "dropped": app.dump.dropped},
        "profile": {"name": app.runtime.profile, "cores": app.runtime.cpus, "ram_mb": app.runtime.ram_total_mb},
        "quiz": app.quiz.stats() if getattr(app, "quiz", None) is not None else None,
        "web": ({"url": app.web.base_url, "port": app.web.port, **app.web.counters}
                if getattr(app, "web", None) is not None else None),
    }


def problems(s: dict) -> list[str]:
    """Things the owner should look at now; empty → everything is fine."""
    out, db, gov = [], s["db"], s["gov"]
    if db["mongo"] and not db["mongo_ok"]:
        out.append(f"MongoDB is down — data is kept on disk ({_n(db['outbox'])} changes waiting)")
    elif db["outbox"]:
        out.append(f"{_n(db['outbox'])} changes not saved to MongoDB yet")
    if gov["state"] != "normal":
        out.append(_GOV.get(gov["state"], gov["state"]).split(" ", 1)[1].capitalize())
    if gov["disk_low"]:
        out.append("Disk space is low")
    if s["lag_peak_ms"] >= _SLOW_LAG_MS:
        out.append(f"Bot was slow to respond (up to {s['lag_peak_ms'] / 1000:.1f} s in the last minute)")
    if s["uploaders_online"] < s["uploaders_configured"]:
        out.append(f"Only {s['uploaders_online']} of {s['uploaders_configured']} uploader bots are online")
    if any(slot[3] for slot in s["slots"].values()):
        out.append("Some work is paused (" + ", ".join(n for n, slot in s["slots"].items() if slot[3]) + ")")
    return out


def _simple(s: dict) -> list[str]:
    issues = problems(s)
    lines = [f"📈 <b>Bot status</b> · up {_uptime(s['uptime_s'])}",
             "🌐 Open to everyone" if s["public"] else "🔒 Admins only (public off)", ""]
    if issues:
        lines += ["⚠️ <b>Needs attention</b>"] + [f"• {p}" for p in issues]
    else:
        lines.append("✅ <b>Everything is fine</b>")

    disk = f" · disk free {_size(s['disk_free_mb'])}" if s["disk_free_mb"] is not None else ""
    lines += ["", "🖥 <b>Server</b>",
              f"• CPU {s['cpu']:.0f}% · free RAM {_size(s['ram_free_mb'])} of {_size(s['ram_total_mb'])}",
              f"• Bot uses {_size(s['bot_mb'])}" + (f" + workers {_size(s['workers_mb'])}" if s["workers_mb"] else "") + disk,
              f"• Reply speed: {s['lag_ms']:.0f} ms now · slowest {s['lag_peak_ms']:.0f} ms (1 min)"]

    running = sum(r for r, _, _ in s["lanes"].values())
    queued = sum(q for _, _, q in s["lanes"].values())
    lines += ["", "👥 <b>Users &amp; tasks</b>",
              f"• Users using the bot now: {s['sessions']}",
              f"• Tasks: {running} running · {queued} waiting"]
    if running or queued:
        lines.append("   " + " · ".join(f"{_LANES.get(n, n)} {r}/{lim}" + (f" (+{q})" if q else "")
                                        for n, (r, lim, q) in s["lanes"].items()))
    busy_gates = [f"{_GATES.get(n, n)} {a}/{lim}" + (f" (+{w} waiting)" if w else "")
                  for n, (a, lim, w, _) in s["gates"].items() if a or w]
    if busy_gates:
        lines.append("• Getting ready: " + " · ".join(busy_gates))
    elif not (running or queued):
        lines.append("• Nothing in the queue")

    if s.get("quiz") is not None:
        q, w = s["quiz"], s.get("web") or {}
        web = (f"📱 Web App: {w['url']}" if w.get("url") else "📱 Web App: off (no tunnel yet)") if w else ""
        lines += ["", "🎯 <b>Quizzes</b>",
                  f"• Playing now: {q['running']}" + (f" · paused {q['paused']}" if q["paused"] else "")
                  + f" · {_n(q['started'])} started · {_n(q['votes'])} answers since start"]
        if web:
            lines.append(f"• {web}" + (f" · {w.get('starts', 0)} opened · {w.get('submits', 0)} submitted" if w.get("url") else ""))

    p = s["pool"]
    busy = [f"{n} {b}/{e}" + (f" (+{w})" if w else "") for n, (b, e, w, _) in s["slots"].items() if b or w]
    failed = [f"{_n(p[k])} {label}" for k, label in (("errors", "failed"), ("timeouts", "timed out")) if p.get(k)]
    lines += ["", "🧮 <b>Workers</b> (parsing · PDFs)",
              (f"• {p['up']} of {p['size']} ready" if p["started"] else f"• {p['size']} · start on the first job")
              + f" · {_n(p.get('jobs', 0))} jobs done" + (f" · ⚠️ {', '.join(failed)}" if failed else ""),
              "• Busy now: " + (" · ".join(busy) if busy else "none"),
              f"• Memory guard: {_GOV.get(s['gov']['state'], s['gov']['state'])}"]

    snd, ups = s["sender"], s["uploaders"]
    lines += ["", "📨 <b>Telegram</b>",
              f"• Messages sent {_n(snd.get('sent', 0))}" + (f" · flood waits {_n(snd['flood_waits'])}" if snd.get("flood_waits") else "")
              + (f" · errors {_n(snd['errors'])}" if snd.get("errors") else ""),
              f"• Users who blocked the bot: {_n(snd.get('blocked', 0))}"]
    if s["uploaders_configured"]:
        total_mb = sum(u["mb_up"] for u in ups)
        lines.append(f"• Uploader bots {s['uploaders_online']}/{s['uploaders_configured']} online · {_size(total_mb)} sent")
        for u in ups:
            state = (f"⏸ paused {u['paused_s']} s" if u["paused_s"] else
                     f"{u['inflight']} uploading" if u["inflight"] else "idle")
            lines.append(f"   {u['bot']} — {_n(u['sent'])} files · {state}")
    else:
        lines.append(f"• Files are sent by the main bot ({_n(sum(u['sent'] for u in ups))} so far)")
    fd = s["fastdl"]
    lines.append(f"• Downloads: {_n(fd.get('fast', 0))} fast · {_n(fd.get('fallback', 0))} normal")

    db = s["db"]
    mongo = ("✅ connected" if db["mongo_ok"] else "❌ down") if db["mongo"] else "not used (disk only)"
    saved = "everything saved" if not (db["outbox"] or db["json_pending"]) else \
        f"{_n(db['outbox'] + db['json_pending'])} changes being saved"
    lines += ["", "🗄 <b>Database</b>", f"• MongoDB {mongo} · {saved}"]
    return lines


def _detail(s: dict) -> list[str]:
    p, g, snd, fd, db = s["pool"], s["gov"], s["sender"], s["fastdl"], s["db"]
    cap = "configured" if g["cap"] is None else ("paused" if g["cap"] == 0 else f"cap {g['cap']}")
    lines = [f"🔧 <b>Technical details</b> · up {_uptime(s['uptime_s'])}", "",
             "<b>Task lanes</b> (running/limit, queued)"]
    lines += [f"• {n}: {r}/{lim}, {q} queued" for n, (r, lim, q) in s["lanes"].items()]
    lines += ["", "<b>Gates</b> (busy/limit, waiting, longest wait)"]
    lines += [f"• {n}: {a}/{lim}, {w} waiting, {mw:.0f} s" for n, (a, lim, w, mw) in s["gates"].items()]
    lines += ["", "<b>Process pool</b>",
              f"• workers {p['up']}/{p['size']} · RSS {s['workers_mb']} MB",
              "• " + " · ".join(f"{k} {_n(p.get(k, 0))}" for k in ("jobs", "errors", "timeouts", "restarts", "recycles", "trims")),
              "", "<b>CPU slots</b> (busy/allowed)"]
    lines += [f"• {n}: {b}/{e}" + (f", {w} waiting" if w else "") + (" ⏸" if paused else "")
              for n, (b, e, w, paused) in s["slots"].items()]
    lines += ["", "<b>Memory guard</b>",
              f"• {g['state']} ({cap}) · free {g['free_mb']} MB · slow below {g['soft']} / stop below {g['hard']} MB",
              f"• admitted {g['admitted_mb']} MB · {g['samples']} samples, every {g['every_s']:g} s"
              + (" · ⚠️ disk low" if g["disk_low"] else ""),
              "", "<b>Sender</b>",
              "• " + " · ".join(f"{k} {v}" for k, v in snd.items()),
              "", "<b>Uploader bots</b>"]
    lines += [f"• {u['bot']}: sent {u['sent']} · in flight {u['inflight']} · uploading {u['uploading']} · "
              f"{u['mb_up']} MB · floods {u['floods']} · errors {u['errors']}"
              + (f" · paused {u['paused_s']} s" if u["paused_s"] else "") for u in s["uploaders"]] or ["• none"]
    pools = ", ".join(f"DC{dc}×{n}" for dc, n in fd.get("pools", {}).items()) or "—"
    lines += ["", "<b>Fast download</b>", f"• fast {fd.get('fast', 0)} · fallback {fd.get('fallback', 0)} · connections {pools} · replaced {fd.get('replaced', 0)}",
              "", "<b>Database</b>",
              f"• mongo {'ok' if db['mongo_ok'] else ('down' if db['mongo'] else 'off')} · outbox {db['outbox']} · "
              f"JSON pending {db['json_pending']} · cache {db['cache']} docs",
              "", "<b>Other</b>",
              f"• loop lag {s['lag_ms']:.0f} ms · peak (1 min) {s['lag_peak_ms']:.0f} ms · "
              f"slowest update {s['slowest_update_s']:.1f} s",
              f"• sessions {s['sessions']} · dump queue {s['dump']['queue']} (dropped {s['dump']['dropped']})",
              f"• profile {s['profile']['name']} · {s['profile']['cores']} cores · {s['profile']['ram_mb']} MB"]
    return lines


def render(snap: dict, detail: bool = False) -> str:
    return "\n".join(_detail(snap) if detail else _simple(snap))


def keyboard(user_id: int, detail: bool = False):
    view = "detail" if detail else "simple"
    switch = btn("📈 Simple view", cb("sts", user_id, "simple")) if detail else \
        btn("🔧 Details", cb("sts", user_id, "detail"))
    return kb([btn("🔄 Refresh", cb("sts", user_id, view)), switch], nav_row(user_id, back="admin"))
