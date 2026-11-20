"""
agent/away.py — thinking ahead like a personal assistant: when the owner is away or busy, what has to be done before (20/11).

The owner 20/11: "תחשוב מראש בתור עוזר אישי". Only facts from his own data — nothing guessed:

    periods   his calendar (primary): an all-day event that says he's away or busy ("ים המלח עבודה", "קורס עבודה",
              מילואים, חופשה, טיסה…) · a hotel check-in → check-out pair · a hotel / flight booking he sent
              (agent/my_files: "הגעה 26/11/26, עזיבה 28/11/26") · what he said ("אני בים המלח מ-26 עד 29", kept in
              data/away.json; it wins over the rest for that place) · a saved fact with explicit dates.
              Pieces of one trip (same place, ≤1 day apart) are one period.
    unsure    the dates rest on a booking and a fact says he changed / cancelled it ("החליף את המלון…") → ONE question
              (the outbox's one-open-question rule decides when it goes out); the plan waits for it until 2 days before.
    plan      per period: every open deadline due before it ends + 2 days → "לסיים עד" the day before leaving (its own
              date when that's earlier), soonest first · the lectures / tutorials he'll miss (the university's site,
              without cancelled ones) → catch-up items after he's back, closed by "ראיתי את ההרצאה בתוכנה". Never a
              study question: only what to finish and what to watch.
    messages  (the worker's batch, agent/catchup and the budget apply) the question if unsure · ONE battle plan 1–5 days
              before leaving (10:00–21:00) · the day before leaving, what's still open · after he's back, what he missed.
    words     "אני בים המלח מ-26 עד 29" · "מה אני צריך לסיים לפני ים המלח?" · "מה אני צריך להשלים?" · "ראיתי את ההרצאה ב…".
The ladder (agent/deadline_ladder) says the earlier date on a deadline that falls inside a period. No LLM.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'away.json'
AHEAD_DAYS = 45
AFTER_DAYS = 2                      # a deadline this many days after he's back still has to be done before
PLAN_DAYS = (1, 5)                  # the battle plan: this many days before leaving
HOURS = (10, 21)
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
WD_SHORT = ['ב׳', 'ג׳', 'ד׳', 'ה׳', 'ו׳', 'ש׳', 'א׳']
MONTHS = {'ינואר': 1, 'פברואר': 2, 'מרץ': 3, 'מרס': 3, 'אפריל': 4, 'מאי': 5, 'יוני': 6, 'יולי': 7, 'אוגוסט': 8,
          'ספטמבר': 9, 'אוקטובר': 10, 'נובמבר': 11, 'דצמבר': 12}

PLACE_RX = re.compile(r'אילת|ים המלח|הצפון|צפון|גליל|גולן|חו["״]ל|אירופה|ארה["״]ב|לונדון|פריז|ברלין|רומא|אתונה|'
                      r'יוון|תאילנד|ניו יורק|מילואים|קורס\s+(?:מה)?עבודה|השתלמות|כנס')
AWAY_RX = re.compile(r'אילת|ים המלח|צפון|גליל|גולן|חו["״]ל|חופש|טיול|נופש|צימר|טיסה|נסיעה ל|מילואים|'
                     r'קורס\s+(?:מה)?עבודה|השתלמות|כנס|סופ["״]ש ב')
BUSY_RX = re.compile(r'קורס|מילואים|השתלמות|כנס')
SKIP_RX = re.compile(r'דדליין|ביטול|בוטל|אין לימודים|יום הולדת|יומולדת|חג|ערב חג|הגשה|מבחן|בוחן|✅|❌')
CHECKIN_RX = re.compile(r"צ['׳]?ק[\s\-]?אין|check[\s\-]?in", re.I)
CHECKOUT_RX = re.compile(r"צ['׳]?ק[\s\-]?אאוט|check[\s\-]?out", re.I)
HOTEL_RX = re.compile(r'מלון|הזמנה|booking|צימר|טיסה|hotel', re.I)
CHANGE_RX = re.compile(r'(?:החליף|החלפתי|ביטל|ביטלתי|שינה|שיניתי|הזיז|הזזתי)\S*\s+(?:את\s+)?(?:ה)?(?:מלון|הזמנה|טיסה|תאריכ)')
DATE2_RX = re.compile(r'(?:הגעה|צ.?ק.?אין|check.?in|מ-?)\D{0,6}(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?'
                      r'.{0,40}?(?:עזיבה|צ.?ק.?אאוט|check.?out|עד)\D{0,6}(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?', re.I | re.S)
_M = '|'.join(MONTHS)
STATED_RX = re.compile(r'^\s*(?:ו?(?:אני|אהיה|אנחנו|נהיה|יש\s+לי))\s+(?:טס\s+|נוסע\s+|יוצא\s+|נוסעים\s+|טסים\s+)?'
                       r'(?P<place>[^\d?]{2,28}?)\s+(?:מ-?|מה-?|בין\s+ה?-?)\s*(?P<a>\d{1,2})(?:[./](?P<am>\d{1,2}))?\s*'
                       r'(?:עד|ל|-|–|ו)\s*(?:ה-?|ל-?)?\s*(?P<b>\d{1,2})(?:[./](?P<bm>\d{1,2}))?'
                       rf'(?:\s*(?:ב|ל)?(?P<mw>{_M}))?\s*[.!]*\s*$')
VAGUE_RX = re.compile(r'^\s*(?:אני|אהיה|אנחנו|נהיה)\s+(?:טס\s+|נוסע\s+)?(?P<place>[בל][א-ת"״\' ]{2,20}?)\s+'
                      r'(?:ב?שבוע הבא|בסופ["״]ש|בסוף השבוע)\s*[.!]*\s*$')
BEFORE_RX = re.compile(r'^\s*(?:ו?מה|איזה|אילו|מה\s+עוד)\s.{0,30}?(?:לסיים|להגיש|לסגור|לעשות|להספיק)\s.{0,12}?לפני\s+'
                       r'(?:ש(?:אני\s+)?(?:טס|נוסע|יוצא|נוסעים)\s*(?:ל)?\s*)?(?:ה)?(?P<place>[א-ת"״\' ]{2,22}?)\s*[?!.]*\s*$')
CATCH_ASK_RX = re.compile(r'^\s*(?:ו?מה)\s+(?:אני\s+)?(?:צריך|נשאר\s+לי|עוד)\s+(?:לי\s+)?להשלים\s*(?:מההקלטות|מהשיעורים)?\s*[?!.]*\s*$')
CATCH_DONE_RX = re.compile(r'^\s*(?:ראיתי|צפיתי(?:\s+ב)?|השלמתי|שמעתי)\s+(?:את\s+)?(?:ה)?(?P<kind>הרצאות|הרצאה|תרגולים|תרגול|'
                           r'שיעורים|שיעור|הקלטה|הקלטות|הכול|הכל)(?P<rest>.{0,40}?)\s*[.!]*\s*$')


# ── periods (pure) ──────────────────────────────────────────────────────────

def _place(text: str) -> str:
    m = PLACE_RX.search(text or '')
    if not m:
        return ''
    p = re.sub(r'\s+', ' ', m.group(0))
    return {'צפון': 'הצפון', 'קורס מהעבודה': 'קורס עבודה'}.get(p, p)


def _span(ev: dict) -> tuple[date, date, bool] | None:
    st, en = ev.get('start') or {}, ev.get('end') or {}
    try:
        if 'date' in st:
            s = date.fromisoformat(st['date'])
            e = date.fromisoformat(en['date']) - timedelta(days=1) if en.get('date') else s
            return s, max(s, e), True
        s = datetime.fromisoformat(st['dateTime']).replace(tzinfo=None)
        e = datetime.fromisoformat(en.get('dateTime') or st['dateTime']).replace(tzinfo=None)
        return s.date(), e.date(), False
    except (KeyError, ValueError, TypeError):
        return None


def from_calendar(events: list[dict]) -> list[dict]:
    """[{place, kind, start, end, src, what}] from his own calendar's events."""
    out, ins, outs = [], [], []
    for ev in events or []:
        if ev.get('_cal', 'primary') != 'primary' or ev.get('_cal_kind') == 'lessons' or ev.get('status') == 'cancelled':
            continue
        sp = _span(ev)
        if not sp:
            continue
        s, e, allday = sp
        title = ev.get('summary') or ''
        where = f"{title} {ev.get('location') or ''}"
        if not allday:
            if CHECKIN_RX.search(title):
                ins.append((s, _place(where), title))
            elif CHECKOUT_RX.search(title):
                outs.append((s, _place(where), title))
            continue
        if SKIP_RX.search(title) or not AWAY_RX.search(title):
            continue
        out.append({'place': _place(where) or title.strip()[:30], 'kind': 'busy' if BUSY_RX.search(title) else 'trip',
                    'start': s, 'end': e, 'src': 'calendar', 'what': title.strip()[:60]})
    for s, pl, title in ins:
        o = next((x for x in sorted(outs) if x[0] >= s and (not pl or not x[1] or x[1] == pl)), None)
        if o and (o[0] - s).days <= 21:
            out.append({'place': pl or o[1] or 'מלון', 'kind': 'trip', 'start': s, 'end': o[0], 'src': 'hotel',
                        'what': re.sub(CHECKIN_RX, '', title).strip(' -—')[:60]})
    return out


