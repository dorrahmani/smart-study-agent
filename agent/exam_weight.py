"""
agent/exam_weight.py — "is this hard topic worth it? does it come up in exams?" — answered from the lecturer's own exams.

The owner 18/10: "אם אני מתעכב על נושא קשה מדי — שידע אם יופיע הרבה במבחנים או לא". scripts/lecturer_exams.py tagged every
question of Naftali Zevuloni's ~30 probability exams by course unit → data/exam_topics/probability.json. Here: which
unit a message is about (words + common aliases) → how often it appeared, how recently, and two real examples.
No LLM. The same table also goes into each probability summary (prompt_block), so every topic is marked by weight.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / 'data' / 'exam_topics'
RX = re.compile(r'(?:מופיע|יופיע|שואלים|ישאלו|נשאל|חשוב|כדאי להשקיע|שווה להשקיע|להשקיע).{0,40}(?:במבחנ|למבחנ|במבחן|למבחן)|'
                r'(?:כמה|האם|זה).{0,12}(?:חשוב|מופיע|נפוץ|שכיח)|משקל.{0,12}במבח|נושא קשה|שווה (?:את )?הזמן')
ALIASES = {                                     # what the owner may say → the unit's own words
    'בייס': 'בייס', 'מותנ': 'מותנה', 'שלמה': 'שלמה', 'אי תלות': 'אי-תלות', 'בלתי תלוי': 'אי-תלות', 'תלות': 'אי-תלות',
    'פואסון': 'בדידים', 'בינומי': 'בדידים', 'גאומטרי': 'בדידים', 'היפרגאומטרי': 'בדידים', 'בדיד': 'בדידים',
    'נורמלי': 'רציפים', 'מעריכי': 'רציפים', 'אחיד': 'רציפים', 'רציף': 'רציפים', 'צפיפות': 'רציפים',
    'משותפת': 'משותפת', 'דו מימד': 'דו-ממדיים', 'דו-מימד': 'דו-ממדיים', 'תוחלת': 'תוחלת', 'אינדיקטור': 'תוחלת',
    'שונות': 'שונות', 'מתאם': 'מתאם', 'קווריאנס': 'שונות', 'מרקוב': 'מרקוב', 'צ\'בישב': 'מרקוב', 'צ׳בישב': 'מרקוב',
    'צ\'רנוף': 'מרקוב', 'גבול המרכזי': 'הגבול', 'גבול מרכזי': 'הגבול', 'clt': 'הגבול', 'קירוב נורמלי': 'הגבול',
    'מספרים הגדולים': 'המספרים', 'התכנסות': 'התכנסות', 'בורל': 'בורל-קנטלי', 'קנטלי': 'בורל-קנטלי',
    'מומנט': 'מומנטים', 'יוצרת': 'מומנטים', 'הילוך': 'הילוכים', 'הכלה': 'ההכלה', 'הפרדה': 'ההכלה',
    'קומבינטור': 'קומבינטוריקה', 'ספירה': 'קומבינטוריקה', 'סימטרי': 'סימטריים',
}


MICRO_ALIASES = {                               # 20/10: micro B (scripts/moodle_exams.py)
    'פארטו': 'בכלכלת חליפין', 'אדג': 'בכלכלת חליפין', 'חליפין': 'חליפין', 'ביטוח': 'אי-ודאות', 'סיכון': 'אי-ודאות',
    'אי ודאות': 'אי-ודאות', 'אי-ודאות': 'אי-ודאות', 'ייצור': 'ייצור', 'רווחה': 'הרווחה', 'מיסוי': 'מיסים', 'מיסים': 'מיסים',
    'סובסיד': 'סובסידיות', 'חיצונ': 'חיצוניות', 'פיגו': 'חיצוניות', 'קואז': 'חיצוניות', 'ציבורי': 'ציבוריים',
    'סמואלסון': 'ציבוריים', 'טרמפיסט': 'ציבוריים',
}
ALIASES_BY = {'probability': ALIASES, 'microeconomics_2': MICRO_ALIASES}
COURSE_WORDS = {'probability': r'הסתברות', 'microeconomics_2': r'מיקרו'}


def courses() -> list[str]:
    """Courses with an exam map — probability first (the oldest, the default)."""
    have = sorted(p.stem for p in DIR.glob('*.json')) if DIR.exists() else []
    return sorted(have, key=lambda c: (c != 'probability', c))


def course_for(text: str) -> str | None:
    for ck, rx in COURSE_WORDS.items():
        if re.search(rx, text or ''):
            return ck
    return None


def _vault(data: dict) -> str:
    return data.get('vault_dir') or 'הסתברות דו-חוגי'


def _src(data: dict) -> str:
    """'של נפתלי' for the lecturer's exams, else the course's own."""
    return 'של נפתלי' if not data.get('course_name') else f"של {data['course_name']}"


def _resolve(text: str, course: str | None = None) -> tuple[str, dict, dict] | None:
    """(course, data, unit row): the named course, else the first course whose units the text matches."""
    named = course or course_for(text)
    for ck in ([named] if named else courses()):
        data = load(ck)
        r = find(text, data.get('stats') or [], ck)
        if r:
            return ck, data, r
    return None


def load(course: str = 'probability') -> dict:
    try:
        return json.loads((DIR / f'{course}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def find(text: str, rows: list[dict], course: str = 'probability') -> dict | None:
    """The unit the message is about: an alias first, then shared words (≥4 letters)."""
    t = (text or '').lower()
    for alias, key in ALIASES_BY.get(course, ALIASES).items():
        if alias in t:
            hit = next((r for r in rows if key in r['topic']), None)
            if hit:
                return hit
    words = {w for w in re.findall(r'[֐-׿]{4,}', t)}
    best = max(rows, key=lambda r: len(words & set(re.findall(r'[֐-׿]{4,}', r['topic']))), default=None)
    if best and words & set(re.findall(r'[֐-׿]{4,}', best['topic'])):
        return best
    return None


def verdict(r: dict) -> str:
    p = r['exam_pct']
    if p >= 60:
        return '🔥 *נושא ליבה*: כמעט בכל מבחן. שווה להשקיע עד שזה יושב.'
    if p >= 30:
        return '⭐ *חשוב*: מופיע בהרבה מבחנים. כדאי לשלוט ברמה של שאלות מבחן.'
    if p >= 10:
        return '🙂 *מופיע לפעמים*: להבין את העיקרון ולפתור שאלה אחת-שתיים, בלי להיתקע.'
    return '💤 *כמעט לא מופיע*: אם הוא תוקע אותך, אפשר להשאיר להמשך.'


def answer(text: str, course: str | None = None) -> str | None:
    hit = _resolve(text, course)
    if not hit:
        return None
    course, data, r = hit
    n = len(data.get('exams') or [])
    ex = '\n'.join(f'• {e}' for e in r['examples'][:2])
    return (f"📊 *{r['topic']}* במבחנים {_src(data)} ({n} מבחנים):\n"
            f"הופיע ב-*{r['exams']}* מבחנים ({r['exam_pct']}%) · {r['question_pct']}% מכל השאלות · "
            f"ב-{r['recent_5']} מתוך 5 המבחנים האחרונים · לאחרונה {r['last'][8:10]}/{r['last'][5:7]}/{r['last'][:4]}\n\n"
            f"{verdict(r)}\n\n*דוגמאות:*\n{ex}\n\n"
            f"💬 רוצה שאביא לך שאלת מבחן כזו עם הפתרון? כתוב \"תביא לי שאלה על {r['topic'].split(' (')[0]}\"")


QUESTION_RX = re.compile(r'(?:תביא|תן|תשלח|שלח)(?:י)?\s+(?:לי\s+)?(?:עוד\s+)?שאל(?:ה|ת מבחן)\s+(?:מבחן\s+)?(?:על|ב|מ)\s*(.+)$')
SOLUTION_RX = re.compile(r'^\s*(?:פתרון|תראה (?:לי )?(?:את ה)?פתרון|מה הפתרון|הפתרון)\s*[?.!]*\s*$')


def _section(path: Path, n) -> str:
    """'## שאלה N' … up to the next question, from a vault exam / solution note."""
    try:
        t = path.read_text(encoding='utf-8')
    except OSError:
        return ''
    m = re.search(rf'^#+\s*שאלה\s*{n}\b.*?(?=^#+\s*שאלה\s*\d+\b|\Z)', t, re.S | re.M)
    return m.group(0).strip() if m else ''


def exam_question(text: str, u: dict, course: str | None = None, pick=None) -> str | None:
    """"תביא לי שאלה על בייס" → a real question from the lecturer's exams on that unit (not the last one shown)."""
    m = QUESTION_RX.search(text or '')
    if not m:
        return None
    hit = _resolve(m.group(1), course or course_for(text))
    if not hit:
        return None
    course, data, r = hit
    from config.paths import VAULT_DIR
    vdir = VAULT_DIR / '🧾 מבחנים' / _vault(data)
    seen = set(u.get('exam_q_seen') or [])
    cands = [(e['date'], q['q']) for e in data['exams'] for q in e['questions'] if r['topic'] in q['topics']
             and f"{e['date']}#{q['q']}" not in seen and (vdir / f"{e['date']} שאלות.md").exists()]
    if not cands:
        return f"אין לי עוד שאלות על {r['topic']} שלא ראית 🙂"
    cands.sort(reverse=True)                                  # recent exams first, a little variety
    import random
    date_, n = (pick or random.choice)(cands[:8])
    body = _section(vdir / f'{date_} שאלות.md', n)
    if not body:
        return None
    from agent.nlm_ask import whatsapp_text                  # LaTeX → readable WhatsApp text
    body = whatsapp_text(body)
    u['exam_q_last'] = {'date': date_, 'q': n, 'course': course}
    u['exam_q_seen'] = (list(seen) + [f'{date_}#{n}'])[-60:]
    return (f"📝 *שאלת מבחן {_src(data)} — {date_[8:10]}/{date_[5:7]}/{date_[:4]}* ({r['topic']})\n\n{body[:3500]}\n\n"
            f"כשתרצה: \"פתרון\" — ואראה את הפתרון שלו.")


