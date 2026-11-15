# 🔐 Security notes

## Never commit

| What | Where it lives | Why |
|---|---|---|
| `.env` | repo root | university password, ID number, every API key |
| `config/*.pickle`, `config/*token*.json`, `config/secrets/` | config/ | Google OAuth tokens |
| `~/.notebooklm/**/storage_state.json` | home dir | a logged-in Google session |
| `config/courses.yaml`, `config/professors.yaml` | config/ | your timetable, groups, course ids |
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

It looks for the *shapes* of API keys, OAuth tokens, Telegram tokens, private keys, cookies, Israeli
mobile numbers, WhatsApp ids, e-mails, IPs, ID numbers, tailnet hostnames, home-directory paths, GPG/SSH
fingerprints and Drive/NotebookLM ids, and for forbidden files. Exit code 1 = do not push.

If a secret ever reached git history, **rotate it** (new key / new password / revoke the OAuth grant).
Deleting it in a later commit does not remove it from history.

## Runtime safety built in

- Dashboard binds to 127.0.0.1 and refuses access without `DASHBOARD_PASSWORD`.
- WhatsApp bridge answers only `WA_OWNER` / `WA_ALLOWED`; everyone else is ignored. It runs in its own venv
  without access to `.env`.
- Telegram bot: if `TELEGRAM_USER_ID` is empty it would answer anyone, so always set it.
- The bot never sends a message to a third party, books, or fills a form without your ✅.
- Hard monthly budget lock (`utils/budget.py`).
- Server: SSH keys only, fail2ban, nightly `scripts/security_check.py`, test-gated deploy (`deploy/hooks/`).
- University terms: your credentials are yours. Don't run this for someone else with their password.
