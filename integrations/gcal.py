"""
integrations/gcal.py — Google Calendar for the personal agent.

Scope: calendar.events ONLY (read/write events) — no Gmail, Drive or account access.
Token: config/gcal_token.json (chmod 600, in the encrypted backup), created once with
scripts/gcal_auth.py on the owner's PC. Gotcha: an OAuth app left in "Testing" gets its
refresh token revoked after 7 days — the consent screen must be "In production".

The agent never writes without a ✅: parse_event() → describe() + buttons → add_event().
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = ROOT / 'config' / 'gcal_token.json'
SCOPES = ['https://www.googleapis.com/auth/calendar.events']
TZ = 'Asia/Jerusalem'
MODEL = 'gemini-3.8-flash'
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']


def configured(token: Path | None = None) -> bool:
    return (token or TOKEN).is_file()


def _tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(TZ)


def _service(token: Path | None = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    p = token or TOKEN
    creds = Credentials.from_authorized_user_file(str(p), SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        p.write_text(creds.to_json(), encoding='utf-8')
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return build('calendar', 'v3', credentials=creds, cache_discovery=False)


PARSE_PROMPT = """עכשיו: {now} (יום {weekday}), אזור זמן Asia/Jerusalem.
הפוך את הבקשה לאירועים ביומן (יכולים להיות כמה). החזר JSON בלבד:
{{"events": [{{"title": "...", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM", "all_day": false,
  "location": "", "notes": ""}}], "question": ""}}
כללים:
- תאריכים יחסיים ("מחר", "ביום שלישי", "יום שני שאחריו", "בעוד שבועיים") — חשב מהתאריך של עכשיו ומהאירועים
  הקודמים באותה הודעה; "יום חמישי הזה" = הקרוב שעוד לא עבר. תאריך כמו 23.10 = השנה (או הבאה אם כבר עבר).
- בלי שעת סיום — שעה אחת אחרי ההתחלה. יום שלם (יום הולדת, חופשה) — all_day=true, start/end כ-YYYY-MM-DD (end = היום שאחרי).
- מבחן / הגשה / אירוע שאין לו שעה ולא צריך — all_day=true ו-notes "השעה לא צוינה".
- רק לפגישה / שיעור / תור בלי שעה, או בקשה לא ברורה — שאלה קצרה בעברית ב-"question".
- הערות חשובות מההודעה (למשל "עוד לא החלטתי אם לגשת") — ב-notes של האירועים הרלוונטיים.
- title קצר בעברית ("מבחן במבוא למדמ\"ח — מועד ב"), בלי "תוסיף ליומן".
הבקשה: {text}"""


def parse_events(text: str, now: datetime | None = None, client=None) -> tuple[list[dict], str]:
    """(validated events, clarifying question or '')."""
    raw = _parse_raw(text, now, client)
    if isinstance(raw, list):                                   # the model sometimes returns a bare list
        raw = {'events': raw}
    if 'events' not in raw:                                     # or a single event object
        raw = {'events': [raw], 'question': raw.get('question', '')}
    events = []
    for ev in raw.get('events') or []:
        if isinstance(ev, dict):
            try:
                events.append(validate(ev))
            except (TypeError, ValueError):
                continue
    if client is None:                                          # 17/11 the model's day checked against the words
        events = fix_day(events, text, now)
    return events[:10], str(raw.get('question') or '').strip()


def fix_day(events: list[dict], text: str, now: datetime | None = None) -> list[dict]:
    """One event, and the request names one clear day (agent/hetime): the event is on that day, whatever date the
    model counted ("ביום ראשון" on a Saturday night is tomorrow). The model's hour stays unless the words give one.
    Not for "לפני/אחרי X", several days, or anything hetime can't tell. Never raises."""
    try:
        if len(events) != 1 or re.search(r'(?<![א-ת])(?:לפני|אחרי|עד)(?![א-ת])', text or ''):
            return events
        from agent import hetime
        w = hetime.parse(text, now or datetime.now(), lessons=[])
        if not w or not w.has_day:
            return events
        ev = dict(events[0])
        start = date.fromisoformat(str(ev['start'])[:10])
        if start == w.at.date():
            return events
        shift = w.at.date() - start
        if ev.get('all_day'):
            ev['start'] = (start + shift).isoformat()
            ev['end'] = (date.fromisoformat(str(ev['end'])[:10]) + shift).isoformat()
        else:
            s0, e0 = datetime.fromisoformat(ev['start']), datetime.fromisoformat(ev['end'])
            s1 = w.at if w.has_time else s0 + shift
            ev['start'], ev['end'] = s1.isoformat(timespec='minutes'), (s1 + (e0 - s0)).isoformat(timespec='minutes')
        try:
            from agent import factcheck
            factcheck.log('calendar_day', {'text': (text or '')[:120], 'model': events[0]['start'], 'fixed': ev['start']})
        except Exception:
            pass
        return [ev]
    except Exception as e:
        print(f'  ⚠️ calendar day check: {e!r}'[:200])
        return events


