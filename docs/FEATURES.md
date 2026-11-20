# Features — what it does, how to turn it on, what it costs

> Every capability of the assistant in one place: what it does, how to turn it on (or off), and what it costs. Most capabilities work with no language model at all, and so cost $0.
>
> Note: the assistant is driven in Hebrew. Quoted Hebrew phrases below are what you write to it, or what it writes back; an English gloss follows the first use.

Three layers. The **core secretary** always runs. The **study domain** runs only with `SSA_STUDY=1`. The **second brain** (Obsidian) is optional on top of either.
Costs are per use, measured on the author's own server in November 2026 at Gemini API list prices (see [COSTS.md](COSTS.md) for the monthly picture). "$0" means no paid call at all.

Legend: **default** = on after setup · **opt-in** = off until you set it · **study** = needs `SSA_STUDY=1`.

---

## 1. Core secretary

### Talking to it

| Feature | What it does | Turn on / off | Cost |
|---|---|---|---|
| **The mind** (`agent/mind.py`) | The agent loop. Gemini reads your message, calls read-only tools (calendar, tasks, memory, files, web…), then answers or proposes an action behind a ✅. At most 7 rounds and 8 tools per message. | **opt-in, recommended**: `SSA_BRAIN=1` (without it the older one-call router answers) · `SSA_BRAIN_MODEL`, `SSA_BRAIN_THINKING=low` | ~$0.005–0.015 a message; capped at `SSA_BRAIN_MAX_COST` ($0.10) a message and `SSA_BRAIN_DAY_CAP` ($1) a day |
| **Fast path** (`agent/fastpath.py`) | "מה הלוז שלי מחר" (what is my schedule tomorrow), "מה יש לי השבוע" (what do I have this week), "מתי ההגשה הבאה" (when is the next deadline), "מה מחכה לי" (what is waiting for me), "איפה השיעור" (where is the class), "מתי השיעור הבא" (when is the next class) — a short schedule question is answered straight from the data, in under a second, before any model. | default | $0 (the same question through the mind: ~$0.015, 4–7 s) |
| **Fact checker** (`agent/factcheck.py`) | Every answer's sessions, weekdays, rooms, hours and deadlines are checked against the stores before it goes out. A cancelled session gets "⚠️ שים לב: … מבוטל" ("note: ... is cancelled"); a wrong weekday is fixed in place. Logged to `data/fact_fixes.jsonl`. Never drops an answer. | default | $0 |
| **Hebrew times in code** (`agent/hetime.py`) | "תזכיר לי מחר בבוקר" (remind me tomorrow morning), "בעוד שעתיים" (in two hours), "ביום חמישי ב-8" (on Thursday at 8) → an Israel-time datetime (DST handled), so simple reminders need no model, and the model's dates are checked against your words. | default | $0 |
| **One interim line** (`agent/interim.py`) | A slow turn (a course question, a web search) sends exactly one "רגע, בודק…" ("one moment, checking...") line, whoever is first, never two. | default | $0 |
| **Plain chat on a subscription** (`agent/chat_sub.py`) | A greeting, a thanks, "תנסח לי…" (phrase this for me...), "מה זה X" (what is X) (nothing personal, no action) can go to a personal Gemini subscription instead of the API. The reply is checked (no "I did", no invented times) or the API answers. | **opt-in**, off by default: `SSA_SUBSCRIPTION=1` + the isolated runner. Read `utils/gemini_sub.py` first | $0 on the subscription; the API price otherwise |
| **Web answers in Python** (`integrations/freeweb.py`) | Weather (Open-Meteo), exchange rates (Bank of Israel), Shabbat times / holidays / Hebrew date (hebcal), news (Google News RSS), opening hours (OpenStreetMap) with no model. A general question: keyless search + Wikipedia → the best passages → one small Gemini call with [n] citations. Pages are data: fenced, guarded, never sent to the subscription. | default · `SSA_WEB_MODEL` | $0 structured · ~$0.0005 general · the old grounded call (~$0.04) only as a fallback |
| **Answers about itself** (`agent/project_qa.py`) | "איך עובד הגיבוי?" (how does the backup work?), "איזה קרונים רצים?" (which crons run?), "מה השתנה השבוע?" (what changed this week?), "מה העלות החודשית?" (what is the monthly cost?) — answered from this repo's own docs, code and live state (crontab, last runs, costs). Owner only. Optional: your other projects via `scripts/pc/projects_digest.py`. | default (owner only) · `SSA_PROJECT_MODEL` | one small call, < $0.01; the same question on the same commit: $0 (cached) |

