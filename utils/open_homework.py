"""
utils/open_homework.py — the homework that is open right now, per course.

Source: data/assignments/_cache.json (written nightly by
collectors/moodle_assignment_scraper.py) + the PDFs that
scripts/download_assignments.py saves as data/assignments/<course>/<title>_assignment.pdf.

Used to steer the weekly practice set and "what to study" toward the
assignment that is due next. The assignment itself is graded work, so the
practice generator never reads it: homework_profile() turns the PDF into generic
skills + a list of its specific objects to avoid, and overlapping_exercises()
checks the finished set against the PDF so drop_exercises() can remove anything
whose solution would answer an assignment question.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSIGN_DIR = ROOT / 'data' / 'assignments'
HORIZON_DAYS = 10
MAX_PDF_BYTES = 15 * 1024 * 1024


def _safe_name(t: str) -> str:                      # same rule as scripts/download_assignments.py
    return re.sub(r'[<>:"/\\|?*]', '', t).strip()[:80]


def _due(a: dict) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(a.get('due_date')))
        return d.replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def open_assignments(course_key: str | None = None, horizon_days: int = HORIZON_DAYS,
                     now: datetime | None = None, assign_dir: Path | None = None) -> list[dict]:
    """Not submitted, due between now and now+horizon, soonest first.
    Each item gets `due` (datetime), `days_left` (int) and `pdf` (Path | None)."""
    d = assign_dir or ASSIGN_DIR
    now = now or datetime.now()
    try:
        items = json.loads((d / '_cache.json').read_text(encoding='utf-8')).get('assignments') or []
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for a in items:
        if not isinstance(a, dict) or a.get('submitted'):
            continue
        if course_key and a.get('course_key') != course_key:
            continue
        due = _due(a)
        if not due or not (now <= due <= now + timedelta(days=horizon_days)):
            continue
        pdf = d / str(a.get('course_key', '')) / f"{_safe_name(a.get('title', 'untitled'))}_assignment.pdf"
        ok = pdf.is_file() and 1000 < pdf.stat().st_size <= MAX_PDF_BYTES
        out.append({**a, 'due': due, 'days_left': (due - now).days, 'pdf': pdf if ok else None})
    out.sort(key=lambda a: a['due'])
    return out


def next_assignment(course_key: str, **kw) -> dict | None:
    items = open_assignments(course_key, **kw)
    return items[0] if items else None


def describe(a: dict) -> str:
    """One Hebrew line: title + due date + days left."""
    left = a.get('days_left')
    when = 'היום' if left == 0 else ('מחר' if left == 1 else f'בעוד {left} ימים')
    return f"{a.get('title', 'מטלה')} — הגשה {a['due']:%d/%m %H:%M} ({when})"


SKILLS_MODEL = 'gemini-3.8-flash'
PROFILE_PROMPT = """קרא את המטלה המצורפת (שיעורי בית להגשה בקורס אוניברסיטאי). החזר JSON בלבד:
{"skills": ["..."], "avoid": ["..."]}

"skills": 4-7 מיומנויות/מושגים שצריך לשלוט בהם כדי לפתור את המטלה, כל אחת עד 12 מילים,
  במילים כלליות בלבד (למשל "חישוב מקדמי פורייה של פונקציה זוגית"): בלי נוסחאות, בלי סימנים מתמטיים,
  בלי הפונקציות/המשוואות/המספרים מהמטלה, בלי לנסח שאלה ובלי רמז לפתרון.
"avoid": לכל שאלה וסעיף במטלה — תיאור קצר וספציפי של האובייקט שלו (הפונקציה, המשוואה, הטענה להוכחה,
  המבנה), כדי שתרגילי תרגול *לא* ישתמשו בהם. כאן מותר לכתוב נוסחאות. בלי פתרונות."""

OVERLAP_PROMPT = """מצורפת מטלה להגשה. למטה תרגילי תרגול שנכתבו כהכנה אליה.
החזר JSON בלבד: {{"overlap": [<מספרי תרגילים>]}}
תרגיל נחשב חפיפה אם פתרון שלו (או של אחד מסעיפיו) עונה בפועל על שאלה/סעיף מהמטלה: אותה פונקציה,
אותה משוואה או אותה טענה — גם עם פרמטר/מספר/סדר שונה, או מקרה פרטי שלה.
תרגיל שרק מתרגל את אותה מיומנות על אובייקט אחר — לא חפיפה. אם אין — {{"overlap": []}}.

