"""
agent/life.py — the "personal life" layer of the agent (The owner 04/10/2026, ideas borrowed from Pika):

  memory     "תזכור שאני לא זמין בימי שישי" · "מה אתה זוכר?" · "תשכח ש…"   (per person, ≤60 facts)
  lists      "תוסיף חלב וביצים לרשימת הקניות" · "מה ברשימת הקניות?" · "קניתי חלב" · "נקה את רשימת הקניות"
             (shared by the household — the owner and the partner see and edit the same lists)
  reminders  "תזכיר לי מחר ב-9 להתקשר לבנק" · "כל יום ב-8 לקחת ויטמין" · "כל שני ב-18:00 …" ·
             "… עד שאעשה" (nag every 30 min, max 4) — fired by the worker; ✅ בוצע (done) / ⏰ עוד שעה (in an hour) / 🌙 מחר (tomorrow)
  email      "תנסח מייל למרצה שאני חולה ולא אגיע" → draft → 1 שלח (send) / 2 בטל (cancel) (never sent without that ✅)

Simple phrasings are matched with regexes (fast, free, deterministic); only reminder times and email
drafting go through Gemini. Storage: data/agent_life.json (atomic writes).
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'agent_life.json'
MODEL = 'gemini-3.8-flash'
MAX_FACTS = 60
NAG_EVERY_MIN = 30
NAG_MAX = 4
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']


def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STORE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    for k in ('memory', 'lists', 'reminders', 'drafts'):
        d.setdefault(k, {} if k != 'reminders' else [])
    return d


def _save(d: dict, path: Path | None = None) -> None:
    p = path or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(p)


def _norm_list(name: str) -> str:
    n = re.sub(r'^(ה|ל|ב)', '', (name or '').strip(' ?.!:,'))
    return {'קנייה': 'קניות', 'משימה': 'משימות', 'מטלות': 'משימות', 'todo': 'משימות'}.get(n, n) or 'קניות'


def _items(text: str) -> list[str]:
    parts = re.split(r'\s*,\s*|\s+ו(?=\S)', text.strip(' .'))
    return [p.strip(' .') for p in parts if p.strip(' .')]


# ── memory ──────────────────────────────────────────────────────────────────

def remember(who: str, fact: str, path: Path | None = None) -> str:
    d = _load(path)
    facts = d['memory'].setdefault(who, [])
    fact = fact.strip(' .')
    if fact and fact not in facts:
        facts.append(fact)
        del facts[:-MAX_FACTS]
        # 17/11 when it was said: the vault note of the fact links to that day (vault_sync/assistant_export). Code only.
        at = d.setdefault('memory_at', {}).setdefault(who, {})
        at[fact] = datetime.now().isoformat(timespec='minutes')
        for k in [k for k in at if k not in facts]:
            del at[k]
        if path is None:                 # 19/11 undoable (agent/journal)
            from agent import journal
            journal.record(who, 'memory', fact, {'fact': fact})
    _save(d, path)
    return f'🧠 זכרתי: {fact}'


def facts(who: str, path: Path | None = None) -> list[str]:
    return list(_load(path)['memory'].get(who, []))


def forget(who: str, what: str, path: Path | None = None) -> str:
    d = _load(path)
    before = d['memory'].get(who, [])
    keep = [f for f in before if what.strip(' .') not in f]
    d['memory'][who] = keep
    _save(d, path)
    return f'🗑️ שכחתי {len(before) - len(keep)} דברים.' if len(keep) < len(before) else 'לא מצאתי משהו כזה בזיכרון.'


# ── shared lists ────────────────────────────────────────────────────────────

def list_add(name: str, items: list[str], path: Path | None = None) -> str:
    d = _load(path)
    name = _norm_list(name)
    cur = d['lists'].setdefault(name, [])
    added = [i for i in items if i not in cur]
    cur.extend(added)
    _save(d, path)
    return f"📝 נוסף לרשימת ה{name}: {', '.join(added)}" if added else f'כבר ברשימת ה{name}.'


def list_show(name: str, path: Path | None = None) -> str:
    name = _norm_list(name)
    cur = _load(path)['lists'].get(name, [])
    if not cur:
        return f'רשימת ה{name} ריקה.'
    return f'📝 *רשימת ה{name}* ({len(cur)}):\n' + '\n'.join(f'• {i}' for i in cur)


def list_remove(name: str, items: list[str], path: Path | None = None) -> str:
    d = _load(path)
    name = _norm_list(name)
    cur = d['lists'].get(name, [])
    gone = [c for c in cur if any(i in c or c in i for i in items)]
    d['lists'][name] = [c for c in cur if c not in gone]
    _save(d, path)
    return (f"✅ הורדתי: {', '.join(gone)}" + (f'\nנשארו {len(d["lists"][name])}.' if d['lists'][name] else '\nהרשימה ריקה 🎉')) \
        if gone else 'לא מצאתי את זה ברשימה.'


def list_clear(name: str, path: Path | None = None) -> str:
    d = _load(path)
    name = _norm_list(name)
    d['lists'][name] = []
    _save(d, path)
    return f'🧹 רשימת ה{name} נוקתה.'


# ── reminders ───────────────────────────────────────────────────────────────

REMINDER_PROMPT = """עכשיו: {now} (יום {weekday}), שעון ישראל. הפוך את הבקשה לתזכורת. JSON בלבד:
{{"text": "<מה להזכיר, קצר, בגוף שני>", "when": "YYYY-MM-DDTHH:MM", "repeat": "none|daily|weekly|weekdays",
  "nag": false, "question": ""}}