def parse_event(text: str, now: datetime | None = None, client=None) -> dict:
    """Single-event convenience (first event; the question is attached)."""
    events, q = parse_events(text, now, client)
    ev = events[0] if events else validate({'title': 'אירוע', 'start': (now or datetime.now()).isoformat()})
    return {**ev, 'question': q or ev.get('question', '')}


def _parse_raw(text: str, now: datetime | None = None, client=None):
    from google.genai import types as T
    now = now or datetime.now()
    if client is None:
        import google.genai as genai
        client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    resp = client.models.generate_content(
        model=MODEL, contents=PARSE_PROMPT.format(now=now.strftime('%Y-%m-%d %H:%M'),
                                                  weekday=WEEKDAY_HE[now.weekday()], text=text),
        config=T.GenerateContentConfig(max_output_tokens=1500, temperature=0.0,
                                       response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op='gcal_parse')
    return json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', (resp.text or '').strip()))


def validate(ev: dict) -> dict:
    """Normalize; raise ValueError on dates the API would reject."""
    ev = {k: (ev.get(k) or ('' if k != 'all_day' else False)) for k in
          ('title', 'start', 'end', 'all_day', 'location', 'notes', 'question')}
    ev['title'] = str(ev['title']).strip()[:200] or 'אירוע'
    if ev['all_day']:
        s = date.fromisoformat(str(ev['start'])[:10])
        e = date.fromisoformat(str(ev['end'])[:10]) if ev['end'] else s + timedelta(days=1)
        ev['start'], ev['end'] = s.isoformat(), (e if e > s else s + timedelta(days=1)).isoformat()
    else:
        s = datetime.fromisoformat(str(ev['start'])[:16])
        e = datetime.fromisoformat(str(ev['end'])[:16]) if ev['end'] else s + timedelta(hours=1)
        if e <= s:
            e = s + timedelta(hours=1)
        ev['start'], ev['end'] = s.isoformat(timespec='minutes'), e.isoformat(timespec='minutes')
    return ev


def describe(ev: dict) -> str:
    def dm(d) -> str:                                  # 16/10: "28/05" was 2027 — say the year when it isn't this one
        return f'{d:%d/%m}' if d.year == date.today().year else f'{d:%d/%m/%Y}'
    if ev.get('all_day'):
        s = date.fromisoformat(ev['start'])
        last = date.fromisoformat(ev['end']) - timedelta(days=1)       # Google's all-day end is exclusive
        when = (f"יום {WEEKDAY_HE[s.weekday()]} {dm(s)} (כל היום)" if last <= s else
                f"{dm(s)}–{dm(last)} ({(last - s).days + 1} ימים)")
    else:
        s, e = datetime.fromisoformat(ev['start']), datetime.fromisoformat(ev['end'])
        when = f"יום {WEEKDAY_HE[s.weekday()]} {dm(s)} {s:%H:%M}–{e:%H:%M}"
    out = f"📅 *{ev['title']}*\n{when}"
    if ev.get('location'):
        out += f"\n📍 {ev['location']}"
    if ev.get('notes'):
        out += f"\n📝 {ev['notes']}"
    return out


def _body(ev: dict) -> dict:
    if ev.get('all_day'):
        start, end = {'date': ev['start']}, {'date': ev['end']}
    else:
        start, end = {'dateTime': ev['start'] + ':00', 'timeZone': TZ}, {'dateTime': ev['end'] + ':00', 'timeZone': TZ}
    body = {'summary': ev['title'], 'start': start, 'end': end, 'reminders': {'useDefault': True}}
    if ev.get('location'):
        body['location'] = ev['location']
    if ev.get('notes'):
        body['description'] = ev['notes']
    if ev.get('private'):                      # our own tag — lets the agent find (only) the events it created
        body['extendedProperties'] = {'private': ev['private']}
    if ev.get('color'):
        body['colorId'] = ev['color']
    if ev.get('no_reminders'):
        body['reminders'] = {'useDefault': False, 'overrides': []}
    return body


def list_tagged(tag: str, value: str, start: date, end: date, service=None) -> list[dict]:
    """Events carrying our private tag (tag=value) between two dates."""
    svc = service or _service()
    tz = _tz()
    items, token = [], None
    while True:
        r = svc.events().list(calendarId='primary', privateExtendedProperty=f'{tag}={value}', singleEvents=True,
                              timeMin=datetime(start.year, start.month, start.day, tzinfo=tz).isoformat(),
                              timeMax=datetime(end.year, end.month, end.day, 23, 59, tzinfo=tz).isoformat(),
                              maxResults=250, pageToken=token).execute()
        items += r.get('items') or []
        token = r.get('nextPageToken')
        if not token:
            return items


def insert_event(ev: dict, service=None) -> dict:
    """Insert into the primary calendar → the created event (id, htmlLink…)."""
    svc = service or _service()
    return svc.events().insert(calendarId='primary', body=_body(ev)).execute()


def add_event(ev: dict, service=None) -> str:
    """Insert into the primary calendar → the event's link."""
    return insert_event(ev, service).get('htmlLink', '')


def exists_similar(ev: dict, service=None) -> bool:
    """Same title already on the calendar overlapping the same time (no duplicates when a request is repeated)."""
    svc = service or _service()
    tz = _tz()
    s = datetime.fromisoformat(ev['start'] if not ev.get('all_day') else ev['start'] + 'T00:00')
    e = datetime.fromisoformat(ev['end'] if not ev.get('all_day') else ev['end'] + 'T00:00')
    items = svc.events().list(calendarId='primary', timeMin=s.replace(tzinfo=tz).isoformat(),
                              timeMax=e.replace(tzinfo=tz).isoformat(), singleEvents=True,
                              maxResults=50).execute().get('items') or []
    norm = lambda t: re.sub(r'\W+', '', t or '')
    return any(norm(it.get('summary')) == norm(ev['title']) for it in items)


def find_events(query: str, days_back: int = 7, days_ahead: int = 365, service=None) -> list[dict]:
    """Existing events whose title shares the query's meaningful words (for move / delete requests)."""
    svc = service or _service()
    t0 = datetime.now(_tz()) - timedelta(days=days_back)
    items = svc.events().list(calendarId='primary', timeMin=t0.isoformat(),
                              timeMax=(t0 + timedelta(days=days_back + days_ahead)).isoformat(),
                              singleEvents=True, orderBy='startTime', maxResults=250).execute().get('items') or []
    stop = {'את', 'של', 'ה', 'יום', 'מהיומן', 'ליומן', 'היומן', 'אירוע', 'האירוע', 'שלי'}
    words = [re.sub(r'^ה', '', w) for w in re.findall(r'\w+', query or '') if w not in stop and len(w) > 1]
    hits = []
    for it in items:
        title = it.get('summary', '')
        norm = re.sub(r'(?<!\w)ה', '', title)
        score = sum(1 for w in words if w in norm or w in title)
        if words and score >= max(1, (len(words) + 1) // 2):
            hits.append((score, it))
    hits.sort(key=lambda x: -x[0])
    return [it for _, it in hits]


def cal_of(event: dict) -> str:
    """The calendar to WRITE an event back to: its own calendar if that one is writable, else refuse.
    15/10: The owner's "לימודי תשפ"ז" is writable (lecturer mails / site changes update it — always after his ✅)."""
    cal = event.get('_cal') or 'primary'
    if cal == 'primary' or any(c['id'] == cal and c.get('write') for c in read_calendars()):
        return cal
    raise PermissionError(f'calendar {cal} is read-only for the agent')


def writable_calendars() -> list[str]:
    return ['primary'] + [c['id'] for c in read_calendars() if c.get('write')]


def move_event(event: dict, new_day: date, service=None) -> dict:
    """Same time/duration, another day (all-day events keep their length)."""
    svc = service or _service()
    st, en = event.get('start') or {}, event.get('end') or {}
    if 'date' in st:
        s0, e0 = date.fromisoformat(st['date']), date.fromisoformat(en['date'])
        body = {'start': {'date': new_day.isoformat()}, 'end': {'date': (new_day + (e0 - s0)).isoformat()}}
    else:
        s0 = datetime.fromisoformat(st['dateTime'])
        e0 = datetime.fromisoformat(en['dateTime'])
        ns = s0.replace(year=new_day.year, month=new_day.month, day=new_day.day)
        body = {'start': {'dateTime': ns.isoformat(), 'timeZone': TZ},
                'end': {'dateTime': (ns + (e0 - s0)).isoformat(), 'timeZone': TZ}}
    return svc.events().patch(calendarId=cal_of(event), eventId=event['id'], body=body).execute()


def set_event_time(event: dict, day: date, hhmm: str = '', hours: int = 3, location: str = '', service=None) -> dict:
    """Put an event on `day` at `hhmm` (an all-day event becomes timed). Full update, not patch — a patch would
    keep start.date next to the new start.dateTime and Google rejects that. Empty hhmm → all-day on `day`."""
    svc = service or _service()
    cal = cal_of(event)
    full = svc.events().get(calendarId=cal, eventId=event['id']).execute()
    if hhmm:
        h, m = map(int, hhmm.split(':'))
        s = datetime(day.year, day.month, day.day, h, m)
        full['start'] = {'dateTime': s.isoformat(), 'timeZone': TZ}
        full['end'] = {'dateTime': (s + timedelta(hours=hours)).isoformat(), 'timeZone': TZ}
    else:
        full['start'] = {'date': day.isoformat()}
        full['end'] = {'date': (day + timedelta(days=1)).isoformat()}
    if location and not full.get('location'):
        full['location'] = location
    return svc.events().update(calendarId=cal, eventId=event['id'], body=full).execute()


def set_location(event: dict, location: str, service=None) -> dict:
    svc = service or _service()
    return svc.events().patch(calendarId=cal_of(event), eventId=event['id'], body={'location': location}).execute()


def when_text(event: dict) -> str:
    st = event.get('start') or {}
    if 'dateTime' in st:
        d = datetime.fromisoformat(st['dateTime'])
        return f"יום {WEEKDAY_HE[d.weekday()]} {d:%d/%m %H:%M}"
    d = date.fromisoformat(st.get('date'))
    return f"יום {WEEKDAY_HE[d.weekday()]} {d:%d/%m}"


def delete_event(event_id: str, service=None, calendar: str = 'primary') -> None:
    svc = service or _service()
    svc.events().delete(calendarId=calendar, eventId=event_id).execute()


ALARM_TAG, ALARM_VALUE = 'ssa_src', 'escalation'


def alarm_body(title: str, at: datetime, minutes: int = 10, note: str = '') -> dict:
    """11/11 "לתקשר אם אני לא עונה": a short event in the owner's own calendar whose popup fires at its start — Google
    Calendar on his phone rings/notifies even when WhatsApp is muted — and whose email reminder lands in his inbox.
    Both are the calendar's own reminders: no send scope, no new account. Tagged, so only ours is ever deleted."""
    end = at + timedelta(minutes=minutes)
    return {'summary': title[:200], 'start': {'dateTime': at.strftime('%Y-%m-%dT%H:%M:00'), 'timeZone': TZ},
            'end': {'dateTime': end.strftime('%Y-%m-%dT%H:%M:00'), 'timeZone': TZ},
            'description': note[:1000], 'colorId': '11',
            'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 0},
                                                             {'method': 'email', 'minutes': 0}]},
            'extendedProperties': {'private': {ALARM_TAG: ALARM_VALUE}}}


