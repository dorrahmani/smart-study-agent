"""scripts/exam_mode_detector.py — Detect upcoming exams and toggle exam mode.

Runs daily 06:00 UTC. Examines:
  - data/assignments/_cache.json — finds deadlines tagged as "exam"/"מבחן"/"מועד א"
  - data/exam_dates.json (optional manual override list)

Writes data/exam_mode.json: {courses: {<course_key>: {exam_date, days_left, started}}}

When a course enters exam mode (≤7 days before exam):
  - Sends one-time WhatsApp notification announcing entry
  - flashcards_daily.py and morning_briefing.py read this file and adjust behavior
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

from config.paths import COURSE_NAMES

EXAM_MODE_PATH = ROOT / 'data' / 'exam_mode.json'
EXAM_DATES_PATH = ROOT / 'data' / 'exam_dates.json'  # optional manual list
ASSIGNMENTS_CACHE = ROOT / 'data' / 'assignments' / '_cache.json'

EXAM_KEYWORDS = ('מבחן', 'מועד א', 'מועד ב', 'בוחן',
                 'exam', 'midterm', 'final', 'test')
EXAM_WINDOW_DAYS = 7



def _load_existing() -> dict:
    if not EXAM_MODE_PATH.exists(): return {'courses': {}}
    try:
        return json.loads(EXAM_MODE_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {'courses': {}}


def _save(state: dict) -> None:
    EXAM_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXAM_MODE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding='utf-8')


def _send(text: str) -> None:
    from utils.notify import deliver            # WhatsApp only (10/11: Telegram deleted)
    deliver(text, markdown=True)


def _looks_like_exam(title: str) -> bool:
    if not title: return False
    t = title.lower()
    return any(kw in t for kw in EXAM_KEYWORDS)


def _find_upcoming_exams() -> list[dict]:
    """Returns list of {course_key, exam_date (YYYY-MM-DD), title, source}."""
    out: list[dict] = []
    today = date.today()
    cutoff = today + timedelta(days=EXAM_WINDOW_DAYS)

    # Source 1: assignments cache
    if ASSIGNMENTS_CACHE.exists():
        try:
            cache = json.loads(ASSIGNMENTS_CACHE.read_text(encoding='utf-8'))
            for a in cache.get('assignments', []) or []:
                if not isinstance(a, dict) or a.get('submitted'): continue
                due = a.get('due_date')
                title = a.get('title', '')
                ck = a.get('course_key', '')
                if not due or not ck: continue
                if not _looks_like_exam(title): continue
                try:
                    dt = datetime.fromisoformat(due).date()
                    if today <= dt <= cutoff:
                        out.append({
                            'course_key': ck,
                            'exam_date':  dt.isoformat(),
                            'title':      title[:80],
                            'source':     'assignments',
                        })
                except Exception: continue
        except Exception: pass

    # Source 2: manual exam dates list
    if EXAM_DATES_PATH.exists():
        try:
            data = json.loads(EXAM_DATES_PATH.read_text(encoding='utf-8'))
            for ex in data.get('exams', []) or []:
                if not isinstance(ex, dict): continue
                ck = ex.get('course_key', '')
                d_str = ex.get('date', '')
                if not (ck and d_str): continue
                try:
                    dt = datetime.strptime(d_str, '%Y-%m-%d').date()
                    if today <= dt <= cutoff:
                        out.append({
                            'course_key': ck,
                            'exam_date':  dt.isoformat(),
                            'title':      ex.get('title', 'מבחן')[:80],
                            'source':     'manual',
                        })
                except Exception: continue
        except Exception: pass

    return out


def run() -> dict:
    state = _load_existing()
    courses = state.get('courses', {}) or {}

    upcoming = _find_upcoming_exams()
    new_courses: list[str] = []
    today = date.today()

    # Update / add courses entering exam mode
    found_keys: set[str] = set()
    for ex in upcoming:
        ck = ex['course_key']
        found_keys.add(ck)
        days_left = (date.fromisoformat(ex['exam_date']) - today).days
        if ck not in courses:
            # New entry into exam mode
            courses[ck] = {
                'exam_date':   ex['exam_date'],
                'title':       ex['title'],
                'days_left':   days_left,
                'started':     today.isoformat(),
                'notified':    True,
                'source':      ex['source'],
            }
            new_courses.append(ck)
        else:
            # Update days_left
            courses[ck]['days_left'] = days_left
            courses[ck]['exam_date'] = ex['exam_date']  # in case it changed

    # Remove courses whose exam has passed (no longer in `found_keys` and date past)
    to_remove = []
    for ck, info in courses.items():
        if ck in found_keys: continue
        try:
            ed = date.fromisoformat(info['exam_date'])
            if ed < today:
                to_remove.append(ck)
        except Exception:
            to_remove.append(ck)
    for ck in to_remove:
        courses.pop(ck, None)

    state['courses'] = courses
    state['last_run'] = datetime.utcnow().isoformat() + 'Z'
    _save(state)

    # Notify on new entries
    if new_courses:
        lines = ['🚨 *מצב בחינה הופעל!*', '']
        for ck in new_courses:
            info = courses[ck]
            ck_he = COURSE_NAMES.get(ck, ck)
            lines.append(
                f'• *{ck_he}*: מבחן בעוד {info["days_left"]} ימים '
                f'({info["exam_date"]})\n'
                f'  📝 {info["title"]}'
            )
        from agent import study_push
        if study_push.on():                   # 05/11: off by default, and then these lines aren't true
            lines.append('')
            lines.append('מעכשיו:')
            lines.append('  • 6 כרטיסיות יומיות (במקום 3)')
            lines.append('  • Briefing יום-יום עם דגש על הקורסים האלה')
            lines.append('  • שאלת מבחן יומית מתוך הסיכומים')
            lines.append('  • שלח "תכנן לי לימוד למבחן ב<קורס> בעוד X ימים" לתוכנית מלאה')
        _send('\n'.join(lines))

    return state


if __name__ == '__main__':
    s = run()
    print(json.dumps(s, ensure_ascii=False, indent=2))
