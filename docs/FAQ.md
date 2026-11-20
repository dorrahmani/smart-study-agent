# ❓ FAQ, troubleshooting and glossary

The questions friends actually asked during setup, with a short answer and where the details are.

First of all, always: `python scripts/setup_check.py`. It knows which path you are on (`SSA_STUDY`), checks only what that path needs, and never prints a secret value.

## 🚀 Setup

**Where do I start?**
`python -m venv venv`, `venv/bin/pip install -r requirements.txt`, then `python scripts/setup_check.py --init` (creates `.env` from `.env.example`). After that `SETUP.md` in order, and after each step `python scripts/setup_check.py` again. The easiest way: open the folder in Claude Code and ask "help me set this up" (it works from `CLAUDE.md` and does not touch the keys).

**A server or my computer?**
A server is recommended: Oracle Cloud Always Free (Ubuntu 22.04 ARM), $0, and the bot is available 24/7. On your computer (Windows/Mac/Linux) you can try it and develop, but the bot answers only while the computer is on and connected (`SETUP.md` §0).

**Does it work on Windows?**
Yes, for trying it out. On Windows the commands are `venv\Scripts\pip` and `venv\Scripts\python` instead of `venv/bin/...`, and the bridge runs with `venv_wa\Scripts\python whatsapp\bridge.py`. The `deploy/systemd/` services are Linux only, so on Windows you run the bridge and the worker in open windows (or in WSL). Scheduling the study layer on Windows: `python scheduler/install_task.py`. For an assistant that always answers: a server.

**Which Python?** 3.12 (3.11 at the least). `setup_check` prints `need 3.11+` if the version is old.

**How long does it take?** Path A (the assistant only): about an hour. Path B (studies): one to two more hours.

**What is the difference between path A and B?**
A = the assistant: a Gemini key, WhatsApp, Google Calendar. `SSA_STUDY=0`. B = adds the study layer (`SSA_STUDY=1`): university login, `config/courses.yaml`, Moodle and Panopto, email, NotebookLM. You can add B later too.

**How do I update to a new version of the project?**
```bash
git pull
venv/bin/pip install -r requirements.txt          # on the server: requirements.lock.txt
PYTHONIOENCODING=utf-8 python -m tests.test_assistant_only
sudo systemctl restart smart-study-wa-worker smart-study-wa-bridge
```
If you set up a deploy gate (`deploy/hooks/README.md`), update on your computer and `git push` to the server, and the gate runs the tests before the code goes live.

**How do I stop the bot?** `sudo systemctl stop smart-study-wa-worker` (and `smart-study-wa-bridge` to disconnect WhatsApp). The study pipeline: `SMART_STUDY_DISABLE=1`.

## 📱 WhatsApp

**Do I need an extra SIM?**
Yes. The bot runs as a **linked device** (like WhatsApp Web) of one WhatsApp number (`WA_BOT_NUMBER`), and you talk to it from your own number (`WA_OWNER`). The bridge ignores messages sent from the account it is linked to, so on the same number it will not hear you. You need a second number for the bot: a second SIM or a cheap eSIM. This is also better in terms of bans (`SETUP.md` §A3).

**What goes in WA_OWNER?** Your personal number, in international format and digits only: `9725XXXXXXXX` (no `+`, no dashes, no leading 0). It is the only number the bot obeys. `WA_BOT_NUMBER` = the bot's number, in the same format.

**How do I connect the bot to WhatsApp?**
1. In `.env`: `WA_TRANSPORT=bridge`, `WA_BOT_NUMBER`, `WA_OWNER`.
2. `python3.12 -m venv venv_wa && venv_wa/bin/pip install neonize`, then `WA_BOT_NUMBER=9725XXXXXXXX venv_wa/bin/python whatsapp/bridge.py`.
3. The pairing code is written to `data/wa_bridge/pair_code.txt`. On the bot's phone: WhatsApp → Linked devices → Link a device → "Link with phone number" → the code.
4. On the server: the services from `deploy/systemd/` (`SETUP.md` §A3 step 4).

**The bot does not answer me.** In order:
1. `python scripts/setup_check.py`: is `WA_OWNER` correct? Is there a "WhatsApp bridge session"?
2. `systemctl is-active smart-study-wa-bridge smart-study-wa-worker` (both should say `active`), and `journalctl -u smart-study-wa-bridge -n 50`.
3. Did you send from the `WA_OWNER` number, not from the bot's number?
4. On the bot's phone: WhatsApp → Linked devices. If the device disappeared, delete `data/wa_bridge/session.sqlite3` and pair again.

