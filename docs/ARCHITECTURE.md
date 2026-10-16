# Architecture — impact map

> Requirements live in [`prompt_v4.md`](../prompt_v4.md) (the spec). This file describes **how the code is built**.
> **Rule:** before changing any file, read its entry here and every file listed under *used by*. Update the entry in the same change.

## Status legend
✅ implemented + tested · 🚧 in progress · ⏳ planned

## Build order (owner-confirmed 2026-10-12)
1. **P0** docs + skeleton ✅
2. **P2a** MHT/HTML sources + TCS/EDQT parsers (offline, golden tests) ✅ — 117 real sheets, 3,400+ questions, 61 tests
3. **P1** bot core ✅ — live-tested on Telegram: warm boot 2.7 s, idle RSS 116 MB, loop lag < 25 ms, 5/5 uploader bots, Atlas Mongo
4. **P2b** marks + scorecard → first end-to-end demo: send an `.mht` → scorecard image ⏳
5. Then spec phases P3 → P10 in order (OCR last).

## Conventions
- Python ≥ 3.12 (developed on 3.14). `from __future__ import annotations` everywhere.
- Every module starts with a header docstring: purpose · depends on · used by · public API · exceptions → outcome.
- Every public function has a docstring. Data classes are `@dataclass(slots=True)`.
- Heavy libraries (lxml is light and allowed; PyMuPDF, ReportLab, WeasyPrint, Pillow-heavy paths, OpenCV, Tesseract) are imported **inside** functions that run in pool workers.
- Pure parsing/rendering functions take bytes/paths and return plain data — picklable, so they run in the process pool unchanged.
- No module deletes task workdirs except `tasks/context.py` (single cleanup path).
- Errors: raise `core.errors` types; never show raw exceptions to users.

## Package map

| Package | Purpose | Status |
|---|---|---|
| `bot/parsers/` | Turn `.mht`/`.html`/URL bytes into `ExamData` (TCS, EDQT). **FROZEN** (golden tests pass). | ✅ |
| `bot/exam/` | Canonical exam JSON, fingerprints, marks, pipeline, results, URL registry, batch | ⏳ |
| `bot/core/` | Config, settings, runtime profile, lease, logging, dump notifier, task log, access, sender, events, shutdown | ⏳ |
| `bot/db/` | One repository API over Mongo + per-document JSON mirror + outbox + LRU (snapshots: P10) | ✅ |
| `bot/tasks/` | `TaskContext`, three lanes, temporary sessions | ✅ |
| `bot/compute/` | Process pool, fair scheduler, governor | ⏳ |
| `bot/uploader/` | Uploader bots as a team (post-soonest dispatch, parallel byte uploads, flood stay/move) | ✅ |
| `bot/net/` | HTTP fetch (proxy, retries), MHT builder | ⏳ |
| `bot/render/` | PyMuPDF cards/scorecard, ReportLab PDFs, WeasyPrint original PDF, fonts, compression | ⏳ |
| `bot/quiz/` | Quiz builder, store, sessions, stats/ranking, auto-quiz, HTML quiz (inline + screens in handlers) | ✅ |
| `bot/pdftools/` | Clean, brand, merge, extractor, legacy fonts, MCQ extraction | ⏳ |
| `bot/ocr/` | OCR engine (built last) | ⏳ |
| `bot/web/` | Web App server (aiohttp) + Cloudflare tunnel | ✅ |
| `bot/handlers/`, `bot/ui/` | Telegram handlers (start/help/nav/cancel/q, exam, results, admin: stats/log/on/off/restart, P9 panels — broadcast, tasks, people, files, settings, db, Admin Panel), keyboards, texts, progress | 🚧 |

---

## File entries

