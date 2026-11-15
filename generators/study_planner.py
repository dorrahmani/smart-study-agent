"""generators/study_planner.py — Generate a day-by-day exam study plan.

Inputs:
  - course_key (or 'all' if multiple subjects)
  - days_until_exam (int)
  - exam_date (optional, for display)

Process:
  1. Gather all summary titles for the course (lectures + tutorials)
  2. Get flashcard stats (which topics is user weak on?)
  3. Get assignment deadlines that may conflict
  4. Call Gemini to produce a structured plan
  5. Return markdown ready for Telegram
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUMMARIES_DIR = ROOT / 'data' / 'summaries'

COURSE_HE = {
    'intro_to_cs':       'מבוא מורחב למדעי המחשב',
    'calculus_2':        'חדו״א 2',
    'discrete_math_2':   'בדידה 2',
    'linear_algebra_2':  'לינארית 2',
    'microeconomics':    'מיקרו א\'',
}


def _course_summaries(course_key: str) -> list[dict]:
    """Return list of {title, date, kind} for all summaries in a course."""
    base = SUMMARIES_DIR / course_key
    out: list[dict] = []
    if not base.exists(): return out
    for sub in ('lectures', 'tutorials'):
        d = base / sub
        if not d.exists(): continue
        for md in sorted(d.glob('*.md')):
            stem = md.stem
            # try to parse YYYY-MM-DD prefix
            try:
                d_str = stem[:10]
                datetime.strptime(d_str, '%Y-%m-%d')
                title = stem[11:] if len(stem) > 11 else stem
            except ValueError:
                d_str = ''
                title = stem
            out.append({
                'title': title, 'date': d_str,
                'kind': 'הרצאה' if sub == 'lectures' else 'תרגול',
            })
    return out


def _flashcard_weak_topics(course_key: str) -> list[dict]:
    """Topics where user has been failing flashcards."""
    deck_path = ROOT / 'data' / 'flashcards_deck.json'
    if not deck_path.exists(): return []
    try:
        deck = json.loads(deck_path.read_text(encoding='utf-8'))
    except Exception:
        return []
    weak: list[dict] = []
    for c in deck:
        if c.get('course_key') != course_key: continue
        last_grade = c.get('last_grade')
        if last_grade is not None and last_grade < 3:
            weak.append({
                'q':       c.get('q', '')[:80],
                'source':  c.get('source_stem', '?'),
                'grade':   last_grade,
            })
    return weak[:10]


def _other_deadlines(course_key: str, days: int) -> list[dict]:
    """Other assignments/exams that may compete for time."""
    cache = ROOT / 'data' / 'assignments' / '_cache.json'
    if not cache.exists(): return []
    try:
        data = json.loads(cache.read_text(encoding='utf-8'))
    except Exception:
        return []
    items = data.get('assignments', []) or []
    out: list[dict] = []
    cutoff = datetime.now() + timedelta(days=days)
    now = datetime.now()
    for a in items:
        if not isinstance(a, dict) or a.get('submitted'): continue
        if a.get('course_key') == course_key: continue  # the course we're studying
        due = a.get('due_date')
        if not due: continue
        try:
            dt = datetime.fromisoformat(due)
            if now < dt < cutoff:
                out.append({
                    'course': a.get('course_key', '?'),
                    'title':  (a.get('title') or '?')[:60],
                    'due':    dt.strftime('%Y-%m-%d'),
                })
        except Exception: continue
    return sorted(out, key=lambda x: x['due'])[:5]


def build_plan(course_key: str, days_until_exam: int,
               exam_date: str = '') -> str:
    """Generate Hebrew Markdown study plan, ready for Telegram."""
    course_he = COURSE_HE.get(course_key, course_key)
    summaries = _course_summaries(course_key)
    weak = _flashcard_weak_topics(course_key)
    conflicts = _other_deadlines(course_key, days_until_exam)

    if not summaries:
        return f'⚠️ אין לי סיכומים של {course_he} עדיין — תן לי יומיים לאסוף חומר.'

    # Build context
    summary_lines = []
    for s in summaries[:30]:
        d = f'[{s["date"]}] ' if s['date'] else ''
        summary_lines.append(f'  - {d}{s["kind"]}: {s["title"]}')
    weak_lines = []
    for w in weak:
        weak_lines.append(f'  - שאלה: "{w["q"]}" (מ-{w["source"]}, ציון {w["grade"]}/5)')
    conflict_lines = []
    for c in conflicts:
        conflict_lines.append(f'  - {c["due"]}: {c["course"]} — {c["title"]}')

    prompt = f"""אתה מאמן לימוד אישי. בנה תוכנית לימוד יומית ל{course_he} למבחן בעוד {days_until_exam} ימים{f' (תאריך: {exam_date})' if exam_date else ''}.