**Can the number be banned?** Yes, it is possible: the bridge uses an unofficial library (neonize). That is why the bot gets its own number, and not your main one.

**The bridge does not see the .env?** On purpose. It runs in a separate `venv_wa`, with no access to the keys and no project code, and only passes messages through the `data/wa_spool` and `data/wa_outbox` folders.

**Can I use Meta's official API?** Yes: the WhatsApp Cloud API with `WA_TOKEN` and `WA_PHONE_NUMBER_ID` (`docs/RUNBOOK.md` §7). It requires a public address for the webhook.

**Can a partner talk to the bot?** Yes: `WA_ALLOWED` (the number, digits) and `WA_PARTNER_NAME`. They can suggest calendar events and receive messages from you, all with your approval (`SETUP.md` §A7).

**Can I add the bot to a group?** Yes: `SSA_GROUPS=1` in `.env`, and in the group tag the bot and write "תפעיל כאן" ("activate here"). It reacts only to a tag from you (`WA_OWNER`), not from others. "תכבה כאן" ("turn off here") turns it off.

## 🔑 Keys and costs

**Where does the Gemini key come from?** https://aistudio.google.com/apikey. Enable billing, and in the same Google Cloud project: Billing → Budgets, set an alert. The key starts with `AIza`.

**Where do keys go?** Only in `.env` (as `GEMINI_API_KEY=...`). Not in a chat, not in the code and not in git. Do not paste them into Claude Code either: open `.env` and paste them yourself. `setup_check` only says whether the key exists.

**Do I need an OpenAI key?** Not required. It is used only for transcription: **voice messages** to the bot, and lectures without captions in the study layer. Without `OPENAI_API_KEY` voice messages are not transcribed (write to the bot in text), and lectures without captions are skipped. `SMART_STUDY_DISABLE_WHISPER=1` = never pay for transcription.

**How much does it cost?** About $3-10 a month for the assistant alone, $15-30 with studies. Details: [COSTS.md](COSTS.md).

| What | About |
|---|---|
| A message to the mind (Gemini) | $0.005-0.015 |
| Schedule questions, reminders, buttons, undo, "what did I miss" | $0 (no model) |
| A web question | $0 built-in · ~$0.0005 general |
| Oracle Always Free server | $0 |
| A lecture summary with captions or local transcription / paid transcription | $0.08 / $0.40-0.50 |

**How do I make it cheaper?** Captions first and local transcription on the server (`SSA_LOCAL_ASR`), the fast path, web search in Python, NotebookLM first for questions about the material, and caps. All of these are on by default. A personal Gemini subscription (`SSA_SUBSCRIPTION=1`) is **off** by default: the subscription's terms apply to you, it is slower, and other people's text never goes to it (`COSTS.md` §5).

**Is the invoice higher than the dashboard?** It happened: until every call was logged together with the thinking tokens, the invoice was higher than the ledger. Trust the invoice and set a budget alert in Google Cloud.

**Is there a cap?** Yes. A hard cap of $50 a month (`utils/budget.py`, `SSA_MONTH_CAP_USD`), and you can set a different cap for each month in `config/budget_caps.json`. Above it every paid call stops (`data/budget_lock.json`), a message arrives, and the bot moves to emergency mode and answers from the data. In addition `SSA_BRAIN_DAY_CAP` (default $1 a day for conversations) and `SSA_BRAIN_MAX_COST` ($0.10 per message). Every call is logged in `data/usage.jsonl`.

**A key leaked to git?** Replace it immediately (revoke in AI Studio and get a new key). Deleting it in the next commit does not delete it from history (`SECURITY.md`).

## 📅 Google Calendar

**How do I connect?** console.cloud.google.com → a project → enable the Google Calendar API → OAuth consent screen (External, add yourself as a test user, then **In production**) → Credentials → an OAuth client ID of type **Desktop app** → download the JSON → `python scripts/gcal_auth.py <client.json>`. Saved to `config/gcal_token.json` (`SETUP.md` §A4).

**The token died after a week?** The app stayed in Testing: there the token expires after 7 days. Move it to In production and run `gcal_auth.py` again.

**On a server?** Sign in on your computer (a browser is needed), then `scp config/gcal_token.json` to the server and `chmod 600`.

## 🛡️ Security and privacy

**Who can send a message in my name?** Nobody without your ✅. The bot obeys only `WA_OWNER`. Every message to another person, calendar event or sign-up on a site is shown to you first, and goes out only after you press the ✅ button. Words like "yes" in the chat do not count as approval.