### The calendar, reminders and tasks

| Feature | What it does | Turn on / off | Cost |
|---|---|---|---|
| **Google Calendar** | "תוסיף ליומן…" (add to the calendar...), "תזיז את הפגישה" (move the meeting), several events in one message, a heads-up in the chat before an event. Every change waits for ✅. | default (after `scripts/gcal_auth.py`) | per message (the mind) |
| **Calendar conflicts** | A partner's proposal that clashes with your calendar shows the clash before you approve; a clash on a day is one proactive note. | default | $0 |
| **Reminders that follow up** (`agent/life.py`, `agent/escalate.py`) | Marked important and not answered → "🔔 שוב" ("again"), then a calendar alarm. The second nudge waits your usual answer time at that hour (rhythm). | default · `SSA_ESCALATE=0` off | $0 |
| **Tasks and "מה מחכה לי"** (`agent/tasks.py`, `agent/open_items.py`, `agent/closure.py`) | Open items in one place; "ביטלתי את המלון" (I cancelled the hotel) closes the item by itself; a closed item never comes back. | default | $0 |
| **One open question at a time** (`whatsapp/outbox.py`) | Only the newest numbered question owns the digits; a quoted reply binds to its own question; letters beside; "which one?" (at most 3 options) when unsure; a bare digit after a few unrelated messages binds nothing. | default | $0 |
| **Approvals that don't rot** (`agent/approvals.py`) | A ✅ you never tapped: one reminder after a day, expired with a note two days later. | default | $0 |
| **Undo** (`agent/journal.py`) | "תבטל את מה שעשית" (undo what you did) — the bot's own last actions (calendar add / edit / move / delete…) are journalled with their inverse; the undo asks ✅ too. Things that left the house (a sent message) say plainly that they can't be undone. | default | $0 |
| **Catch-up** (`agent/catchup.py`) | While you are away, non-urgent messages are held; when you are back they come as ONE digest ("מה פספסתי", what did I miss), their questions after it. Urgent things are never held. | default | $0 |
| **Thinking ahead before a trip** (`agent/away.py`) | A trip or a busy period in your own data (an all-day event like "ים המלח עבודה" (Dead Sea work), a hotel check-in/out pair, a booking you sent, "אני בים המלח מ-26 עד 29" (I am at the Dead Sea from the 26th to the 29th)) → one battle plan 1–5 days before: what to finish and by when, and what you'll miss. When a booking changed, it asks once. "מה אני צריך לסיים לפני…?" (what do I need to finish before...?), "מה אני צריך להשלים?" (what do I need to complete?). Never a study question. | default | $0 (no model) |
| **Deadline load radar** (`agent/load_radar.py`) | A week in the next 10 days with 3+ deadlines → one heads-up a week. | default | $0 |
| **Your rhythm** (`agent/rhythm.py`) | Learns, per weekday × hour, when you actually answer what the bot starts. Non-urgent openers wait (≤3 h) for a warm hour; kinds you always ignore back off. Never delays homework, deadlines or escalations. | default | $0 (statistics over local files) |
| **👍 / 👎 on answers** | React to a long answer with 👍 or 👎 (3 a day are asked for). 👎 goes to the improvement backlog with the question and the answer. | default | $0 |
| **Weekly personal summary** (`agent/weekly_personal.py`) | Sunday morning: what you closed, what got pushed off, the next 7 days, your streaks (and the week's grades with the study domain). Never a study question. | default (the Weekly Bot Report cron) · `--personal-dry` to preview | $0 |
| **Noise control** (`agent/noise.py`) | "תפסיק לשלוח לי X" (stop sending me X) mutes that kind (with an undo button); every proactive kind is named; quiet hours; a daily cap. | default · `SSA_QUIET_HOURS=23-7` | $0 |

### People, messages, files

| Feature | What it does | Turn on / off | Cost |
|---|---|---|---|
| **Messages, coordination, bookings** | "שלח לאמא ש…" (tell mom that...), a contact card then "תיצור איתם קשר" (contact them), a web sign-up in a real browser up to payment. You see what goes out and tap ✅. | default | per message |
| **Your files are kept** (`agent/my_files.py`) | Every PDF / photo / link you send is kept with a summary and its identifiers (booking numbers, amounts, dates), so "מה מספר ההזמנה של המלון?" (what is the hotel booking number?) works weeks later. | default | the read itself (one call per file); finding it again $0 |
| **Partner mode** | A second allowed number: calendar proposals, messages between you, all behind your ✅. | `WA_ALLOWED`, `WA_PARTNER_NAME` | per message |
| **Friends' group helper** (`agent/group_helper.py`) | Tag the bot in a group: "תענה לחברים כאן" (answer the friends here) — it answers your friends about a PUBLIC repo only (setup questions), never anything of yours. Opt-in per group and per friend. | opt-in: `SSA_GROUPS=1` + `SSA_STARTER_REPO=owner/repo` | one small call per answer, capped per friend and per group a day |

### Reliability and safety nets

| Feature | What it does | Turn on / off | Cost |
|---|---|---|---|
| **Emergency mode** (`agent/emergency.py`, `utils/llm_health.py`) | Gemini down, a 429, or the month's budget locked: the bot doesn't go quiet. Deterministic answers (fast path, reminders, buttons) still work; a turn that needed a model gets the closest answer from your data and one honest line. A breaker retries after 2 min (doubling to 15). "✅ חזרתי לעבוד כרגיל" ("back to working normally") when it's over. | default | $0 |
| **Nightly consistency check** (`agent/consistency.py`, `scripts/consistency_check.py`) | Finds where two stores disagree: a reminder about something you closed, a cancelled session still on the calendar, a reopened item, duplicate bot events, orphan files. Fixes only the provably safe ones (with an undo in `data/consistency_fixes.jsonl`); tells you at most once a day. | cron 02:50 | $0 |
| **WhatsApp link watch** (`scripts/wa_bridge_watch.py`) | The linked device was logged out or the bridge is down → an alert **outside** WhatsApp (e-mail and/or an ntfy push), once per incident, plus "חזר" ("back"). Includes the re-link steps. | cron every 5 min · `NTFY_TOPIC` optional | $0 |
| **Weekly backup restore test** (`scripts/backup_restore_test.py`) | Restores the newest Drive backup into a temp folder, compares counts and bytes, parses the files, checks the GPG bundle's structure and recipient (never decrypts), the tar archives and the crontab copy. One message only on a FAIL. `rclone copy` only. | cron Sunday 05:30 UTC · `BACKUP_GPG_RECIPIENT` | $0 |
| **Health page** (`utils/system_health.py`, dashboard "בריאות" (health)) | One page: every cron job's last run, the bridge, the backups and their restore test, NotebookLM login, costs. Silent unwrapped jobs are grey, not alarming. | dashboard | $0 |
| **Budget caps** (`utils/budget.py`, `config/budget_caps.json`) | A hard monthly lock (default $50, `SSA_MONTH_CAP_USD`); a lower cap for one month in `config/budget_caps.json`; a pace guard that stops optional paid work (the site build) when the month spends too fast. | default | — |
| **Self-improvement loop** (`agent/improve.py`, [SELF_IMPROVE.md](SELF_IMPROVE.md)) | 👎 and complaints become cases; a nightly scout proposes fixes; repeated multi-round tasks are **codified into Python tools** ([SKILLS.md](SKILLS.md)) instead of prompt tweaks. Every fix passes the full test gate and waits for your ✅. | optional (needs the Claude job runner, see RUNBOOK) | the runner's own subscription; $0 API |

---

## 2. Study domain (`SSA_STUDY=1`)

Built over a real semester at Tel Aviv University, which stays the worked example. Your own university: `config/courses.yaml`, `SSA_UNIVERSITY`, and the optional `config/university_pages.json` / `config/campus.json` / `config/student_lines.txt`.

| Feature | What it does | Turn on / off | Cost |
|---|---|---|---|
| **Obligations from Moodle** (`agent/obligations.py`, `scripts/moodle_light_check.py`) | Every Moodle assignment and quiz per course, exact deadline, what's already submitted. A light check 3× a weekday re-reads known open items and skips a group page you have no access to. | study | $0 |
| **Every obligation, the full list** (`agent/obligations.py`) | "מה אני צריך להגיש?" (what do I need to submit?) → per course: open, submitted, exams. Items no collector can see (homework that is never submitted, a declared project) come from `config/obligations.yaml` (example: `config/obligations.example.yaml`). Things that aren't on Moodle close by your words ("הגשתי את …", I submitted ...). | study | $0 |
| **A named deadline is tracked within 15 minutes** (`agent/obligation_ingest.py`) | A university mail or a Moodle post that says "Assignment 2 … due 06.12 23:59" is tracked at once, then confirmed by a Moodle check. Only the number, the date and the how-to-submit hint are read; an unverified mail is flagged. | study | $0 (reads what the mail / announcement checks already fetched) |
| **The deadline ladder** (`agent/deadline_ladder.py`) | 7 days, 3 days, the day before, the morning of, 6 h and 2 h before, firmer each time, until it's submitted. ≤3 calm nudges a day; the last 6 h may break quiet hours. "תוסיף הגשה משימה ב׳ בתוכנה 1 ל-6/12" (add a submission, assignment B in Software 1, for 6/12) becomes a tracked deadline right away. | study | $0 |
| **Deadlines in your own calendar** (`agent/deadline_cal.py`) | Every tracked deadline and exam as an event in your primary Google Calendar, kept in step (moved, ✅ submitted, ❌ cancelled; nothing is ever deleted). This is a standing authorisation, so no card for each one. | **opt-in**: `auto_deadlines_to_calendar: true` in `config/assistant.yaml` (example: `config/assistant.example.yaml`, false) | $0 (one Calendar call per change) |
| **Grades + the grader's feedback** (`agent/grades.py`) | When a grade or the grader's comments come back: ONE short message (course, item, grade/max, the grader's words, points lost by topic). A log per course and a **weakness profile** (points lost by topic × how often the topic comes up in exams), only when you ask: "על מה אני צריך להתמקד?" (what should I focus on?), "מה המשוב בתוכנה?" (what is the feedback in Software?). | study · `SSA_GRADES=0` off | $0 (no model, read-only GETs) |
| **Exam statistics** (`agent/exam_plan.py`) | From past exams, no model: the share of questions per topic, what comes back every year, the exam's structure, what is not relevant this semester. "על מה לשים את הכסף בהסתברות?" (what is worth betting on in probability?) | study (needs past exams in the vault) | $0 |
| **Course questions, notebook first** (`agent/course_lookup.py`) | A question about the material asks the course's NotebookLM notebook first (answers with sources), and Gemini only if the notebook doesn't know. | study | NotebookLM $0; the Gemini read ~$0.002 |
| **Lecture transcripts in NotebookLM** (`generators/nlm_lectures.py`) | Every lecture's full transcript and its summary go into the course notebook under one clean title, idempotent, broken (Cyrillic) captions kept out. | study (daily pipeline) | $0 |
| **Lecture summaries** | Your group's Panopto recordings → **captions first** (a recording without them waits up to 48 h, unless an exam is close) → **local transcription on the server's CPU ($0, `SSA_LOCAL_ASR=1`)** → the paid transcriber last → a Hebrew summary (board reading in math courses) → PDF by mail and WhatsApp. A formula check on long summaries. | study | ~$0.08 with captions or local transcription; ~$0.40–0.50 with the paid transcriber |
| **University updates for your groups only** | University mail and Moodle posts filtered by your day, hour and tutor; schedule changes (cancelled / room / moved) go into `data/schedule_overrides.json` and every schedule answer reads them. | study | the triage model only for unclear ones |
| **Campus walks** (`agent/campus.py`) | Back-to-back sessions across the campus that you can't walk in the break → one note a day. | study | $0 |
| **Pre-class primer, course site, review on request** | A short recap before class; a private site with everything by topic; "בחן אותי" (quiz me) only when you ask. | study | primer ~$0.001; site build capped by `SSA_SITE_API_CAP` ($0.10 a night) |

---

## 3. Second brain (Obsidian, optional)

| Feature | What it does | Turn on / off | Cost |
|---|---|---|---|
| **The assistant's own area** (`vault_sync/assistant_export.py`, `scripts/vault_assistant.py`) | People, documents, open items, the week, what got done, a daily journal, what it learned about you, mutes — as linked notes, written only when the text changes. | cron 08:40 / 23:40 · `--dry` | $0 (no model) |
| **Maps** (`vault_sync/maps.py`) | A map per course and per area, linked day notes and memories, so the graph is navigable. | with the above | $0 |
| **Search over your own notes** (`vault_sync/personal_index.py`) | BM25 (Hebrew-aware) over the notes you wrote; the mind's memory tool finds and names them. | with the above | $0 |
| **Your notes are yours** (`vault_sync/mine.py`, `vault_sync/notewriter.py`) | Notes in `✍️ שלי` ("Mine") go PC → Drive → server with `rclone copy` only: never overwritten, never deleted, a conflict keeps both. An Inbox line becomes yours only after your ✅. Retired lessons stop coming back. | with the above · `scripts/vault_pull.ps1` on the PC | $0 |
| **Privacy** (`vault_sync/privacy.py`) | Phones, e-mails, IDs and secrets are scrubbed from every note; `private: true` notes never go to NotebookLM. | always | $0 |
