"""
generators/study_focus.py — "what to study this weekend", per course.

Three signals, cheapest first:
  1. Past exams — data/exams_text/<course>/*.md are OCR'd with
     "## שאלה N (נושא: X)" headers, so topic frequency is counted in code
     (no model): in how many distinct exams did each topic appear.
  2. Weak spots — utils.mastery (flashcards, quiz, practice, homework);
     wrong flashcards alone as a fallback while there is no data.
  3. What was actually taught — section headers of the course's summaries,
     this week's marked.
One cheap model call then crosses them into 3 concrete priorities. When a
course has no past exams yet the message says so instead of pretending.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = 'gemini-3.8-flash'

_TOPIC = re.compile(r'^#{1,4}\s*שאלה[^\n(]*\((?:נושא|נושאים)\s*:\s*([^)]+)\)', re.M)
_SOL = re.compile(r'(?i)[\s_()-]*(sol(ution)?|פתרון)\w*[)]?')


def _exam_id(stem: str) -> str:
    """Same exam ⇔ same year + moed; file names are inconsistent
    ('פתרון מבחן מועד א 2024-2-2' and 'מבחן מיקרו א- מועד א 2024-2')."""
    y = re.search(r'20\d\d', stem)
    m = re.search(r'מועד\s*([אבג])', stem) or re.search(r'(?i)moed[\s_-]*([abc])', stem)
    if y and m:
        return f'{y.group(0)}-{m.group(1)}'
    return _SOL.sub('', stem).strip()

PROMPT = """אתה יועץ לימודים לסטודנט בקורס "{course}". בנה לו "מה ללמוד בסופ"ש" — קצר וממוקד.

## מה נלמד בקורס עד עכשיו (כותרות מהסיכומים; ⭐ = השבוע)
{covered}

## נושאים במבחני עבר (בכמה מבחנים שונים הופיע כל נושא, מתוך {n_exams})
{exam_topics}

## איפה הסטודנט חלש (מפת שליטה: כרטיסיות, חידון, סטי תרגול, שיעורי בית)
{weak}

## שיעורי בית פתוחים (לא הוגשו)
{homework}

כתוב בעברית, Markdown של טלגרם (*מודגש*), עד 900 תווים:
*🎯 {course}*
1. *<נושא>* — <למה עכשיו: מופיע ב-X מבחנים / חלש אצלך (Y%) / נלמד השבוע ויבנה עליו הלאה>. <פעולה אחת קונקרטית>
2. ...
3. ...
כללים:
- רק נושאים *שכבר נלמדו* (מהרשימה הראשונה) או שהמטלה דורשת. נושא ממבחני עבר שעוד לא נלמד — אל תכלול.
- עדיפות: מה שצריך כדי לפתור מטלה שמוגשת בקרוב > נלמד + שכיח במבחנים + חלש במפת השליטה > נלמד השבוע > השאר.
- אם יש מטלה פתוחה: פריט 1 הוא המטלה — "*📝 <שם המטלה> (הגשה <תאריך>)*" + אילו נושאים מהחומר לחזור עליהם לפניה,
  והפעולה: "תרגול לקראת המטלה ב<קורס>" (סט תרגילים דומים — לא פתרון המטלה). אל תפתור ואל תרמוז על פתרון שאלות המטלה.
- פעולה = משהו שעושים בשעה: לפתור סט תרגול (כתוב את הבקשה לבוט במירכאות, למשל "סט תרגול על <נושא> ב<קורס>"),
  לחזור על סיכום מסוים, או לפתור שאלה ממבחן של שנה מסוימת אם רלוונטי.