def insert_alarm(title: str, at: datetime, note: str = '', service=None) -> str:
    """→ the event id (for deleting it once the owner answers)."""
    svc = service or _service()
    return svc.events().insert(calendarId='primary', body=alarm_body(title, at, note=note)).execute().get('id', '')


def delete_alarm(event_id: str, service=None) -> bool:
    """Only an event carrying our escalation tag is deleted."""
    svc = service or _service()
    ev = svc.events().get(calendarId='primary', eventId=event_id).execute()
    if ((ev.get('extendedProperties') or {}).get('private') or {}).get(ALARM_TAG) != ALARM_VALUE:
        return False
    svc.events().delete(calendarId='primary', eventId=event_id).execute()
    return True


def set_reminder(event_id: str, minutes, service=None) -> None:
    """A phone notification from Google Calendar `minutes` before the start (all-day: before midnight).
    17/11 several at once ("1 2" = a day before and two hours before): a list of minutes."""
    svc = service or _service()
    many = sorted({int(m) for m in (minutes if isinstance(minutes, (list, tuple, set)) else [minutes])}, reverse=True)
    svc.events().patch(calendarId='primary', eventId=event_id, body={'reminders': {
        'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': m} for m in many[:5]]}}).execute()


# (label, minutes before start). All-day events start at 00:00, so "the evening before" = 240.
REMINDERS_TIMED = [('🔔 יום לפני', 1440), ('🔔 שעתיים לפני', 120), ('🔔 חצי שעה לפני', 30)]
REMINDERS_ALLDAY = [('🔔 שבוע לפני', 6 * 1440 + 900), ('🔔 יום לפני (09:00)', 900), ('🔔 ערב לפני (20:00)', 240)]