- "מחר" / "בעוד שעה" / "ביום שני" — חשב מעכשיו. בלי שעה בכלל — "question": "באיזו שעה?".
- "כל יום" → daily; "כל יום שני" → weekly (when = המופע הקרוב); "כל יום חול / ראשון עד חמישי" → weekdays.
- "עד שאעשה" / "תנדנד" / "נודניק" → nag=true.
הבקשה: {text}"""


def parse_reminder(text: str, now: datetime | None = None, client=None) -> dict:
    from google.genai import types as T
    now = now or datetime.now()
    if client is None:
        import google.genai as genai
        client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    resp = client.models.generate_content(
        model=MODEL, contents=REMINDER_PROMPT.format(now=now.strftime('%Y-%m-%d %H:%M'),
                                                     weekday=WEEKDAY_HE[now.weekday()], text=text),
        config=T.GenerateContentConfig(max_output_tokens=2000, temperature=0.0, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op='agent.reminder_parse')
    r = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', (resp.text or '').strip()))
    if isinstance(r, list):
        r = r[0] if r else {}
    return {'text': str(r.get('text') or '').strip(), 'when': str(r.get('when') or ''),
            'repeat': r.get('repeat') if r.get('repeat') in ('none', 'daily', 'weekly', 'weekdays') else 'none',
            'nag': bool(r.get('nag')), 'question': str(r.get('question') or '').strip()}


QUICK_RX = re.compile(r'^\s*(?:תזכיר|תזכירי)\s+לי\s+(.+?)\s*[.!]*\s*$')
QUICK_WHAT_RX = re.compile(r'^(?:ל[א-ת]{2,}|על\s+[א-ת"״\'׳]{2,})')       # "לקנות חלב" / "על הפאדל" — said as he'd read it
QUICK_LEFT_RX = re.compile(r'\d|(?<![א-ת])(?:מחר|היום|הערב|בוקר|ערב|לילה|צהריים|שעה|שעות|דקות|לפני|אחרי|עד|כל|יום|'
                           r'שבוע|חודש|בעוד|עוד|ביום|ב(?:ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת))(?![א-ת])|[?\n]')


def quick_reminder(text: str, now: datetime | None = None) -> dict | None:
    """17/11: "תזכיר לי לקנות חלב מחר ב-8 בבוקר" — the thing and the time, read in code (agent/hetime): no Gemini call.
    Only when every time word was understood and nothing else in it looks like a time, a repeat or a condition
    ("עד שאגיד", "כל יום", "יום לפני הטיסה"); otherwise None and the model reads it, as before."""
    m = QUICK_RX.match(text or '')
    if not m or len(m.group(1)) > 120:
        return None
    from agent import hetime
    now = now or datetime.now()
    body = m.group(1)
    try:
        w = hetime.parse(body, now)
    except Exception as e:
        print(f'  ⚠️ hetime: {e!r}'[:200])
        return None
    if not w or w.at <= now + timedelta(minutes=1):
        return None
    what = hetime.strip(body, w)
    if not QUICK_WHAT_RX.match(what) or QUICK_LEFT_RX.search(what) or len(what) < 4 or len(what.split()) > 6:
        return None
    what = re.sub(r'^על\s+', '', what)
    when = w.at.isoformat(timespec='minutes') if w.has_time else w.at.date().isoformat()
    return {'text': what, 'when': when, 'repeat': 'none', 'nag': False, 'question': '', 'by': 'code'}


def fix_day(r: dict, text: str, now: datetime | None = None) -> dict:
    """The model's reminder, its DAY checked against the words (agent/hetime): "ביום ראשון" on a Saturday night is
    tomorrow, whatever the model counted. Only one clear day in the words, no "לפני/אחרי X", no repeat. Never raises."""
    try:
        if not r.get('when') or r.get('repeat', 'none') != 'none' or re.search(r'(?<![א-ת])(?:לפני|אחרי)(?![א-ת])', text or ''):
            return r
        from agent import hetime
        w = hetime.parse(text, now or datetime.now(), lessons=[])
        if not w or not w.has_day or str(r['when'])[:10] == w.at.date().isoformat():
            return r
        fixed = w.at.isoformat(timespec='minutes') if w.has_time else w.at.date().isoformat() + str(r['when'])[10:16]
        _log_fix('reminder_day', {'text': (text or '')[:120], 'model': r['when'], 'fixed': fixed})
        return {**r, 'when': fixed}
    except Exception as e:
        print(f'  ⚠️ reminder day check: {e!r}'[:200])
        return r


def _log_fix(kind: str, detail: dict) -> None:
    try:
        from agent import factcheck
        factcheck.log(kind, detail)
    except Exception:
        pass


def add_reminder(chat: str, r: dict, path: Path | None = None) -> dict:
    d = _load(path)
    rec = {'id': secrets.token_hex(3), 'chat': chat, 'text': r['text'], 'due': r['when'][:16],
           'repeat': r.get('repeat', 'none'), 'nag': bool(r.get('nag')), 'nags': 0, 'active': True}
    if r.get('important'):
        rec['important'] = True
    if r.get('guessed'):
        rec['guessed'] = True
    d['reminders'].append(rec)
    _save(d, path)
    if path is None:                     # 19/11 undoable (agent/journal)
        from agent import journal
        journal.record(chat, 'reminder', rec['text'], {'rid': rec['id']})
    return rec


def describe_reminder(rec: dict) -> str:
    due = datetime.fromisoformat(rec['due'])
    rep = {'daily': ' · כל יום', 'weekly': f" · כל יום {WEEKDAY_HE[due.weekday()]}",
           'weekdays': ' · ראשון–חמישי'}.get(rec.get('repeat'), '')
    nag = ' · אנדנד עד שתסמן ✅' if rec.get('nag') else ''
    imp = ' · 🚨 חשוב: לא תגיב — אנדנד ואקפיץ מהיומן' if rec.get('important') else ''
    return f"⏰ {rec['text']} — יום {WEEKDAY_HE[due.weekday()]} {due:%d/%m %H:%M}{rep}{nag}{imp}"


NAG_RX = re.compile(r'(?<![א-ת])(?:תחפור|תנדנד|נודניק|תציק|אל\s+תרפה|תשב\s+עליי|עד\s+ש(?:אגיד|אעשה|אסמן|אאשר|אבטל|'
                    r'אסיים|אטפל|זה\s+ייעשה|יבוטל))(?![א-ת])')


# 04/11 "תזכיר לי לבטל מנוי Netflix" → "באיזו שעה?", and the next morning "תזכיר לי" → "מה תרצה שאזכיר?": the request
# was lost between the two. A secretary doesn't ask: she picks a sensible time and lets him move it with one tap.
def default_when(now: datetime, when: str = '') -> datetime:
    """No hour given → that day at 09:00 (a date was given), else today 18:00 before 16:00, else tomorrow 09:00."""
    m = re.match(r'(\d{4}-\d{2}-\d{2})', when or '')
    if m:
        try:
            t = datetime.fromisoformat(m.group(1)).replace(hour=9)
            if t > now + timedelta(minutes=30):
                return t
        except ValueError:
            pass
    if now.hour < 16:
        return now.replace(hour=18, minute=0, second=0, microsecond=0)
    return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)


def _has_hour(when: str) -> bool:
    return bool(re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', when or ''))


def set_reminder(chat: str, text: str, own_words: str = '', now: datetime | None = None, parse=None,
                 path: Path | None = None):
    """One place for "תזכיר לי …" (the router and the agent loop): → (kind, body, buttons), or None when there's
    nothing to remind of. Importance comes from the owner's own words only (own_words; the request itself by default)."""
    now = now or datetime.now()
    quick = None if parse else quick_reminder(text, now)       # 17/11 the plain ones are read in code (agent/hetime)
    r = quick or (parse or parse_reminder)(text)
    if not quick and parse is None:
        r = fix_day(r, own_words or text, now)
    if not r.get('text'):
        return None
    from agent import escalate
    r['important'] = escalate.important(own_words or text)
    if NAG_RX.search(f'{own_words} {text}'):           # 12/11 "תחפור לי עד שאגיד לך שבוטל" → nag false, and the bot
        r['nag'] = True                                # had answered "אשב עליך עד שתבטל"
    guessed = not _has_hour(r.get('when', ''))
    if guessed:
        r['when'] = default_when(now, r.get('when', '')).isoformat(timespec='minutes')
        r['guessed'] = True
    rec = add_reminder(chat, r, path)
    if guessed:
        return ('buttons', describe_reminder(rec) + '\n\nלא אמרת מתי, אז קבעתי ככה. לשנות?',
                [('👍 ככה טוב', 'noop:r'), ('⏰ בעוד שעה', f"rmd:at1h:{rec['id']}"),
                 ('🌆 הערב ב-20:00', f"rmd:eve:{rec['id']}"), ('☀️ מחר ב-9:00', f"rmd:morn:{rec['id']}"),
                 ('🗑️ לבטל', f"rmd:cancel:{rec['id']}")])
    btns = [('👍 סבבה', 'noop:r'), ('🗑️ לבטל', f"rmd:cancel:{rec['id']}")]
    if not rec.get('important'):
        btns.insert(1, ('🚨 חשוב — תעקוב אם לא אגיב', f"rmd:imp:{rec['id']}"))
    return ('buttons', describe_reminder(rec) + '\n\nנקבע ✅', btns)


