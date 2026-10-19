"""Every user-facing string (English), keyed — so wording/translation never touches logic (spec §6.9).

Depends on: nothing. Used by: handlers, access, tasks, error handling.

Public API: ``t(key, **kwargs) -> str`` (HTML). Unknown keys return the key itself (visible in tests).
"""

from __future__ import annotations

TEXTS: dict[str, str] = {
    # home & help
    "home": "👋 <b>Welcome, {name}!</b>\n\n"
            "<blockquote>📊 Exam response sheet → <b>marks</b>, clean <b>PDF</b> or a <b>quiz</b>\n"
            "🎯 Quizzes here and in groups, with leaderboards\n"
            "📄 PDF tools — clean & merge</blockquote>\n"
            "<b>Quick start:</b> just send your response sheet (<code>.mht</code> file or link) — no command needed.",
    "home_links": "\n\n📢 Channel: {channel}\n👤 Admin: {admin}",
    "help": "❓ <b>How to use</b>\n\n"
            "📊 <b>Exam Tools</b>\n"
            "<blockquote>Send your TCS / EDQT response sheet — the <code>.mht</code> file(s) or the link. "
            "Several files or links of one exam can be sent together.</blockquote>\n"
            "🎯 <b>Quizzes</b> — /quizzes to browse · after sending a sheet tap <b>🎯 Make Quiz</b>. Play here, "
            "in a group, as a Web App or an HTML file; share with 📤.\n"
            "🧾 <b>JSON files</b> — /json: PDF or quiz from a question JSON file; /json_batch as a reply to "
            "a <code>.zip</code> of JSON files: all of them, one by one <i>(admins, unless opened)</i>.\n"
            "📄 <b>PDF Tools</b> — remove watermarks / links, merge PDFs.\n\n"
            "⌨️ /start · /help · /quizzes · /quiz <i>(yours)</i> · /cancel · /q <i>(your queue position)</i>",
    "help_admin": "\n\n⚙️ <b>Admin commands</b> <i>(also in ⚙️ Admin Panel)</i>\n<blockquote expandable>"
                  "/stats — is the bot healthy · /task — tasks, cancel one · /cq — clear the queue\n"
                  "/bc — broadcast · /stopbc — stop it · /msg — message one user\n"
                  "/results — result PDF of an exam\n"
                  "/links — saved exam links, Hindi and English as separate .txt files (reply to a list of links: "
                  "check it)\n"
                  "/batch — PDFs of many exam links: reply to a list (or pick an exam) → each shift's Hindi + English "
                  "PDFs, section-wise; runs beside your own tasks (⏹ on its message stops it)\n"
                  "/slides — an exam link or .mht → a PDF with one question per landscape page, exam details on "
                  "page 1, the answer key at the end (custom look, or the sheet's own boxes; whole exam or one section)\n"
                  "/admin — admins · /groups — groups (links, members, admins) · /user — everything about a user, "
                  "their groups too\n"
                  "/qz — quiz settings, auto quiz, Web App, Mini App link\n"
                  "/load_quiz — reply to a quiz file (from the dump, or a zip of them), an exam bundle or a JSON: load it\n"
                  "/es — exam settings · /ps — PDF settings · /wm — watermark\n"
                  "/ls — files · /clean — delete leftovers · /log — the log file\n"
                  "/on · /off — open or close the bot to everyone · /restart</blockquote>",
    "help_owner": "\n👑 <b>Owner</b>: /bot — bot settings and config.env · /db — database (counts and sizes) · /rm — delete a file or folder\n"
                  "/auth · /deauth — in a group (or <code>/auth -100…</code>): it keeps working while the bot is /off",
    "exam_how_to": "📤 <b>Send your response sheet</b>\n\n"
                   "<blockquote>• the <b>link</b> of your response sheet (digialm.com / cbexams.com), or\n"
                   "• the saved <b>.mht / .mhtml</b> file(s)</blockquote>\n"
                   "<i>Saving a page: Chrome → ⋮ → Download / Save page as → Webpage, Single File (.mhtml).</i>",
    "coming_soon": "🚧 <b>Coming soon</b>\nThis part is being built.",
    "btn_exam": "📊 Exam Tools", "btn_quiz": "🎯 Quizzes", "btn_pdf": "📄 PDF Tools", "btn_stats": "👤 My Stats",
    "btn_help": "❓ Help", "btn_admin": "⚙️ Admin Panel", "btn_back": "⬅️ Back", "btn_home": "🏠 Home",
    "btn_cancel": "✖️ Cancel", "btn_done": "✅ Done", "btn_joined": "✅ I've joined", "btn_close": "✖️ Close",
    # /results (admins)
    "results_empty": "📭 <b>No results stored yet</b>\nResults are saved when users get their marks from a response sheet.",
    "results_none": "🔎 No exam with stored results matches <b>{query}</b>.\nTry fewer words, or send /results for the full list.",
    # access
    "not_your_button": "This button isn't for you.",
    "public_off": "🔒 <b>Admins only for now</b>\nThe bot is under maintenance — please try again later.",
    "force_sub": "📢 <b>One step first</b>\nJoin our channel(s) below, then tap <b>✅ I've joined</b>.",
    "force_sub_still": "You haven't joined yet.",
    "banned": "🚫 You can't use this bot.",
    "admins_only": "🔒 This is for admins only.",
    "slow_down": "Please slow down a little 🙂",
    # a chat refused the bot (sender.undeliverable) — shown on a button press only (alerts are plain text)
    "cant_cant_write": "⚠️ I can't post in this chat — a group admin must let me send messages (or make me an admin).",
    "cant_slowmode": "⏳ Slow mode is on in this chat — make me an admin so I can post, or try again later.",
    "cant_gone": "⚠️ I can't reach this chat any more.",
    "anon_admin": "🕶 <b>You are writing as the group</b> (anonymous admin), so I can't tell who you are.\n"
                  "<i>Turn off “Remain anonymous” for a moment, or use this in a private chat with me.</i>",
    # tasks & queue
    "task_exists": "⏳ <b>You already have a task</b>\nWait for it to finish, or send /cancel to stop it.",
    "queued": "🧾 <b>Queued</b> — you are <b>#{pos}</b> in line. I'll update this message when it starts.",
    "queue_none": "📭 You have no task in the queue.",
    "queue_pos": "⏳ Your task <code>{task}</code> ({kind}) is <b>{state}</b>.",
    "cancelled": "✖️ <b>Cancelled.</b> Send a new sheet any time.",
    "nothing_to_cancel": "Nothing to cancel.",
    "timeout": "⌛ <b>Timed out</b>\nNo answer for a while, so I closed this. Send your sheet again any time.",
    "restarting": "♻️ <b>Restarting</b> — please send it again in a minute.",
    "waiting_resources": "⏳ Waiting for free resources…",
    "prepare_queue": "⏳ <b>In the queue</b> — you are <b>#{pos}</b>\n<i>Many sheets are being read right now; "
                     "yours starts automatically.</i>",
    "downloading_files": "⬇️ <b>Getting your files…</b>",
    "reading_sheet": "🔎 <b>Reading your response sheet…</b>",
    "error_download_failed": "⬇️ <b>Couldn't get your files from Telegram</b>\nPlease send them again.",
    "file_too_large": "📦 <b>File too large</b>\nA response sheet is at most {limit} MB.",
    # errors
    "error_generic": "😕 <b>Something went wrong</b>\nIt has been reported automatically.\n<i>Error ID:</i> <code>{error_id}</code>",
    "error_parse_error": "📄 <b>Not a response sheet</b>\nThis file doesn't look like a TCS / EDQT response sheet.",
    "error_not_an_exam_file": "📄 <b>Not a response sheet</b>\nPlease send the <code>.mht / .mhtml</code> file of the sheet.",
    "error_unknown_format": "📄 <b>Unsupported sheet</b>\nOnly TCS (digialm) and EDQT (cbexams) sheets are supported.",
    "error_no_questions": "📄 <b>No questions found</b>\nThe file may be incomplete — save the page again and resend.",
    "error_other_language": "🌐 <b>This sheet is not in Hindi or English</b>\nYour marks work as usual; custom PDFs, "
                            "slides and quizzes are made only from Hindi or English sheets. The <i>as in the sheet</i> "
                            "PDF works.",
    "error_mixed_sources": "⚠️ <b>Mixed files</b>\nPlease send files of one exam at a time (TCS and EDQT were mixed).",
    "error_different_exams": "⚠️ <b>Different exams</b>\nThese files belong to different exams or candidates — send one at a time.",
    "error_source_unavailable": "🔗 <b>I couldn't open that link</b>\n<i>Chrome → ⋮ → Download / Save page as → .mhtml, "
                                "then send me the file.</i>",
    "link_expired": "🔗 <b>Link expired</b>\nThe exam website removed this page (error {code}).",
    "link_blocked": "🚫 <b>Blocked by the exam website</b> (error {code})\nSome sheets open only from India.",
    "link_site_down": "🛠 <b>Exam website is down</b> (error {code})\nPlease try again in a while.",
    "link_network": "📡 <b>Couldn't reach the exam website</b>\nNetwork timeout — please try again in a while.",
    "link_not_exam": "📄 <b>No response sheet on that page</b>\nThe link may be incomplete or need a login.",
    "link_failed": "⚠️ <b>I couldn't open that link.</b>",
    "link_save_hint": "<i>If it opens for you: Chrome → ⋮ → Download / Save page as → Webpage, Single File (.mhtml), "
                      "then send me the file.</i>",
    "link_unsupported": "🔗 <b>Unsupported link</b>\nI read response-sheet links from <b>digialm.com</b> (TCS) and "
                        "<b>cbexams.com</b> (EDQT).\n<i>For other sites, save the page as .mhtml and send the file.</i>",
    "inputs_all_failed": "❌ <b>None of your inputs could be read</b>\nSend the response sheet again (.mhtml file or link).",
    "error_limit_exceeded": "⚠️ That's over the limit for this feature.",
    "error_user_blocked": "",
}


def t(key: str, **kwargs) -> str:
    """Text for ``key`` formatted with ``kwargs``."""
    text = TEXTS.get(key, key)
    try:
        return text.format(**kwargs) if kwargs else text
    except (KeyError, IndexError):
        return text