def deadline_event_id(key: str) -> str:
    """Stable Google event id for a homework key (ids allow [a-v0-9], 5-1024 chars; hex fits)."""
    import hashlib
    return 'ssa' + hashlib.sha1(key.encode('utf-8')).hexdigest()


def sync_deadlines(items: list[dict], done_keys: set[str], service=None) -> int:
    """Opt-in (SSA_DEADLINES_TO_CALENDAR=1): mirror homework deadlines into the calendar as
    30-minute events ending at the due time; a submitted one is renamed "✅ …". Idempotent
    (stable event ids: insert, or update when it exists). Returns events written."""
    from config.paths import COURSE_NAMES
    svc = service or _service()
    n = 0
    for a in items:
        try:
            due = datetime.fromisoformat(str(a.get('due_date'))).replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        key = a['key'] if 'key' in a else f"{a.get('course_key')}:{a.get('assign_id')}"
        mark = '✅ ' if key in done_keys else '📝 '
        ev = {'title': f"{mark}{a.get('title', 'מטלה')} — {COURSE_NAMES.get(a.get('course_key', ''), '')}",
              'start': (due - timedelta(minutes=30)).isoformat(timespec='minutes'),
              'end': due.isoformat(timespec='minutes'), 'notes': 'נוצר אוטומטית ע"י Smart Study'}
        body = _body(validate(ev))
        eid = deadline_event_id(key)
        try:
            svc.events().update(calendarId='primary', eventId=eid, body=body).execute()
        except Exception as e:                       # googleapiclient HttpError: e.resp.status
            status = getattr(getattr(e, 'resp', None), 'status', None) or getattr(e, 'status_code', None)
            if str(status) == '404' or (status is None and '404' in str(e)):
                svc.events().insert(calendarId='primary', body={**body, 'id': eid}).execute()
            else:
                raise
        n += 1
    return n


