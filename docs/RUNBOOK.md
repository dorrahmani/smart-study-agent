# Runbook — Smart Study Agent

What to do when something does not work. Sections 1–2 are based on commands that were run on the real system (09/2026); section 3 (building from scratch) has not yet been practiced end to end.

---

## 0. Quick map

| What | Where |
|---|---|
| Python | 3.12 (deadsnakes). `venv` is a symlink to `venv312`; the old one (3.10) is in `venv310` — to go back: `ln -sfn venv310 venv` + restart the services |
| The server | `ubuntu@<your-server>` · key: `~/.ssh/smart_study.key` · Oracle Cloud Always Free (ARM, Ubuntu 22.04) |
| Live code | `~/smart_study_agent` (never edit there — only `git push vm`) |
| git repo on the server | `~/smart_study_agent.git` · hooks: `deploy/hooks/` |
| Secrets | `.env` (600) · `config/gmail_token.pickle` · `config/secrets/` · `~/.config/rclone/rclone.conf` · `~/.notebooklm/profiles/default/storage_state.json` |
| Services | `dashboard.service` · `smart-study-wa-worker.service` · `smart-study-wa-bridge.service` (settings: `deploy/systemd/`). `smart-study-bot.service` (Telegram) **has been off since 10/11/2026** — the bot was deleted; the file stays aside, do not enable it |
| cron (UTC!) | `crontab -l` · source of truth: `deploy/crontab.txt` · wrapper log: `/tmp/cron_health.log` |
| Nightly run log | `data/daily_full_log.txt` (+ 14 days back, compressed) · timestamp: `data/last_pipeline_run.json` |
| Backup | `gdrive:smart-study-agent-backup/<YYYY-MM-DD>/` · 14 days · every night at 04:00 UTC |
| Dashboard | **https://your-server.your-tailnet.ts.net** (Tailscale Serve, tailnet only — from any device connected to Tailscale in the account user6@example.com) · or `דשבורד.bat` (SSH tunnel) |

```bash
ssh -i ~/.ssh/smart_study.key ubuntu@<your-server>
```

## 1. How to know everything is fine

- **Every evening during the semester**: in WhatsApp every new summary (when there is a lecture) + an email with a PDF.
- **Every morning**: the morning message in WhatsApp. If yesterday's run did not run / did not finish, a 🚨 alert arrives in WhatsApp (`scripts/cron_heartbeat.py`, once a day after 07:00) — see §3.
- **Automatic alerts** (WhatsApp only, `utils/notify.py`; Telegram was deleted 10/11): ⚠️ a scheduled task failed · ⚠️ pipeline health report · 🚨 cron has not run for 10 hours (`scripts/cron_heartbeat.py`) · 💾 disk / memory (`memory_watchdog.sh`).
- **An alert that WhatsApp did not receive** (the bridge is down / no owner chat) is not lost: it is recorded in `data/undelivered_alerts.jsonl`, shown in the dashboard (📭) and in the weekly bot report. To check: `tail -5 data/undelivered_alerts.jsonl`.
- **Manual check** (a minute):
  ```bash
  cd ~/smart_study_agent && tail -5 /tmp/cron_health.log && cat data/last_pipeline_run.json && systemctl is-active dashboard smart-study-wa-worker smart-study-wa-bridge
  ```

## 2. Common failures

### 🔑 Panopto/Moodle do not log in ("login failed")
Almost always: **the TAU password changed**.
1. Update `TAU_PASSWORD` in `.env` **on your computer**.
2. Copy it to the server without printing it (automatic backup to `backups/env.<ts>`):
   ```bash
   grep -E "^TAU_PASSWORD=" .env | ssh -i ~/.ssh/smart_study.key ubuntu@<your-server> 'cd ~/smart_study_agent && install -m 600 .env backups/env.$(date +%Y%m%d-%H%M%S) && python3 -c "import sys;n=sys.stdin.readline().rstrip(\"\n\");L=open(\".env\").read().split(\"\n\");open(\".env\",\"w\").write(\"\n\".join(n if l.startswith(\"TAU_PASSWORD=\") else l for l in L))" && chmod 600 .env'
   ```
