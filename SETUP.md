# 🛠️ Setup, step by step

The easy way: open Claude Code (or another coding assistant) in the folder and ask: "Read README.md and SETUP.md and walk me through the setup step by step, path A. I type the passwords and keys myself in .env, not in the chat."

You can also do it on your own. After every step run `python scripts/setup_check.py`. It knows which path you are on (`SSA_STUDY`) and checks only what that path needs.

| Path | What you need | Time |
|---|---|---|
| **A: the assistant only** | A Gemini key, an extra WhatsApp number for the bot (SIM/eSIM), Google Calendar | About an hour |
| **B: + studies** (optional, can be added later) | University login, `config/courses.yaml`, Moodle and Panopto, email, NotebookLM | One to two more hours |

---

## 0. Where it will run

| Option | Good for | Notes |
|---|---|---|
| **A server** (recommended): Oracle Cloud Always Free, Ubuntu 22.04 ARM | An assistant that is available 24/7 | Free. `docs/RUNBOOK.md` §3 = build from scratch |
| **Your computer** (Windows/Mac/Linux) | Trying it out, developing | The bot answers only while the computer is on |

You need **Python 3.12** (3.11 at the least) and **git**.

# Part A: the assistant only

## A1. Install

```bash
git clone <your-private-repo> assistant && cd assistant
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt          # on the server: requirements.lock.txt (pinned versions)
venv/bin/playwright install --with-deps chromium  # only if you want "sign me up to <link>" (site sign-ups)
python scripts/setup_check.py --init               # creates .env from the example, with SSA_STUDY=0
```

(The sign-up command is a Hebrew bot command: "תרשום אותי ל<קישור>".)

## A2. The keys in .env

Every variable is explained in `.env.example` (part A). The minimum:

| Variable | Where from |
|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey. Enable billing, and in the same Google Cloud project: Billing → Budgets, set an alert |
| `WA_OWNER` | **Your number**, digits only (9725XXXXXXXX). This is the only number the bot obeys |
| `WA_BOT_NUMBER` | The number the bridge is linked to (§A3) |
| `WA_OWNER_NAME` | What the bot calls you |
| `SSA_STUDY` | Stays `0` on this path |
| `SSA_BRAIN=1` | Recommended: the mind (Gemini with read-only tools). Without it the old router answers, with a single call |
| `OPENAI_API_KEY` (optional) | Only if you want the bot to understand **voice messages** (transcription). Without it you write to it in text |

> ⚠️ Never send `.env` to anyone, paste it in a chat or upload it to git. On the server: `chmod 600 .env`.

## A3. WhatsApp (your number)

The bot is a **linked device** (like WhatsApp Web) of a WhatsApp number (`WA_BOT_NUMBER`), and you write to it from your own number (`WA_OWNER`). You need a **separate number for the bot** (a second SIM or a cheap eSIM): the bridge ignores messages sent from the account it is linked to, so on the same number it will not hear you. This is also an unofficial library (neonize), and if a number gets banned, better that it is the bot's number and not yours.

1. In .env: `WA_TRANSPORT=bridge`, `WA_BOT_NUMBER`, `WA_OWNER`.
2. A separate environment for the bridge, deliberately with no access to .env and the keys:
   ```bash
   python3.12 -m venv venv_wa && venv_wa/bin/pip install neonize
   WA_BOT_NUMBER=9725XXXXXXXX venv_wa/bin/python whatsapp/bridge.py
   ```
3. On the first run a pairing code is written to `data/wa_bridge/pair_code.txt`. On the bot's phone: WhatsApp → Linked devices → Link a device → "Link with phone number" → type the code.
4. Services on the server (`deploy/systemd/`; update the paths if the user is not `ubuntu`):
   ```bash
   sudo cp deploy/systemd/smart-study-wa-bridge.service deploy/systemd/smart-study-wa-worker.service /etc/systemd/system/
   sudo mkdir -p /etc/systemd/system/smart-study-wa-bridge.service.d && sudo cp deploy/systemd/hardening/smart-study-wa-bridge.conf /etc/systemd/system/smart-study-wa-bridge.service.d/
   sudo systemctl daemon-reload && sudo systemctl enable --now smart-study-wa-bridge smart-study-wa-worker
   ```

