"""Per-task log message in the dump channel: posted when the task starts, edited when it ends (spec §6.1).

Sent by the uploader pool (``app.uploads``) so the main bot never spends its channel budget on logs; the
**same** bot edits the message at the end (only the sender may edit). Includes every input with its size
and the private link of its dump copy (owner 2026-10-12), and the outputs with their dump links.

Depends on: ``uploader.workers``, ``exam.inputs`` (input lines). Used by: ``bot.app`` (lane hooks).
Public API: ``TaskLog(app)`` · ``await started(task)`` · ``await finished(task)``. Never raises.
"""

from __future__ import annotations

import html
import logging
import time

log = logging.getLogger(__name__)

_STATUS = {"completed": "✅ completed", "cancelled": "❌ cancelled", "error": "⚠️ error", "blocked": "🚫 user blocked bot",
           "timeout": "⏱ timeout", "restart": "🔁 restart"}


def _fmt_ts(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "—"


class TaskLog:
    def __init__(self, app) -> None:
        self.app = app

    def _enabled(self) -> bool:
        return bool(self.app.settings.get("tasklog.enabled"))

    def _text(self, task, final: bool) -> str:
        from bot.exam.inputs import inputs_summary
        d = task.data
        who = f'<a href="tg://user?id={task.user_id}">{html.escape(d.get("user_name") or str(task.user_id))}</a> · <code>{task.user_id}</code>'
        if d.get("username"):
            who += f" · @{d['username']}"
        lines = [f"🧾 <b>Task</b> <code>{task.task_id}</code> · {html.escape(task.kind)} · {task.lane} lane",
                 f"👤 {who} · {self.app.access.role(task.user_id)}",
                 f"⏳ created {_fmt_ts(task.created)} · started {_fmt_ts(task.started)} · waited "
                 f"{int((task.started or time.time()) - task.created)}s"]
        if isinstance(d.get("batch"), dict):  # /json_batch
            lines.append(f"📦 zip: {html.escape(d['batch'].get('zip_name') or '')} · "
                         f"{len(d['batch'].get('files') or [])} JSON files")
        elif isinstance(d.get("json"), dict):  # /json
            lines.append(f"📄 JSON: {html.escape(d['json'].get('title') or '')} · {d['json'].get('questions', 0)} questions")
        ins = inputs_summary(task)
        if ins:
            lines.append("📥 <b>Inputs</b>\n" + "\n".join(ins))
        if final:
            lines.append(f"🏁 ended {_fmt_ts(task.ended)} · {int(task.elapsed)}s · {_STATUS.get(task.status, task.status)}"
                         f" · by {html.escape(task.ended_by or 'system')}"
                         + (f" · error <code>{task.error_id}</code>" if task.error_id else ""))
            outs = [f"{html.escape(o.get('name', '?'))} ({o.get('size_h', '')})" + (f' <a href="{o["link"]}">dump</a>' if o.get("link") else "")
                    for o in task.outputs]
            lines.append("📤 " + (", ".join(outs) if outs else "—"))
            if isinstance(d.get("json"), str):  # an exam's reused data (/json tasks keep a dict here)
                lines.append(f"♻️ {html.escape(d['json'])}")
        return "\n".join(lines)[:4000]

    async def started(self, task) -> None:
        if not self._enabled():
            return
        try:
            bot, msg = await self.app.uploads.send_message(self.app.config.dump_channel_id, self._text(task, False),
                                                           reply_to=task.data.get("anchor_id"))
            task.data["log_bot"], task.data["log_msg_id"] = bot, msg.id
        except Exception as e:
            log.warning("task log start failed: %r", e)

    async def finished(self, task) -> None:
        if not self._enabled() or (task.status == "cancelled" and not task.data.get("inputs")):
            return
        try:
            text = self._text(task, True)
            if task.data.get("log_msg_id"):
                try:
                    await self.app.uploads.edit_text(task.data["log_bot"], self.app.config.dump_channel_id, task.data["log_msg_id"], text)
                    return
                except Exception:
                    pass
            await self.app.uploads.send_message(self.app.config.dump_channel_id, text, reply_to=task.data.get("anchor_id"))
        except Exception as e:
            log.warning("task log end failed: %r", e)