def _year(d: int, m: int, y: str | None, today: date) -> date | None:
    try:
        if y:
            yy = int(y)
            return date(yy + 2000 if yy < 100 else yy, m, d)
        x = date(today.year, m, d)
        return x if x >= today - timedelta(days=60) else date(today.year + 1, m, d)
    except ValueError:
        return None


def from_files(records: list[dict], today: date) -> list[dict]:
    """A booking he sent (hotel / flight): its arrival and departure dates."""
    out = []
    for r in records or []:
        text = f"{r.get('name', '')}\n{r.get('summary', '')}"
        if not HOTEL_RX.search(text):
            continue
        m = DATE2_RX.search(text)
        if not m:
            continue
        a = _year(int(m.group(1)), int(m.group(2)), m.group(3), today)
        b = _year(int(m.group(4)), int(m.group(5)), m.group(6), today)
        if not a or not b or b < a or (b - a).days > 30:
            continue
        out.append({'place': _place(text) or 'מלון', 'kind': 'trip', 'start': a, 'end': b, 'src': 'booking',
                    'what': (r.get('name') or '')[:60]})
    return out


def parse_stated(text: str, today: date) -> dict | None:
    """"אני בים המלח מ-26 עד 29" → {place, kind, start, end}. The month: written, else the coming one."""
    m = STATED_RX.match(text or '')
    if not m:
        return None
    raw = m.group('place').strip()
    place = _place(raw) or re.sub(r'^(?:ב|ל)(?=[א-ת]{3})', '', raw).strip()
    if not place or len(place) > 24 or re.search(r'\d', place):
        return None
    mw = MONTHS.get(m.group('mw') or '')
    if not (_place(raw) or AWAY_RX.search(raw)) and not (mw or m.group('am') or m.group('bm')):
        return None              # "יש לי משמרת מ-16 עד 22" is hours, not a trip: an unknown place needs a month
    if re.search(r'משמרת|בעבודה|בבית|זמין|פנוי|בקמפוס|בשיעור|בתרגול', raw):
        return None
    a, b = int(m.group('a')), int(m.group('b'))
    am = int(m.group('am')) if m.group('am') else None
    bm = int(m.group('bm')) if m.group('bm') else None
    ma = am or (bm if bm and a <= b else None) or mw or (today.month if a >= today.day - 3 else today.month % 12 + 1)
    s = _year(a, ma, None, today)
    mb = bm or (ma if b >= a else ma % 12 + 1)
    e = _year(b, mb, None, today)
    if not s or not e:
        return None
    if e < s:
        e = _year(b, mb, str(s.year + 1), today) or e
    if e < s or (e - s).days > 60:
        return None
    return {'place': place, 'kind': 'busy' if BUSY_RX.search(raw) else 'trip', 'start': s, 'end': e,
            'src': 'owner', 'what': text.strip()[:80]}