CALENDARS = Path(__file__).resolve().parent.parent / 'data' / 'calendars.json'


def read_calendars(path: Path | None = None) -> list[dict]:
    """Calendars the agent READS besides 'primary' (15/10: The owner's lessons live in his own "לימודים" calendar):
    data/calendars.json = {"read": [{"id": "...@group.calendar.google.com", "name": "לימודים", "kind": "lessons"}]}.
    Writes (add / move / delete / edit) always stay on 'primary' — the bot never touches these."""
    try:
        return [c for c in json.loads((path or CALENDARS).read_text(encoding='utf-8')).get('read') or [] if c.get('id')]
    except (OSError, ValueError):
        return []


def _list_all(svc, **kw) -> list[dict]:
    """events().list over 'primary' + the read-only calendars, merged by start; each item tagged '_cal'."""
    items = [dict(it, _cal='primary') for it in (svc.events().list(calendarId='primary', **kw).execute().get('items') or [])]
    for c in read_calendars():
        try:
            items += [dict(it, _cal=c['id'], _cal_kind=c.get('kind', ''), _cal_name=c.get('name', ''))
                      for it in (svc.events().list(calendarId=c['id'], **kw).execute().get('items') or [])]
        except Exception as e:
            print(f'  ⚠️ calendar {c.get("name") or c["id"][:12]}: {e}')
    if read_calendars():
        items.sort(key=lambda it: (it.get('start') or {}).get('dateTime') or (it.get('start') or {}).get('date') or '')
        items = _dedupe(items)
    return items


