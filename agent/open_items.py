"""
agent/open_items.py — what of the owner's own is still open, and closing it on his own plain words (15/11).

15/11 16:07 "ביטלתי את המלון", "אני ביטלתי", "זה סגור אני ביטלתי": the bot's own 09:00 reminder "בטל את המלון הקודם
בטבריה" was open, the bot itself had asked about it at 13:03, and all three got "לא ביצעתי את זה, כי לא הייתי בטוח שזו
בקשה שלך". The mind's gate reads 'open' as "declined something", and a report that it's done is not a decline.
Closing his own reminder / task on his own first-person past-tense statement is not an outward action — nothing is
sent, booked or paid — so it needs no ✅. 13:05 "ביקשתי מהם במייל ביטול, מחכה לעדכון" is not done: it's waiting.
No LLM here:

    items()   his open things: reminders (fired in the last day, or still to fire), reminder questions still open,
              open tasks
    said()    'done' | 'waiting' | None
    pick()    the item the words are about: shared words; with no words of its own ("אני ביטלתי", "זה סגור"), the only
              item the bot raised in the last day. Two equally good → the caller asks, with a button per item.
    close() / wait()  through what exists: life.reminder_action / tasks.action + closure.record + life.close_matching +
              now_note. A waiting item comes back tomorrow at 09:00 as a check, never as a refusal.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

RAISED_HOURS = 24
# "ביקשתי מהם ביטול, מחכה לעדכון" (and the 15/11 typo "מחאה לעדכון"): sent, not done
WAIT_RX = re.compile(r'(?<![א-ת])ו?(?:מחכה|מחכים|ממתין|ממתינה|ממתינים|אחכה|נחכה|מחאה\s+ל(?:עדכון|תשובה|אישור)|'
                     r'עוד\s+לא\s+(?:ענו|חזרו|אישרו|ענה|ענתה|חזר)|טרם\s+(?:ענו|אישרו)|'
                     r'ביקשתי\s+(?:מהם|ממנו|ממנה|מהמלון|מה)|פניתי\s+(?:אליהם|אליו|אליה|ל))(?![א-ת])')
# first person past, his own doing ("ביטלתי", "החזרתי"), or the plain "זה סגור" / "בוצע"
DONE_RX = re.compile(r'(?<![א-ת])(?:ש?(?:ביטלתי|בטלתי|החזרתי|שילמתי|עשיתי|סיימתי|סגרתי|מילאתי|נרשמתי|הבאתי|אספתי|הגשתי|'
                     r'טיפלתי|קניתי|הזמנתי|חידשתי|קבעתי|התקשרתי|דיברתי|העברתי|החלפתי)|בוצע|טופל|סודר|הסתדר|'
                     r'(?:זה\s+)?סגור|נסגר|בוטל|התבטל)(?![א-ת])')
# with no words naming the thing, only a plain report closes the one item the bot raised: "אני ביטלתי", "זה סגור", "בוצע"
BARE_RX = re.compile(r'(?<![א-ת])(?:[א-ת]{2,}תי|זה\s+(?:סגור|נסגר|בוטל|טופל|בוצע|הסתדר)|בוצע|טופל)(?![א-ת])|'
                     r'(?:מחכה|ממתין|ביקשתי)')
NOT_RX = re.compile(r'(?<![א-ת])(?:לא|טרם|אם|כש|לפני|אחרי|האם|איך|מתי|צריך\s+ל|אפשר\s+ל)\s+(?:כבר\s+)?$')
ASK_RX = re.compile(r'\?\s*$|^\s*(?:תזכיר|תוסיף|תקבע|תשלח|תגיד|תכתוב|תבדוק|תמחק|תבטל|תיצור|תפנה|מה|איך|למה|האם|מתי|איפה|כמה|מי)'
                    r'(?![א-ת])')
FILLER = re.compile(r'(?<![א-ת])(?:אני|כבר|זה|את|גם|הרגע|עכשיו|היום|אתמול|בסוף|תכלס|אוקיי|טוב|נו|אחי|וואלה|היי|'
                    r'מעדכן|אותך|לך|רק|שאני|בקשר|לגבי|בסדר|סבבה|יאללה|הכל|הכול|ממש|לגמרי|מזמן|תודה)(?![א-ת])')


def said(text: str) -> str | None:
    """'waiting' | 'done' | None (a question, a request, "עוד לא ביטלתי", a long message about something else)."""
    t = (text or '').strip()
    if not t or len(t) > 140 or ASK_RX.search(t):
        return None
    if WAIT_RX.search(t):
        return 'waiting'
    for m in DONE_RX.finditer(t):
        if not NOT_RX.search(t[:m.start()]):
            return 'done'
    return None


_FINAL = str.maketrans('םןץףך', 'מנצפכ')
_PRE1, _PRE2 = 'הובלשמכ', ('וה', 'שה', 'לה', 'בה', 'מה', 'וב', 'ול', 'וש', 'כש', 'של')


def _norm(w: str) -> str:
    """'ביטלתי' / 'ביטול' / 'בטל' → 'בטל'; 'מלון' / 'מלונות' → 'מלנ'. closure._stem is cruder on purpose (for long mails),
    and there 'מלון' and 'מייל' meet."""
    if len(w) > 4:
        w = re.sub(r'(?:תי|נו|ים|ות|יה|תם)$', '', w)
    return (w[:1] + re.sub(r'[וי]', '', w[1:]))


def _forms(word: str) -> set[str]:
    w = re.sub(r'["\'׳״]', '', word).translate(_FINAL)
    out = {w}
    if len(w) > 3 and w[0] in _PRE1:
        out.add(w[1:])
    if len(w) > 4 and w[:2] in _PRE2:
        out.add(w[2:])
    return {f for f in map(_norm, out) if len(f) >= 3}


_GENERIC = set().union(*(_forms(w) for w in (
    'לשלם לשלוח למלא לאשר להירשם לרשום לענות להשיב לחתום לעדכן להסדיר לבחור להגיש לתאם לקבוע לבדוק לעשות טופס בקשה '
    'סקר תשלום אישור שאלון קורס הרצאה שיעור תרגיל מטלה מייל הודעה משהו דבר').split()))


def _words(text: str) -> list[set[str]]:
    return [f for f in (_forms(w) for w in re.findall(r'[א-ת"\'׳״]{3,}', text or '')) if f]


def _shared(a: str, b: str) -> int:
    """How many words of `a` name something in `b` (a generic word alone — "תרגיל", "טופס" — names nothing)."""
    wb = set().union(*_words(b)) if _words(b) else set()
    hits = [f for f in _words(a) if f & wb]
    return len(hits) if any(f - _GENERIC for f in hits) else 0


def _content(text: str) -> str:
    """The words that name the thing: no verb of being done or waiting, no filler."""
    t = DONE_RX.sub(' ', WAIT_RX.sub(' ', text or ''))
    return FILLER.sub(' ', t)


def _sent_recently(chat: str, hours: float = RAISED_HOURS) -> list[str]:
    try:
        from whatsapp import outbox
        return [r.get('text') or '' for r in outbox.recent_sent(chat, hours=hours, n=40)]
    except Exception:
        return []


def items(chat: str, now: datetime | None = None, life_path: Path | None = None, tasks_path: Path | None = None,
          options_path: Path | None = None) -> list[dict]:
    """[{'kind': 'reminder'|'task', 'id', 'text', 'raised': bool}] — his, still open. 'raised' = the bot brought it up in the
    last day (it fired, it was asked about, a note named it)."""
    from agent import closure, life, tasks
    now = now or datetime.now()
    lo = (now - timedelta(hours=RAISED_HOURS)).isoformat(timespec='minutes')
    hi = now.isoformat(timespec='minutes')
    closed = closure.items(chat, path=closure.path_beside(tasks_path) if tasks_path else None, tasks_path=tasks_path)
    asked = set()
    try:
        from whatsapp import outbox
        for o in outbox.open_questions(chat, options_path):
            for p in o.get('payloads') or []:
                if str(p).startswith(('rmd:done:', 'task:done:')):
                    asked.add(str(p).split(':', 2)[2])
    except Exception:
        pass
    sent = _sent_recently(chat) if options_path is None else []
    out: list[dict] = []
    for r in life._load(life_path)['reminders']:
        if r.get('chat') != chat or r.get('repeat', 'none') != 'none':
            continue
        fired = any(lo <= str(r.get(k) or '') <= hi for k in ('due', 'nag_at', 'waiting'))
        if not (r.get('active') or fired or r.get('id') in asked) or r.get('closed'):
            continue
        if closure.match(closed, r.get('text', '')) and r.get('id') not in asked:
            continue
        raised = fired or r.get('id') in asked or any(r.get('text', '') and r['text'] in s for s in sent)
        out.append({'kind': 'reminder', 'id': r['id'], 'text': r.get('text', ''), 'raised': bool(raised)})
    for t in tasks.open_tasks(chat, tasks_path):
        raised = t['id'] in asked or any(t['text'] in s for s in sent)
        out.append({'kind': 'task', 'id': t['id'], 'text': t['text'], 'raised': bool(raised)})
    return out


def pick(text: str, found: list[dict]) -> list[dict]:
    """The items `text` is about, best first: [] none, [one] that one, [a, b…] a tie (ask which)."""
    from agent import closure
    content = _content(text)
    if _words(content):
        nums = closure._nums(content)

        def fits(i: dict) -> int:                        # "עשיתי טעות בתרגיל 3" is not "להגיש תרגיל 3": a generic word
            other = closure._nums(i['text'])             # alone names nothing; nor is "תרגיל 4" "תרגיל 3"
            return 0 if nums and other and not nums & other else _shared(content, i['text'])
        score = {id(i): fits(i) for i in found}
        hits = [i for i in found if score[id(i)]]
        if not hits:
            return []
        top = max(score[id(i)] for i in hits)
        hits = [i for i in hits if score[id(i)] == top]
        if len(hits) > 1:                                # a tie: the one the bot just raised, if only one was
            raised = [i for i in hits if i['raised']]
            hits = raised if len(raised) == 1 else hits
        return hits
    raised = [i for i in found if i['raised']]
    return raised                                        # one → that; several → ask; none → not ours


def close(chat: str, item: dict, life_path: Path | None = None, tasks_path: Path | None = None) -> str:
    """His own item, closed the way a ✅ press would, and remembered as closed everywhere."""
    from agent import closure, life, tasks
    if item['kind'] == 'task':
        return tasks.action(chat, 'done', item['id'], path=tasks_path)
    d = life._load(life_path)
    rec = next((r for r in d['reminders'] if r.get('id') == item['id'] and r.get('chat') == chat), None)
    if rec:
        rec['active'] = False
        rec.pop('nag_at', None)
        rec['closed'] = datetime.now().isoformat(timespec='minutes')
        life._save(d, life_path)
    closure.record(chat, item['text'], 'done', path=closure.path_beside(tasks_path) if tasks_path else None)
    if life_path is None:
        life.close_matching(chat, item['text'])
        try:
            from agent import now_note
            now_note.add(chat, f"סגור, לא להזכיר שוב: {item['text']}", 'decision')
        except Exception as e:
            print(f'  ⚠️ open item note: {e!r}'[:160])
    _close_question(chat, item)
    return f"✅ סגרתי: {item['text']}. לא אזכיר יותר."


def _close_question(chat: str, item: dict) -> None:
    """Its numbered question (⏰ … 1️⃣ ✅ בוצע) is answered too: it mustn't come back as "עוד שאלה פתוחה"."""
    try:
        from whatsapp import outbox
        for n, o in enumerate(outbox.open_questions(chat), 1):
            if any(str(p).endswith(':' + item['id']) for p in o.get('payloads') or []):
                outbox.close_open(chat, n)
                return
    except Exception as e:
        print(f'  ⚠️ open item question: {e!r}'[:160])