3. Check: `venv/bin/python3 scripts/find_panopto_folders.py` → "login succeeded!" (the script prints this in Hebrew).

### 📧 Gmail: `invalid_grant`
The token was revoked/expired. On your computer: `python scripts/gmail_reauth.py` (opens a browser) ← then:
```bash
scp -i ~/.ssh/smart_study.key config/gmail_token.pickle ubuntu@<your-server>:smart_study_agent/config/ && ssh -i ~/.ssh/smart_study.key ubuntu@<your-server> 'chmod 600 ~/smart_study_agent/config/gmail_token.pickle'
```

### 📓 NotebookLM: "cookie פג" / Authentication expired
On your computer: `python -m notebooklm login --browser chrome` (notebooklm-py ≥ 0.8.3; the account `user3@example.com` — the 16 notebooks are there) ← then:
```bash
scp -i ~/.ssh/smart_study.key ~/.notebooklm/profiles/default/storage_state.json ubuntu@<your-server>:.notebooklm/profiles/default/storage_state.json && ssh -i ~/.ssh/smart_study.key ubuntu@<your-server> 'chmod 600 ~/.notebooklm/profiles/default/storage_state.json'
```
(NotebookLM is not blocking — if it fails, summaries and emails continue.)
Usually this happens by itself: on the server `scripts/nlm_keepalive.py` checks every 20 minutes (`logs/nlm_keepalive.log`), and on your computer the task `SmartStudy_NLM_Session` (every 4 hours, at login, on wake from sleep) renews it and copies it to the server when it dies — log: `~/.smart_study/nlm_session.log`. Manually: `python scripts/pc/nlm_session_refresh.py --force`.

### 🔑 A master token for NotebookLM (a permanent solution, the user's decision)
The server holds a *copy* of Chrome's session on the computer, and Google drops the copy after hours to days (15/11). With a master token the server issues its own session and renews it by itself when it drops — without the computer. Once: on the server `venv/bin/pip install 'notebooklm-py[headless]==0.8.6'`; on the computer `pip install 'notebooklm-py[headless]==0.8.6'` and then `python -m notebooklm login --master-token --account user3@example.com` (a Google sign-in window — it may ask for a password / approval on the phone; the user does that part themselves), then copy `master_token.json` and `storage_state.json` from `~/.notebooklm/profiles/default/` to the same folder on the server (chmod 600). ⚠️ The risk: the token is full, long-lived access to the whole account (Gmail, Drive...) until you change the password or remove the "Android device" at myaccount.google.com → Security. Whoever gets the file on the server gets the account. Suitable only for this dedicated account, not for a personal account.


### 💸 No summaries and no error — the budget lock
`cat data/budget_lock.json` — if it exists, the monthly spend passed $50. Check in the dashboard (costs) **why** before deleting the file. A lock from a previous month clears by itself.

### 🎙️ Transcription fails (`insufficient_quota` / 429)
The OpenAI credit ran out → platform.openai.com/settings/organization/billing.

### 🚨 "No new summary for N days" in a course
1. `grep -E "PIPELINE-WARN|סינון מערכת שעות|הרצאות רלוונטיות" data/daily_full_log.txt` — what did the filter do? (The log lines are in Hebrew: "timetable filter", "relevant lectures".)
2. Is `folder_id` still `TO_BE_FILLED`? → `scripts/find_panopto_folders.py` ← update `config/courses.yaml` ← commit ← `git push vm`.
3. "No recording matches my timetable" → did the class move? Update `my_sessions` (day/hour) in `courses.yaml`.
4. Was a recording really not uploaded? Check in Panopto in a browser.