def from_facts(facts: list[str], today: date) -> list[dict]:
    out = []
    for f in facts or []:
        for sent in re.split(r'[.;\n]', f):
            p = parse_stated(re.sub(r'^\s*המשתמש\s+(?:יהיה|נמצא|נוסע|טס)?\s*', 'אני ', sent.strip()), today)
            if p:
                out.append({**p, 'src': 'fact'})
    return out


def changed_booking(facts: list[str], place: str) -> str:
    """A fact that says the booking for this place changed ("המשתמש החליף את המלון בים המלח למלון הגלים") → it."""
    for f in facts or []:
        if CHANGE_RX.search(f) and (not place or place in f):
            return f
    return ''


def periods(today: date, events=(), records=(), facts=(), stated=()) -> list[dict]:
    """His away / busy periods that haven't ended, merged, soonest first. Pure."""
    raw = from_calendar(list(events)) + from_files(list(records), today) + from_facts(list(facts), today)
    said = [dict(s, start=date.fromisoformat(s['start']) if isinstance(s['start'], str) else s['start'],
                 end=date.fromisoformat(s['end']) if isinstance(s['end'], str) else s['end'], src='owner')
            for s in stated or []]
    said_places = {s['place'] for s in said}
    raw = [r for r in raw if r['place'] not in said_places] + said     # what he said wins for that place
    raw = [r for r in raw if r['end'] >= today and (r['start'] - today).days <= AHEAD_DAYS]
    raw.sort(key=lambda r: (r['start'], r['end']))
    merged: list[dict] = []
    for r in raw:
        m = next((x for x in merged if (x['place'] == r['place'] or 'מלון' in (x['place'], r['place']))
                  and r['start'] <= x['end'] + timedelta(days=1) and x['start'] <= r['end'] + timedelta(days=1)), None)
        if m:
            m['start'], m['end'] = min(m['start'], r['start']), max(m['end'], r['end'])
            m['parts'].append(r)
            if m['place'] == 'מלון':
                m['place'] = r['place']
            if r['kind'] == 'trip':
                m['kind'] = 'trip'
        else:
            merged.append({**r, 'parts': [r]})
    for m in merged:
        srcs = {p['src'] for p in m['parts']}
        m['pid'] = f"{m['place']}|{m['start'].isoformat()}"
        m['sure'], m['why'] = True, ''
        if 'owner' in srcs:
            continue
        firm_end = max((p['end'] for p in m['parts'] if p['src'] in ('calendar', 'fact')), default=None)
        firm_start = min((p['start'] for p in m['parts'] if p['src'] in ('calendar', 'fact')), default=None)
        rests_on_booking = firm_end is None or firm_end < m['end'] or firm_start > m['start']
        ch = changed_booking(list(facts), m['place'])
        if rests_on_booking and ch:
            m['sure'], m['why'] = False, ch
    return merged