(Official alternative: the Meta WhatsApp Cloud API with `WA_TOKEN` and `WA_PHONE_NUMBER_ID`, see `docs/RUNBOOK.md` §7.)

## A4. Google Calendar

1. https://console.cloud.google.com → new project → APIs: **Google Calendar API**.
2. OAuth consent screen: External, add yourself as a test user, and afterwards **In production** (in Testing mode the token dies after 7 days).
3. Credentials → OAuth client ID → **Desktop app** → download the JSON.
4. `python scripts/gcal_auth.py <client.json>`: permission for events only, saved to `config/gcal_token.json`.
5. On the server: sign in on your computer, copy the token file to the server with `scp`, then `chmod 600`.

## A5. Scheduling

The worker does the live work: reminders, follow-up when you do not answer, tasks, alerts before events, the morning message. Cron only has maintenance and safety nets:

```bash
crontab deploy/crontab.assistant.txt     # UTC. Update the paths if they are not /home/ubuntu/smart_study_agent
```

It contains: a heartbeat, a budget check, an encrypted backup to Drive (needs `BACKUP_GPG_RECIPIENT`), a weekly backup restore test (`scripts/backup_restore_test.py`, without decrypting), a nightly consistency check (`scripts/consistency_check.py`), monitoring of the WhatsApp link with an alert outside WhatsApp (`scripts/wa_bridge_watch.py`: email and/or `NTFY_TOPIC`), a weekly report, a token check, the nightly security check and the improvement loop (§A7).

## A6. Test

```bash
PYTHONIOENCODING=utf-8 python -m tests.test_assistant_only   # no model, Panopto, NotebookLM or courses.yaml
python scripts/setup_check.py                                # "✅ ready"
```

Then send "היי" ("hi") to the bot from your number. Try: "תוסיף ליומן מחר ב-20 ארוחת ערב" (add dinner to the calendar tomorrow at 20:00), "תזכיר לי בעוד שעה להתקשר לאמא" (remind me in an hour to call mom), "מה מחכה לי" (what is waiting for me).

## A7. Extras (optional)

