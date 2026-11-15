"""scripts/flashcards_daily.py — Daily flashcard maintenance.

Run twice daily:
  1. Generate new flashcards from summaries created in the last 7 days
     (only if the summary doesn't already have cards in the deck)
  2. Send today's due cards on WhatsApp (3 cards max)

Cron suggestion:
  0 16 * * * /path/run_with_alert.sh "Flashcards Daily" bash -c \
    "cd /home/ubuntu/smart_study_agent && venv/bin/python3 scripts/flashcards_daily.py"
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

from agent import study_push
if not (study_push.on() or '--force' in sys.argv):     # 05/11 the owner: review questions only when he asks
    print('study push is off (SSA_STUDY_PUSH) — not sending')
    sys.exit(0)

from config.paths import SUMMARIES_DIR, COURSE_NAMES
from generators.flashcard_generator import cards_from_summary
from generators import sm2



def _send(text: str, reply_markup: dict | None = None) -> str | None:
    """WhatsApp only (10/11: Telegram deleted). Inline-keyboard rows → utils.notify's [[(label, payload)]] buttons."""
    from utils.notify import deliver
    buttons = [[(b['text'], b['callback_data']) for b in row]
               for row in (reply_markup or {}).get('inline_keyboard', [])] or None
    return deliver(text, buttons, markdown=True)


# ─── 1. Generate cards from new summaries ──────────────────────────────
print('=== Generate phase ===')
deck_before = sm2._load()
seen_stems = {c.get('source_stem') for c in deck_before}

cutoff = datetime.now().date() - timedelta(days=7)
MIN_BYTES = 8 * 1024  # 8KB — skip thin tutorials with little content
# Per-course cap on new cards per run to avoid runaway costs
PER_COURSE_NEW_CAP = 24
new_count = 0
for ck in COURSE_NAMES:
    course_new = 0
    # Lectures first (highest signal-to-cost); tutorials only after capped
    subs = ('lectures',) if course_new >= PER_COURSE_NEW_CAP else ('lectures', 'tutorials')
    for sub in subs:
        d = SUMMARIES_DIR / ck / sub
        if not d.exists(): continue
        for md in d.glob('*.md'):
            if course_new >= PER_COURSE_NEW_CAP: break
            if md.stat().st_size < MIN_BYTES: continue  # skip thin
            if md.stem in seen_stems: continue
            mtime = datetime.fromtimestamp(md.stat().st_mtime).date()
            if mtime < cutoff: continue
            cards = cards_from_summary(md, course_key=ck)
            if cards:
                added = sm2.add_cards(cards)
                course_new += added
                new_count += added
                print(f'  + {ck}/{sub}/{md.stem}: {added} cards')

print(f'Total new cards: {new_count}')

# ─── 2. Send today's due cards ──────────────────────────────────────────
print('\n=== Review phase ===')

# Exam mode: bump daily card count from 3 → 6 for courses in exam mode
exam_state_path = ROOT / 'data' / 'exam_mode.json'
exam_courses: set[str] = set()
if exam_state_path.exists():
    try:
        exam_state = json.loads(exam_state_path.read_text(encoding='utf-8'))
        exam_courses = set((exam_state.get('courses') or {}).keys())
    except Exception:
        exam_courses = set()

daily_limit = 6 if exam_courses else 3
# Only this semester's courses (+ any course in exam mode). The deck still holds
# last semester's 635 cards; sending those daily would bury the new material.
try:
    from config.paths import ACTIVE_COURSES
    _courses = set(ACTIVE_COURSES) | exam_courses
except ImportError:
    _courses = None
due = sm2.due_cards(limit=daily_limit, courses=_courses)
# Prioritize exam-mode courses if any
if exam_courses:
    due = sorted(due, key=lambda c: 0 if c.get('course_key') in exam_courses else 1)
    print(f'  Exam mode active for {len(exam_courses)} courses — limit={daily_limit}')
if not due:
    s = sm2.stats()
    if s['total'] == 0:
        print('No deck yet.')
    else:
        print(f'No cards due today. (deck={s["total"]}, seen={s["seen"]})')
    sys.exit(0)

# Send each card as a separate WhatsApp message with numbered grading options.
# Inline buttons: "1 — לא ידעתי", "3 — בקושי", "4 — ידעתי", "5 — קל!"
for i, card in enumerate(due, 1):
    ck_he = COURSE_NAMES.get(card.get('course_key', ''), card.get('course_key', ''))
    msg = (
        f'🎯 *כרטיסית {i}/{len(due)}* ({ck_he})\n\n'
        f'*שאלה:*\n{card["q"]}\n\n'
        f'_(ענה בהודעה הבאה. או לחץ "תראה תשובה" אם לא יודע.)_'
    )
    cid = card['id']
    kb = {
        'inline_keyboard': [
            [{'text': '👀 תראה תשובה', 'callback_data': f'fc:show:{cid}'}],
            [
                {'text': '1️⃣ לא ידעתי', 'callback_data': f'fc:grade:{cid}:1'},
                {'text': '3️⃣ בקושי', 'callback_data': f'fc:grade:{cid}:3'},
                {'text': '4️⃣ ידעתי', 'callback_data': f'fc:grade:{cid}:4'},
                {'text': '5️⃣ קל!', 'callback_data': f'fc:grade:{cid}:5'},
            ],
        ]
    }
    _send(msg, reply_markup=kb)
    print(f'  → sent card {cid}')

# Summary stats footer
s = sm2.stats()
_send(
    f'📊 *מאגר flashcards:*\n'
    f'  • סה"כ: {s["total"]}\n'
    f'  • לא נראו עדיין: {s["never_seen"]}\n'
    f'  • לחזרה היום: {s["due_today"] - len(due)}'
)
