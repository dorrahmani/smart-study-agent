"""
agent/calendar_edit.py — "החלפנו את המסעדה של אבא בללכת למסעדה-ב" → the right event, the right change, one ✅.

The owner 09/10: a secretary understands a plain sentence about a change — which event it's about and what changed
(title / place / day / time, any combination) — without "תעביר את X ליום Y" phrasing. One cheap Gemini call
sees the message and the upcoming events (title, time, place) and returns a patch; the bot shows before → after
and changes nothing without ✅. If no event clearly matches, it returns None and the message goes on as usual.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta

MODEL = 'gemini-3.8-flash'
HINT = re.compile(r'החלפנו|שינינו|עברנו|במקום|כבר לא ב|לא ב-?\S+ אלא|העבירו|הזיזו|נדחה|נדחתה|הוקדם|הוקדמה|'
                  r'תשנה|תשני|תעדכן|תעדכני|תחליף|תחליפי|השתנה|השתנתה|זה יהיה ב|יהיה ב|זה עבר|עבר ל|הפגישה עם|'
                  r'בסוף (?:זה|נלך|נהיה|ניפגש)|החלטנו')

PROMPT = """המשתמש כתב לעוזר שלו הודעה. היום {now}. אם היא מתארת שינוי באירוע קיים ביומן שלו — מצא את האירוע ואת השינוי.
האירועים הקרובים (מספר: כותרת | זמן | מקום):
{events}

ההודעה: "{text}"

החזר JSON בלבד:
{{"match": מספר האירוע או null (אם אין התאמה ברורה / זה לא שינוי באירוע קיים),
  "title": "כותרת חדשה או null — שנה רק את החלק שהשתנה ושמור על השאר (למשל 'מסעדה לאבא - מסעדה-א' → 'מסעדה לאבא - מסעדה-ב')",
  "location": "מקום חדש או null — רק מה שהמשתמש כתב, בלי להמציא כתובת",
  "date": "YYYY-MM-DD חדש או null", "start": "HH:MM חדש או null", "end": "HH:MM חדש או null",
  "explain": "משפט קצר בעברית: מה הבנת"}}