# ── the plan (pure) ─────────────────────────────────────────────────────────

def _d(x: date) -> str:
    return f'{x:%d/%m}'


def _range(p: dict) -> str:
    s, e = p['start'], p['end']
    return _d(s) if s == e else (f'{s.day}–{e:%d/%m}' if s.month == e.month else f'{_d(s)}–{_d(e)}')


def _where(p: dict) -> str:
    pl = p['place']
    return pl if pl.startswith('ב') and not pl.startswith('ברלין') else f"ב{pl}"


def deadlines_for(p: dict, store: dict, now: datetime) -> list[dict]:
    """Open deadlines due from now until AFTER_DAYS after the period → [{rec, due, finish_by, before}]."""
    from agent import obligations as ob
    last = datetime.combine(p['end'] + timedelta(days=AFTER_DAYS), datetime.max.time())
    by = p['start'] - timedelta(days=1)
    out = []
    for r in ob.open_items(store, now):
        due = ob._dt(r.get('due'))
        if not due or ob.status(r, now) != 'open' or not now <= due <= last:
            continue
        out.append({'rec': r, 'due': due, 'finish_by': min(by, due.date()), 'before': due.date() <= by})
    return sorted(out, key=lambda x: x['due'])


def missed_for(p: dict, sessions_on) -> list[dict]:
    """The lectures / tutorials on his timetable during the period (cancelled ones left out)."""
    out = []
    d = p['start']
    while d <= p['end']:
        try:
            rows = sessions_on(d) or []
        except Exception:
            rows = []
        for s in rows:
            if (s.get('override') or {}).get('status') == 'cancelled':
                continue
            out.append({'id': f"{s['at']:%Y%m%d%H%M}|{s.get('ck') or ''}", 'ck': s.get('ck') or '',
                        'course': s.get('course') or '', 'kind': s.get('kind') or '', 'at': s['at'].isoformat(timespec='minutes'),
                        'done': ''})
        d += timedelta(days=1)
    return out


