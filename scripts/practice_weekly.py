"""scripts/practice_weekly.py — Thursday practice set per course.

For every active course that got new summaries in the last 7 days, or has an
open assignment due in the next 10 days: build a practice set (problems + full
worked solutions) and send the PDF to Telegram — ready for the weekend.
With an open assignment the set is aimed at its skills (different problems;
the graded assignment itself is never solved).

Guards: semester gate, budget lock, and a per-(ISO week, course) sent log so a
re-run (manual or cron retry) never sends the same week twice.

Cron (deploy/crontab.txt): Thursday 16:00 UTC = 19:00 Israel.
    venv/bin/python3 scripts/practice_weekly.py [--dry-run] [--force]
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

SENT_LOG = ROOT / 'data' / 'practice' / 'weekly_sent.json'


def _week_key(d: date | None = None) -> str:
    y, w, _ = (d or date.today()).isocalendar()
    return f'{y}-W{w:02d}'


def _load_sent() -> dict:
    try:
        return json.loads(SENT_LOG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _mark_sent(key: str, pdf: str) -> None:
    sent = _load_sent()
    sent[key] = pdf
    SENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = SENT_LOG.with_suffix('.tmp')
    tmp.write_text(json.dumps(sent, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(SENT_LOG)


def main(dry_run: bool = False, force: bool = False) -> int:
    from agent import study_push
    if not (dry_run or force or study_push.on()):      # 05/11 the owner: on request only ("תן לי תרגילים ב…")
        print('study push is off (SSA_STUDY_PUSH) — not building')
        return 0
    from config.paths import ACTIVE_COURSES, COURSE_NAMES
    from utils.semester import load_config, semester_active
    from utils.budget import budget_lock_reason
    from generators.practice_set import _recent_summaries, practice_pdf
    from utils.open_homework import next_assignment, describe

    ok, why = semester_active(load_config(), respect_no_class_dates=False)
    if not ok and not force:
        print(f'skip: {why}')
        return 0
    lock = budget_lock_reason()
    if lock:
        print(f'skip: budget lock — {lock}')
        return 0

    week = _week_key()
    sent = _load_sent()
    failures = 0
    explained = False       # 29/10: four sets, each repeating the same 3 lines of instructions — only the first says it
    for ck in ACTIVE_COURSES:
        key = f'{week}/{ck}'
        if key in sent:
            print(f'{ck}: already sent this week ({sent[key]})')
            continue
        srcs = _recent_summaries(ck, days=7, topic=None)
        hw = next_assignment(ck)
        hw = hw if hw and hw.get('pdf') else None
        if not srcs and not hw:
            print(f'{ck}: no new summaries and no open assignment this week')
            continue
        print(f'{ck}: {len(srcs)} summaries' + (f' + open assignment: {describe(hw)}' if hw else '') + ' → practice set')
        if dry_run:
            continue
        try:
            # this week's material; with no lecture this week (holiday) the latest summaries
            pdf = practice_pdf(ck, days=7 if srcs else None, n=6, homework=hw)
        except Exception as e:
            print(f'  ⚠️ {ck}: {e}')
            failures += 1
            continue
        caption = f'🏋️ סט תרגול שבועי — {COURSE_NAMES.get(ck, ck)}\n'
        from generators.practice_set import aimed_at_homework
        if hw and aimed_at_homework(pdf.with_suffix('.md').read_text(encoding='utf-8')):
            caption += f'🎯 מכוון למטלה: {describe(hw)}\nאותן מיומנויות, תרגילים אחרים — המטלה עצמה נשארת לך.\n'
        from generators.practice_grader import FOLLOW_UP, register_sent
        caption += ('קודם לפתור, הפתרונות בחלק ב׳.\n' + FOLLOW_UP) if not explained else 'אותו דבר — Reply עם צילום לבדיקה.'
        explained = True
        from utils.notify import deliver_document
        sent_to = deliver_document(pdf, caption)        # WhatsApp (grades a reply with a photo); no Telegram since 10/11
        if sent_to:
            _mark_sent(key, pdf.name)
            _, chat, mid = sent_to
            register_sent(pdf.with_suffix('.md'), ck, chat, mid)
        else:
            failures += 1
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    sys.exit(main(dry_run='--dry-run' in sys.argv, force='--force' in sys.argv))