## תרגילי התרגול
{exercises}
"""
_FORBIDDEN = re.compile(r'[$\\=^_{}]|\d')          # any math notation or number → drop the skill line


def _client(client=None):
    if client is not None:
        return client
    import os
    import google.genai as genai
    return genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))


def _json(text: str) -> dict:
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', (text or '').strip())
    return json.loads(raw)


def homework_profile(a: dict, client=None) -> dict:
    """{'skills': '- …\\n- …', 'avoid': [...]} for an assignment, cached next to its PDF.

    Practice sets are written from the generic skills (+ a list of the
    assignment's specific objects to stay away from), never from the
    assignment itself: a generator that saw the questions wrote near-copies
    of 3 of 5 (live test 02/10/2026) and its worked solutions solved them.
    {'skills': '', 'avoid': []} when there is no PDF or on any error."""
    empty = {'skills': '', 'avoid': []}
    pdf = a.get('pdf') if a else None
    if not pdf:
        return empty
    pdf = Path(pdf)
    cache = pdf.with_name(pdf.stem + '_profile.json')
    sig = f'{pdf.stat().st_size}:{int(pdf.stat().st_mtime)}'
    try:
        c = json.loads(cache.read_text(encoding='utf-8'))
        if c.get('sig') == sig:
            return {'skills': c.get('skills', ''), 'avoid': c.get('avoid', [])}
    except (OSError, ValueError):
        pass
    try:
        from google.genai import types as T
        c = _client(client)          # keep a reference: a GC'd Client closes its HTTP session mid-call
        resp = c.models.generate_content(
            model=SKILLS_MODEL, contents=[pdf_part(a), PROFILE_PROMPT],
            config=T.GenerateContentConfig(max_output_tokens=3000, temperature=0.0,
                                           response_mime_type='application/json',
                                           thinking_config=T.ThinkingConfig(thinking_level='low')))
        from utils.usage_tracker import log_gemini
        log_gemini(resp, SKILLS_MODEL, op='homework_profile')
        data = _json(resp.text)
    except Exception as e:
        print(f'  ⚠️ homework profile failed: {e}')
        return empty
    skills = '\n'.join('- ' + str(s).strip().lstrip('- ') for s in data.get('skills') or []
                       if str(s).strip() and not _FORBIDDEN.search(str(s)))[:1200]
    avoid = [str(x).strip()[:200] for x in (data.get('avoid') or []) if str(x).strip()][:20]
    if skills:
        cache.write_text(json.dumps({'sig': sig, 'skills': skills, 'avoid': avoid}, ensure_ascii=False),
                         encoding='utf-8')
    return {'skills': skills, 'avoid': avoid}


def homework_skills(a: dict, client=None) -> str:
    return homework_profile(a, client)['skills']


def overlapping_exercises(a: dict, exercises_md: str, client=None) -> set[int] | None:
    """Exercise numbers whose solution would answer a question of the assignment.
    None when the check itself failed (callers must then not ship the set)."""
    try:
        from google.genai import types as T
        c = _client(client)          # keep a reference: a GC'd Client closes its HTTP session mid-call
        resp = c.models.generate_content(
            model=SKILLS_MODEL,
            contents=[pdf_part(a), OVERLAP_PROMPT.format(exercises=exercises_md[:40000])],
            config=T.GenerateContentConfig(max_output_tokens=1000, temperature=0.0,
                                           response_mime_type='application/json',
                                           thinking_config=T.ThinkingConfig(thinking_level='low')))
        from utils.usage_tracker import log_gemini
        log_gemini(resp, SKILLS_MODEL, op='homework_overlap')
        return {int(x) for x in _json(resp.text).get('overlap') or []}
    except Exception as e:
        print(f'  ⚠️ homework overlap check failed: {e}')
        return None


_SECTION = re.compile(r'^###\s*(?:פתרון\s+)?תרגיל\s+(\d+)\b')


def drop_exercises(md: str, nums: set[int]) -> str:
    """Remove '### תרגיל N …' and '### פתרון תרגיל N' sections (up to the next ##/### header)."""
    if not nums:
        return md
    out, skipping = [], False
    for line in md.splitlines(keepends=True):
        if line.startswith('#'):
            m = _SECTION.match(line)
            skipping = bool(m and int(m.group(1)) in nums) or (skipping and not line.startswith(('## ', '### ', '# ')))
        if not skipping:
            out.append(line)
    return ''.join(out)


def pdf_part(a: dict):
    """The assignment PDF as a Gemini content part (Gemini reads Hebrew PDFs natively;
    text extraction scrambles RTL). None when there is no PDF."""
    if not a or not a.get('pdf'):
        return None
    from google.genai import types as T
    return T.Part.from_bytes(data=Path(a['pdf']).read_bytes(), mime_type='application/pdf')
