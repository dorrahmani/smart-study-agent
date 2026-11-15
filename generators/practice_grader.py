"""
generators/practice_grader.py — grade a photographed solution to a practice-set exercise.

Flow in Telegram: the practice-set PDF arrives → you solve on paper → you REPLY to
that message with a photo (or an album) and "תרגיל 2" → step-by-step feedback:
what's right, the first wrong step, the correct step. The result feeds the
mastery map (score) and the mistake notebook (each mistake → a flashcard).

Which set? (1) the practice message the photo replies to, (2) the latest set of
a course named in the caption, (3) the most recent set sent to this chat.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / 'data' / 'practice' / 'sent_index.json'
MODEL = os.getenv('SSA_PRACTICE_GRADER_MODEL', 'gemini-3.8-flash')
FOLLOW_UP = 'פתרת? השב (Reply) להודעה הזו עם צילום הפתרון וכתוב "תרגיל 2" — אבדוק צעד-אחר-צעד.'


# ── which practice set ───────────────────────────────────────────────────────

def _load_index(path: Path | None = None) -> dict:
    try:
        return json.loads((path or INDEX).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def register_sent(md_path: Path | str, course_key: str, chat_id, message_id=None,
                  path: Path | None = None) -> None:
    """Remember a practice set that was sent, so a reply/caption can find it. Never raises."""
    try:
        idx = _load_index(path)
        entry = {'md': str(md_path), 'course': course_key, 'ts': datetime.now().isoformat(timespec='seconds')}
        if message_id is not None:
            idx[f'msg:{chat_id}:{message_id}'] = entry
        idx[f'course:{chat_id}:{course_key}'] = entry
        idx[f'last:{chat_id}'] = entry
        p = path or INDEX
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception as e:
        print(f'  ⚠️ practice index failed: {e}')


def resolve_set(chat_id, reply_to_message_id=None, course_hint: str | None = None,
                path: Path | None = None) -> dict | None:
    idx = _load_index(path)
    for key in ([f'msg:{chat_id}:{reply_to_message_id}'] if reply_to_message_id else []) + \
               ([f'course:{chat_id}:{course_hint}'] if course_hint else []) + [f'last:{chat_id}']:
        e = idx.get(key)
        if e and Path(e['md']).is_file():
            return {**e, 'via_reply': key.startswith('msg:')}
    return None


# ── sections of a set ────────────────────────────────────────────────────────

def _section(md: str, header_rx: str) -> str:
    m = re.search(header_rx + r'.*?(?=^###\s|^##\s|\Z)', md, re.M | re.S)
    return m.group(0).strip() if m else ''


def exercise(md: str, n: int) -> tuple[str, str]:
    """(statement section, topic from its header '### תרגיל N (רמה) — נושא')."""
    sec = _section(md, rf'^###\s*תרגיל\s+{n}\b')
    head = sec.splitlines()[0] if sec else ''
    topic = head.split('—', 1)[1].strip() if '—' in head else ''
    return sec, topic


def solution(md: str, n: int) -> str:
    return _section(md, rf'^###\s*פתרון\s+תרגיל\s+{n}\b')


# ── grading ──────────────────────────────────────────────────────────────────

PROMPT = """אתה מתרגל אוניברסיטאי שבודק פתרון בכתב יד של סטודנט לתרגיל מסט תרגול. התמונות מצורפות.
{target}

החזר JSON בלבד:
{{"exercise": <מספר התרגיל>, "verdict": "correct|partial|wrong|unreadable",
  "score": <0-100>, "summary": "<משפט אחד>",
  "good": ["<מה נעשה נכון — עד 3>"],
  "first_error": "<הצעד הראשון שבו הפתרון סוטה: מה כתוב שם ולמה זה לא נכון; ריק אם נכון>",
  "fix": "<איך הצעד הזה צריך להיראות + איך ממשיכים משם (בלי לכתוב את כל הפתרון מחדש)>",
  "mistakes": [{{"what": "<הטעות, משפט אחד>", "correct": "<הנכון + למה, 1-2 משפטים>"}}]}}