- **Partner:** `WA_ALLOWED` (their number) and `WA_PARTNER_NAME`. They can suggest calendar events and receive messages from you, all with your approval.
- **Initiative:** `SSA_PROACTIVE=1` turns on the layer that "notices things": up to 3 messages a day, at least 90 minutes apart, never during quiet hours (`SSA_QUIET_HOURS`). "תפסיק לשאול על זה" (stop asking about this) mutes a topic.
- **Telegram** as a second notification channel: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_USER_ID` (without the ID the bot answers everyone, so it is mandatory).
- **Dashboard** (Streamlit): `DASHBOARD_PASSWORD`, then `venv/bin/streamlit run dashboard/app.py --server.address 127.0.0.1 --server.port 8501`. It listens only on 127.0.0.1. From outside, through an SSH tunnel or Tailscale Serve (tailnet only, **never Funnel**).
- **The improvement loop and "קלוד: ..." ("Claude: ...")**: change tasks run in a coding agent (Claude Code CLI) under an isolated user, with an egress firewall (`deploy/claude_agent/`). Every fix passes the tests on a separate branch and waits for ✅. This setup requires an account with the coding agent. Without it "קלוד: ..." cannot change anything, and the rest of the assistant works as usual.
- **Server hardening** (recommended): `deploy/ssh/00-hardening.conf`, `deploy/fail2ban/`, `deploy/systemd/hardening/` (details in `docs/RUNBOOK.md` §2). The nightly security check: `scripts/security_check.py`. The constants at the top of the file (ports, sudoers, users) match the original server, so on the first run use `--dry` and update them for your server. `SSA_AUTHORIZED_KEYS` = the fingerprint of your key.
- **Second brain in Obsidian:** `scripts/vault_assistant.py` (in cron, `--dry` to preview) writes an area for the assistant in the vault, maps and search over your notes. Your notes travel computer → Drive → server with `rclone copy` only (`scripts/vault_pull.ps1`).
- **A personal Gemini subscription (off by default):** `SSA_SUBSCRIPTION=1` moves simple conversation and summaries to the subscription instead of the API. Read `docs/COSTS.md` §5 and `utils/gemini_sub.py` first: the subscription's terms apply to you, and other people's text never goes there.
- **A cap per month:** `config/budget_caps.json` (for example a low cap in the exam month). A cap is never set below what has already been spent.
- **Deploy gate:** a bare repo on the server + `deploy/hooks/` (see `deploy/hooks/README.md`). Every `git push` runs `tests/test_safety_critical.py`, and a deploy that fails is rejected.

# Part B: the study domain 🎓

It can be added at any point. Built for Tel Aviv University. For another university see §B5.

## B1. Turn it on

```bash
python scripts/setup_check.py --init --student     # creates config/courses.yaml and config/professors.yaml from the examples
```

And in .env (part B):

| Variable | Where from |
|---|---|
| `SSA_STUDY=1` | Turns the domain on |
| `TAU_USERNAME`, `TAU_ID`, `TAU_PASSWORD` | Your university login details (the same ones as Moodle) |
| `SSA_ACTIVE_COURSES` | The keys of this semester's courses from `config/courses.yaml` |
| `OPENAI_API_KEY` (optional) | Paid transcription, strictly last: first captions (waited for up to 48 hours), then local transcription on the server (`SSA_LOCAL_ASR`, $0, see `.env.example` §6) |
| `SSA_UNIVERSITY`, `SSA_COURSE_SITE_URL` (optional) | Your university's name and course site, for the mind's lines |
| `GMAIL_CREDENTIALS_JSON`, `RECIPIENT_EMAIL` (optional) | Summaries by email (§B3) |

With `SSA_STUDY=1` the bot gains: obligation tracking (including an assignment mentioned in an email, within 15 minutes), a deadline ladder, grades and feedback, exam statistics, university email, Moodle, summaries, NotebookLM, reviews and exams. Full list: `docs/FEATURES.md` §2.

Optional: `config/obligations.yaml` (obligations that no collector sees, from `config/obligations.example.yaml`) and `config/assistant.yaml` with `auto_deadlines_to_calendar: true` if you want every deadline to enter your calendar automatically (from `config/assistant.example.yaml`, where it is `false`).

## B2. The timetable: `config/courses.yaml`

The generated file is an example. For each course:

1. **`name` and `course_code`:** as in my.tau.
2. **`my_sessions`:** the classes **of your group**: `type` (lecture/tutorial), `day` (sun..thu), `start`, `end`, `group`, `location`. The system takes only your recordings from this, and filters updates that concern other groups.
3. **`my_staff` / `other_staff`** (optional): the names of the teaching assistants of your group and of other groups, in Hebrew and English. The relevance filter (`integrations/univ_relevance.py`) uses them.
4. **`sources.panopto.folder_id`:** once .env is filled in, `python scripts/find_panopto_folders.py` prints your folders. A folder appears only after the first recording has been uploaded.
5. **`sources.moodle.course_id`:** the number in the address `moodle.tau.ac.il/course/view.php?id=...`.
6. **`settings.schedule.semester_start` / `semester_end`:** outside this window the pipeline does not collect or send. `no_class_dates` for days without classes.

Then `COURSE_NAMES` / `COURSE_ALT_NAMES` in `config/paths.py` if you added a course that is not there (the display name and alternative names that the bot recognizes).

Test: `python dry_run.py` (no sending), then `python scripts/dress_rehearsal.py --no-email` on one lecture.

## B3. Email

1. In the same Google Cloud project also enable the **Gmail API** (and Drive if you want a backup).
2. Summaries by email: put the content of the OAuth client's JSON on one line in `GMAIL_CREDENTIALS_JSON`, then `python scripts/gmail_reauth.py` (saves `config/gmail_token.pickle`).
3. The university email (read-only): `python scripts/tau_mail_auth.py`.

## B4. NotebookLM

There is no official API. The `notebooklm-py` library uses a regular Google login.

```bash
python -m notebooklm login            # on your computer, opens a browser
python scripts/sync_notebooklm_all.py # a notebook per course + upload of whatever is new
```

On the server: `scripts/vnc_notebooklm_login.sh` (login through VNC on the server itself, the most stable), or copy `~/.notebooklm/profiles/default/storage_state.json` from your computer. Google sometimes logs out a copied login, and `scripts/nlm_keepalive.py` checks and alerts. If NotebookLM goes down, summaries and emails continue as usual.

## B5. Moodle and Panopto (another university?)

- The login to TAU (SSO) is in `collectors/panopto_collector.py` and `collectors/moodle_collector.py`. The addresses are in `config/courses.yaml` → `settings.panopto.base_url` / `settings.moodle.base_url`.
- Settings for another university without touching the code: `config/university_pages.json` (the pages to search and the domain), `config/campus.json` (buildings and walking times), `config/student_lines.txt` (extra lines for the mind), `SSA_UNI_MAIL_DOMAIN` (the email domain that is not scrubbed from the notes).
- At another university you need to adapt the login form (the field names) and the base addresses. my.tau (`integrations/tau_portal.py`) and the university email are specific to TAU. Without them the bot simply does not answer exam-hours questions from the site.
- `scripts/panopto_cookie_probe.py` checks how long the login lasts.

## B6. Full scheduling

`deploy/crontab.txt` contains everything in `crontab.assistant.txt` plus the nightly pipeline (`scripts/run_daily.sh`), the pre-class primer, submission tracking, the university email, Moodle messages and NotebookLM. Delete the lines of whatever you did not enable, then `crontab deploy/crontab.txt`. On a Windows computer: `python scheduler/install_task.py` (Task Scheduler).

---

## Tests

```bash
PYTHONIOENCODING=utf-8 python -m tests.test_safety_critical    # the gate: all domains
PYTHONIOENCODING=utf-8 python -m tests.conversation_evals      # real conversations, no network
PYTHONIOENCODING=utf-8 python -m tests.test_assistant_only     # path A only
```

In a clean clone the tests run against `config/courses.example.yaml` (it is copied to `config/courses.yaml` if that is missing). This file does not turn the study domain on: only `SSA_STUDY=1` does. Some tests know the course keys in the example, so if you deleted a course from the example and a test fails, that is the reason.

## What costs money, and how to stop it

- About $3-10 a month for the assistant alone, $15-30 with studies. Details, the price of each capability and ways to save: [docs/COSTS.md](docs/COSTS.md).
- Gemini (the bot, file reading, summaries), OpenAI (transcription only: voice messages to the bot, and lectures without captions in the study domain). Every call is logged in `data/usage.jsonl` and shown in the dashboard.
- A hard cap of $50 a month (`utils/budget.py`): above it everything stops (`data/budget_lock.json`) and a message arrives. On top of that there is a daily cap on conversations (`SSA_BRAIN_DAY_CAP`), a cap per month in `config/budget_caps.json` and a cap on building the site (`SSA_SITE_API_CAP`). When the budget runs out the bot moves to emergency mode and answers from the data.
- To stop the bot: `sudo systemctl stop smart-study-wa-worker`. To turn off the study pipeline: `SMART_STUDY_DISABLE=1`. Without paid transcription: `SMART_STUDY_DISABLE_WHISPER=1`.

## Updating to a new version

```bash
git pull
venv/bin/pip install -r requirements.txt          # on the server: requirements.lock.txt
PYTHONIOENCODING=utf-8 python -m tests.test_assistant_only
sudo systemctl restart smart-study-wa-worker smart-study-wa-bridge
```

With a deploy gate (`deploy/hooks/README.md`): update on your computer and `git push` to the server, and the gate runs the tests before the code goes live.

FAQ and the troubleshooting table: [docs/FAQ.md](docs/FAQ.md).

## Before uploading to GitHub

```bash
python scripts/install_git_hooks.py      # once: every push runs the scan first and is blocked on any finding
python scripts/release_scan.py           # must say "clean"
python scripts/release_scan.py --values ~/my_private_values.txt   # your own private list (phone, email, ID, IP), kept outside the repo
```

See [SECURITY.md](SECURITY.md).