# חומר הקורס הזמין (לפי סדר כרונולוגי):
{chr(10).join(summary_lines)}

# נקודות חולשה (נכשל בכרטיסיות לימוד):
{chr(10).join(weak_lines) if weak_lines else '  (אין נתונים — סטודנט מתחיל ללמוד)'}

# דדליינים אחרים שיכולים להתחרות על זמן:
{chr(10).join(conflict_lines) if conflict_lines else '  (אין)'}

# חוקי תכנון:
1. **חלק לימים** — יום-יום, מהיום (יום 1) עד יום המבחן.
2. **שלבים סטנדרטיים** — לימוד חדש (ימים 1-50%), חזרה ממוקדת (50-80%), מבחני עבר (80-95%), סקירה קלה ויום מנוחה (95-100%).
3. **רענן נושאים חלשים** — ב-2 ימים האמצעיים תוסיף סשן ייעודי לנקודות החולשה.
4. **שעות יומיות** — סמן מומלץ (1-4 שעות לימוד יומיות, בהתאם למשך הקורס).
5. **יום אחרון = רק סקירה קלה + שינה**.
6. **ציין שעות ביום** — לדוגמה "סשן 1 (09:00-11:00): ..."

# פורמט תשובה — Markdown נקי לטלגרם:
📅 *תוכנית למבחן {course_he} — {days_until_exam} ימים*

*סה"כ שעות מומלצות:* XX
*חומר לימוד:* X הרצאות + X תרגולים

━━━━━━━━━━━━━━━━━━━━━━━━

📅 *יום 1 (תאריך)* — Y שעות
- 🌅 בוקר (09:00-11:00): סשן 1 — נושא X
- 🌆 ערב (19:00-21:00): סשן 2 — נושא Y
- 🎯 מטרה: ...

[המשך לכל הימים...]

━━━━━━━━━━━━━━━━━━━━━━━━

💡 *טיפים אישיים:* 1-2 הצעות מותאמות לחולשות שלי
⚠️ *אזהרות:* אם יש דדליינים אחרים שמתנגשים

חשוב — בעברית, ישיר וקצר. אל תפלוטו בלה-בלה. הצג רק את הטבלה היומית."""

    try:
        from google import genai
        from google.genai import types as genai_types
        key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=4000, temperature=0.4,
            ),
        )
        try:
            from utils.usage_tracker import log_gemini
            log_gemini(resp, 'gemini-2.5-pro', op='study_planner.build')
        except Exception: pass
        out = (resp.text or '').strip()
        if not out:
            return '⚠️ Gemini לא החזיר תוכנית. נסה שוב בעוד דקה.'
        return out
    except Exception as e:
        return f'⚠️ שגיאה בבניית תוכנית: {e}'


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print('usage: python -m generators.study_planner <course_key> <days> [exam_date]')
        sys.exit(1)
    ck = sys.argv[1]
    days = int(sys.argv[2])
    ed = sys.argv[3] if len(sys.argv) > 3 else ''
    print(build_plan(ck, days, ed))