def list_reminders(chat: str, path: Path | None = None) -> str:
    rs = sorted((r for r in _load(path)['reminders'] if r['chat'] == chat and r['active']), key=lambda r: r['due'])
    return ('*התזכורות שלך:*\n' + '\n'.join(describe_reminder(r) for r in rs)) if rs else 'אין תזכורות פעילות.'


def cancel_reminder(chat: str, what: str, path: Path | None = None) -> str:
    """'תבטל את התזכורת של הרופא' — the active reminder whose text shares the words."""
    d = _load(path)
    words = [re.sub(r'^ה', '', w) for w in re.findall(r'\w+', what or '') if len(w) > 1]
    mine = [r for r in d['reminders'] if r['chat'] == chat and r.get('active')]
    hit = [r for r in mine if any(w in r['text'] for w in words)] if words else mine[-1:]
    if not hit:
        return 'לא מצאתי תזכורת כזו. "אילו תזכורות יש לי" יראה את כולן.'
    for r in hit:
        r['active'] = False
    _save(d, path)
    return '🗑️ ביטלתי: ' + ', '.join(r['text'] for r in hit)


def close_matching(chat: str, text: str, path: Path | None = None) -> int:
    """05/11: something the owner closed — a one-off reminder about the same thing goes with it (a repeating one stays)."""
    from agent import closure
    d = _load(path)
    hit = [r for r in d['reminders'] if r.get('chat') == chat and r.get('active') and r.get('repeat', 'none') == 'none'
           and closure.same(text, r.get('text', ''))]
    for r in hit:
        r['active'] = False
    if hit:
        _save(d, path)
    return len(hit)