- בלי הקדמות ובלי סיכום בסוף.
"""


def exam_topic_counts(course_key: str, exams_dir: Path | None = None) -> tuple[Counter, int]:
    """(topic → number of distinct exams it appeared in, number of distinct exams).
    An exam and its solution file count once."""
    d = (exams_dir or ROOT / 'data' / 'exams_text') / course_key
    by_exam: dict[str, set] = {}
    if d.is_dir():
        for p in d.glob('*.md'):
            topics = {' '.join(part.split()).strip(' .') for t in
                      _TOPIC.findall(p.read_text(encoding='utf-8', errors='ignore'))
                      for part in re.split(r'[,;،]', t)}
            by_exam.setdefault(_exam_id(p.stem), set()).update(t for t in topics if len(t) > 2)
    counts: Counter = Counter()
    for topics in by_exam.values():
        counts.update(topics)
    return counts, len(by_exam)


def weak_cards(course_key: str, limit: int = 8) -> list[str]:
    from generators import sm2
    cards = [c for c in sm2._load() if c.get('course_key') == course_key and c.get('last_review')]
    weak = [c for c in cards if (c.get('last_grade') is not None and c['last_grade'] < 3)
            or float(c.get('ease', 2.5)) < 2.2]
    weak.sort(key=lambda c: (c.get('last_grade', 5), float(c.get('ease', 2.5))))
    return [str(c.get('q', ''))[:140] for c in weak[:limit]]


def lesson_topics(md: str) -> list[str]:
    """Topic lines of a summary: the bullets under '## מפת השיעור' (timestamps
    and links stripped); falls back to the ### section titles."""
    m = re.search(r'^## מפת השיעור\s*\n(.*?)(?=^## |\Z)', md, re.M | re.S)
    raw = re.findall(r'^\s*[-*]\s+(.+)$', m.group(1), re.M) if m else []
    if not raw:
        raw = re.findall(r'^###\s+(.+)$', md, re.M)
    out = []
    for t in raw:
        t = re.sub(r'\]\([^)]*\)', ']', t)                      # drop link targets
        t = re.sub(r'\[\d{1,2}:\d{2}(:\d{2})?\]', '', t)
        t = t.replace('[]', '').strip(' —-:')
        if t and not re.match(r'(פתיחה|סיום|הפסקה|קישור|מושגים|טיפים|חידון)', t):
            out.append(t[:90])
    return out


def covered_topics(course_key: str, days_new: int = 7, summaries_dir: Path | None = None) -> tuple[str, int]:
    """(compact listing of the course's summaries + their topics, #summaries this week)."""
    root = summaries_dir or ROOT / 'data' / 'summaries'
    cutoff = (datetime.now() - timedelta(days=days_new)).strftime('%Y-%m-%d')
    files = []
    for sub in ('lectures', 'tutorials'):
        d = root / course_key / sub
        if d.is_dir():
            files += [p for p in d.glob('*.md') if p.stat().st_size > 2000]
    files.sort(key=lambda p: p.name)
    lines, new = [], 0
    for p in files:
        star = p.name[:10] >= cutoff
        new += star
        heads = lesson_topics(p.read_text(encoding='utf-8', errors='ignore'))[:14]
        lines.append(f"{'⭐ ' if star else ''}{p.stem}: " + ' · '.join(heads))
    return '\n'.join(lines)[-40_000:], new


def build_study_focus(course_key: str) -> str | None:
    """Telegram-markdown block for one course, or None when nothing was taught this
    week and no homework is open."""
    from config.paths import COURSE_NAMES
    from utils.open_homework import open_assignments, describe, homework_skills
    course = COURSE_NAMES.get(course_key, course_key)
    covered, new = covered_topics(course_key)
    hws = open_assignments(course_key)
    if not new and not hws:
        return None
    skills = homework_skills(hws[0]) if hws else ''      # the nearest one: its skills, never its questions
    counts, n_exams = exam_topic_counts(course_key)
    try:                                                  # all signals; flashcards alone as a fallback
        from utils.mastery import weak_topics
        weak = weak_topics(course_key) or weak_cards(course_key)
    except Exception:
        weak = weak_cards(course_key)
    prompt = PROMPT.format(
        course=course, covered=covered, n_exams=n_exams,
        exam_topics='\n'.join(f'- {t}: {k}' for t, k in counts.most_common(40)) or '(אין מבחני עבר במערכת)',
        weak='\n'.join(f'- {q}' for q in weak) or '(אין עדיין נתונים)',
        homework=('\n'.join(f'- {describe(a)}' for a in hws)
                  + (f'\nמיומנויות שהמטלה הקרובה דורשת:\n{skills}' if skills else ''))
                 or '(אין מטלה פתוחה ב-10 הימים הקרובים)')

    import google.genai as genai
    from google.genai import types as T
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    resp = client.models.generate_content(
        model=MODEL, contents=prompt,
        config=T.GenerateContentConfig(max_output_tokens=3000, temperature=0.3,
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op='study_focus')
    text = (resp.text or '').strip()
    if not text:
        return None
    if not n_exams:
        text += '\n_ℹ️ אין עדיין מבחני עבר במערכת לקורס הזה — ההמלצה מבוססת על החומר והכרטיסיות בלבד._'
    return text
