# 🔐 Security notes

## Never commit

| What | Where it lives | Why |
|---|---|---|
| `.env` | repo root | every API key; with the student domain also your university password and ID number |
| `config/*.pickle`, `config/*token*.json`, `config/secrets/` | config/ | Google OAuth tokens |
| `~/.notebooklm/**/storage_state.json` | home dir | a logged-in Google session |
| `config/courses.yaml`, `config/professors.yaml` | config/ | student domain: your timetable, groups, tutors, course ids |
| `config/recipients*.json`, `config/*.env` | config/ | other people's addresses / bridge config |
| `data/`, `logs/`, `backups/` | repo root | summaries, transcripts, conversations, contacts, usage |
| `venv_wa/`, `data/wa_bridge/` | repo root | the WhatsApp linked-device session |

All of these are in `.gitignore`. `python scripts/setup_check.py` also warns if one of them is tracked.

## Before every push

```bash
python scripts/release_scan.py                     # what `git add -A` would commit
python scripts/release_scan.py --all               # every file, gitignored too (before zipping/sharing a folder)
python scripts/release_scan.py --values FILE       # plus YOUR literal values (phone, e-mail, ID, server IP),
                                                   # one per line, in a file kept OUTSIDE the repo
```

Automated, so it can't be forgotten:

- `python scripts/install_git_hooks.py` installs `deploy/git-hooks/pre-push`: every `git push` runs the scan
  first (plus your private values file, if `~/.config/ssa/private_values.txt` exists) and is refused on any finding.
- `.github/workflows/release-scan.yml` runs the same scan on GitHub for every push and pull request.

Both are pattern-based and contain no personal values. It looks for the *shapes* of API keys, OAuth tokens, Telegram tokens, private keys, cookies, Israeli
mobile numbers, WhatsApp ids, e-mails, IPs, ID numbers, tailnet hostnames, home-directory paths, GPG/SSH
fingerprints and Drive/NotebookLM ids, and for forbidden files. Exit code 1 = do not push.

If a secret ever reached git history, **rotate it** (new key / new password / revoke the OAuth grant).
Deleting it in a later commit does not remove it from history.

## Runtime safety built in

- Dashboard binds to 127.0.0.1 and refuses access without `DASHBOARD_PASSWORD`.
- WhatsApp bridge answers only `WA_OWNER` / `WA_ALLOWED`; everyone else is ignored. It runs in its own venv
  without access to `.env`.
- Telegram bot: if `TELEGRAM_USER_ID` is empty it would answer anyone, so always set it.
- The bot never sends a message to a third party, adds to your calendar, books, or submits a form without your ✅.
  Words in the chat never confirm a ✅; only the button does.
- Prompt injection: text from mail, web pages, documents and other people is data. The model's tools only read;
  an action stands only if a separate check that sees nothing but your own last messages says you asked for it
  (`agent/mind.py`). After a turn read mail or past chats it cannot send queries out, and a turn that read
  untrusted text writes nothing to memory.
- Web sign-ups (`agent/web_agent.py`): never types into password / card / CVV / ID / SMS-code fields, never clicks
  pay, hands CAPTCHAs back to you, stays on the site it was sent to, and the final click waits for your ✅.
- Partner and contacts: the partner can only propose calendar events; contacts in a coordination see only that
  thread. Neither reaches the model's tools.
- Proactive messages are capped per day, spaced out, silent in quiet hours, and muted per topic on request.
- Hard monthly budget lock (`utils/budget.py`) plus a daily cap on conversations.
- Assistant only (`SSA_STUDY=0`, the default): no university credentials, Moodle/Panopto session or NotebookLM
  login on the machine at all, and the model is not offered the course / university-mail tools.
- Self-improvement / "קלוד: …" jobs run as an isolated user with an egress firewall (`deploy/claude_agent/`),
  on a branch, through the full test gate, and deploy only after your ✅.
- Server: SSH keys only, fail2ban, nightly `scripts/security_check.py`, test-gated deploy (`deploy/hooks/`).
- University terms: your credentials are yours. Don't run this for someone else with their password.