def _dl_line(x: dict, p: dict) -> str:
    from agent import obligations as ob
    r, due = x['rec'], x['due']
    what = f"{r.get('title', '')} — {ob._course_name(r.get('course', ''))}"
    when = f"{WD_SHORT[due.weekday()]} {_d(due.date())}" + (f" {due:%H:%M}" if (due.hour, due.minute) != (23, 59) else '')
    if x['before']:
        return f"{what} · מוגש {when} (לפני שאתה יוצא)"
    return f"{what} · מוגש {when} → *לסיים עד {WD_SHORT[x['finish_by'].weekday()]} {_d(x['finish_by'])}*"


def _missed_lines(missed: list[dict]) -> list[str]:
    out = []
    for m in missed:
        at = datetime.fromisoformat(m['at'])
        out.append(f"• {WD_SHORT[at.weekday()]} {_d(at.date())} {at:%H:%M} — {m['kind']} ב{m['course']}")
    return out


def plan_text(ps: list[dict], store: dict, now: datetime, sessions_on, head: str = '') -> str:
    """The battle plan for one or more periods close together: what to finish, by when, soonest first; what he'll
    miss. Pure (given sessions_on)."""
    parts = [head] if head else []
    for i, p in enumerate(ps):
        label = f"🧳 *{_where(p)} {_range(p)}*" if p['kind'] == 'trip' else f"💼 *{p['place']} {_range(p)}*"
        if not p.get('sure'):
            label += ' _(התאריכים לפי היומן וההזמנה — עוד לא אישרת)_'
        lines = [label]
        dls = deadlines_for(p, store, now)
        if i:                                              # already listed under the earlier period
            seen = {x['rec']['id'] for q in ps[:i] for x in deadlines_for(q, store, now)}
            dls = [x for x in dls if x['rec']['id'] not in seen]
        if dls:
            lines.append(f"לסיים לפני שאתה יוצא (יום {WEEKDAY_HE[p['start'].weekday()]} {_d(p['start'])}):")
            lines += [f"{n}. {_dl_line(x, p)}" for n, x in enumerate(dls, 1)]
        else:
            lines.append('✅ אין הגשה שנופלת עליה או מיד אחריה.')
        missed = missed_for(p, sessions_on)
        if missed:
            lines.append('🎥 שיעורים שתפספס (להשלים מההקלטות אחרי):')
            lines += _missed_lines(missed)
        parts.append('\n'.join(lines))
    if len(ps) > 1:
        gap = (ps[1]['start'] - ps[0]['end']).days - 1
        if 0 <= gap <= 2:
            free = [ps[0]['end'] + timedelta(days=k + 1) for k in range(gap)]
            parts.append('⚠️ בין לבין יש לך רק ' + (' ו-'.join(f"{WEEKDAY_HE[d.weekday()]} {_d(d)}" for d in free)
                                                   if free else 'אפס ימים') + ' — עדיף לסגור הכול לפני שאתה יוצא.')
    return '\n\n'.join(parts)