def _norm_title(s: str) -> str:
    s = re.sub(r'^יום\s+', '', (s or '').strip())
    return re.sub(r'[\s\-–—:,.״"\'׳]+', ' ', s).strip()


def _dedupe(items: list[dict]) -> list[dict]:
    """The same thing in two calendars ('טיול ענפי לצפון' + 'יום טיול ענפי לצפון', same day) → shown once.
    Only across calendars, same start, one title containing the other; the primary copy wins."""
    out: list[dict] = []
    for it in items:
        st = (it.get('start') or {}).get('dateTime') or (it.get('start') or {}).get('date')
        a = _norm_title(it.get('summary', ''))
        dup = next((o for o in out if o.get('_cal') != it.get('_cal') and a
                    and ((o.get('start') or {}).get('dateTime') or (o.get('start') or {}).get('date')) == st
                    and (a in _norm_title(o.get('summary', '')) or _norm_title(o.get('summary', '')) in a)), None)
        if dup is None:
            out.append(it)
        elif dup.get('_cal') != 'primary' and it.get('_cal') == 'primary':
            out[out.index(dup)] = it
    return out


def events_on(day: date, service=None) -> list[dict]:
    svc = service or _service()
    tz = _tz()
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return _list_all(svc, timeMin=start.isoformat(), timeMax=(start + timedelta(days=1)).isoformat(),
                     singleEvents=True, orderBy='startTime', maxResults=50)


IMPORTANT_RX = re.compile(r'מבחן|בוחן|מועד [אבג]|הגשה|מטלה|exam|quiz|deadline', re.I)


def _summary(it: dict, hide_private: bool) -> str:
    """Event title — or "תפוס" for an event marked private in Google Calendar, when viewed by the partner."""
    if hide_private and it.get('visibility') in ('private', 'confidential'):
        return 'תפוס'
    return it.get('summary', '(ללא כותרת)')


def _span(it: dict) -> tuple[datetime, datetime]:
    st, en = it.get('start') or {}, it.get('end') or {}
    if 'dateTime' in st:
        return (datetime.fromisoformat(st['dateTime']).replace(tzinfo=None),
                datetime.fromisoformat(en.get('dateTime', st['dateTime'])).replace(tzinfo=None))
    s = datetime.fromisoformat(st.get('date'))
    return s, datetime.fromisoformat(en.get('date')) if en.get('date') else s + timedelta(days=1)


