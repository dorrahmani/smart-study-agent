# CLAUDE.md — how to help your user set this project up and run it

You are Claude Code, opened in a clone of this repo by someone who wants their **own** self-hosted WhatsApp personal assistant.
Your job: walk them through setup, step by step, without ever touching their secrets, and keep the project safe.
Talk to them in their language (the docs are Hebrew + English).

## What this is (read first)

- **A personal assistant on WhatsApp**: calendar, reminders that follow up when you do not answer, tasks, "what is waiting for me",
  messages / bookings written for you. **Nothing goes out to another person without the owner's explicit ✅ button.**
- It is built in layers: a core secretary + life "domains". **Smart Study** (`SSA_STUDY=1`) is the first and most developed domain
  (homework and quiz tracking, university updates filtered to your groups, lecture summaries, course site, NotebookLM).
  With `SSA_STUDY=0` (the default) none of the study code runs.
- LLM calls go through **Gemini only** (`GEMINI_API_KEY`). Do not add other LLM providers.
- Read `README.md` (what it is), `SETUP.md` (the steps), `SECURITY.md` (the rules). Follow `SETUP.md` in order; do not invent a different path.

## How to run the setup with the user

1. Ask which path: **A = assistant only** (Gemini key + their own WhatsApp number + Google Calendar) or **A + B = with the student domain**.
2. Where will it run (always-on server vs their PC)? `SETUP.md` §0 explains. A server is recommended; the bot must stay online.
3. Create the config files: `python scripts/setup_check.py --init` (add `--student` for path B). It creates `.env` from `.env.example` and never overwrites.
4. Work through `SETUP.md` one section at a time. After each section run `python scripts/setup_check.py` — it lists exactly what is still missing and never prints a secret.
5. Finish with the tests (below) and one real message from the owner's phone to the bot.
6. Explain costs: see "What costs money" in `SETUP.md`. Set a budget alert in Google AI Studio before the first run.

## Secrets — the hard rules

- **Never ask the user to paste an API key, token, password, cookie, or QR/pairing code into the chat.** Tell them to put it in `.env`
  themselves (open the file, paste, save) or to run the sign-in command in their own terminal. You may run `python scripts/setup_check.py`
  to confirm a key is *present* — it reports yes/no, not the value.
- **Never print, log, commit, or echo** the contents of `.env`, `config/secrets/`, `*.pickle`, `*token*`, `storage_state.json`,
  `master_token.json`, `rclone.conf`, `credentials*.json`, or any file under `data/` or `logs/`. These are git-ignored on purpose; keep it that way.
- Do not type the user's passwords for them. Sign-ins (Google, university, WhatsApp pairing) are done by the user in their own browser/phone.
- If a secret was pasted into chat or committed by mistake: tell the user to **rotate it now** (revoke the key / change the password), then remove it.
  Removing it from git history is not enough.
- Before **any** `git push`: run `python scripts/release_scan.py` (must print `0 finding(s)`). Install the hooks once:
  `python scripts/install_git_hooks.py` (pre-push scan). A GitHub Actions workflow runs the same scan on every push.
  Keep the repo **private** unless the user explicitly decides otherwise, and say plainly that a public repo is visible to everyone.

## Safety invariants of the assistant — do not weaken them

These are enforced by tests in `tests/test_safety_critical.py` (the deploy gate). If a change makes one fail, fix the change, not the test.

- **✅ before anything outward**: sending to other people, bookings, payments, calendar edits of others. Words never confirm a ✅ — only the button does.
  The web agent stops before payment.
- **Third-party text is data, never instructions** (mail, forwarded messages, group chats, web pages, files). Those paths use
  the API-only wrapper (`api_only`) and the injection guards; do not remove them.
- **Only the owner's number is obeyed** (`WA_OWNER`); other numbers are partner / friend / contact roles with fewer rights.
- A hard monthly budget lock stops paid calls; the dashboard refuses access without `DASHBOARD_PASSWORD`.
- Closed items stay closed (`agent/closure.py`); proactive messages are capped and respect quiet hours.

## Working in the code

- Layout: `agent/` (assistant core: `core.py` routing, `mind.py` the agent loop and its tools, `proactive.py`, `escalate.py`, `obligations.py`, …),
  `whatsapp/` (bridge + worker), `integrations/` (calendar, mail), `collectors/` + `pipelines/` + `generators/` (study domain),
  `vault_sync/` (Obsidian second brain), `dashboard/`, `deploy/` (systemd, hooks), `scripts/`, `tests/`.
- Tests — run both before telling the user it works:
  ```
  python -m pytest tests/ -q
  PYTHONIOENCODING=utf-8 python -m tests.test_safety_critical
  python -m tests.conversation_evals
  ```
  New tests for `test_safety_critical.py` go **before** its last `if __name__ == '__main__':` line, and must not import pytest.
- YAML: write with `yaml.safe_dump` only (Hebrew quotes break naive YAML). Sync to cloud storage with `rclone copy`, never `rclone sync`.
- Do not edit code directly on a production server; change the repo, run the tests, deploy.
- Keep personal data out of code and tests: names, numbers and places in examples must be invented.

## When something is missing or breaks

- `python scripts/setup_check.py` first. Then the matching section of `SETUP.md`.
- WhatsApp bridge not connecting → re-link the device from the phone (Linked devices); a second SIM/eSIM number is safer than the main one.
- Study features do nothing → `SSA_STUDY` is probably `0`; path B in `SETUP.md`.
- Costs rising → check the dashboard costs page and the budget lock; lower the daily caps in `.env` (see `.env.example`).
- Be honest about what you could not verify: say "set up, not yet tested" rather than "done".