def exam_solution(text: str, u: dict) -> str | None:
    last = u.get('exam_q_last')
    if not last or not SOLUTION_RX.match(text or ''):
        return None
    from config.paths import VAULT_DIR
    data = load(last.get('course') or 'probability')
    body = _section(VAULT_DIR / '🧾 מבחנים' / _vault(data) / f"{last['date']} פתרון.md", last['q'])
    if not body:
        return 'לא מצאתי את הפתרון לשאלה הזו 😕'
    from agent.nlm_ask import whatsapp_text
    body = whatsapp_text(body)
    return f"✅ *{'הפתרון של נפתלי' if (last.get('course') or 'probability') == 'probability' else 'הפתרון'}* ({last['date'][8:10]}/{last['date'][5:7]}/{last['date'][:4]}, שאלה {last['q']})\n\n{body[:3500]}"


def prompt_block(course: str) -> str:
    """For the summary prompt: the units' exam weight, so each topic in the summary is marked."""
    data = load(course)
    rows = data.get('stats') or []
    if not rows:
        return ''
    n = len(data.get('exams') or [])
    lines = '\n'.join(f"- {r['topic']}: {r['exams']}/{n} מבחנים, {r['question_pct']}% מהשאלות, "
                      f"{r['recent_5']}/5 אחרונים" for r in rows)
    return ("\n## שכיחות הנושאים במבחנים של המרצה (חובה לשימוש)\n"
            f"ליד כל נושא מרכזי בסיכום (בכותרת שלו) הוסף שורה: \"📊 במבחנים: X/{n} מבחנים — ליבה/חשוב/לפעמים/נדיר\" לפי הטבלה. "
            "בסוף הסיכום — 'מה הכי חשוב למבחן מהשיעור הזה' לפי השכיחות.\n" + lines + '\n')
