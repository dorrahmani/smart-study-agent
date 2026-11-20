"""
agent/tasks.py — things the owner has to do himself by a date → the calendar, and nudges until he says it's done (02/11).

The owner: "שלחת לי לזכור להחזיר ספרים לספריה — תוסיף את זה ליומן ותוודא שאני סוגר את המשימה הזו מצדי, תנדנד לי".
Not homework (utils/deadlines + agent/loops do that) — a library return, a payment, a form, a registration.
    add()        an all-day "📌 …" event on the due date (tagged, so only ours is touched) + the task, open
    tick()       a nudge with ✅ / 🌙 / 🗑️ buttons: daily at 10:00 from 3 days before, twice on the day (10:00,
                 17:00), daily while late, up to LATE_DAYS — then one last "עדיין פתוח?" and it stops
    action()     ✅ done → the event becomes "✅ …"; 🌙 → tomorrow 10:00; 🗑️ → "❌ לא רלוונטי — …"
    close_text() "החזרתי את הספרים" / "אמרתי לך שהחזרתי" / "זה כבר לא רלוונטי" — closes the task it's about
05/11: closing is for good. Tasks from one mail thread close together, and what's closed is never created again or said
again (agent/closure.py). "אבל אמרתי לך שהחזרתי" must not be needed twice.
Sources: the mail triage's "task" (integrations/tau_mail) and the owner asking for one. No LLM here.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from agent import closure

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'tasks.json'
TAG, VALUE = 'ssa_src', 'task'
LEAD_DAYS = 3
SLOTS = (10, 17)                      # 17:00 only on the due day
LATE_DAYS = 5
DAYS_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
SIBLING_DAYS = 7                      # two tasks from mails about one thing have due dates this close
# 05/11 the verb anywhere in a short message ("אבל אמרתי לך שהחזרתי"), not only as its first word
DONE_RX = re.compile(r'(?<![א-ת])ש?(?:ביטלתי|החזרתי|שילמתי|עשיתי|סיימתי|סגרתי|מילאתי|נרשמתי|שלחתי|הבאתי|אספתי|הגשתי|טיפלתי|קניתי|'
                     r'הזמנתי|חידשתי|האכלתי|בוצע|טופל|נעשה|סודר|סגור|הוחזרו?|שולם)(?![א-ת])')
DROP_RX = re.compile(r'(?<![א-ת])(?:לא\s+רלוונטי|לא\s+רלבנטי|ירד\s+מהפרק|התבטל|בוטל|לא\s+צריך\s+יותר|כבר\s+לא\s+צריך|'
                     r'עזוב\s+את\s+זה|תוריד\s+את\s+זה)(?![א-ת])')
NOT_DONE_RX = re.compile(r'(?<![א-ת])(?:לא|טרם|אם|כש|לפני|אחרי|האם|איך|מתי)\s+(?:כבר\s+)?$')
ASKING_RX = re.compile(r'\?\s*$|^\s*(?:תזכיר|תזכירי|תוסיף|תקבע|תשלח|תגיד|תכתוב|תבדוק|תמחק|תבטל|מה|איך|למה|האם|מתי|איפה|כמה|מי)(?![א-ת])')
_STOP = {'את', 'של', 'על', 'עם', 'לא', 'גם', 'כל', 'עד', 'זה', 'אני', 'כבר', 'היום', 'מחר', 'לספרייה', 'הספרייה'}


def about(chat: str, text: str, path: Path | None = None) -> list[dict]:
    """Open tasks this message talks about, spelling aside ("ספרים" / "הספר"). Used for a question about their details."""
    stems = {w[:3] for w in _words(text)}
    return [t for t in _load(path)['tasks'] if t.get('chat') == chat and t.get('status') == 'open'
            and stems & {w[:3] for w in _words(t.get('text', ''))}]


def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    d.setdefault('tasks', [])
    return d


def _save(d: dict, path: Path | None = None) -> None:
    p = path or STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')


def _words(t: str) -> set[str]:
    ws = {w for w in re.findall(r'[֐-׿\w"׳]{3,}', t or '')}
    return {w[1:] if w[:1] in 'בלהומש' and len(w) > 3 else w for w in ws} - _STOP


class _Cal:
    def insert(self, ev: dict) -> str:
        from integrations import gcal
        return gcal.insert_event(ev).get('id', '')

    def rename(self, eid: str, title: str) -> None:
        from integrations import gcal
        gcal._service().events().patch(calendarId='primary', eventId=eid, body={'summary': title}).execute()


def _same_mail(a: dict, mail_id: str, thread: str) -> bool:
    return bool((mail_id and a.get('mail') == mail_id) or (thread and a.get('thread') == thread))


def _days_apart(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except ValueError:
        return 9999


def covered(chat: str, text: str, due: str, src: str = '', mail_id: str = '', thread: str = '',
            now: datetime | None = None, path: Path | None = None) -> bool:
    """A task for this already exists, open or closed (05/11: two tasks from one library thread, a day apart)."""
    text = re.sub(r'\s+', ' ', text or '').strip()[:120]
    mine = _words(text)
    for t in _load(path)['tasks']:
        if t['chat'] != chat:
            continue
        if _same_mail(t, mail_id, thread):
            return True
        if t['due'] == due[:10] and len(mine & _words(t['text'])) >= min(2, len(mine)):
            return True
        if t['status'] == 'open' and src and t.get('src') and _days_apart(t['due'], due) <= SIBLING_DAYS \
                and closure.same(text, t['text']):
            return True
    return bool(closure.is_closed(chat, text, src, mail_id, thread, now or datetime.now(), 'task',
                                  closure.path_beside(path), path))


def add(chat: str, text: str, due: str, src: str = '', cal=None, path: Path | None = None,
        now: datetime | None = None, mail_id: str = '', thread: str = '') -> dict | None:
    """A new open task (None when it's already covered, or the date's bad)."""
    now = now or datetime.now()
    try:
        d_due = date.fromisoformat(due[:10])
    except ValueError:
        return None
    if d_due < now.date() - timedelta(days=1):
        return None
    text = re.sub(r'\s+', ' ', text).strip()[:120]
    if covered(chat, text, d_due.isoformat(), src, mail_id, thread, now, path):
        return None
    d = _load(path)
    rec = {'id': secrets.token_hex(3), 'chat': chat, 'text': text, 'due': d_due.isoformat(), 'src': src,
           'created': now.isoformat(timespec='minutes'), 'status': 'open', 'sent': [], 'event': '',
           'mail': mail_id, 'thread': thread}
    try:
        rec['event'] = (cal or _Cal()).insert({
            'title': f'📌 {text}', 'start': d_due.isoformat(), 'end': (d_due + timedelta(days=1)).isoformat(),
            'all_day': True, 'notes': f'משימה — הבוט מזכיר עד שתסמן ✅{f" · מקור: {src}" if src else ""}',
            'private': {TAG: VALUE, 'task_id': rec['id']}})
    except Exception as e:
        print(f'  ⚠️ task calendar: {e!r}'[:200])
    d['tasks'].append(rec)
    _save(d, path)
    return rec


def _when(t: dict, now: datetime) -> str:
    due = date.fromisoformat(t['due'])
    n = (due - now.date()).days
    day = f'{DAYS_HE[due.weekday()]} {due:%d/%m}'
    if n > 1:
        return f'עד {day} — עוד {n} ימים'
    if n == 1:
        return f'עד מחר ({day})'
    if n == 0:
        return 'היום! ' + ('(תזכורת אחרונה להיום)' if now.hour >= SLOTS[1] else '')
    return f'⚠️ באיחור של {-n} {"יום" if n == -1 else "ימים"} (היה עד {day})'


def _slot(t: dict, now: datetime) -> str | None:
    """The nudge slot due now ('2026-11-05 10') — None when none is due or it was already sent."""
    due = date.fromisoformat(t['due'])
    if t.get('snooze') and now < datetime.fromisoformat(t['snooze']):
        return None
    start = max(due - timedelta(days=LEAD_DAYS), datetime.fromisoformat(t['created']).date())
    if now.date() < start or now.date() > due + timedelta(days=LATE_DAYS):
        return None
    hours = SLOTS if now.date() == due else SLOTS[:1]
    h = max((h for h in hours if now.hour >= h), default=None)
    if h is None:
        return None
    key = f'{now.date()} {h:02d}'
    return None if key in t.get('sent', []) else key


def buttons(t: dict) -> list[tuple[str, str]]:
    return [('✅ עשיתי', f"task:done:{t['id']}"), ('🌙 מחר', f"task:tomorrow:{t['id']}"),
            ('🗑️ לא רלוונטי', f"task:drop:{t['id']}")]


def tick(now: datetime | None = None, path: Path | None = None) -> list[tuple[str, str, list]]:
    """[(chat, text, buttons)] — the nudges due now (each slot once)."""
    now = now or datetime.now()
    d = _load(path)
    out = []
    changed = False
    for t in d['tasks']:
        if t['status'] != 'open':
            continue
        key = _slot(t, now)
        if not key:
            continue
        c = closure.is_closed(t['chat'], t['text'], t.get('src', ''), t.get('mail', ''), t.get('thread', ''), now, 'task',
                              closure.path_beside(path), path)
        if c and c.get('ts', '') >= t['created']:
            _close(t, 'done', now=now)       # closed under another name (a note he answered) — not nagged again
            changed = True
            continue
        t.setdefault('sent', []).append(key)
        t['sent'] = t['sent'][-30:]
        t.pop('snooze', None)
        last = now.date() >= date.fromisoformat(t['due']) + timedelta(days=LATE_DAYS)
        text = f"📌 *{t['text']}*\n🗓️ {_when(t, now)}" + ('\n\nזו התזכורת האחרונה — עדיין פתוח?' if last else '')
        out.append((t['chat'], text, buttons(t)))
    if out or changed:
        _save(d, path)
    return out


def _close(t: dict, status: str, cal=None, now: datetime | None = None) -> None:
    t['status'] = status
    t['closed'] = (now or datetime.now()).isoformat(timespec='minutes')
    if t.get('event'):
        try:
            (cal or _Cal()).rename(t['event'], ('✅ ' if status == 'done' else '❌ לא רלוונטי — ') + t['text'])
        except Exception as e:
            print(f'  ⚠️ task calendar close: {e!r}'[:200])


def _siblings(d: dict, t: dict) -> list[dict]:
    """Open tasks that are this one: the same mail / thread, or two mail tasks saying the same thing days apart."""
    out = []
    for x in d['tasks']:
        if x is t or x['chat'] != t['chat'] or x['status'] != 'open':
            continue
        if _same_mail(x, t.get('mail', ''), t.get('thread', '')) or \
                (x.get('src') and t.get('src') and _days_apart(x['due'], t['due']) <= SIBLING_DAYS
                 and closure.same(x['text'], t['text'])):
            out.append(x)
    return out


def _settle(chat: str, texts: list[str], path: Path | None = None) -> None:
    """The rest of the bot hears it: the assistant's week note, and one-off reminders about the same thing."""
    if path is not None:                 # a test's own file: nothing global
        return
    try:
        from agent import life, now_note
        for text in texts:
            now_note.add(chat, f'סגור, לא להזכיר שוב: {text}', 'decision')
            life.close_matching(chat, text)
    except Exception as e:
        print(f'  ⚠️ task settle: {e!r}'[:200])


