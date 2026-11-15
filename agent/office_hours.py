"""
agent/office_hours.py — the lecturer's Zoom office hours → the owner's calendar, and a reminder only when he's free (02/11).

The owner: "תוסיף לי את זה לאוטומציה שיוסיף לי את השעות פגישה ואז תזכורת שאם אני פנוי אצטרף".
Naftali (probability) posts his next meetings — date · day · hour · Zoom link — on MEETINGS, at different hours each
time, a day or two ahead. Every 2 hours (09–22) the page is read:
    new meeting            → added to the primary calendar (tagged, 1 hour, the Zoom link in the description) — the owner
                             asked for exactly this, so no question; one short line says what was added
    hour / day changed     → moved
    gone before it happened → marked "❌ בוטל —" (never deleted)
The reminder is agent/event_reminders: 30 min before, and only if nothing else in the calendar overlaps it. No LLM.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEETINGS = 'https://lecturer-site.example.com/duhugi/main/meetings.html'
TAG, VALUE = 'ssa_src', 'office_hours'
TITLE = '🎥 קבלת קהל — נפתלי (הסתברות, זום)'
STATE = ROOT / 'data' / 'office_hours.json'
EVERY = timedelta(hours=2)
LEAD_MIN = 30


def parse(page: str) -> list[dict]:
    """[{key, start 'YYYY-MM-DDTHH:MM', zoom}] — the table rows (commented-out rows ignored)."""
    from collectors.lecturer_site import _cells, _text
    page = re.sub(r'<!--.*?-->', '', page or '', flags=re.S)
    out = []
    for row in re.findall(r'<tr\b.*?</tr>', page, flags=re.S | re.I):
        cells = [_text(c) for c in _cells(row)]
        d = re.search(r'(\d{1,2})[./](\d{1,2})[./](\d{2,4})', cells[0] if cells else '')
        t = re.search(r'\b(\d{1,2}):(\d{2})\b', ' '.join(cells[1:3]))
        if not d or not t:
            continue
        y = int(d.group(3))
        try:
            start = datetime(y + 2000 if y < 100 else y, int(d.group(2)), int(d.group(1)), int(t.group(1)), int(t.group(2)))
        except ValueError:
            continue
        z = re.search(r'href="(https?://[^"]*zoom\.us/[^"]*)"', row, re.I)
        zoom = z.group(1) if z else ''
        mid = re.search(r'/j/(\d+)', zoom)
        out.append({'key': mid.group(1) if mid else start.strftime('%Y%m%d%H%M'),
                    'start': start.strftime('%Y-%m-%dT%H:%M'), 'zoom': zoom})
    return out


def _fetch() -> str:
    from collectors.lecturer_site import _get
    return _get(MEETINGS)


class _Cal:
    """The few calendar calls this needs (a fake one in the tests)."""

    def list(self, start: date, end: date) -> list[dict]:
        from integrations import gcal
        return gcal.list_tagged(TAG, VALUE, start, end)

    def insert(self, ev: dict) -> None:
        from integrations import gcal
        gcal.insert_event(ev)

    def move(self, event: dict, start: datetime) -> None:
        from integrations import gcal
        gcal.set_event_time(event, start.date(), start.strftime('%H:%M'), hours=1)

    def rename(self, event: dict, title: str) -> None:
        from integrations import gcal
        gcal._service().events().patch(calendarId='primary', eventId=event['id'], body={'summary': title}).execute()


def _key_of(ev: dict) -> str:
    return ((ev.get('extendedProperties') or {}).get('private') or {}).get('oh_key', '')


def _start_of(ev: dict) -> datetime | None:
    s = (ev.get('start') or {}).get('dateTime')
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None) if s else None
    except ValueError:
        return None


def sync(now: datetime | None = None, page: str | None = None, cal=None) -> dict:
    """The page vs the calendar → {'added': [rows], 'moved': [rows], 'cancelled': [titles]}."""
    now = now or datetime.now()
    cal = cal or _Cal()
    rows = [r for r in parse(page if page is not None else _fetch()) if r['start'] > now.strftime('%Y-%m-%dT%H:%M')]
    have = {_key_of(e): e for e in cal.list(now.date(), now.date() + timedelta(days=40)) if _key_of(e)}
    res: dict = {'added': [], 'moved': [], 'cancelled': []}
    for r in rows:
        start = datetime.fromisoformat(r['start'])
        ev = have.get(r['key'])
        if ev is None:
            cal.insert({'title': TITLE, 'start': r['start'], 'end': (start + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
                        'all_day': False, 'notes': f"זום: {r['zoom']}\nמהאתר של נפתלי: {MEETINGS}",
                        'private': {TAG: VALUE, 'oh_key': r['key']}, 'no_reminders': True})
            res['added'].append(r)
        elif _start_of(ev) and _start_of(ev) != start and not (ev.get('summary') or '').startswith('❌'):
            cal.move(ev, start)
            res['moved'].append(r)
    keys = {r['key'] for r in rows}
    for k, ev in have.items():
        st = _start_of(ev)
        if k not in keys and st and st > now and not (ev.get('summary') or '').startswith(('❌', '✅')):
            title = ev.get('summary', TITLE)
            cal.rename(ev, f'❌ בוטל — {title}')
            res['cancelled'].append(title)
    return res


def _when(r: dict) -> str:
    s = datetime.fromisoformat(r['start'])
    days = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
    return f"{days[s.weekday()]} {s:%d/%m} ב-{s:%H:%M}"


def tick(now: datetime | None = None, state_path: Path | None = None, page: str | None = None, cal=None) -> str | None:
    """Every 2 hours, 09–22 → one short line when something changed, else None."""
    now = now or datetime.now()
    if not 9 <= now.hour < 22:
        return None
    p = state_path or STATE
    try:
        st = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        st = {}
    last = st.get('last')
    if last and now - datetime.fromisoformat(last) < EVERY:
        return None
    st['last'] = now.isoformat(timespec='seconds')            # first — a failing site must not retry every minute
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')
    try:
        res = sync(now, page, cal)
    except Exception as e:
        print(f'  ⚠️ office hours: {e!r}'[:200])
        return None
    lines = []
    if res['added']:
        lines.append('🎥 הוספתי ליומן קבלות קהל של נפתלי (זום): ' + ' · '.join(_when(r) for r in res['added'])
                     + f'\nאזכיר {LEAD_MIN} דק׳ לפני — רק אם אתה פנוי.')
    if res['moved']:
        lines.append('🔁 קבלת קהל של נפתלי זזה: ' + ' · '.join(_when(r) for r in res['moved']))
    if res['cancelled']:
        lines.append(f"❌ קבלת קהל של נפתלי ירדה מהאתר — סימנתי ביומן ({len(res['cancelled'])})")
    return '\n'.join(lines) or None


def is_office_hours(ev: dict) -> bool:
    return ((ev.get('extendedProperties') or {}).get('private') or {}).get(TAG) == VALUE


def zoom_of(ev: dict) -> str:
    m = re.search(r'https?://\S*zoom\.us/\S+', ev.get('description') or '')
    return m.group(0) if m else ''


def busy_with(ev: dict, events: list[dict]) -> str:
    """The title of another timed event overlapping this one ('' = free). All-day / cancelled / done don't count."""
    s = _start_of(ev)
    if not s:
        return ''
    e = s + timedelta(hours=1)
    for o in events:
        if o is ev or o.get('id') == ev.get('id') or is_office_hours(o):
            continue
        os_ = _start_of(o)
        if not os_ or (o.get('summary') or '').lstrip().startswith(('❌', '✅')):
            continue
        try:
            oe = datetime.fromisoformat((o.get('end') or {}).get('dateTime')).replace(tzinfo=None)
        except (TypeError, ValueError):
            oe = os_ + timedelta(hours=1)
        if os_ < e and s < oe:
            return o.get('summary', '')
    return ''
