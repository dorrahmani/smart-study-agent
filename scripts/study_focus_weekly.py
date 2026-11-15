"""scripts/study_focus_weekly.py — Friday "what to study this weekend".

One Telegram message: for each active course taught this week, 3 priorities
crossing what was taught × open homework × past-exam topic frequency × weak flashcards
(generators/study_focus.py).

Guards: semester gate, budget lock, one send per ISO week (sent log).
Cron (deploy/crontab.txt): Friday 07:00 UTC = 10:00 Israel.
    venv/bin/python3 scripts/study_focus_weekly.py [--dry-run] [--force]
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

SENT_LOG = ROOT / 'data' / 'study_focus_sent.json'


def _week_key(d: date | None = None) -> str:
    y, w, _ = (d or date.today()).isocalendar()
    return f'{y}-W{w:02d}'


def main(dry_run: bool = False, force: bool = False) -> int:
    from agent import study_push
    if not (dry_run or force or study_push.on()):      # 05/11 the owner: learning content on request only ("מה ללמוד" answers)
        print('study push is off (SSA_STUDY_PUSH) — not sending')
        return 0
    from config.paths import ACTIVE_COURSES
    from utils.semester import load_config, semester_active
    from utils.budget import budget_lock_reason
    from generators.study_focus import build_study_focus
    from utils.notify import deliver as send            # WhatsApp only (10/11: Telegram deleted)

    ok, why = semester_active(load_config(), respect_no_class_dates=False)
    if not ok and not force:
        print(f'skip: {why}')
        return 0
    lock = budget_lock_reason()
    if lock:
        print(f'skip: budget lock — {lock}')
        return 0
    try:
        sent = json.loads(SENT_LOG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        sent = {}
    week = _week_key()
    if week in sent and not force:
        print(f'already sent for {week}')
        return 0

    blocks, failures = [], 0
    for ck in ACTIVE_COURSES:
        try:
            b = build_study_focus(ck)
        except Exception as e:
            print(f'  ⚠️ {ck}: {e}')
            failures += 1
            continue
        print(f'{ck}: {"ok" if b else "nothing taught this week"}')
        if b:
            blocks.append(b)
    if not blocks:
        return 1 if failures else 0
    text = '*📚 מה ללמוד בסופ"ש*\n\n' + '\n\n'.join(blocks)
    if dry_run:
        print(text)
        return 0
    if not send(text, markdown=True):
        return 1
    sent[week] = date.today().isoformat()
    SENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    SENT_LOG.write_text(json.dumps(sent, ensure_ascii=False, indent=1), encoding='utf-8')
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    sys.exit(main(dry_run='--dry-run' in sys.argv, force='--force' in sys.argv))
