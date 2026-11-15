#!/usr/bin/env python3
"""
scripts/tau_watch.py — the university watcher (cron, Asia/Jerusalem). No LLM.

  calendar  refresh the exam/lesson schedule cache (records moved exams + published rooms for the agent)
  grades    check the grades page; a new/changed grade → WhatsApp to the owner right away (never to the partner)

The WhatsApp worker only READS the caches this writes — a login takes ~40s and must not block messages.
Three failed logins in a row → one heads-up to the owner (maybe the university password changed).

crontab (TZ=Asia/Jerusalem):
  45 6,9,19 * * *  …/venv/bin/python scripts/tau_watch.py calendar
  30 9,14,20 * * * …/venv/bin/python scripts/tau_watch.py grades
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

from config.paths import DATA_DIR  # noqa: E402

STATE = DATA_DIR / 'tau_watch_state.json'
LOCK = DATA_DIR / 'tau_watch.lock'


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _failed(st: dict, what: str, err: Exception) -> None:
    st['fails'] = st.get('fails', 0) + 1
    print(f'{datetime.now():%F %T} {what} FAILED ({st["fails"]} in a row): {err}', flush=True)
    if st['fails'] == 3:
        from whatsapp import outbox
        chat = outbox.owner_chat()
        if chat:
            outbox.send_text(chat, '⚠️ כבר 3 פעמים ברצף לא הצלחתי להיכנס לאתר האוניברסיטה (my.tau). '
                                   'אם החלפת סיסמה — צריך לעדכן אותה בשרת. עד אז ציונים/חדרי מבחן לא מתעדכנים.')


def main(mode: str) -> int:
    from integrations import tau_portal
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)      # never two browsers at once
        except OSError:
            print('another tau_watch is running — skipping', flush=True)
            return 0
        st = _state()
        try:
            if mode == 'calendar':
                ev = tau_portal.fetch()
                old = tau_portal._cached()
                if old:
                    tau_portal._record_changes(tau_portal.exam_changes(old['events'], ev) +
                                               tau_portal.room_changes(old['events'], ev))
                tau_portal._save(ev)
                print(f'{datetime.now():%F %T} calendar ok: {len(ev)} events', flush=True)
            elif mode == 'grades':
                changes = tau_portal.check_grades()
                print(f'{datetime.now():%F %T} grades ok: {len(changes)} new', flush=True)
                if changes:
                    from whatsapp import outbox
                    chat = outbox.owner_chat()
                    for ch in changes:
                        if chat:
                            outbox.send_text(chat, tau_portal.grade_alert_text(ch))
            else:
                print(__doc__)
                return 2
            st['fails'] = 0
            st['last_ok'] = datetime.now().isoformat(timespec='seconds')
        except Exception as e:
            _failed(st, mode, e)
        STATE.write_text(json.dumps(st), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ''))