כללים:
- בדוק את הדרך, לא רק את התשובה הסופית. תשובה נכונה מדרך שגויה = partial.
- דרך אחרת מהפתרון הרשמי אבל נכונה — מלא הניקוד.
- "unreadable" רק אם באמת אי אפשר לקרוא; אז score=0 ו-mistakes ריק.
- נוסחאות בכתיב פשוט שקריא בטלגרם (C(5,2), x², √x, ≤, ∑, 1/32) — **בלי LaTeX ובלי $**. עברית.

{material}
"""


def _client():
    import google.genai as genai
    return genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))


def grade(md_path: Path | str, course_key: str, images: list[Path], n: int | None = None,
          client=None) -> dict:
    """Grade, record to mastery + mistakes, return the parsed result (+ 'text' for Telegram)."""
    from google.genai import types as T
    md = Path(md_path).read_text(encoding='utf-8')
    if n:
        ex, topic = exercise(md, n)
        if not ex:
            raise ValueError(f'אין תרגיל {n} בסט הזה')
        target = f'התרגיל: מספר {n}.'
        material = f'## התרגיל\n{ex}\n\n## הפתרון הרשמי (לשימושך בלבד)\n{solution(md, n)}'
    else:
        topic = ''
        target = 'לא צוין מספר תרגיל — זהה לפי התמונה איזה תרגיל מהסט נפתר.'
        material = f'## סט התרגול המלא (תרגילים + פתרונות רשמיים)\n{md[:60000]}'
    parts = [T.Part.from_bytes(data=p.read_bytes(),
                               mime_type='image/png' if p.suffix.lower() == '.png' else 'image/jpeg')
             for p in images]
    c = client or _client()                         # keep a reference (a GC'd Client closes mid-call)
    resp = c.models.generate_content(
        model=MODEL, contents=parts + [PROMPT.format(target=target, material=material)],
        config=T.GenerateContentConfig(max_output_tokens=8000, temperature=0.0,
                                       response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='medium')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op='practice_grade')
    r = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', (resp.text or '').strip()))
    n = n or int(r.get('exercise') or 0) or None
    if n and not topic:
        ex, topic = exercise(md, n)
    else:
        ex = exercise(md, n)[0] if n else ''
    topic = topic or 'סט תרגול'
    r['exercise'], r['topic'] = n, topic

    if r.get('verdict') != 'unreadable':
        from utils.mastery import record as mastery
        from utils.mistakes import record as mistake
        mastery(course_key, topic, float(r.get('score') or 0) / 100, 'practice')
        statement = re.sub(r'^###[^\n]*\n', '', ex).strip()[:400]
        for m in r.get('mistakes') or []:
            mistake(course_key, 'practice', topic=topic, question=statement or f'תרגיל {n}: {topic}',
                    wrong=str(m.get('what', '')), correct=str(m.get('correct', '')))
    r['text'] = format_feedback(r)
    return r


ICON = {'correct': '✅', 'partial': '🟡', 'wrong': '❌', 'unreadable': '📷'}


def format_feedback(r: dict) -> str:
    v = r.get('verdict', '')
    if v == 'unreadable':
        return '📷 לא הצלחתי לקרוא את הכתב. צלם שוב באור טוב, ישר מלמעלה, דף אחד בכל תמונה.'
    out = [f"{ICON.get(v, '•')} תרגיל {r.get('exercise') or '?'} ({r.get('topic', '')}) — {r.get('score', '?')}/100",
           str(r.get('summary', '')).strip()]
    good = [g for g in r.get('good') or [] if str(g).strip()]
    if good:
        out.append('\n👍 נכון אצלך:\n' + '\n'.join(f'• {g}' for g in good))
    if (r.get('first_error') or '').strip():
        out.append(f"\n🔍 איפה זה נשבר:\n{r['first_error']}")
    if (r.get('fix') or '').strip():
        out.append(f"\n🛠️ איך צריך:\n{r['fix']}")
    if r.get('mistakes'):
        k = len(r['mistakes'])
        out.append('\n📕 ' + ('טעות אחת נוספה' if k == 1 else f'{k} טעויות נוספו')
                   + ' למחברת הטעויות — יחזרו אליך ככרטיסיות.')
    return '\n'.join(x for x in out if x)