### `bot/parsers/models.py` ✅
- **Purpose:** data classes produced by every parser: `ExamData`, `ExamMeta`, `Candidate`, `Section`, `Question`, `Option`, `Asset`, `Marking`.
- **Rich text format:** question/option content is a *mini-markup* string — plain text plus only `<sup>`, `<sub>`, `<b>`, `<i>`, `<u>`, `<br/>` and `<img id="…"/>` placeholders that point to `ExamData.assets`. The same string renders in ReportLab, PyMuPDF and HTML; `plain()` strips it for polls and hashes.
- **Depends on:** stdlib only. **Used by:** all parsers, `bot/exam/*`, renderers, tests.

### `bot/parsers/source.py` ✅
- **Purpose:** load inputs into an `ExamSource` (main HTML bytes + assets by URL + origin URL + hints). `load_mht(bytes)`, `load_html(bytes)`, `load_bytes(bytes, name)`, `load_path(path)`.
- **Speed:** a dedicated MIME splitter (`_split_mht`) + lazy asset decoding (`raw_parts` decoded on first `asset()` call) — 58 ms per sheet vs ~200 ms with stdlib `email`; `_load_mht_email` is the tolerant fallback. `test_fast_mht_splitter_matches_stdlib_email` proves both give identical output.
- **Exceptions:** `ParseError("not_an_exam_file")` when no HTML part exists → caller tells the user the file isn't a response sheet.
- **Depends on:** stdlib `email`. **Used by:** parsers, `net/` (later), tests.

### `bot/parsers/common.py` ✅
- **Purpose:** shared helpers — whitespace/Unicode normalisation, Devanagari detection, date/time parsing, shift-from-time, exam-name cleanup, `RichText` (lxml node → mini-markup, MathML→text, images → `Asset`s), `strip_option_label`, `tidy`, `to_date_from_url_token`.
- **Hidden-text rule:** text inside elements styled white / zero font size is dropped (portals plant anti-copy numbers such as `3521`), images inside them are kept; `display:none` elements are skipped entirely.
- **Used by:** `tcs.py`, `edqt.py`.

### `bot/parsers/mathml.py` ✅
- **Purpose:** convert Wiris/MathML (simple subset) to mini-markup text; return `None` when unsupported so the caller keeps the image.

### `bot/parsers/tcs.py` ✅ / `bot/parsers/edqt.py` ✅
- **Purpose:** `detect(source) -> bool`, `parse(source) -> ExamData` for each portal family. Verified facts in spec §12.2.
- **TCS specifics:** every question in a section takes the section's majority language (sheets are single-language; formulas fool per-question detection). Marking note → `ExamData.marking`.
- **EDQT specifics:** roll-number watermark removed; option cell colours → correct/chosen; `_EN`/`_HI` images split into per-language content; empty admin-view headers fall back to hints and the URL date token.
- **Exceptions:** `ParseError("no_questions")` → task cancelled with a friendly message; details to dump.

### `bot/parsers/__init__.py` ✅
- **Purpose:** `detect_family`, `parse_source`, `parse_sources` (merge EDQT parts: order = hint → PART letter → send order; duplicate sections kept once; refuses mixed families / different exams), `hints_from_path`.

### `tests/parsers/` ✅
- `test_units.py` — helpers (no samples). `test_samples.py` — golden files in `tests/golden/parsers/` (PII-free summary + SHA-1 of all question content) + invariants on every sample + spot checks a human verified.
- Regenerate goldens only on purpose: `python -m tests.tools.make_golden`, then review `git diff tests/golden`.