### 🚨 "The pipeline did not run" / "cron has not run for 10 hours"
```bash
ls -l ~/smart_study_agent/scripts/*.sh      # all of them must have x (-rwxr...)
crontab -l | head; systemctl is-active cron
tail -20 /tmp/cron_health.log; tail -5 /tmp/*.log
```
A manual run of the nightly run (also outside the semester):
```bash
cd ~/smart_study_agent && SMART_STUDY_FORCE=1 setsid nohup scripts/run_with_alert.sh "Daily Pipeline (manual)" scripts/run_daily.sh > /tmp/manual_run.out 2>&1 < /dev/null &
```

### 🛑 The deploy was rejected ("SAFETY TESTS FAILED")
This is the gate doing its job — the server was not touched. Read the test that failed, fix locally:
```bash
PYTHONIOENCODING=utf-8 python -m tests.test_safety_critical
```
**Never bypass the gate.**

### ↩️ A deploy broke something
```bash
git revert <sha> && git push vm
```
(Force-push is blocked on purpose.)

### 📵 The WhatsApp bridge disconnected (an alert outside WhatsApp)
`scripts/wa_bridge_watch.py` (cron every 5 minutes, no LLM, `bridge.py` untouched) reads `data/wa_bridge/status.json`, the existence of `session.sqlite3`, `systemctl is-active` and the log of `smart-study-wa-bridge`. **Logged out / missing session → an immediate alert; disconnected or the service down for 10+ minutes → an alert.** A disconnect of a few seconds (websocket EOF and then "connected to WhatsApp") is normal and does not alert. One alert per failure + one "back" message; state in `data/wa_bridge_watch.json`, log in `logs/wa_bridge_watch.log`.
**Channels (all outside WhatsApp, whatever is configured):** an email to the user themselves (`RECIPIENT_EMAIL` in .env or `settings.email.recipient` in courses.yaml) through the existing Gmail token (`config/gmail_token.pickle`, send permission) · and ntfy.sh if `NTFY_TOPIC` is set in .env (a long random topic, **not in git**). If `invalid_grant` — see "Gmail" above; if no channel worked → it is recorded in `data/undelivered_alerts.jsonl` and retried every 5 minutes.
**Turning on ntfy (once, the user):** 1. On the phone: install the ntfy app (app store). 2. On the server: `echo "NTFY_TOPIC=ssa-$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" >> ~/smart_study_agent/.env` and then `grep ^NTFY_TOPIC ~/smart_study_agent/.env` to see the name. 3. In the app: ＋ ← Subscribe to topic ← paste the name (default server ntfy.sh). 4. Test: `curl -d "test" ntfy.sh/<name>` — a notification should arrive. (Anyone who knows the name can read it — that is why it is long, random and not kept in git.)
**Reconnecting (the bridge does not print a QR — it pairs with a code):**
1. `ssh <user>@<your-server>`
2. `cd ~/smart_study_agent/data/wa_bridge && mkdir -p old && mv session.sqlite3* old/` (move aside, do not delete)
3. `sudo systemctl restart smart-study-wa-bridge; sleep 40; cat ~/smart_study_agent/data/wa_bridge/pair_code.txt` (an 8-character code; it also arrives as a push notification on the bot's phone)
4. On the phone of the bot's number: WhatsApp ← Linked devices ← Link a device ← "Link with phone number instead" ← type the code.
5. `cat ~/smart_study_agent/data/wa_bridge/status.json` should show `connected`; within 5 minutes a "back" message arrives. If `WA_BOT_NUMBER` is missing from `config/wa_bridge.env` the bridge will not pair.
**A test without bringing anything down:** `venv/bin/python3 scripts/wa_bridge_watch.py --simulate logged_out` (prints the message, does not send).

### 🔌 The server does not answer SSH
1. Wait 5 minutes (the watchdog on the computer tries to revive it).
2. Oracle Cloud Console → Compute → Instances → smart-study-agent → **Reboot**.
3. After it comes up: `systemctl is-active dashboard smart-study-wa-worker smart-study-wa-bridge cron` + `python -m tests.test_safety_critical`.

### 🔐 Restoring secrets from the backup (encrypted)
Since 28.09.2026 the secrets (`.env`, `config/secrets`, the Gmail token, `rclone.conf`, the NotebookLM session) are kept on Drive **only** as `secrets.tar.gz.gpg`, encrypted to the key `<YOUR_GPG_FINGERPRINT>`.
**The private key exists only on your computer** (`gpg --list-secret-keys`). Without it the backup cannot be opened — see "Backing up the key" below.
```bash
# On the VM: download the encrypted file (safe — it is encrypted)
rclone copyto gdrive:smart-study-agent-backup/<YYYY-MM-DD>/secrets.tar.gz.gpg /tmp/s.gpg
# On the computer: decrypt and extract to a private folder
scp -i ~/.ssh/smart_study.key ubuntu@<your-server>:/tmp/s.gpg . && gpg -d s.gpg | tar -xzf - -C <private-dir>
```
**Backing up the private key (once, mandatory):** `gpg --armor --export-secret-keys <YOUR_GPG_FINGERPRINT> > backup-key.asc` ← keep it in the password manager / a USB drive, and delete the file from the disk.

### 🛡️ Server hardening (what is configured and where)
- Firewall: only 22/tcp (`sudo ufw status`). CUPS and rpcbind are off.
- SSH: `deploy/ssh/00-hardening.conf` → `/etc/ssh/sshd_config.d/` (no root, no passwords, 3 attempts, only `ubuntu`). A single authorized key: `ssh-key-2026-05-08` = `~/.ssh/smart_study.key`.
- fail2ban: `deploy/fail2ban/sshd-local.conf` (a one-hour ban, growing on repeats). Check: `sudo fail2ban-client status sshd`.
- **A change to sshd always with a safety net**: `sudo systemd-run --unit=ssh-revert --on-active=180 <the revert>` → reload → a new login → `sudo systemctl stop ssh-revert.timer`.

### 🔒 The nightly security check (`scripts/security_check.py`)
Checks every night that the hardening has not regressed: ports against a list, ufw, the egress rules of claudeagent/geminiagent, the permissions of secret files,
groups/users/sudoers/SSH keys/sshd, fail2ban, the hardening of the bridge and the dashboard, the isolated gate, updates, SSH logins, tailscale,
tokens in logs and in git (a count only, never the value), a live check of the tokens, and api_only/caps/gate tests. No LLM.
- **cron** (UTC, 03:30 = 05:30 in Israel; the alert waits for the end of quiet hours, except a suspected break-in, which is sent at once):
  `30 3 * * * TZ=Asia/Jerusalem /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Security Check" bash -c "cd /home/ubuntu/smart_study_agent && PYTHONIOENCODING=utf-8 venv/bin/python3 scripts/security_check.py" >> /home/ubuntu/smart_study_agent/logs/security_check.log 2>&1`
- **Manual:** `venv/bin/python3 scripts/security_check.py --dry` (a table only, no file and no alert); `--no-live` without calls to Google.
- **Result:** `data/security_status.json` (the latest) + `data/security_history.jsonl` (a line per night). A WhatsApp alert only on a new FAIL;
  the weekly bot report shows "🔒 security: N checks passed, X warnings".
- **An intentional change** (a new SSH key, a sudoers file, a new listener) will appear once as FAIL/WARN: update the matching constant at the top of the script
  (`EXPOSED_OK`, `AUTHORIZED_KEYS`, `SUDOERS_D`, `SHELL_USERS`, `API_ONLY_EXPECTED`...) in the same commit as the change.
- **Common fixes:** logs in /tmp readable by everyone → `chmod 600 /tmp/*.log`; a reboot is required → `sudo reboot` (at a quiet hour);
  an open secret file → `chmod 600 <the file>`; a non-isolated gate → the last two lines in `deploy/hooks/README.md`.

## 3. Building a new server from scratch (a disaster)

Goal: under an hour. You need: your computer (code + `.env`) and the backup on Drive.

1. **Oracle**: a new instance, Ubuntu 22.04 ARM (Ampere A1, 4 OCPU / 24GB), the public key `ssh-key-2026-05-16`.
2. **System**:
   ```bash
   sudo apt update && sudo apt install -y software-properties-common git ffmpeg rclone ufw gnupg
   sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt install -y python3.12 python3.12-venv python3.12-dev
   sudo ufw allow 22/tcp && sudo ufw enable
   ```
3. **Code** (from the computer): create a bare repo on the server, install the hooks from `deploy/hooks/`, then `git remote set-url vm ...` + `git push vm`.
4. **Environment**:
   ```bash
   cd ~/smart_study_agent && python3.12 -m venv venv && venv/bin/pip install -r requirements.lock.txt && venv/bin/playwright install --with-deps chromium
   ```
5. **Secrets**: `scp .env` from the computer (chmod 600), or decrypt `secrets.tar.gz.gpg` (see "Restoring secrets") · `rclone.conf` (see `scripts/vm_setup_rclone.sh`).
   Hardening: `sudo install -m 644 deploy/ssh/00-hardening.conf /etc/ssh/sshd_config.d/` (with the safety net!) · `sudo apt install fail2ban && sudo install -m 644 deploy/fail2ban/sshd-local.conf /etc/fail2ban/jail.d/`
   The public key for the backup: `gpg --armor --export <YOUR_GPG_FINGERPRINT>` on the computer → `gpg --import` on the server.
6. **Data from the backup** (every path returns to its place):
   ```bash
   D=<YYYY-MM-DD>; rclone copy gdrive:smart-study-agent-backup/$D/ ~/smart_study_agent/ && chmod 600 ~/smart_study_agent/.env ~/smart_study_agent/config/*.pickle
   ```
7. **Services and cron**:
   ```bash
   sudo cp deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now dashboard smart-study-wa-worker smart-study-wa-bridge   # not smart-study-bot (Telegram, deleted 10/11)
   sudo install -m 644 deploy/logrotate/smart-study /etc/logrotate.d/smart-study
   crontab deploy/crontab.txt
   ```
8. **Verification**: `python -m tests.test_safety_critical` · `scripts/dress_rehearsal.py --no-email` · `דשבורד.bat` (update the IP).

## 4. Start of a new semester — checklist

1. `config/courses.yaml`: `semester_start`, `semester_end`, `no_class_dates` (a single source of truth — `run_daily.sh` also reads from here).
2. New courses in `courses.yaml` (+ `my_sessions` from the calendar) and in `config/paths.py` (`COURSE_NAMES`, `ACTIVE_COURSES`).
3. `scripts/find_panopto_folders.py` → `folder_id` for each course.
4. `python -m tests.test_safety_critical` → commit → `git push vm`.
5. `scripts/dress_rehearsal.py` on an old lecture (the email only to you).
6. After the first lecture: check that only your group was picked up.

## 5. ⚠️ Before sharing the code (GitHub / friends)
- `python scripts/release_scan.py` must return "clean" (and also `--all` if you share a folder and not git).
- Never: `.env`, `config/*.pickle`, `storage_state.json`, `config/courses.yaml` (your timetable), `data/`, `logs/`.
- If a secret ever got into history — rotate the secret; deleting it from the file is not enough.

## 6. Tailscale (access to the dashboard from anywhere)
- The server `smart-study` (<server-tailnet-ip>) in the tailnet of user6@example.com. The dashboard: `tailscale serve --bg 8501` → `https://your-server.your-tailnet.ts.net` (**tailnet only** — not Funnel, not public). No port is opened in ufw/Oracle.
- Check: `tailscale serve status` (should show "(tailnet only)"). **Never** `tailscale funnel` — that would expose the dashboard to the internet.
- A new device: install Tailscale and sign in with the same account. To remove a device: login.tailscale.com → Machines → Remove.
- **ACL (01/10):** the server is tagged `tag:server`; the policy: the personal devices (autogroup:member) → each other + `tag:server:443` only; the server has no access out to the devices. Verified: PC→dashboard 200, server→PC blocked (ping + tcp).
- ⚠️ `pkill -f "tailscale serve"` also kills the SSH session that runs it (the text appears in the command line) — do not use it.

## 7. Personal agent — WhatsApp + calendar (activation)
The code is off until you fill in the variables in `.env` (see SETUP.md).

**Google Calendar:**
1. In the project's Google Cloud Console: enable the Calendar API; on the OAuth consent screen: **In production** mode (in Testing the token dies after 7 days); an OAuth client of type Desktop → download the JSON.
2. On the computer: `python scripts/gcal_auth.py <client.json>` → approval in the browser (permission: events only).
3. `scp -i ~/.ssh/smart_study.key config/gcal_token.json ubuntu@<your-server>:smart_study_agent/config/` and then on the server `chmod 600 ~/smart_study_agent/config/gcal_token.json`.
4. Test in WhatsApp: "מה יש לי מחר ביומן" ("what do I have tomorrow in the calendar").
5. (Optional) deadlines into the calendar: `SSA_DEADLINES_TO_CALENDAR=1` in .env.

**WhatsApp:**
1. developers.facebook.com → an app (Business) → the WhatsApp product. Register the user's number as a recipient (a code in WhatsApp).
2. A System User + a permanent token with the permissions `whatsapp_business_messaging`, `whatsapp_business_management`.
3. In .env (600): `WA_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_OWNER=9725XXXXXXXX` (`SSA_NOTIFY_CHANNEL` is no longer read — WhatsApp has been the only channel since 10/11).
4. `config/wa_receiver.env` (600) — **only**: `WA_APP_SECRET`, `WA_VERIFY_TOKEN` (a random string), `WA_OWNER`, `WA_SPOOL_DIR=/home/ubuntu/smart_study_agent/data/wa_spool`.
5. Services:
   `sudo cp deploy/systemd/smart-study-wa-{receiver,worker}.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now smart-study-wa-receiver smart-study-wa-worker`
6. Funnel (only port 8443 → the receiver; the dashboard stays on 443 inside the tailnet):
   In the Tailscale policy: `"nodeAttrs": [{"target": ["tag:server"], "attr": ["funnel"]}]`, and then on the server
   `sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8090` · check: `tailscale funnel status` — must show **only** 8443.
7. In Meta → Webhooks: Callback `https://your-server.your-tailnet.ts.net:8443/wa/webhook`, Verify token = `WA_VERIFY_TOKEN`, subscribe to `messages`.
8. Test: send "היי" ("hi") to the bot → a menu. Logs: `logs/wa_worker.log`, `journalctl -u smart-study-wa-receiver`.
**Emergency shutdown:** `sudo tailscale funnel --https=8443 off` (disconnects from the internet at once) · "עצור סוכן" ("stop agent") in the chat (stops actions).

### 🧠 The bot's mind (`agent/mind.py`)
A regular message from the user goes through a Gemini loop with read-only tools: calendar, tasks, submissions, timetable, email, course materials, summaries, memory, the web. Exact commands ("עצור סוכן", "קלוד:", buttons) stay as they were. Groups, friends, the partner and contacts do not enter the loop.
**Turning it off:** `SSA_BRAIN=0` in `.env` and then restart the worker: the previous route returns. A failure, timeout, budget lock or daily cap fall back to it by themselves.
**Limits:** 7 rounds and 8 tools per message, `SSA_BRAIN_MAX_COST` ($0.10), `SSA_BRAIN_DAY_CAP` ($1 a day), `SSA_BRAIN_TIMEOUT` (70 seconds).
**Log:** `data/mind_log.jsonl`, metadata only (tools, cost, time, what was blocked).

### 🧪 The conversation set for regression (`tests/conversation_evals.py`)
Real cases (anonymized) from the live log, grouped by failure type: a claim of execution after the gate refused, a task text for Claude built from the history, a lone "yes"/digit, "yes done" to a question from the bot, a continuation by the partner, a known alert (`agent/known_alerts.py`), email, forwarded text, the cost cap. Gemini does not run — what the model "decided" is written in the case.
**Run:** `PYTHONIOENCODING=utf-8 python -m tests.conversation_evals` (a scoreboard by type). The gate in the deploy runs all the `test_conv_*` through `tests/test_safety_critical.py`.
**A new case:** copy a line from `data/agent_conversations.jsonl`, remove numbers/identifiers/addresses, and write in `live` what the bot did. A case the code does not yet meet goes in with `gap='...'`: it is reported, and does not fail the gate.

### 📊 The weekly bot report (`scripts/weekly_bot_report.py`)
A short message to the user once a week: API spend (from `data/usage.jsonl`, and the bot's share), the number of requests, how many actions the gate stopped, failures, Claude tasks (ran/deployed/rejected) and what was deployed (`git log` over every `deploy A -> B` in `logs/deploy.log*`). No study content, no LLM. Once per ISO week (`data/weekly_bot_report_sent.json`).
**Check:** `venv/bin/python3 scripts/weekly_bot_report.py --dry` (prints, does not send, does not mark). `--force` sends again in the same week.
17/11: the same run first sends a separate message — **"🗓️ השבוע שלך" ("Your week")** (`agent/weekly_personal.py`: what was closed, what was postponed, the next 7 days, streaks; from files only, once per ISO week, `data/weekly_personal_sent.json`, not during quiet hours). `--personal-dry` prints only it. The report itself also shows 👍/👎 (`data/feedback_ratings.jsonl`) and **the user's rhythm** (`agent/rhythm.py` → `data/rhythm.json`; the full table: `venv/bin/python3 -m agent.rhythm`).
**Activation (crontab -e on the VM, Sunday 09:00 Israel time in winter / 10:00 in summer):**
```
0 7 * * 0 TZ=Asia/Jerusalem /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Weekly Bot Report" bash -c "cd /home/ubuntu/smart_study_agent && PYTHONIOENCODING=utf-8 venv/bin/python3 scripts/weekly_bot_report.py" >> /home/ubuntu/smart_study_agent/logs/weekly_bot_report.log 2>&1
```
The deploy git repo: `~/smart_study_agent.git` (or `SSA_GIT_DIR` in .env).

### 👩‍💼 The second brain in Obsidian (`scripts/vault_assistant.py`, `vault_sync/`)
Everything the assistant knows and does → `Smart Study Agent/👩‍💼 העוזר/` ("the assistant"; people, documents, open now, the week, closed, obligations by course, a daily journal, things I learned, mutes, the bot). No model, no network except rclone: $0. A note is written only when its text changed.
- **✍️ Mine** (the notes the user writes on the PC): `vault_pull.ps1` uploads only this folder to `gdrive:smart-study-agent/vault_inbox` (+ `.ssa_manifest.txt`), and the pull skips it. On the VM: `vault_sync/mine.py` pulls to `data/vault_inbox_staging` and merges: never deletes and never overwrites (an old version → `.replaced/`, a conflict → both copies).
- A line `- [ ] תזכיר לי ...` ("remind me ...") / `לעוזר: ...` ("to the assistant: ...") in a note of theirs → a question in WhatsApp with ✅ (once per line, within the questions budget). Only their ✅ turns it into their own message (`vi:` in core).
- `private: true` (the journal, the bot, ✍️ Mine; `config/vault_privacy.json` to change per domain) → is not uploaded to NotebookLM.
**Check:** `venv/bin/python3 scripts/vault_assistant.py --dry --out /tmp/vault_dry` (writes only there, does not send).
**Activation (crontab -e on the VM):**
```
40 8,23 * * * TZ=Asia/Jerusalem /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Vault Assistant" bash -c "cd /home/ubuntu/smart_study_agent && PYTHONIOENCODING=utf-8 venv/bin/python3 scripts/vault_assistant.py" >> /home/ubuntu/smart_study_agent/logs/vault_assistant.log 2>&1
```

### 🧠 Self-improvement (`agent/improve.py`, called from `scripts/agent_daily_review.py`)
Runs on a **Claude subscription** only (`claude_job` tasks as `claudeagent`, including waiting for quota). No Gemini, no API key.
1. **A pre-check with no model** (every day, in the "Agent Review" cron): the user's complaints ("you did not understand", "again", "I already said"), a question that came back, "I have no way to", slow/expensive turns, gate stops, rejected deploys, failures, pipeline alerts, cost per feature, failed Claude tasks, the user's Claude Code sessions (`data/claude_sessions`), habits by day of the week.
2. **Pace** (`data/improve_state.json`): a day → after 5 empty runs every 3 days → again 5 → a week. A complaint / 👎 / a gate stop / a rejected deploy / 3+ failures / a security failure → back to a day. A day with no signal at all = an empty run with no model.
3. **The scout** (a silent Claude task, `kind=scout`): only analyzes, writes `.improve_findings.json` in the ws. A = a fix in files that may be deployed from the phone + a case in `tests/improve_cases.json` → up to 2 fix tasks (last in the queue, **only at night 23:00–07:30**, so they do not delay the user's "קלוד:"). B → `data/improvement_backlog.json`. C → one question for the weekly report.
4. **Approval**: one message per run, only when there is something to approve: "N improvements ready" [🚀 deploy all] [🔍 details] [⏳]. The deploy passes the same gate. `settings.auto_docs` (off): a fix that touches only docs / test cases is deployed by itself.
5. **The loop may not change** (`improve.DENY_RX`, also checked in deploy): claude_job/improve/the review, core, mind, whatsapp/, deploy/, budget, notify, security, config, requirements, test code. `tests/improve_cases.json` is deployed from the phone only as an addition (deleting/changing a case → rejected).
**WhatsApp:** "קלוד, תבדוק את עצמך עכשיו" ("Claude, check yourself now") · "כל כמה זמן אתה משתפר?" ("how often do you improve?") · "קלוד, תפרוס לבד תיקוני תיעוד" ("Claude, deploy doc fixes by yourself") / "קלוד, בלי פריסה לבד" ("Claude, no self-deploy").
**A dry test:** `PYTHONIOENCODING=utf-8 venv/bin/python3 scripts/agent_daily_review.py --dry-run` (prints the signals and the decision, does not save and does not run).

### 🗂️ The bot knows the projects (`agent/project_qa.py`, 17/11)
The user (only the owner — the `project` tool in `agent/mind.py`, `owner_only`) asks "how does X work", "which crons run", "what changed this week", "what is the cost".
- **This repo:** a local index of HEAD (`data/project_index.json`, rebuilt when HEAD moves): docs, docstrings, non-secret config, deploy/, environment variables, `git log`. No .env, secrets, tokens, data/, logs/; every snippet goes through a secrets filter.
- **Live state:** `crontab -l`, `/tmp/cron_health.log`, `agent/weekly.cost_lines`, the budget lock, `utils/pipeline_health`, `systemctl is-active`.
- **The other projects (optional):** `scripts/pc/projects_digest.py --push` on the computer (manually or from a scheduled task; `SSA_SERVER` in .env) → `data/projects_digest.json` on the server. A project marked in `SSA_SENSITIVE_RX`: only documentation and structure. Aliases: `config/project_aliases.json`.
- **Cost:** one Gemini flash call per question, a cache by commit (`data/project_qa_cache.json`). Test: `python scripts/project_qa_eval.py` (≥90%), a single question: `python -m agent.project_qa "איזה קרונים רצים?"` ("which crons run?").