**Does it have access to my passwords?** No. Your keys exist only in `.env` on your own server. When signing up on sites (`agent/web_agent.py`) it never types a password, credit card, ID number or SMS code, and does not click "pay". On path A there are no university details on the machine at all. On path B `TAU_PASSWORD` is in your `.env` only.

**Someone sends the bot an email or a message with instructions?** Text from an email, site, document or another person is data, not a command. The model's tools only read, and an action goes through only if a separate check, which sees only your own messages, confirms that you asked for it (`SECURITY.md`).

**Does anyone else see my messages?** No. Everything runs on your server with your keys. Whoever gave you the project sees nothing.

**Can I upload to GitHub?** Yes, to a **private** repo. Before every push: `python scripts/release_scan.py` (must say "0 finding(s)"), and once `python scripts/install_git_hooks.py` so that every push runs the scan. Never upload `.env`, token files or `data/`.

**Is the dashboard exposed?** No. It listens only on 127.0.0.1 and requires `DASHBOARD_PASSWORD`. From outside: an SSH tunnel or Tailscale Serve (not Funnel).

## 🎓 The study layer (SSA_STUDY)

**What is SSA_STUDY?** The flag of the study layer. `0` (the default) = the assistant only, with no study code at all. `1` = tracking of assignments and quizzes from Moodle (including an assignment mentioned in an email, within 15 minutes), a deadline ladder, grades and the grader's feedback, exam statistics, updates from the university for your group only, lecture summaries from Panopto, a course site, NotebookLM, exams and a timetable. All in [FEATURES.md](FEATURES.md) §2.

**Can every deadline enter my calendar automatically?** Yes, but only if you explicitly ask: `config/assistant.yaml` with `auto_deadlines_to_calendar: true` (from the example `config/assistant.example.yaml`). Only these events are written without a card, and nothing is deleted: what was submitted is marked "✅ הוגש" ("submitted"). Every other calendar action still waits for ✅.

**I am not in Tel Aviv.** The assistant (path A) works anywhere. Some of the adaptations are in files: `SSA_UNIVERSITY`, `config/university_pages.json`, `config/campus.json`, `config/student_lines.txt`. The study layer was built for TAU: at another university you need to adapt the login form in `collectors/panopto_collector.py` and `collectors/moodle_collector.py` and `settings.panopto.base_url` / `settings.moodle.base_url` in `config/courses.yaml`. my.tau and the university email are specific to TAU (`SETUP.md` §B5).

**How do I add a course?** In `config/courses.yaml`: `name`, `course_code`, `my_sessions` (day, hour, group), `sources.panopto.folder_id` (`python scripts/find_panopto_folders.py`), `sources.moodle.course_id`. Then its key into `SSA_ACTIVE_COURSES` (`SETUP.md` §B2).

**NotebookLM does not connect on the server?** On your computer `python -m notebooklm login`, or on the server `scripts/vnc_notebooklm_login.sh`, or copy `~/.notebooklm/profiles/default/storage_state.json`. `scripts/nlm_keepalive.py` checks and alerts. Without NotebookLM the summaries and emails continue.

## 🧩 Extending and code

**How do I add a layer of my own?** A flag in `config/features.py`, a module in `agent/`, its tools in `TOOLS` of `agent/mind.py` (filtered by `active_tools()`), and the periodic checks in `tick_list()` of `whatsapp/worker.py`. A layer that is off is not loaded. The study layer is built this way, so copy from it (`README.md` "Built in layers").

**Which model?** Only Gemini (`GEMINI_API_KEY`). There is no other LLM provider in the code, and none should be added (`CLAUDE.md`). OpenAI is used only for voice transcription.

**What is "קלוד: ..." ("Claude: ...")?** A request to change the bot itself. It runs in a coding agent (Claude Code CLI) under an isolated user with a firewall (`deploy/claude_agent/`), on a separate branch, passes all the tests and waits for ✅. Without this setup "קלוד:" changes nothing, and the rest works.

**How do I run the tests?** `PYTHONIOENCODING=utf-8 python -m tests.test_assistant_only` (path A), `python -m tests.test_safety_critical` (the gate), `python -m tests.conversation_evals`, `python -m pytest tests/ -q`.

**A test fails after I deleted a course from the example?** Some tests know the course keys of `config/courses.example.yaml`. Restore them in the example (put your own courses in `config/courses.yaml`).

## 🤖 What the assistant can do (and examples)

The assistant is driven in Hebrew; the examples are what you write to it, with English glosses.