### P1 core files ✅
| File | Purpose | Depends on | Used by |
|---|---|---|---|
| `bot/__main__.py` | entry point, uvloop, re-exec after /restart | config, runtime, app | — |
| `bot/app.py` | `App` container: boot order (repo → settings → lease → main client → background uploaders), startup report, `/stats` text, hooks (`on_user_blocked`, task error/close), graceful stop | everything | handlers via `client.app` |
| `bot/core/config.py` | `.env` + env vars → frozen `Config`; `ConfigError` → exit 2 | dotenv | app, db |
| `bot/core/runtime.py` | profile detection, cgroup-aware CPUs/RAM, auto worker/lane sizes | psutil | app, settings |
| `bot/core/settings.py` | `DEFAULTS` + live `settings/global`; `value()` resolves `auto`; validation; subscribers | db.repo | everything |
| `bot/core/ratelimit.py` | priority token bucket | — | sender, access |
| `bot/core/sender.py` | the only way to talk to Telegram: chat+global buckets, priorities, FloodWait retry, blocked-user hook | ratelimit | all |
| `bot/core/dump.py` | batched fire-and-forget dump events, error reports with ids, file uploads | sender | all |
| `bot/core/logging.py` | console + 10 MB file; rotated file handed to dump before deletion | — | app |
| `bot/core/access.py` | `@guard`: new user → ban → public off → private-only → callback owner → role → rate → force-sub; error wrapper | forcesub, repo, ui | handlers |
| `bot/core/forcesub.py` | live membership check, positive cache, in-flight dedupe, fail-open + owner alert | settings, dump | access, events |
| `bot/core/events.py` | chat-member updates: force-sub invalidation, bot blocked, bot added/promoted/demoted/removed in groups; groups found from their messages; `register_group` (/groups add/re-check); `refresh_info` (members, admins, private invite link of a group where the bot is admin) | app | handlers, people |
| `bot/core/lease.py` | single-instance lease in Mongo `_lease/main` with handover | mongo | app |
| `bot/core/tasklog.py` | per-task dump log message (start → edited at end) | sender | app/lanes hooks |
| `bot/core/delivery.py` | output to the user: uploader bot → dump, then main bot `copy_message`; direct upload fallback | uploader, sender | output runners |
| `bot/core/fastdl.py` | parallel Telegram downloads (kept-open media connections per DC, one part per connection, shared by all downloads), Kurigram fallback | Kurigram raw | exam inputs |
| `bot/compute/__init__.py` | `Compute.run(kind, owner, fn, *args)`: fair slot → governor admission (heavy) → process pool; `run_cpu` fallback to a thread | pool, scheduler, governor | pipeline, inputs |
| `bot/compute/pool.py` | one lazy spawn `ProcessPoolExecutor`, `max_tasks_per_child`, thread caps + low priority, crash retry, timeout kill, live resize/recycle | stdlib, psutil | compute |
| `bot/compute/scheduler.py` | fair (fewest-running owner first) work-conserving slots per kind; governor caps | stdlib | compute |
| `bot/compute/governor.py` | normal / throttled / critical from free RAM + CPU; admission by memory estimate; RSS recycle; disk low; alerts | runtime, psutil | compute |
| `bot/exam/jobs.py` | pure pool jobs: inspect / score sheets, dump file names (no Telegram/DB imports) | parsers, marks | pipeline, inputs |
| `bot/exam/schema.py` | exam JSON build (no candidate data), q_hash / answer_sig / fingerprint / option hashes, strict compare, chosen remap | parsers, keys | bundle, registry, PDF/quiz builders |
| `bot/exam/imgproc.py` | per-image analysis + enhancement (classify, text size → scale, crop, contrast, upscale, reflow gaps) | Pillow, numpy | bundle |
| `bot/exam/bundle.py` | pool jobs: sheet signature + responses; write / read the bundle zip (JSON + images, content_bbox) | schema, jobs, Pillow | registry, renderers |
| `bot/exam/registry.py` | exam bank: per-exam lock, reuse / variant, bundle upload to dump, `exams` record; background after Marks | bundle, compute, uploader, repo | pipeline |
| `bot/exam/results.py` | save results (unmasked) + per-shift catalogue in `exams`; `/results` rows (recomputed marking), ranking, overall + shift-wise stats, PDF context, caption | repo, keys, marks | pipeline, results handler, tools |
| `bot/render/results_pdf.py` | result PDF on a ReportLab canvas (pool job, flat RAM) | reportlab, rl_accel, theme | results handler |
| `bot/render/rl_text.py` | shapable Noto fonts, mini-markup → ReportLab markup with font-run splitting by cmap, fix for ReportLab's mixed-font word shaping (B-015), bold runs named bold (B-034), `glyph_safe` look-alikes for characters no font has (B-037) | reportlab, uharfbuzz | exam_pdf |
| `bot/render/exam_pdf.py` | custom exam PDF from the bundle: 1/2 columns (hybrid), centred banners, KeepTogether questions, hugging chosen boxes, answer strip, answers after / grid, explanation boxes that split at a column's end, `<tbl>` tables as grids (D-050) | rl_text, theme, bundle | pipeline.run_custom |
| `bot/render/theme.py` | shared glass-banner PDF theme from settings: colours, banner, watermark, clickable header/footer, page numbers | reportlab, fonts | every ReportLab PDF |
| `bot/render/original_pdf.py` | Original PDF as the portal shows it: candidate rows (allowlist) + portal watermark removed, buttons hidden by CSS, every link → owner's link; WeasyPrint with an archive-only fetcher, join parts, PyMuPDF stamps | weasyprint, pymupdf, lxml, parsers, fonts, compress, watermark | pipeline.run_original |
| `bot/render/watermark.py` | the page watermark as one transparent PNG (one text, centred, along the page diagonal / vertical), made once and cached on disk by a hash of the settings | Pillow, fonts | original_pdf, theme.watermark (ReportLab PDFs) |
| `bot/render/compress.py` | lossless-looking PDF save: image pass above 220 DPI (optional), font subsetting, garbage collection + deflate | pymupdf | original_pdf (merged / batch outputs next) |
| `bot/broadcast/bc.py` | Broadcast engine: users / groups (bot admin) / IDs, `copy_message` (albums as albums) on the sender's BULK lane, `broadcast.concurrency` workers, blocked users and dead groups marked, live text + report | sender, repo, ui.progress | handlers/broadcast |
| `bot/handlers/broadcast.py` | `/bc` collect → target card (preview, confirm) → run; `/stopbc`; `/msg`; `intercept` (group −1) catches messages a step waits for | broadcast.bc, sessions | handlers |
| `bot/handlers/taskview.py` | `/task` (running / waiting / open tasks: kind, lane, user, time, stage; a cancel button each) and `/cq` (clear queue / cancel all); every cancel asks first and tells the user | tasks.lanes, repo.users | handlers, Admin Panel |
| `bot/handlers/people.py` | `/admin` (owner adds/removes, "Sure?"), `/groups` (paged list with member counts, group card: link / private invite link, members, admins, authorise / ban, refresh), `/user` (Telegram profile, stored record, task, exams with rolls + marks, groups they are in — checked live —, ban/unban, message, full `.txt` report) | settings, repo, waits, events | handlers, Admin Panel |
| `bot/handlers/waits.py` | `WAITERS` registry + `intercept` (handler group −1): messages an admin step waits for (broadcast messages, IDs) never reach the exam handlers | access.guard, sessions | handlers |
| `bot/handlers/files.py` | `/ls` (sizes, paging, files sent on tap, `_db` 🔒 owner only), `/clean` (dead task folders + web cache; never `_db` or a live task), `/rm` (owner; `_db`: JSON flush → backup zip to dump → second confirm, 60 s buttons, LRU dropped) — `safe_path` keeps everything inside DOWNLOAD_ROOT | repo, uploads, tasks | handlers, Admin Panel |
| `bot/ui/settings_panel.py` | `PANELS` (es / ps / wm / bot: pages of `Field`s), `render` (title button per setting, current value green ✅, ✏️ for typed values), `parse` (typed text → value) | core.settings | handlers/settings |
| `bot/handlers/settings.py` | `/es` `/ps` `/wm` (admin), `/bot` (owner) + `🔑 config.env` (secrets shown only as set / not set, owner's message deleted, `.env` written, restart button) | settings_panel, waits, dotenv | handlers, Admin Panel |
| `bot/handlers/dbpanel.py` | `/db` (owner): Mongo/JSON/outbox state, collections with counts, newest-first docs, a doc (inline, 📎 whole .json), delete ("Sure?"), open by ID, an exam's links, export (zip: one JSON per document → dump; one collection or all), Rebuild JSON from Mongo — all on one panel message | repo, uploads, waits | handlers, Admin Panel |
| `bot/handlers/adminpanel.py` | ⚙️ Admin Panel hub: health line, a button per admin tool (owner: Bot settings, Database); in-message screens replace the panel, conversation tools run their command through its own guard | admin handler modules, stats_view | handlers/basic.nav |
| `bot/handlers/results.py` | `/results` flow: exam → scope → marking (per section) → heavy-lane task | results, compute, delivery | handlers |
| `tools/import_results.py` | import saved sheets into `exam_results` (stop the bot first) | parsers, results | owner |
| `tools/import_users.py` | users of another Mongo database (old `noteshub`) → Telegram data only; existing users keep theirs (stop the bot first) | repo, pymongo | owner |
| `bot/jsonq/parse.py` | question JSON files (`/json`) → ExamData → bundle: HTML → mini-markup (styles, lists), image links, each language's own answer + explanation, same-text files = one language; summary + fingerprint; `inspect_zip` (`/json_batch`: safe unzip, natural order, skips listed) | parsers.common, exam.bundle | jsonq.runner, handlers.jsoncmd |
| `bot/jsonq/fetch.py` | image links → files (public hosts only, manual redirects, size caps, sniffed image types) | aiohttp | jsonq.runner |
| `bot/jsonq/runner.py` | `/json` task: bank `exams["json:<fp>"]` (download → bundle → dump) → separate PDFs per language (explanations) → quizzes (shuffle once, serial numbers, explanation cards); `run_json_batch` — every file of a zip one by one, one summary of links | jsonq, exam_pdf, quiz.build / maker / store | handlers.jsoncmd |
| `bot/quiz/loader.py` · `bot/handlers/loadquiz.py` | `/load_quiz` (admins): quiz files `qz_….json` (or a zip) back into the database — checked, ID kept unless taken, already-here by content / fingerprint, card channel readable; exam bundles / exam JSON / question JSON → the `/json` card | quiz.store, quiz.maker, jsonq | handlers registry, handlers.jsoncmd |
| `bot/handlers/jsoncmd.py` | `/json` + `.json` documents, `/json_batch` (reply to a zip): one choices card (make, language, answers / explanations in the PDF, shuffle / explanations / seconds for the quiz) | jsonq | handlers registry |
| `bot/core/tgfix.py` | Kurigram 2.2.26 fixes (quiz poll explanation serialisation) | pyrogram raw | app, tests |
| `bot/quiz/build.py` | exam bundle → quiz questions: poll when it fits (no image, ≤ 285 / 100 chars, x² as Unicode) else card; skipped count; fingerprint | exam.bundle | quiz.maker |
| `bot/render/quiz_card.py` | quiz card PNG (PyMuPDF htmlbox, 1080 px, frosted card, A–D pills, bilingual one column, 2-card split, explanation box, HTML tables, tall-picture line heights (B-036), `/ps` card colours (D-052)) | fonts, exam_pdf._Images | quiz.maker |
| `bot/quiz/maker.py` | task runners: make quiz (bank → dedup → build → cards drawn 6 at a time + uploaded to the image dump → save → quiz dump post → page); HTML file (cached) | registry, build, quiz_card, delivery | exam handler, quiz handler |
| `bot/quiz/store.py` | quiz ids, `quiz_stats` per quiz (first private / Web App attempt ranked, practice, group counters, top100), board text | repo | engine, web, handlers |
| `bot/quiz/engine.py` | play engine: sessions per chat, card file_ids, polls, votes (raw `UpdateMessagePollVote`), timers, pause / resume / stop, results, boards, restart notice | sender, store | app, quiz handler, autoquiz |
| `bot/quiz/ui.py` | quiz page / list / time picker texts + buttons (Web App button when the tunnel is up) | store | quiz handler, maker |
| `bot/quiz/html.py` | the page's payload (with / without answers, data-URI or URL images), offline file, server-side scoring | bundle, templates/quiz.html | maker, web |
| `bot/quiz/autoquiz.py` | auto quiz loop (admin groups, auto_quiz on, authorised only while /off) | engine, sender | app |
| `bot/web/server.py` | Web App: `/app`, `/api/start` (initData HMAC, access), `/api/submit` (server scoring, ranked), `/img`, `/font.ttf`, `/api/health`; signed tokens | aiohttp, quiz.html, store | app |
| `bot/web/tunnel.py` | cloudflared quick / named tunnel, URL from its log, restart with backoff | stdlib | web.server |
| `bot/handlers/quiz.py` | `/quizzes`, `/quiz`, deep links (`qz_`, `lb_qz_`, `wa_qz_`), page buttons, group start (bot admin, /off rule), shared controls, review / retry, inline search, 👤 My Stats | engine, store, ui, maker | handlers registry, basic |
| `templates/quiz.html` | one HTML/JS quiz page: offline file and Web App (start options, exam screen, palette, section lock, timer, result + review) | — | quiz.html |
| `bot/tasks/gate.py` | fair queues with visible positions before the lanes (`prepare`, `fetch`) | stdlib | exam handler |
| `bot/core/looplag.py` | event-loop lag monitor | — | app, /stats |
| `bot/db/*` | see `docs/DATABASE.md`; `updates.py` applies Mongo operators to dicts so both stores match | pymongo, orjson | everything |
| `bot/tasks/context.py` | `TaskContext` — the single cleanup path | — | lanes, runners |
| `bot/tasks/lanes.py` | `TaskManager`: light/heavy/background lanes, live limits, priority, one task per user, cancel, shutdown | context | app, handlers |
| `bot/tasks/session.py` | in-memory per-user interactive state + inactivity timers | — | handlers |
| `bot/ui/progress.py` | throttled `ProgressMessage`, transfer bars | sender | tasks |
| `bot/ui/keyboards.py`, `texts.py` | callback data ≤ 64 B with user id, coloured buttons, home menu; all English texts | Kurigram | handlers |
| `bot/ui/stats_view.py` | `/stats` + Admin Panel screen: `collect(app)` → dict, `render(snap, detail)` (simple / technical view), Refresh/Details keyboard | psutil, keyboards | handlers/admin, basic, app |

## Deploy files (2026-10-13)
| File | Role |
|---|---|
| `Dockerfile` | 2-stage image (Python 3.13, Debian 12): Tesseract hin/eng, Pango, cloudflared, tini; `DOWNLOAD_ROOT=/data/downloads`, `ENV_FILE=/data/.env` |
| `docker-compose.yml` · `.dockerignore` | one service, `./data` volume, graceful stop; no secrets / samples / data in the image |
| `deploy/install_ubuntu.sh` · `deploy/rvsh-bot.service` | without Docker: apt packages, `.venv`, systemd unit |
| `deploy/colab.sh` · `colab_launcher.ipynb` · `.github/workflows/bot.yml` | run the bot on Google Colab (`colab.sh`: deploy zip + your `.env`, no git) / GitHub Actions (testing; Mongo required; Actions hands over to its next run before the 6 h job limit) | — | — |
| `tests/load/` | load tests: 200 quizzes at once, 10 000 plays of one quiz, 10 000 Web App plays (numbers printed with `pytest -s`) | quiz engine, store, web | — |
| `bot/core/codeupdate.py` · `bot/handlers/update.py` | `/update` (owner): code zip → plan (safe paths, compiles, requirements diff) → backup → write → pip → import test → restart as a new process (`__main__.relaunch`, exit 3 when supervised); rollback on failure |
| `bot/core/config.py` `env_file_path()` | `$ENV_FILE` or `.env` — read at boot, edited by `/bot` (`handlers/settings.ENV_FILE`) |