def _next_due(rec: dict) -> datetime | None:
    due = datetime.fromisoformat(rec['due'])
    if rec['repeat'] == 'daily':
        return due + timedelta(days=1)
    if rec['repeat'] == 'weekly':
        return due + timedelta(days=7)
    if rec['repeat'] == 'weekdays':
        nxt = due + timedelta(days=1)
        while nxt.weekday() in (4, 5):                     # Friday, Saturday
            nxt += timedelta(days=1)
        return nxt
    return None


def due_now(now: datetime | None = None, path: Path | None = None) -> list[tuple[dict, list]]:
    """Reminders to send now → [(reminder, buttons)]. Advances repeats / nags; call once per tick."""
    now = now or datetime.now()
    d = _load(path)
    out = []
    for rec in d['reminders']:
        if not rec.get('active'):
            continue
        due = datetime.fromisoformat(rec.get('nag_at') or rec['due'])
        if due > now:
            continue
        buttons = [('✅ בוצע', f"rmd:done:{rec['id']}"), ('⏰ עוד שעה', f"rmd:snooze:{rec['id']}"),
                   ('🌙 מחר', f"rmd:tomorrow:{rec['id']}")]
        out.append((dict(rec), buttons))
        if rec.get('nag') and rec.get('nags', 0) < NAG_MAX:
            rec['nags'] = rec.get('nags', 0) + 1
            rec['nag_at'] = (now + timedelta(minutes=NAG_EVERY_MIN)).isoformat(timespec='minutes')
        else:
            _advance(rec)
    if out:
        _save(d, path)
    return out