def conflicts(events: list[dict], service=None, window_days: int = 3, hide_private: bool = False) -> list[str]:
    """Warnings for proposed events: a direct overlap with anything in the owner's calendar, or an exam /
    submission within ±window_days. Hebrew lines, deduplicated."""
    if not events:
        return []
    svc = service or _service()
    tz = _tz()
    out = []
    for ev in events:
        s = datetime.fromisoformat(ev['start'] if not ev.get('all_day') else ev['start'] + 'T00:00')
        e = datetime.fromisoformat(ev['end'] if not ev.get('all_day') else ev['end'] + 'T00:00')
        lo, hi = s - timedelta(days=window_days), e + timedelta(days=window_days)
        items = _list_all(svc, timeMin=lo.replace(tzinfo=tz).isoformat(), timeMax=hi.replace(tzinfo=tz).isoformat(),
                          singleEvents=True, orderBy='startTime', maxResults=100)
        for it in items:
            a, b = _span(it)
            name = _summary(it, hide_private)
            when = f"יום {WEEKDAY_HE[a.weekday()]} {a:%d/%m}" + ('' if (b - a) >= timedelta(days=1) else f' {a:%H:%M}')
            if ev.get('all_day') and 'השעה לא צוינה' in (ev.get('notes') or '') and (b - a) < timedelta(days=1) \
                    and not IMPORTANT_RX.search(name):
                continue            # 10/11: "wedding on 22/11" (no hour yet) was warned about a lecture that morning
            if a < e and s < b and not ((b - a) >= timedelta(days=1) and not IMPORTANT_RX.search(name)):
                out.append(f'⏰ חופף ל: {name} ({when})')
            elif IMPORTANT_RX.search(name) and b > datetime.now():       # an exam that already ended isn't a warning (09/10 review)
                out.append(f'📚 {name} — {when}')
    return list(dict.fromkeys(out))


def upcoming_items(days: int = 200, service=None, hide_private: bool = False) -> list[tuple[datetime, str]]:
    """[(start, title)] of the exams / submissions in the coming `days`, soonest first (structured upcoming_text)."""
    svc = service or _service()
    t0 = datetime.now(_tz())
    items = _list_all(svc, timeMin=t0.isoformat(), timeMax=(t0 + timedelta(days=days)).isoformat(),
                      singleEvents=True, orderBy='startTime', maxResults=250)
    return [(_span(it)[0], _summary(it, hide_private)) for it in items if IMPORTANT_RX.search(_summary(it, hide_private))]


def upcoming_text(days: int = 90, service=None, hide_private: bool = False, query: str = '') -> str:
    """Exams / submissions in the coming `days` (for "מתי המבחן הקרוב שלי"). `query` ("בדידה") narrows
    to the matching ones — "כמה ימים נשארו למבחן בבדידה" wants one answer, not the whole list."""
    svc = service or _service()
    t0 = datetime.now(_tz())
    items = _list_all(svc, timeMin=t0.isoformat(), timeMax=(t0 + timedelta(days=days)).isoformat(),
                      singleEvents=True, orderBy='startTime', maxResults=250)
    hits = [it for it in items if IMPORTANT_RX.search(_summary(it, hide_private))]
    if not hits:
        return (f'📚 אין ביומן מבחנים או הגשות ב-{days} הימים הקרובים.\n'
                f'אם יש לך — כתוב לי אותם (אפשר כמה בהודעה אחת) ואוסיף.')

    def when(it):
        a, _ = _span(it)
        left = (a.date() - date.today()).days
        return a, 'היום' if left == 0 else ('מחר' if left == 1 else f'בעוד {left} ימים')

    q = query.strip().replace('"', '').replace('״', '')
    mine = [it for it in hits if q and q in _summary(it, hide_private).replace('"', '').replace('״', '')]
    if mine:
        a, w = when(mine[0])
        out = f"📚 *{_summary(mine[0], hide_private)}*\nיום {WEEKDAY_HE[a.weekday()]} {a:%d/%m} — *{w}*"
        if a.hour or a.minute:
            out += f' ({a:%H:%M})'
        return out + ''.join(f"\nואחריו: {_summary(it, hide_private)} — {when(it)[0]:%d/%m}" for it in mine[1:3])
    lines = ['📚 *הקרובים ביומן:*'] if not q else [f'📚 לא מצאתי ביומן מבחן/הגשה ב"{q}". הקרובים:']
    for it in hits[:10]:
        a, w = when(it)
        lines.append(f"• יום {WEEKDAY_HE[a.weekday()]} {a:%d/%m} ({w}) — {_summary(it, hide_private)}")
    return '\n'.join(lines)


