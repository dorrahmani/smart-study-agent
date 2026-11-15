"""
agent/closure.py — what the owner closed stays closed (05/11).

05/11 10:09 he pressed ✅ on "להחזיר את ספרי החדו"א…"; 10:12 the noticing layer told him about the same mail ("אבל אמרתי
לך שהחזרתי"), and a second task from that mail thread stayed open. Every source kept its own idea of "done".
This module is the one memory they all ask:

    items()      closed tasks (data/tasks.json, status != open) + what he said is over in words (data/closed_items.json)
    record()     "החזרתי את המפתח" / a closed note with no task behind it → a saved record
    match()      is this new thing the same thing? By id first (mail / thread), then by meaning: shared Hebrew stems.

    mode    who asks                                       what counts
    note    proactive notes, reask, reminders, to-dos      a task or a saved record, loose meaning match
    task    a new mail task, calendar, event reminders     a closed task / closed note (plain words only by id)
    digest  the mail digest                                a closed task, same sender or a strong match: a digest line
                                                           is the one place a false match costs a real mail

A mail arriving more than GRACE after the closing is a new request (next month's rent), unless it's the same thread.
No LLM. Reading never raises: no memory just means nothing is suppressed.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'closed_items.json'
GRACE = timedelta(days=14)
KEEP_DAYS = 180
MAX_RECORDS = 300
_FINAL = str.maketrans('םןץףך', 'מנצפכ')
_DATE_RX = re.compile(r'\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?')


def _stem(w: str) -> str:
    """'להחזירם' / 'החזר' / 'שהחזרתי' → 'חזר'; 'הספרים' / 'ספרי' / 'לספרייה' → 'ספר'. Crude on purpose: it only has to
    make two spellings of one word meet, and a match always needs two of them."""
    w = re.sub(r'["\'׳״]', '', w).lower().translate(_FINAL)
    if re.fullmatch(r'[a-z0-9_.-]+', w):
        return w[:6]
    if len(w) > 4 and w[:2] in ('שה', 'מה'):
        w = w[2:]
    while len(w) > 3 and w[0] in 'והבל':
        w = w[1:]
    if len(w) > 3:
        w = re.sub(r'(?:תי|נו|תמ|ימ|ות|מ|נ|ה)$', '', w) or w
    return (w[:1] + re.sub(r'[וי]', '', w[1:]))[:3]


_STOP = {_stem(w) for w in (
    'אני אתה זה הזה הזאת זאת גם כבר עוד רק היום מחר אתמול צריך חייב אבל שלי שלך אותם אותו אותה אמרתי הכל הכול עכשיו '
    'הרגע בעצם באמת מזמן בסוף תכלס בזה עבור לפני אחרי ימים יום שבוע עדיין לגבי מייל הודעה נוסף חדש תזכורת מעולה סליחה '
    'משהו דבר דברים שיש אין שום וואלה נראה '
    # who the mail is from, not what it's about (replay 05/11: "הפקולטה למדעים מדויקים" met "הספרייה למדעים מדויקים")
    'הפקולטה פקולטה למדעים מדויקים הנדסה אוניברסיטה אוניברסיטת תל אביב סטודנט סטודנטים שלום יקר יקרים בברכה').split()}
_GENERIC = {_stem(w) for w in (
    'לשלם לשלוח למלא לאשר להירשם לרשום לענות להשיב לחתום לעדכן להסדיר לבחור להגיש לתאם לקבוע לבדוק לעשות טופס בקשה סקר '
    'תשלום פריטים אישור שאלון קורס הרצאה שיעור תרגיל מטלה').split()}


def stems(text: str) -> set[str]:
    out = set()
    for w in re.findall(r'[א-תa-zA-Z0-9"\'׳״]{3,}', text or ''):
        if not re.fullmatch(r'[\d"\'׳״]+', w) and (s := _stem(w)) and s not in _STOP:
            out.add(s)
    return out


def _nums(text: str) -> set[str]:
    """"תרגיל 3" ≠ "תרגיל 4". Dates don't count: two mails about one loan can name 08/11 and 09/11."""
    return set(re.findall(r'\d+', _DATE_RX.sub(' ', text or '')))