def _advance(rec: dict) -> None:
    rec.pop('nag_at', None)
    rec['nags'] = 0
    nxt = _next_due(rec)
    if nxt:
        rec['due'] = nxt.isoformat(timespec='minutes')
    else:
        rec['active'] = False


def reminder_action(chat: str, op: str, rid: str, now: datetime | None = None, path: Path | None = None) -> str:
    now = now or datetime.now()
    d = _load(path)
    rec = next((r for r in d['reminders'] if r['id'] == rid and r['chat'] == chat), None)
    if not rec:
        return 'התזכורת כבר לא קיימת.'
    if op == 'done':
        if rec.get('nag_at'):                              # stop nagging this occurrence
            _advance(rec)
        if rec.get('repeat', 'none') == 'none':            # 15/11 a one-off he pressed ✅ on is closed (agent/open_items)
            rec['active'] = False
            rec['closed'] = now.isoformat(timespec='minutes')
        msg = '✅ מעולה!' + (' אזכיר שוב בפעם הבאה.' if rec.get('active') else '')
    elif op == 'snooze':
        rec['nag_at'] = (now + timedelta(hours=1)).isoformat(timespec='minutes')
        rec['active'] = True
        msg = '⏰ אזכיר עוד שעה.'
    elif op == 'tomorrow':
        rec['nag_at'] = (now + timedelta(days=1)).replace(hour=9, minute=0).isoformat(timespec='minutes')
        rec['active'] = True
        msg = '🌙 אזכיר מחר ב-09:00.'
    elif op == 'cancel':
        rec['active'] = False
        rec['closed'] = now.isoformat(timespec='minutes')
        msg = '🗑️ התזכורת בוטלה.'
    elif op == 'imp':                                      # 11/11: follow it up if he doesn't react (agent/escalate.py)
        rec['important'] = True
        msg = ('🚨 סימנתי כחשוב. כשהתזכורת תצא ולא תגיב — אחרי רבע שעה אנדנד כאן, ואחרי עוד 20 דק׳ תקפוץ לך '
               'התראה מיומן גוגל בטלפון.')
    elif op in ('at1h', 'eve', 'morn'):                    # the one-tap times after a guessed one
        if op == 'at1h':
            t = now + timedelta(hours=1)
        elif op == 'eve':
            t = now.replace(hour=20, minute=0, second=0, microsecond=0)
            t = t if t > now + timedelta(minutes=10) else t + timedelta(days=1)
        else:
            t = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        rec.update(due=t.isoformat(timespec='minutes'), active=True)
        rec.pop('nag_at', None)
        rec.pop('guessed', None)
        msg = f"⏰ זז: {describe_reminder(rec)}"
    else:
        return '🤷'
    if rec.get('active'):
        rec.pop('closed', None)
    _save(d, path)
    return msg


