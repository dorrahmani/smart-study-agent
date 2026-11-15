"""
agent/booking.py — "תזמין לי ולבת הזוג מקום למסעדת a ב-29.10 ב-20:00" → the agent actually books (The owner 10/10).

1. parse the request (one cheap call): restaurant, city, date, time, party size, occasion
2. find the restaurant on the web (Google-grounded): name, city, phone, WhatsApp, booking link
3. Ontopo restaurants: real availability for that date/time/size (Ontopo's own public search, no browser) —
   "20:00 is waitlist only; free: 19:30 on the terrace, 21:00 at the bar" — and a link with the date/time/size
   already filled in. Restaurants with a WhatsApp number: the coordinator books in the owner's name (his ✅ first).
Never enters payment/card details; never books without the owner's choice.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime

MODEL = 'gemini-3.8-flash'
BOOK_RX = re.compile(r'(?:תזמין|תזמיני|תשריין|תשרייני|תסגור|תסגרי|תבדוק אם יש)\s+(?:לי|לנו)?\s*(?:ול\S+\s+)?'
                     r'(?:מקום|שולחן|הזמנה)|(?:מקום|שולחן)\s+(?:ב|ל)מסעד')
# past tense = already booked → a calendar entry, not a booking request (10/10: "הזמנו מקום במסעדת a…")
DONE_RX = re.compile(r'הזמנו|הזמנתי|הזמינה|הזמין|סגרנו|סגרתי|יש לנו (?:הזמנה|מקום|שולחן)|קבענו')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
METHOD_HE = {'seat': 'פנוי', 'standby': 'רשימת המתנה', 'phone': 'רק בטלפון', 'disabled': 'מלא'}


def _client():
    import google.genai as genai
    return genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))


def parse_request(text: str, client=None) -> dict:
    from google.genai import types as T
    client = client or _client()
    resp = client.models.generate_content(
        model=MODEL, contents=f'היום {datetime.now():%A %d/%m/%Y}. חלץ מבקשת הזמנת מקום JSON בלבד: '
                              '{"restaurant": "שם המסעדה כפי שנכתב", "city": "עיר או ריק", "date": "YYYY-MM-DD", '
                              '"time": "HH:MM", "size": מספר אנשים (אני + X = 2), "occasion": "סיבה או ריק"}. '
                              f'הבקשה: "{text}"',
        config=T.GenerateContentConfig(temperature=0, max_output_tokens=2000, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op='booking.parse')
    try:
        d = json.loads(resp.text or '{}')
    except ValueError:
        d = {}
    try:
        d['size'] = max(1, min(int(d.get('size') or 2), 20))
    except (TypeError, ValueError):
        d['size'] = 2
    return d


FIELDS = {'שם': 'name', 'עיר': 'city', 'כתובת': 'address', 'טלפון': 'phone', 'וואטסאפ': 'whatsapp', 'הזמנה': 'url',
          'אתר': 'site', 'בטוח': 'sure'}


def lookup(restaurant: str, city: str = '', client=None) -> dict:
    """The restaurant's details from the web (grounded). Fields missing → ''."""
    from google.genai import types as T
    client = client or _client()
    q = (f'מצא את המסעדה "{restaurant}"{" ב" + city if city else ""} בישראל. החזר בדיוק בפורמט (שורה לכל שדה; '
         f'"לא ידוע" אם לא מצאת; אל תמציא):\nשם: \nעיר: \nכתובת: \nטלפון: \nוואטסאפ: \n'
         f'הזמנה: <קישור להזמנת מקום — עדיפות ל-ontopo.com, אחרת טאביט, אחרת אתר המסעדה>\n'
         f'אתר: <אתר המסעדה>\nבטוח: כן/לא')
    resp = None
    for model in (MODEL, 'gemini-2.5-flash'):
        resp = client.models.generate_content(model=model, contents=q, config=T.GenerateContentConfig(
            tools=[T.Tool(google_search=T.GoogleSearch())], temperature=0.1))
        from utils.usage_tracker import log_gemini
        log_gemini(resp, model, op='booking.lookup')
        gm = resp.candidates[0].grounding_metadata if resp.candidates else None
        if gm and gm.grounding_chunks:
            break
    out = {v: '' for v in FIELDS.values()}
    for line in (resp.text or '').splitlines():
        m = re.match(r'\s*\**\s*(\S+?)\s*\**\s*:\s*(.+)$', line)
        if m and m.group(1) in FIELDS:
            val = m.group(2).strip().strip('*').strip()
            out[FIELDS[m.group(1)]] = '' if re.search(r'לא ידוע|אין|unknown', val) else val
    if m := re.search(r'https?://\S+', out['url']):
        out['url'] = m.group(0).rstrip(').,')
    else:
        out['url'] = ''
    out['sure'] = out['sure'].startswith('כן')
    if m := re.search(r'https?://\S+', out.get('site', '')):
        out['site'] = m.group(0).rstrip(').,')
    else:
        out['site'] = ''
    return out