def same(a: str, b: str, min_shared: int = 2) -> bool:
    """Two texts about one thing: ≥2 shared stems, at least half of the shorter text's, one of them not a generic
    "to fill / form" word. A long mail always shares two words with something."""
    na, nb = _nums(a), _nums(b)
    if na and nb and not na & nb:
        return False
    sa, sb = stems(a), stems(b)
    shared = sa & sb
    return len(shared) >= max(min_shared, (min(len(sa), len(sb)) + 1) // 2) and bool(shared - _GENERIC)


def _sender(s: str) -> str:
    return re.sub(r'\s*\(.*?\)\s*', '', (s or '').strip().lower())


def _same_sender(a: str, b: str) -> bool:
    a, b = _sender(a), _sender(b)
    return bool(a and b) and (a in b or b in a)


def _when(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    try:
        return datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def path_beside(tasks_path: Path | None) -> Path | None:
    """A test's tasks file keeps its closed records next to it."""
    return Path(tasks_path).with_name('closed_items.json') if tasks_path else None


def _load(path: Path | None = None) -> list[dict]:
    try:
        d = json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return d if isinstance(d, list) else []


def record(chat: str, text: str, why: str = 'done', who: str = '', mail_id: str = '', thread: str = '',
           origin: str = 'words', now: datetime | None = None, path: Path | None = None) -> dict:
    """Save one closed thing. origin 'words' = a sentence of his with nothing behind it; 'note' = the bot's own note."""
    now = now or datetime.now()
    text = ' '.join((text or '').split())[:300]
    rec = {'chat': chat, 'text': text, 'why': why, 'who': who, 'mail': mail_id, 'thread': thread, 'origin': origin,
           'ts': now.isoformat(timespec='minutes')}
    cut = (now - timedelta(days=KEEP_DAYS)).isoformat()
    keep = [r for r in _load(path) if r.get('ts', '') >= cut and not (r.get('chat') == chat and r.get('text') == text)]
    keep.append(rec)
    p = path or STATE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(keep[-MAX_RECORDS:], ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(p)
    except OSError as e:
        print(f'  ⚠️ closed items: {e}')
    return rec


def items(chat: str | None = None, path: Path | None = None, tasks_path: Path | None = None) -> list[dict]:
    """Everything closed (chat None: everyone's). Closed tasks are read live, so an old tasks.json needs no migration."""
    out = [r for r in _load(path or path_beside(tasks_path)) if chat is None or r.get('chat') == chat]
    try:
        from agent import tasks
        for t in tasks._load(tasks_path)['tasks']:
            if t.get('status') != 'open' and (chat is None or t.get('chat') == chat):
                out.append({'chat': t.get('chat'), 'text': t.get('text', ''), 'who': t.get('src', ''),
                            'mail': t.get('mail', ''), 'thread': t.get('thread', ''), 'why': t.get('status'),
                            'origin': 'task', 'ts': t.get('closed') or t.get('created', '')})
    except Exception as e:
        print(f'  ⚠️ closed tasks: {e!r}'[:200])
    return out


def match(closed: list[dict], text: str, who: str = '', mail_id: str = '', thread: str = '', when=None,
          mode: str = 'note') -> dict | None:
    """The closed item this is about, or None. `when` = when this thing arrived (a mail's day, an event's day)."""
    at = _when(when)
    for c in closed or []:
        if (mail_id and c.get('mail') == mail_id) or (thread and c.get('thread') == thread):
            return c
    for c in closed or []:
        if c.get('origin') == 'words' and mode != 'note':
            continue
        ts = _when(c.get('ts'))
        if at and ts and at > ts + GRACE:
            continue
        if mode == 'digest':
            if (_same_sender(who, c.get('who', '')) and same(text, c.get('text', ''))) or \
                    same(text, c.get('text', ''), 3):
                return c
        elif same(text, c.get('text', '')):
            return c
    return None


def is_closed(chat: str | None, text: str, who: str = '', mail_id: str = '', thread: str = '', when=None,
              mode: str = 'note', path: Path | None = None, tasks_path: Path | None = None) -> dict | None:
    return match(items(chat, path, tasks_path), text, who, mail_id, thread, when, mode)