# ── email drafts (sent only after ✅) ───────────────────────────────────────

EMAIL_PROMPT = """נסח מייל קצר ומנומס בעברית לפי הבקשה של {name}. JSON בלבד:
{{"to": "<כתובת מייל אם צוינה, אחרת ריק>", "to_name": "<למי>", "subject": "...", "body": "<גוף המייל, 3-8 שורות, חתימה: {name}>"}}
עובדות שאולי רלוונטיות: {facts}
הבקשה: {text}"""


def draft_email(chat: str, text: str, name: str = 'המשתמש', client=None, path: Path | None = None) -> dict:
    from google.genai import types as T
    if client is None:
        import google.genai as genai
        client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    resp = client.models.generate_content(
        model=MODEL, contents=EMAIL_PROMPT.format(name=name, text=text, facts='; '.join(facts(chat, path)[:20]) or '-'),
        config=T.GenerateContentConfig(max_output_tokens=3000, temperature=0.4, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op='agent.email_draft')
    e = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', (resp.text or '').strip()))
    m = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', text)
    e['to'] = (m.group(0) if m else str(e.get('to') or '').strip())
    d = _load(path)
    did = secrets.token_hex(3)
    d['drafts'][did] = {**e, 'chat': chat, 'ts': datetime.now().isoformat(timespec='seconds')}
    _save(d, path)
    return {**e, 'id': did}


def take_draft(did: str, chat: str, path: Path | None = None) -> dict | None:
    d = _load(path)
    dr = d['drafts'].pop(did, None)
    _save(d, path)
    return dr if dr and dr.get('chat') == chat else None


def set_draft_to(did: str, to: str, path: Path | None = None) -> None:
    d = _load(path)
    if did in d['drafts']:
        d['drafts'][did]['to'] = to
        _save(d, path)


# ── router ──────────────────────────────────────────────────────────────────

RX = {
    'remember': re.compile(r'^\s*(?:תזכור|תזכרי|זכור|זכרי|תרשום לך)\s*(?:ש|:)\s*(.+)$', re.S),
    'facts': re.compile(r'מה (?:אתה|את) זוכר|מה זכור לך|מה אתה יודע עליי'),
    'forget': re.compile(r'^\s*(?:תשכח|תשכחי|שכח)\s+(?:ש|את\s+)?(.+)$'),
    'list_add_a': re.compile(r'^\s*(?:תוסיף|תוסיפי|הוסף|הוסיפי|תכניס|תכניסי|תרשום|תרשמי)\s+(.+?)\s+לרשימ(?:ת|ה)\s*(?:ה)?(\S+)?\s*$'),
    'list_add_b': re.compile(r'^\s*(?:תוסיף|תוסיפי|הוסף|הוסיפי|תכניס|תכניסי)\s+לרשימ(?:ת|ה)\s*(?:ה)?(\S+)\s+(.+)$'),
    'list_show': re.compile(r'(?:מה (?:יש )?ב?רשימ(?:ת|ה)|תראה (?:לי )?את רשימת|תראי (?:לי )?את רשימת|^רשימת)\s*(?:ה)?(\S+)'),
    'list_remove': re.compile(r'^\s*(?:תמחק|תמחקי|מחק|תוריד|תורידי|קניתי|קנינו|סיימתי|עשיתי)\s+(.+?)(?:\s+מ(?:ה)?רשימ(?:ת|ה)\s*(?:ה)?(\S+))?\s*$'),
    'list_clear': re.compile(r'(?:נקה|תנקה|תנקי|תאפס|תאפסי)\s+(?:את\s+)?רשימת\s*(?:ה)?(\S+)'),
    'reminders_list': re.compile(r'(?:אילו|איזה|מה ה)\s*תזכורות|התזכורות שלי'),
    'reminder_cancel': re.compile(r'(?:תבטל|תבטלי|בטל|תמחק|תמחקי|מחק)\s+(?:את\s+)?(?:ה)?תזכורת\s*(?:של|על|ל)?\s*(.*)$'),
    'reminder': re.compile(r'^\s*(?:תזכיר|תזכירי)\s+לי\b|^\s*תזכורת\b|^\s*תנדנד'),
    'email': re.compile(r'(?:תנסח|תנסחי|תכתוב|תכתבי|כתוב)\s+(?:לי\s+)?(?:מייל|אימייל|email)'),
}


# 'תזכיר לי מה סיכמנו…' asks to RECALL something, not to set a reminder (08/10) — unless a time is given
RECALL = re.compile(r'^\s*(?:תזכיר|תזכירי)\s+לי\s+(?:מה|איך|איפה|מי|למה|את מה|על מה|מתי היה|מה היה)\b'
                    r'(?!.*(?:\bב-?\d|מחר|בעוד|בשעה|בבוקר|בערב|בצהריים|היום ב))')


def route(chat: str, text: str, role: str = 'owner', path: Path | None = None, ask_time: bool = True):
    """→ list of (kind, text, buttons) if this is a life request, else None. ask_time=False: a reminder that can't be set yet
    isn't answered here (04/11 "תזכיר לי לבטל מנוי" → "באיזו שעה?", and nothing remembered the request)."""
    t = (text or '').strip()
    if not t:
        return None
    if m := RX['remember'].match(t):
        return [('text', remember(chat, m.group(1), path), None)]
    if RX['facts'].search(t):
        fs = facts(chat, path)
        return [('text', ('🧠 *מה שאני זוכר:*\n' + '\n'.join(f'• {f}' for f in fs)) if fs else 'עוד לא ביקשת שאזכור משהו.', None)]
    if m := RX['forget'].match(t):
        return [('text', forget(chat, m.group(1), path), None)]
    if m := RX['list_clear'].search(t):
        return [('text', list_clear(m.group(1), path), None)]
    if (m := RX['list_add_b'].match(t)) or (m2 := RX['list_add_a'].match(t)):
        name, items = (m.group(1), _items(m.group(2))) if m else (m2.group(2) or 'קניות', _items(m2.group(1)))
        before = _load(path)['lists'].get(_norm_list(name), [])
        said = list_add(name, items, path)
        new = [i for i in items if i not in before]            # only what this added comes off again
        if path is None and new:         # 19/11 undoable (agent/journal)
            from agent import journal
            journal.record(chat, 'list_add', ', '.join(new) + f' (רשימת {name})', {'list': name, 'items': new})
        return [('text', said, None)]
    if m := RX['list_show'].search(t):
        return [('text', list_show(m.group(1), path), None)]
    if (m := RX['list_remove'].match(t)) and (m.group(2) or _load(path)['lists']):
        name = m.group(2) or next((n for n, items in _load(path)['lists'].items()
                                   if any(i in m.group(1) or m.group(1) in i for i in items)), None)
        if name:
            return [('text', list_remove(name, _items(m.group(1)), path), None)]
    if RX['reminders_list'].search(t):
        return [('text', list_reminders(chat, path), None)]
    if m := RX['reminder_cancel'].search(t):
        return [('text', cancel_reminder(chat, m.group(1), path), None)]
    if RX['reminder'].search(t) and not RECALL.search(t):
        res = set_reminder(chat, t, path=path)
        if res is None:                                    # "תזכיר לי" with nothing to remind of
            return [('text', '🤔 על מה להזכיר?', None)] if ask_time else None
        return [res]
    if role == 'owner' and RX['email'].search(t):
        e = draft_email(chat, t, path=path)
        to = e.get('to') or '❓ (חסרה כתובת — שלח לי אותה)'
        body = f"✉️ *טיוטה*\nאל: {to}\nנושא: {e.get('subject', '')}\n\n{e.get('body', '')}"
        if not e.get('to'):
            return [('text', body + '\n\nלמי לשלוח? כתוב את כתובת המייל.', None), ('await_email_to', e['id'], None)]
        return [('buttons', body + '\n\nלשלוח?', [('📤 שלח', f"em:send:{e['id']}"), ('❌ בטל', f"em:no:{e['id']}")])]
    return None
