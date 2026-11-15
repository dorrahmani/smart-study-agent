#!/usr/bin/env python3
"""
scripts/tau_mail_watch.py — university mailbox → triage queue (cron every 15 min, Asia/Jerusalem).

Fetch + one Gemini triage call per batch of NEW mail; the WhatsApp worker delivers from data/mail_queue.jsonl.
Silent when there's nothing new (no LLM call at all). Three failures in a row → one WhatsApp heads-up.

crontab:  */15 * * * *  …/venv/bin/python scripts/tau_mail_watch.py
"""
from __future__ import annotations

import fcntl
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / '.env', override=True)


def main() -> int:
    from utils.budget import install_guard
    install_guard()                                # the watchers read others' words on a clock: the lock holds here too
    from integrations import tau_mail
    if not tau_mail.configured():
        return 0                                   # not connected yet — nothing to do, no noise
    lock_path = ROOT / 'data' / 'tau_mail.lock'
    with open(lock_path, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        fails_path = ROOT / 'data' / 'tau_mail_fails.json'
        try:
            fails = json.loads(fails_path.read_text(encoding='utf-8')).get('n', 0)
        except (OSError, ValueError):
            fails = 0
        try:
            out = tau_mail.check()
            print(f'{datetime.now():%F %T} mail ok: {len(out)} new', flush=True)
            fails = 0
        except Exception as e:
            fails += 1
            print(f'{datetime.now():%F %T} mail FAILED ({fails}): {e}', flush=True)
            if fails == 3:
                from whatsapp import outbox
                chat = outbox.owner_chat()
                if chat:
                    outbox.send_text(chat, '⚠️ כבר 3 פעמים ברצף לא הצלחתי לקרוא את המייל האוניברסיטאי '
                                           '(אולי ההרשאה בוטלה). עד שזה יתוקן לא אעדכן על מיילים.')
        fails_path.write_text(json.dumps({'n': fails}), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