def _close_all(d: dict, hits: list[dict], status: str, cal=None, path: Path | None = None,
               now: datetime | None = None) -> list[dict]:
    """Close these and their siblings; → the siblings closed along the way."""
    extra: list[dict] = []
    for t in hits:
        extra += [x for x in _siblings(d, t) if x not in hits and x not in extra]
    for t in hits + extra:
        _close(t, status, cal, now)
    _settle(hits[0]['chat'], [t['text'] for t in hits + extra], path)
    if path is None:                     # 19/11 "תבטל את מה שעשית" can open it again (agent/journal)
        from agent import journal
        journal.record(hits[0]['chat'], 'task_close', '; '.join(t['text'] for t in hits + extra),
                       {'ids': [t['id'] for t in hits + extra]})
    return extra


def _also(extra: list[dict]) -> str:
    return (' וגם: ' + '; '.join(x['text'] for x in extra)) if extra else ''


def action(chat: str, op: str, tid: str, now: datetime | None = None, cal=None, path: Path | None = None) -> str:
    now = now or datetime.now()
    d = _load(path)
    t = next((t for t in d['tasks'] if t['id'] == tid and t['chat'] == chat), None)
    if not t:
        return 'המשימה כבר לא קיימת.'
    if t['status'] != 'open':
        return '👍 כבר סגור.'
    if op == 'done':
        extra = _close_all(d, [t], 'done', cal, path, now)
        msg = f"✅ מעולה — סגרתי: {t['text']}{_also(extra)} (וסימנתי ביומן)."
    elif op == 'tomorrow':
        t['snooze'] = (now + timedelta(days=1)).replace(hour=SLOTS[0], minute=0, second=0, microsecond=0).isoformat()
        msg = '🌙 אזכיר מחר ב-10:00.'
    elif op == 'drop':
        _close_all(d, [t], 'dropped', cal, path, now)
        msg = '🗑️ הורדתי. לא אזכיר יותר.'
    else:
        return '🤷'
    _save(d, path)
    return msg