def week_text(start: date, days: int = 7, service=None, hide_private: bool = False) -> str:
    """Upcoming events over several days (for "מה יש לי ביומן?" with no day named)."""
    svc = service or _service()
    tz = _tz()
    t0 = datetime(start.year, start.month, start.day, tzinfo=tz)
    items = _list_all(svc, timeMin=t0.isoformat(), timeMax=(t0 + timedelta(days=days)).isoformat(),
                      singleEvents=True, orderBy='startTime', maxResults=100)
    if not items:
        return f'🗓️ ב-{days} הימים הקרובים היומן פנוי.'
    lines, cur = [f'🗓️ *{days} הימים הקרובים*'], None
    for it in items:
        st = it.get('start') or {}
        if 'dateTime' in st:
            dt = datetime.fromisoformat(st['dateTime'])
            d, t = dt.date(), dt.strftime('%H:%M')
        else:
            d, t = date.fromisoformat(st.get('date')), 'כל היום' + _multi_day(it)
            d = max(d, start)                     # 31/10 "חופשה בטבריה" 30/10–01/11 sat under Friday: "היה אתמול"
        if d != cur:
            lines.append(f"\n*יום {WEEKDAY_HE[d.weekday()]} {d:%d/%m}*")
            cur = d
        loc = '' if hide_private else (it.get('location') or '').split(',')[0].strip()
        lines.append(f"• {t} — {_summary(it, hide_private)}" + (f" · 📍 {loc}" if loc else '')   # 23/10: "איפה ההרצאה?"
                     + ('' if hide_private else _override(it)))
    return '\n'.join(lines)


def _override(it: dict) -> str:
    """16/11 a lesson the university changed (agent/schedule): " — ❌ מבוטל (הודעה במודל): …" on its line."""
    try:
        from agent import schedule
        return schedule.event_suffix(it)
    except Exception:
        return ''


def _multi_day(it: dict) -> str:
    """An all-day event of several days → " (30/10–01/11)": its last day is end.date minus one (Google's end is exclusive).
    31/10 the partner asked "מה הלוז של המשתמש היום" during a trip and got "היומן ריק, היה רשום אתמול"."""
    st, en = it.get('start') or {}, it.get('end') or {}
    try:
        first, last = date.fromisoformat(st['date']), date.fromisoformat(en['date']) - timedelta(days=1)
    except (KeyError, ValueError, TypeError):
        return ''
    return f' ({first:%d/%m}–{last:%d/%m})' if last > first else ''


def day_text(day: date, service=None, hide_private: bool = False) -> str:
    items = events_on(day, service)
    head = f"🗓️ *יום {WEEKDAY_HE[day.weekday()]} {day:%d/%m}*"
    if not items:
        return head + '\nהיומן פנוי.'
    lines = [head]
    for it in items:
        st = it.get('start') or {}
        if 'dateTime' in st:
            t = datetime.fromisoformat(st['dateTime']).strftime('%H:%M')
        else:
            t = 'כל היום' + _multi_day(it)
        loc = '' if hide_private else (it.get('location') or '').split(',')[0].strip()
        lines.append(f"• {t} — {_summary(it, hide_private)}" + (f" · 📍 {loc}" if loc else '')   # 23/10: "איפה ההרצאה?"
                     + ('' if hide_private else _override(it)))
    return '\n'.join(lines)