def wait(chat: str, item: dict, now: datetime | None = None, life_path: Path | None = None) -> tuple[str, list]:
    """"ביקשתי מהם, מחכה לתשובה": not closed — it comes back tomorrow at 09:00 as a check. → (text, buttons)."""
    from agent import life
    now = now or datetime.now()
    at = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    d = life._load(life_path)
    rec = next((r for r in d['reminders'] if r.get('id') == item['id'] and r.get('chat') == chat), None) \
        if item['kind'] == 'reminder' else None
    if rec:
        rec.update(active=True, nag_at=at.isoformat(timespec='minutes'), waiting=now.isoformat(timespec='minutes'))
        life._save(d, life_path)
        rid = rec['id']
    else:                                                # a task: a one-off check tomorrow, in his words of the task
        rec = life.add_reminder(chat, {'text': f"לבדוק אם ענו: {item['text']}", 'when': at.isoformat(timespec='minutes')},
                                life_path)
        rid = rec['id']
    _close_question(chat, item)
    return (f"👍 הבנתי, ביקשת ואתה מחכה לתשובה. לא סוגר עדיין: אזכיר לך מחר ב-09:00 לבדוק אם ענו לגבי "
            f"*{item['text']}*.",
            [('👍 סבבה', 'noop:r'), ('✅ כבר נסגר', f"oi:done:{item['kind'][0]}:{item['id']}"),
             ('🗑️ לא צריך תזכורת', f'rmd:cancel:{rid}')])