כללים: אל תשנה שדה שהמשתמש לא שינה. "החלפנו X ב-Y" על מקום/מסעדה = גם הכותרת (אם השם הישן מופיע בה) וגם המקום.
כותרת: שמור בדיוק על המבנה והניסוח של הכותרת הקיימת והחלף רק את השם/המילה שהשתנו (לא לנסח מחדש).
match: אם אירוע אחד מתאים באופן סביר (גם התאמה חלקית בשם — "הטיול לצפון" = "טיול ענפי לצפון") — בחר אותו.
null רק אם אין שום אירוע מתאים, או שההודעה בכלל לא מתארת שינוי באירוע קיים.
שעה חדשה בלי שעת סיום → null בסיום (נשמור על אותו משך).
אם ההודעה אומרת שהאירוע/השיעור *בוטל* — title: '❌ בוטל — ' + הכותרת הקיימת, ואל תשנה זמן או מקום.
תאריך יחסי ("ליום חמישי שאחרי", "שבוע אחרי", "ליום שלפני") — חשב ביחס לתאריך הנוכחי של האירוע;
("ליום חמישי", "למחר", "לשבוע הבא") — ביחס להיום. כתוב את התאריך המלא שחישבת."""


def _fmt(ev: dict) -> str:
    st = ev.get('start') or {}
    when = st.get('dateTime', '')[:16].replace('T', ' ') if 'dateTime' in st else f"{st.get('date', '')} (כל היום)"
    return f"{ev.get('summary', '')} | {when} | {ev.get('location', '') or '-'}"


def upcoming(days: int = 90, service=None) -> list[dict]:
    from integrations import gcal
    svc = service or gcal._service()
    t0 = datetime.now(gcal._tz()) - timedelta(hours=6)
    items = gcal._list_all(svc, timeMin=t0.isoformat(), timeMax=(t0 + timedelta(days=days)).isoformat(),
                           singleEvents=True, orderBy='startTime', maxResults=80)
    ok = set(gcal.writable_calendars())                     # only events the bot may change
    return [it for it in items if it.get('_cal', 'primary') in ok]


def plan(text: str, events: list[dict], client=None) -> dict | None:
    """→ {'event', 'title', 'location', 'date', 'start', 'end', 'explain'} or None."""
    if not events:
        return None
    import google.genai as genai
    from google.genai import types as T
    client = client or genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    listing = '\n'.join(f'{i}: {_fmt(e)}' for i, e in enumerate(events))
    resp = client.models.generate_content(
        model=MODEL, contents=PROMPT.format(now=datetime.now().strftime('%A %d/%m/%Y %H:%M'), events=listing, text=text),
        config=T.GenerateContentConfig(temperature=0, max_output_tokens=2500, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op='calendar_edit')
    try:
        d = json.loads(resp.text or '{}')
    except ValueError:
        return None
    i = d.get('match')
    if not isinstance(i, int) or not 0 <= i < len(events):
        return None
    patch = {k: (d.get(k) or None) for k in ('title', 'location', 'date', 'start', 'end')}
    if not any(patch.values()):
        return None
    if patch['date'] and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', patch['date']):
        patch['date'] = None
    for k in ('start', 'end'):
        if patch[k] and not re.fullmatch(r'\d{1,2}:\d{2}', patch[k]):
            patch[k] = None
    return {'event': events[i], **patch, 'explain': (d.get('explain') or '').strip()}


def describe(p: dict) -> str:
    """Before → after, only what changes."""
    ev = p['event']
    st = ev.get('start') or {}
    lines = []
    if p.get('title') and p['title'] != ev.get('summary'):
        lines.append(f"✏️ {ev.get('summary', '')}  →  *{p['title']}*")
    if p.get('location') and p['location'] != (ev.get('location') or ''):
        lines.append(f"📍 {ev.get('location') or '(בלי מקום)'}  →  *{p['location']}*")
    if p.get('date') or p.get('start') or p.get('end'):
        old = st.get('dateTime', st.get('date', ''))[:16].replace('T', ' ')
        new_day = p.get('date') or (st.get('dateTime') or st.get('date', ''))[:10]
        new_t = p.get('start') or (st.get('dateTime', '')[11:16] if 'dateTime' in st else '')
        lines.append(f"📅 {old}  →  *{(new_day + ' ' + new_t).strip()}*")
    return '\n'.join(lines)


def apply(p: dict, service=None) -> dict:
    """Write the change (title/location patch; day/time via a full update so all-day ↔ timed stays valid)."""
    from integrations import gcal
    svc = service or gcal._service()
    ev = p['event']
    body = {}
    if p.get('title'):
        body['summary'] = p['title']
    if p.get('location'):
        body['location'] = p['location']
    if body:
        ev = dict(svc.events().patch(calendarId=gcal.cal_of(ev), eventId=ev['id'], body=body).execute(),
                  _cal=ev.get('_cal', 'primary'))
    if p.get('date') or p.get('start') or p.get('end'):
        st, en = ev.get('start') or {}, ev.get('end') or {}
        if 'dateTime' in st:
            s0, e0 = datetime.fromisoformat(st['dateTime']), datetime.fromisoformat(en['dateTime'])
            hours = max((e0 - s0).total_seconds() / 3600, 0.25)
            day = date.fromisoformat(p['date']) if p.get('date') else s0.date()
            hhmm = p.get('start') or s0.strftime('%H:%M')
            if p.get('end'):
                h1, m1 = map(int, hhmm.split(':'))
                h2, m2 = map(int, p['end'].split(':'))
                hours = max(((h2 * 60 + m2) - (h1 * 60 + m1)) / 60, 0.25)
            ev = gcal.set_event_time(ev, day, hhmm, hours=hours, service=svc)
        else:
            day = date.fromisoformat(p['date']) if p.get('date') else date.fromisoformat(st['date'])
            ev = gcal.set_event_time(ev, day, p.get('start') or '', service=svc)
    return ev