| Area | Write to it |
|---|---|
| 📅 Calendar | "תוסיף ליומן מחר ב-20 ארוחת ערב" (add dinner to the calendar tomorrow at 20:00) · "מה יש לי השבוע" (what do I have this week) · "תזיז את הפגישה למחר" (move the meeting to tomorrow) |
| ⏰ Reminders | "תזכיר לי בעוד שעה להתקשר לאמא" (remind me in an hour to call mom) · "כל יום ב-8 לקחת ויטמין" (every day at 8 take a vitamin) · "אילו תזכורות יש לי" (which reminders do I have) |
| ⏳ Tasks | "מה מחכה לי" (what is waiting for me) · "ביטלתי את המלון" (I cancelled the hotel; closes the item) · "תבטל את מה שעשית" (undo what you did; reverses the bot's last action) · "מה פספסתי" (what did I miss) |
| 🧳 Trips | "אני בים המלח מ-26 עד 29" (I am at the Dead Sea from the 26th to the 29th) · "מה אני צריך לסיים לפני ים המלח?" (what do I need to finish before the Dead Sea?) |
| 📎 Your files | "מה מספר ההזמנה של המלון?" (what is the hotel booking number?), from any file or link you sent |
| 🌐 Web | "מה מזג האוויר מחר בחיפה" (what is the weather tomorrow in Haifa) · "כמה זה 100 דולר בשקלים" (how much is 100 dollars in shekels) · "מתי נכנסת שבת" (when does Shabbat start) |
| 🛠️ About itself | "איך עובד הגיבוי?" (how does the backup work?) · "מה העלות החודשית?" (what is the monthly cost?), owner only |
| 🤝 Messages and coordination | "תתאם עם יוסי פגישה בשבוע הבא" (arrange a meeting with Yossi next week) · "שלח לאמא ש..." (tell mom that...) · a contact card + "תיצור איתם קשר" (contact them), all with ✅ |
| 🌐 Site sign-up | "תרשום אותי ל<קישור>" (sign me up to <link>): in a real browser, up to just before payment (needs `playwright install chromium`) |
| 🔗 Reading | A link, file or photo: "מה הדדליין פה?" (what is the deadline here?), "תסכם לי" (summarize for me). A voice message: with `OPENAI_API_KEY` |
| 🧠 Memory | "תזכור ש..." (remember that...) · "מה אתה זוכר עליי" (what do you remember about me) |
| 🔕 Quiet | "תפסיק לשאול על זה" (stop asking about this) · quiet hours in `SSA_QUIET_HOURS` (default 23-7) |
| 🎓 Studies (`SSA_STUDY=1`) | "מה חסר לי?" (what am I missing?) · "מה אני צריך להגיש?" (what do I need to submit?) · "יצא פתרון לתרגיל 3?" (was the solution to exercise 3 released?) · "על מה אני צריך להתמקד?" (what should I focus on?) · "על מה לשים את הכסף בהסתברות?" (what is worth betting on in probability?) · "בחן אותי" (quiz me) |

**Can it book me a place / set an appointment / cancel a booking?** Yes, by writing to the business (a contact card + "תיצור איתם קשר ותזמין..." ("contact them and book...")) or by signing up on a site with a link. It prepares the message or fills in the form, shows you, and sends only after your ✅. On a site it stops before payment (`agent/booking.py`, `agent/web_agent.py`).

**Does it send messages on its own?** Only the morning message and reminders you asked for. The layer that "notices things" (`SSA_PROACTIVE=1`) is off by default: up to 3 messages a day, at least 90 minutes apart, never during quiet hours.

**An important reminder when WhatsApp is silenced?** What you marked as important and did not answer gets another message, and then a Google Calendar alert that rings even when WhatsApp is muted.

**What it does not do:** it does not phone anyone (it only writes in WhatsApp), does not pay, does not type passwords, has no separate app (it lives in WhatsApp), and has no connection to Instagram/Telegram as a conversation channel (Telegram only for alerts).

## 🧯 Troubleshooting table

| What you see | What to do |
|---|---|
| `no .env — run: python scripts/setup_check.py --init` | Run `--init`, then fill in `.env` |
| `GEMINI_API_KEY — https://aistudio.google.com/apikey` (❌) | There is no key in `.env`. Create one in AI Studio and paste it |
| `GEMINI_API_KEY does not look like a Google key` | A Gemini key starts with `AIza`. Maybe another key was pasted, or one with a space/quotes |
| `WA_BOT_NUMBER == WA_OWNER` | The same number: the bot will not hear you. You need a second number for the bot |
| `TELEGRAM_USER_ID is empty while a bot token is set` | Without `TELEGRAM_USER_ID` the Telegram bot answers anyone. Fill it in, or delete `TELEGRAM_BOT_TOKEN` |
| `need 3.11+ (3.12 recommended)` | Install Python 3.12 and recreate the `venv` |
| `<package> — pip install -r requirements.txt` / `ModuleNotFoundError` | The environment is not active or an install is missing: `venv/bin/pip install -r requirements.txt` |
| `ffmpeg not found` | Only for the study layer (audio and board reading). Install ffmpeg or set `FFMPEG_PATH` |
| `config/courses.yaml missing` | `python scripts/setup_check.py --init --student`, then edit |
| `SSA_ACTIVE_COURSES lists keys not in courses.yaml` | The keys in `SSA_ACTIVE_COURSES` must be exactly as in `config/courses.yaml` |
| `still example ids (Panopto folder / Moodle id)` | `python scripts/find_panopto_folders.py` and the `course_id` from the Moodle address |
| `tracked by git (remove + rotate!)` | A secret file is in git: `git rm --cached`, and replace the secret |
| `not yet: ...` / `WhatsApp bridge session` missing | Pairing has not been done yet (see the WhatsApp section above) |
| The bot is silent, no error | `cat data/budget_lock.json`: did it pass $50 for the month? See "Is there a cap?" |
| `invalid_grant` (Gmail/Calendar) | The token was revoked: `python scripts/gmail_reauth.py` or `gcal_auth.py` again |
| Tests fail after changing courses | `config/courses.example.yaml` needs the original courses |

## 📖 Variable glossary (.env)

| Variable | What |
|---|---|
| `GEMINI_API_KEY` | **Required.** The Gemini key, the only LLM. `GOOGLE_API_KEY` = a fallback, also Gemini |
| `WA_TRANSPORT` | `bridge` = a linked device (the default) |
| `WA_OWNER` | **Required.** Your number, `9725XXXXXXXX`. The only one the bot obeys |
| `WA_BOT_NUMBER` | **Required.** The bot's number (an extra SIM/eSIM) |
| `WA_OWNER_CHAT` | Only if the bridge reports a different chat id (`...@lid`) |
| `WA_OWNER_NAME` / `WA_PARTNER_NAME` | What the bot calls you / your partner |
| `WA_ALLOWED` | Extra numbers allowed to talk to the bot (a partner), comma-separated |
| `SSA_GROUPS` | `1` = the bot in groups, only when you tag it |
| `SSA_PROACTIVE` | `1` = the layer that "notices things" (up to 3 a day) |
| `SSA_QUIET_HOURS` | Quiet hours, default `23-7` |
| `SSA_BRAIN` | `1` = the mind (recommended). Without it the old router answers |
| `SSA_BRAIN_MODEL` / `SSA_BRAIN_DAY_CAP` / `SSA_BRAIN_MAX_COST` | The conversation model / the daily cap ($1) / the per-message cap ($0.10) |
| `SSA_MONTH_CAP_USD` / `SSA_SITE_API_CAP` | The monthly cap ($50, or per month in `config/budget_caps.json`) / the nightly site build ($0.10) |
| `SSA_SUBSCRIPTION` | `1` = a personal Gemini subscription for part of the work. **Off by default**, read `COSTS.md` first |
| `NTFY_TOPIC` | An alert outside WhatsApp when the link to the bridge drops (optional, in addition to email) |
| `SSA_NOTIFY_CHANNEL` | Where alerts go: `whatsapp` or `telegram` |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_USER_ID` | A second alert channel. Without the ID the bot answers everyone |
| `DASHBOARD_PASSWORD` | Required if you run the dashboard |
| `SSA_STUDY` | `0` = the assistant only, `1` = the study layer |
| `TAU_USERNAME` / `TAU_ID` / `TAU_PASSWORD` | The university login (only with `SSA_STUDY=1`) |
| `SSA_ACTIVE_COURSES` | The keys of the semester's courses from `config/courses.yaml` |
| `OPENAI_API_KEY` | Transcription only: voice messages, and lectures without captions when local transcription is not available |
| `SSA_LOCAL_ASR` / `SSA_ASR_PYTHON` / `SSA_ASR_MODEL` | Local lecture transcription on the server ($0) / its venv / the model folder |
| `SMART_STUDY_DISABLE` / `SMART_STUDY_DISABLE_WHISPER` | Turn off the study pipeline / never pay for transcription |
| `GMAIL_CREDENTIALS_JSON` / `RECIPIENT_EMAIL` | Summaries by email |
| `SSA_AUTHORIZED_KEYS` | The fingerprints of the SSH keys allowed on the server (for the security check) |
| `BACKUP_GPG_RECIPIENT` | An encrypted secrets backup to Drive |
| `SSA_STARTER_REPO` | A public `owner/repo` that the bot helps friends set up (optional, off when empty) |