# 16/11 a follow-up he didn't have a reminder for: "שלחתי מייל למרצה ומחכה לתשובה", "ביקשתי החזר, מחכה לאישור". Both a
# request he made and a reply he's waiting for: "מחכה לך בבית" / "מחכה לשיעור" aren't.
TRACK_RX = re.compile(r'(?<![א-ת])(?:שלחתי|ביקשתי|פניתי|כתבתי|התקשרתי|הגשתי\s+בקשה|השארתי\s+הודעה|הזמנתי|ערערתי)(?![א-ת])')
TRACK_WAIT_RX = re.compile(r'(?<![א-ת])(?:מחכה|ממתין|ממתינה|מחאה|לתשובה|לאישור|לעדכון|להחזר|שיחזרו|שיענו|יענו|יחזרו)(?![א-ת])')
TRACK_DAYS = 2
TRACK_PREFIX = 'לבדוק אם ענו: '


def track(chat: str, text: str, now: datetime | None = None, life_path: Path | None = None) -> tuple[str, list]:
    """His own words become a check in two days at 10:00 (nothing goes out; a mail that looks like the answer closes the
    loop earlier — agent/core.mail_tick → answered()). → (text, buttons)."""
    from agent import life
    now = now or datetime.now()
    at = (now + timedelta(days=TRACK_DAYS)).replace(hour=10, minute=0, second=0, microsecond=0)
    what = re.sub(r'\s+', ' ', text).strip(' .!')[:120]
    rec = life.add_reminder(chat, {'text': TRACK_PREFIX + what, 'when': at.isoformat(timespec='minutes')}, life_path)
    d = life._load(life_path)
    for r in d['reminders']:
        if r.get('id') == rec['id']:
            r['waiting'] = now.isoformat(timespec='minutes')
    life._save(d, life_path)
    day = 'מחרתיים' if TRACK_DAYS == 2 else f'בעוד {TRACK_DAYS} ימים'
    return (f"👍 רשמתי שאתה מחכה לתשובה. אם לא אראה שהגיעה, אבדוק איתך {day} ב-10:00.",
            [('👍 סבבה', 'noop:r'), ('🗑️ לא צריך', f"rmd:cancel:{rec['id']}")])