def clusters(ps: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    for p in ps:
        if out and (p['start'] - out[-1][-1]['end']).days <= AFTER_DAYS + 1:
            out[-1].append(p)
        else:
            out.append([p])
    return out


def notes_for(ps: list[dict], store: dict, now: datetime) -> dict[str, str]:
    """{obligation id: the line the ladder adds} for deadlines inside a period. Pure."""
    out = {}
    for p in ps:
        for x in deadlines_for(p, store, now):
            if x['rec']['id'] in out or x['before'] or x['finish_by'] < now.date():
                continue
            out[x['rec']['id']] = (f"🧳 אתה {_where(p)} {_range(p)} → לסיים עד יום {WEEKDAY_HE[x['finish_by'].weekday()]} "
                                   f"{_d(x['finish_by'])}.")
    return out


# ── live ────────────────────────────────────────────────────────────────────

def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    d.setdefault('stated', [])
    d.setdefault('periods', {})
    return d


def _save(d: dict, path: Path | None = None) -> None:
    p = path or STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    os.replace(tmp, p)


_cache: dict = {'at': None, 'events': []}


def _events(now: datetime) -> list[dict]:
    if _cache['at'] and now - _cache['at'] < timedelta(minutes=30) and _cache['at'] <= now:
        return _cache['events']
    from integrations import gcal
    if not gcal.configured():
        return []
    svc = gcal._service()
    tz = gcal._tz()
    t0 = datetime(now.year, now.month, now.day, tzinfo=tz) - timedelta(days=2)
    items = svc.events().list(calendarId='primary', timeMin=t0.isoformat(),
                              timeMax=(t0 + timedelta(days=AHEAD_DAYS + 2)).isoformat(),
                              singleEvents=True, orderBy='startTime', maxResults=250).execute().get('items') or []
    items = [dict(it, _cal='primary') for it in items]
    _cache.update(at=now, events=items)
    return items


def current(chat: str, now: datetime | None = None, path: Path | None = None) -> list[dict]:
    """His periods now, from the live sources (a source that fails is just empty)."""
    now = now or datetime.now()
    try:
        events = _events(now)
    except Exception as e:
        print(f'  ⚠️ away calendar: {e!r}'[:200])
        events = []
    try:
        from agent import my_files
        records = my_files.records(chat)
    except Exception:
        records = []
    try:
        from agent import life
        facts = life.facts(chat)
    except Exception:
        facts = []
    st = _load(path)
    ps = periods(now.date(), events, records, facts, [s for s in st['stated'] if s.get('chat') in (None, chat)])
    for p in ps:
        if (st['periods'].get(p['pid']) or {}).get('confirmed'):
            p['sure'] = True
    return ps


def sessions_on(day: date) -> list[dict]:
    from agent import fastpath
    return fastpath.sessions(datetime.combine(day, datetime.min.time()), day)


def finish_notes(store: dict, now: datetime) -> dict[str, str]:
    """For agent/deadline_ladder: the earlier date on deadlines inside a period."""
    from whatsapp import outbox
    chat = outbox.owner_chat()
    return notes_for(current(chat, now), store, now) if chat else {}


def _ask(p: dict, now: datetime) -> tuple[str, list]:
    from agent import proactive
    st = proactive._load()
    btns = proactive._buttons({'kind': 'trip', 'offer': {'title': f"{p['place']} {_range(p)}", 'pid': p['pid']},
                               'opts': [(f"✅ כן, {_range(p)}", 'awy'), ('✏️ תאריכים אחרים', 'awn')]}, st, now)
    proactive._save(st, None, now)
    pieces = ', '.join(f"{q['what']} {_range(q)}" for q in p['parts'][:3])
    text = (f"🧳 לפי היומן וההזמנה אתה {_where(p)} {_range(p)} ({pieces}).\n"
            f"שמרתי גם ש\"{p['why'][:80]}\" — התאריכים נשארו אותו דבר?")
    return text, [b for b in btns if not b[1].startswith('sec:mute:')]


def tick(now: datetime | None = None, chat: str | None = None, path: Path | None = None, ps=None, store=None,
         sessions=None) -> list[tuple[str, object]]:
    """→ at most one message: the one question, the battle plan, the eve reminder, or what he missed. Never raises."""
    try:
        from agent.core import Out
        from agent import obligations as ob
        now = now or datetime.now()
        if not HOURS[0] <= now.hour < HOURS[1]:
            return []
        if chat is None:
            from whatsapp import outbox
            chat = outbox.owner_chat()
        if not chat:
            return []
        ps = current(chat, now, path) if ps is None else ps
        store = ob.load() if store is None else store
        sessions = sessions or sessions_on
        st = _load(path)
        today = now.date()
        out = None
        for group in clusters(ps):
            first = group[0]
            s = st['periods'].setdefault(first['pid'], {})
            left = (first['start'] - today).days
            unsure = [p for p in group if not p['sure'] and not (st['periods'].get(p['pid']) or {}).get('asked')]
            if unsure and 1 <= left <= AHEAD_DAYS and left <= 10:
                p = unsure[0]
                text, btns = _ask(p, now)
                st['periods'].setdefault(p['pid'], {})['asked'] = now.isoformat(timespec='minutes')
                out = Out('buttons', text, buttons=btns)
                break
            waiting = any(not p['sure'] for p in group) and left > 2
            if PLAN_DAYS[0] <= left <= PLAN_DAYS[1] and not s.get('plan_sent') and not waiting:
                head = f"🗺️ *תוכנית קרב* — יוצא בעוד {left} ימים" if left > 1 else '🗺️ *תוכנית קרב* — יוצא מחר'
                out = Out('text', plan_text(group, store, now, sessions, head))
                s['plan_sent'] = now.isoformat(timespec='minutes')
                if left == 1:
                    s['eve_sent'] = s['plan_sent']
                break
            if left == 1 and not s.get('eve_sent'):
                dls = deadlines_for(first, store, now)
                s['eve_sent'] = now.isoformat(timespec='minutes')
                if dls:
                    out = Out('text', f"🧳 מחר אתה {_where(first)}. עוד פתוח לסיים היום:\n"
                                      + '\n'.join(f"• {_dl_line(x, first)}" for x in dls))
                    break
        if out is None:                                    # back: what he missed
            for pid, s in st['periods'].items():
                if s.get('back_sent') or not s.get('end') or s.get('missed') is None:
                    continue
                if date.fromisoformat(s['end']) < today:
                    s['back_sent'] = now.isoformat(timespec='minutes')
                    todo = [m for m in s['missed'] if not m.get('done')]
                    if todo:
                        out = Out('text', f"🎥 *חזרת — מה פספסת ב{s.get('place', '')}:*\n" + '\n'.join(_missed_lines(todo))
                                  + '\n\nההקלטות בפנופטו. כשראית: "ראיתי את ההרצאה ב<קורס>".')
                        break
        for p in ps:                                       # the catch-up list is kept from the timetable as it was
            s = st['periods'].setdefault(p['pid'], {})
            s.update(place=p['place'], start=p['start'].isoformat(), end=p['end'].isoformat())
            if p['start'] <= today + timedelta(days=PLAN_DAYS[1]) and s.get('missed') is None and p['sure']:
                s['missed'] = missed_for(p, sessions)
        _save(st, path)
        return [(chat, out)] if out else []
    except Exception as e:
        print(f'  ⚠️ away tick: {e!r}'[:200])
        return []


def confirm(chat: str, offer: dict, yes: bool, now: datetime | None = None, path: Path | None = None) -> str:
    """The answer to the one question (agent/proactive.answer, ops awy / awn)."""
    now = now or datetime.now()
    pid = offer.get('pid') or ''
    st = _load(path)
    s = st['periods'].setdefault(pid, {})
    if not yes:
        s['denied'] = now.isoformat(timespec='minutes')
        _save(st, path)
        place = pid.split('|')[0] or 'שם'
        return f'✏️ כתוב לי את התאריכים, למשל: "אני ב{place} מ-26 עד 29".'
    s['confirmed'] = now.isoformat(timespec='minutes')
    _save(st, path)
    ps = [p for p in current(chat, now, path) if p['pid'] == pid]
    if not ps:
        return '👍 רשמתי.'
    from agent import obligations as ob
    return '👍 רשמתי.\n\n' + plan_text(ps, ob.load(), now, sessions_on)


def route(text: str, chat: str, now: datetime | None = None, path: Path | None = None) -> str | None:
    now = now or datetime.now()
    t = (text or '').strip()
    if not t or len(t) > 120:
        return None
    from agent import obligations as ob
    p = parse_stated(t, now.date())
    if p:
        st = _load(path)
        st['stated'] = [s for s in st['stated'] if not (s.get('place') == p['place'] and s.get('chat') == chat)]
        st['stated'].append({**p, 'start': p['start'].isoformat(), 'end': p['end'].isoformat(), 'chat': chat,
                             'ts': now.isoformat(timespec='minutes')})
        _save(st, path)
        ps = [q for q in current(chat, now, path) if q['place'] == p['place'] and q['start'] == p['start']]
        body = plan_text(ps or [{**p, 'parts': [p], 'sure': True, 'pid': ''}], ob.refresh(now, chat), now, sessions_on)
        return f"✅ רשמתי: אתה {_where(p)} {_range(p)}.\n\n" + body
    m = VAGUE_RX.match(t)
    if m:
        place = _place(m.group('place')) or m.group('place')[1:].strip()
        return f"🧳 רשמתי לעצמי. מאיזה יום עד איזה יום אתה ב{place}? (למשל: \"אני ב{place} מ-26 עד 29\")"
    m = BEFORE_RX.match(t)
    if m:
        ps = current(chat, now, path)
        if not ps:
            return '🧳 לא מצאתי ביומן או בקבצים שלך נסיעה או תקופה עמוסה ב-45 הימים הקרובים. מתי אתה יוצא? ("אני בים המלח מ-26 עד 29")'
        want = _place(m.group('place')) or m.group('place').strip()
        hit = [p for p in ps if want and (want in p['place'] or p['place'] in want)]
        if not hit and not re.search(r'נסיע|טיס|חופש|טיול|יוצא|נוסע|קורס|מילואים', m.group(0)):
            return None                                    # "לפני השיעור" / "לפני המבחן" — not a trip
        group = next((g for g in clusters(ps) if hit and hit[0] in g), None) or clusters(ps)[0]
        return plan_text(group, ob.refresh(now, chat), now, sessions_on)
    if CATCH_ASK_RX.match(t):
        return catchup_text(path)
    m = CATCH_DONE_RX.match(t)
    if m:
        return close_catchup(m.group('kind'), m.group('rest'), now, path)
    return None


def _open_catchups(st: dict) -> list[tuple[str, dict]]:
    return [(pid, m) for pid, s in st['periods'].items() for m in s.get('missed') or []
            if not m.get('done') and datetime.fromisoformat(m['at']) < datetime.now()]


def catchup_text(path: Path | None = None) -> str:
    todo = _open_catchups(_load(path))
    if not todo:
        return '✅ אין לך שיעורים להשלים מתקופות שהיית בחוץ.'
    return '🎥 *להשלים מההקלטות:*\n' + '\n'.join(_missed_lines([m for _, m in todo]))


def close_catchup(kind: str, rest: str, now: datetime, path: Path | None = None) -> str | None:
    st = _load(path)
    todo = _open_catchups(st)
    if not todo:
        return None
    from agent import deadline_ladder
    ck = deadline_ladder._course_of(rest or '')
    every = kind in ('הכול', 'הכל')
    k = '' if every or kind.startswith(('שיעור', 'הקלט')) else 'תרגיל' if kind.startswith('תרגול') else 'שיעור'
    hits = [m for _, m in todo if (every or not ck or m['ck'] == ck) and (not k or k in m['kind'])]
    if not hits or (not every and not ck and len({m['ck'] for m in hits}) > 1):
        return None
    many = every or kind in ('הרצאות', 'תרגולים', 'שיעורים', 'הקלטות')
    hits = sorted(hits, key=lambda m: m['at'])
    for m in hits if many else hits[:1]:
        m['done'] = now.isoformat(timespec='minutes')
    _save(st, path)
    left = len(_open_catchups(st))
    done = hits if many else hits[:1]
    return (f"✅ סימנתי: {', '.join(m['kind'] + ' ב' + m['course'] + ' ' + m['at'][8:10] + '/' + m['at'][5:7] for m in done)}."
            + (f" נשארו {left} להשלים." if left else ' השלמת הכול 👏'))