def _said_done(text: str) -> tuple[str, re.Match] | None:
    """('done' | 'dropped', the words that said so), or None: a question, a request, "עוד לא החזרתי"."""
    if not text or len(text) > 120 or ASKING_RX.search(text):
        return None
    from agent.open_items import WAIT_RX                 # 15/11 "שלחתי להם מייל, מחכה לתשובה" is sent, not done
    if WAIT_RX.search(text):
        return None
    for why, rx in (('dropped', DROP_RX), ('done', DONE_RX)):
        for m in rx.finditer(text):
            if not NOT_DONE_RX.search(text[:m.start()]):
                return why, m
    return None


def _in_note(t: dict, note: str) -> bool:
    return t['text'] in note or closure.same(t['text'], note)


def close_text(chat: str, text: str, cal=None, path: Path | None = None, context: str = '',
               now: datetime | None = None) -> str | None:
    """The owner said something is done / no longer relevant, in words → the open task it's about closes, with its siblings.
    `context` = the bot's note he's answering. None: not such a message (the normal flow goes on)."""
    said = _said_done(text)
    if not said:
        return None
    why, m = said
    words = closure.stems(text[:m.start()] + ' ' + text[m.end():])
    verb = closure.stems(m.group(0)) if why == 'done' else set()
    d = _load(path)
    mine = [t for t in d['tasks'] if t['chat'] == chat and t['status'] == 'open']
    score = {t['id']: len((words or verb) & closure.stems(t['text'])) for t in mine}
    hits = [t for t in mine if score[t['id']]]
    if not hits and not words and context:
        hits = [t for t in mine if _in_note(t, context)]
    if len(hits) > 1:
        top = max(score[t['id']] for t in hits)
        hits = [t for t in hits if score[t['id']] == top]
    if len(hits) > 1 and context:
        hits = [t for t in hits if _in_note(t, context)] or hits
    if len(hits) > 1 and not all(x in _siblings(d, hits[0]) for x in hits[1:]):
        return '🤔 על מה מהן? ' + ' · '.join(t['text'] for t in hits[:4]) + ' — כתוב למשל "' + \
               text.strip()[:30] + ' ' + hits[0]['text'][:30] + '".'
    if hits:
        status = 'dropped' if why == 'dropped' else 'done'
        extra = _close_all(d, hits, status, cal, path, now)
        _save(d, path)
        if status == 'dropped':
            return f"🗑️ הורדתי: {hits[0]['text']}{_also(extra)}. לא אזכיר יותר."
        return f"✅ מעולה — סגרתי: {hits[0]['text']}{_also(hits[1:] + extra)} (וסימנתי ביומן)."
    if context and (not words or len(words & closure.stems(context)) >= 2):
        closure.record(chat, context, why, origin='note', now=now, path=closure.path_beside(path))   # no task behind it
        _settle(chat, [context[:150]], path)
        return '👍 צודק, סגור. לא אחזור על זה.'
    if len(words) >= 2:                  # nothing open matches: kept quietly, so a later note about it stays away
        closure.record(chat, text, why, now=now, path=closure.path_beside(path))
    return None


def open_tasks(chat: str, path: Path | None = None) -> list[dict]:
    return sorted((t for t in _load(path)['tasks'] if t['chat'] == chat and t['status'] == 'open'), key=lambda t: t['due'])