def waiting(chat: str, life_path: Path | None = None) -> list[dict]:
    """What he's waiting on: open reminders marked waiting (his "מחכה לתשובה")."""
    from agent import life
    return [r for r in life._load(life_path)['reminders']
            if r.get('chat') == chat and r.get('active') and r.get('waiting') and not r.get('closed')]


def answered(chat: str, mails: list[dict], life_path: Path | None = None) -> list[tuple[dict, dict]]:
    """[(waiting reminder, the mail that looks like its answer)]: two words that name the thing (not generic) shared by
    the reminder and the mail's sender / subject / summary. Each reminder once."""
    out, used = [], set()
    for r in waiting(chat, life_path):
        what = _content(r.get('text', '').replace(TRACK_PREFIX, ''))
        want = [f for f in _words(what) if f - _GENERIC]  # one entry per word (its forms), generic words out
        if len(want) < 2:
            continue
        for m in mails:
            body = ' '.join(str(m.get(k) or '') for k in ('from', 'subject', 'summary', 'task'))
            got = set().union(*_words(body)) if _words(body) else set()
            if sum(1 for f in want if f & got) >= 2 and r['id'] not in used:
                out.append((r, m))
                used.add(r['id'])
                break
    return out


def button(chat: str, payload: str) -> str:
    """oi:done:r:<id> / oi:done:t:<id> (a press is his explicit choice) · oi:other."""
    parts = payload.split(':')
    if len(parts) < 4 or parts[1] != 'done':
        return '👍 אז תכתוב לי במילים על מה מדובר, ואטפל.'
    kind = {'r': 'reminder', 't': 'task'}.get(parts[2])
    found = [i for i in items(chat) if i['kind'] == kind and i['id'] == parts[3]]
    if not found:
        from agent import life
        rec = next((r for r in life._load()['reminders'] if r.get('id') == parts[3] and r.get('chat') == chat), None)
        if kind == 'reminder' and rec:
            found = [{'kind': 'reminder', 'id': rec['id'], 'text': rec.get('text', ''), 'raised': True}]
    return close(chat, found[0]) if found else '👍 כבר סגור.'