def find_ontopo(info: dict, session=None) -> str:
    """An Ontopo page even when the web search returned the restaurant's own site:
    'https://www.arestauranttlv.com/' → Ontopo page 'arestauranttlv' (if it exists). '' if none."""
    import requests
    if p := ontopo_page(info.get('url', '')):
        return p
    s = session or requests.Session()
    s.headers.update({'User-Agent': UA})
    cands = []
    for u in (info.get('url', ''), info.get('site', '')):
        if m := re.search(r'https?://(?:www\.)?([a-z0-9-]+)\.', u or '', re.I):
            cands.append(m.group(1).lower())
    for c in dict.fromkeys(cands):
        try:
            if '"postSlug":"' in s.get(f'https://ontopo.com/he/il/page/{c}', timeout=15).text:
                return c
        except Exception:
            continue
    return ''


# ── Ontopo ─────────────────────────────────────────────────────────────────

def ontopo_page(url: str) -> str:
    """'https://ontopo.co.il/arestauranttlv' / '…/page/arestauranttlv' → 'arestauranttlv' ('' if not Ontopo)."""
    m = re.search(r'ontopo\.(?:co\.il|com)/(?:[a-z]{2}/[a-z]{2}/page/)?([A-Za-z0-9_-]+)', url or '')
    return m.group(1) if m and m.group(1) not in ('he', 'en') else ''


def ontopo_availability(page: str, date: str, time: str, size: int, session=None) -> dict:
    """Ontopo's public availability search (what its own 'מצאו לי שולחן' button calls).
    → {'areas': [(area, [(HH:MM, method)])], 'link': prefilled booking page}. date YYYY-MM-DD, time HH:MM."""
    import requests
    s = session or requests.Session()
    s.headers.update({'User-Agent': UA, 'Accept-Language': 'he', 'Origin': 'https://ontopo.com',
                      'Referer': f'https://ontopo.com/he/il/page/{page}'})
    html = s.get(f'https://ontopo.com/he/il/page/{page}', timeout=20).text
    m = re.search(r'"slug_venue":"(\d+)","postSlug":"(\d+)"', html)
    if not m:
        raise ValueError('לא מצאתי את המסעדה באונטופו')
    venue_id, post = m.groups()
    tok = s.post('https://ontopo.com/api/loginAnonymously', timeout=20).json()['jwt_token']
    d8, t4 = date.replace('-', ''), time.replace(':', '')
    body = {'slug': post, 'locale': 'he', 'criteria': {'date': d8, 'time': t4, 'size': str(size)},
            'data': {'analytics': {'market_id': '', 'venue_id': venue_id, 'referrer_domain': None,
                                   'device_id': str(uuid.uuid4()), 'platform': 'web', 'distributor_id': 'il',
                                   'origin': 'venue_page', 'sub_origin': None}}}
    r = s.post('https://ontopo.com/api/availability_search', json=body, headers={'token': tok}, timeout=20)
    r.raise_for_status()
    areas = [(a.get('name', ''), [(f"{o['time'][:2]}:{o['time'][2:]}", o.get('method', '')) for o in a.get('options', [])])
             for a in r.json().get('areas', [])]
    return {'areas': areas, 'link': f'https://ontopo.com/he/il/page/{page}?date={d8}&time={t4}&size={size}'}


def free_slots(av: dict, time: str, n: int = 2) -> list[tuple[str, str]]:
    """The free ('seat') times nearest to the requested one → [(HH:MM, area)]."""
    want = int(time[:2]) * 60 + int(time[3:])
    free = {(t, area) for area, opts in av.get('areas', []) for t, m in opts if m == 'seat'}
    return sorted(free, key=lambda x: (abs(int(x[0][:2]) * 60 + int(x[0][3:]) - want), x[0]))[:n]


def slot_link(page: str, date: str, time: str, size: int) -> str:
    return f'https://ontopo.com/he/il/page/{page}?date={date.replace("-", "")}&time={time.replace(":", "")}&size={size}'


def availability_text(av: dict, time: str) -> str:
    """'ב-20:00 יש רק רשימת המתנה. פנוי: 19:30 (מרפסת), 21:00 (בר)'."""
    at = [(area, m) for area, opts in av['areas'] for t, m in opts if t == time]
    free = [(t, area) for area, opts in av['areas'] for t, m in opts if m == 'seat']
    if any(m == 'seat' for _, m in at):
        areas = ', '.join(a for a, m in at if m == 'seat')
        return f'✅ יש מקום ב-{time}' + (f' ({areas})' if areas else '')
    head = f"ב-{time}: {METHOD_HE.get(at[0][1], 'אין') if at else 'אין מקום'}"
    if not free:
        return f'😕 {head}, ואין שולחן פנוי בשעות הקרובות.'
    near = sorted(set(free), key=lambda x: abs(int(x[0][:2]) * 60 + int(x[0][3:]) - int(time[:2]) * 60 - int(time[3:])))[:3]
    return f'😕 {head}.\n✅ פנוי: ' + ', '.join(f'{t} ({a})' for t, a in near)