def clarify(chat: str, found: list[dict], text: str = '') -> tuple[str, list] | None:
    """15/11: the same refusal 4 times. Instead: which of his open things is this about — a button each."""
    shown = [i for i in found if i.get('text')][:3]
    if not shown:
        return None
    btns = [(f"{i['text'][:28]}", f"oi:done:{i['kind'][0]}:{i['id']}") for i in shown] + [('משהו אחר', 'oi:other')]
    lead = 'רק לוודא, על מה מדובר?' if said(text) != 'done' else 'סבבה. מה סגרת?'
    return lead, btns


def handle(chat: str, text: str, now: datetime | None = None) -> tuple[str, str, list] | None:
    """The whole thing for core: → ('text'|'buttons', body, buttons) or None (not a status report of his)."""
    how = said(text)
    if not how:
        return None
    try:
        found = items(chat, now)
    except Exception as e:
        print(f'  ⚠️ open items: {e!r}'[:200])
        return None
    if not _words(_content(text)) and not BARE_RX.search(text):
        return None                                      # a bare "סגור" / "סבבה סגור" is slang for "deal", not "done"
    hits = pick(text, found)
    if not hits and how == 'done':                       # said again after it closed: not a refusal, not a new close
        try:
            from agent import closure
            lo = ((now or datetime.now()) - timedelta(hours=RAISED_HOURS)).isoformat(timespec='minutes')
            recent = [c for c in closure.items(chat) if str(c.get('ts', '')) >= lo]
            again = pick(text, [{'kind': 'closed', 'id': '', 'text': c.get('text', ''), 'raised': True} for c in recent])
            if len(again) == 1:
                return 'text', f"👍 כבר סגור אצלי: {again[0]['text']}. לא אזכיר.", []
        except Exception as e:
            print(f'  ⚠️ open items (closed): {e!r}'[:160])
    if not hits and how == 'waiting' and TRACK_RX.search(text) and TRACK_WAIT_RX.search(text):
        body, btns = track(chat, text, now)              # 16/11 nothing of his was open: a new thing to follow up
        return 'buttons', body, btns
    if not hits:
        return None
    if len(hits) > 1:
        c = clarify(chat, hits, text)
        return ('buttons', c[0], c[1]) if c else None
    if how == 'waiting':
        body, btns = wait(chat, hits[0], now)
        return 'buttons', body, btns
    return 'text', close(chat, hits[0]), []
