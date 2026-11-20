"""
agent/event_reminders.py — reminders IN THE CHAT before calendar events (The owner 09/10: "שתזכיר לי גם בצ'אט על אירועים").

Defaults (no LLM — the worker checks every minute, the calendar is read at most every 5 minutes):
  • a timed event → 60 min before ("u_lead" — "תזכיר לי שעתיים לפני כל אירוע" changes it)
  • a lesson from the timetable sync (tag tau_src=lessons) → 20 min before, with the room
  • exams → nothing here (the exam-eve / exam-morning messages cover them) · all-day events → the morning brief
  • an event starting before 09:00 → a heads-up the evening before (21:30): the bot is silent 23:00–07:00
Per event: "תזכיר לי על המסעדה יום לפני" / the buttons after adding an event / "בלי תזכורת ל…".
Leads are minutes before the start; for an all-day event, before its 00:00 (240 = 20:00 the evening before).
State in data/event_reminders.json (shared by the owner and partner flows): settings, overrides, what was sent.
"""
from __future__ import annotations

import json
import os
import re
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'event_reminders.json'
DEFAULT_LEAD = 60
LESSON_LEAD = 20
WINDOW_MIN = 15                     # a reminder due up to 15 min ago still goes out (worker restart, slow tick)
EXAM_RX = re.compile(r'מבחן|בחינה|מועד [אבג]')
PREP_RX = re.compile(r'יום הולדת|יומולדת|יום נישואין|חתונה|בר מצווה|בת מצווה|ברית|טיול|חופשה|טיסה|נסיעה|חג|ארוחת חג')
_cache: dict = {'at': 0.0, 'events': []}
_tt_cache: dict = {'at': 0.0, 'rows': []}


def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STORE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    d.setdefault('settings', {'lead': DEFAULT_LEAD, 'lessons': LESSON_LEAD, 'on': True})
    d.setdefault('override', {})
    d.setdefault('sent', {})
    return d


def _save(d: dict, path: Path | None = None, now: datetime | None = None) -> None:
    p = path or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    cutoff = ((now or datetime.now()) - timedelta(days=3)).isoformat()   # the caller's clock (02/11: tests ran on 26/10)
    d['sent'] = {k: v for k, v in d['sent'].items() if v >= cutoff}          # keep the file small
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# ── settings (from the chat) ───────────────────────────────────────────────

def set_override(event_id: str, leads: list[int], path: Path | None = None) -> None:
    d = _load(path)
    d['override'][event_id] = sorted(set(int(x) for x in leads), reverse=True)
    _save(d, path)


def set_setting(path: Path | None = None, **kw) -> None:
    d = _load(path)
    d['settings'].update(kw)
    _save(d, path)


_UNITS = {'דקה': 1, 'דקות': 1, "דק'": 1, 'דק׳': 1, 'שעה': 60, 'שעות': 60, 'יום': 1440, 'ימים': 1440}


def parse_lead(text: str) -> int | None:
    """'שעתיים לפני' → 120 · 'חצי שעה לפני' → 30 · 'יום לפני' → 1440 · '45 דקות לפני' → 45 · 'רבע שעה' → 15."""
    t = text or ''
    for word, m in (('חצי שעה', 30), ('רבע שעה', 15), ('שעה וחצי', 90), ('שעתיים', 120), ('יומיים', 2880),
                    ('שבוע', 10080), ('ערב לפני', 240)):
        if word in t:
            return m
    if m := re.search(r'(\d{1,3})\s*(דקות|דקה|דק[\'׳]|שעות|שעה|ימים|יום)', t):
        return int(m.group(1)) * _UNITS.get(m.group(2), 1)
    if re.search(r'\bיום\b', t):
        return 1440
    if re.search(r'\bשעה\b', t):
        return 60
    return None


# ── which reminders are due ────────────────────────────────────────────────

def _start(ev: dict) -> tuple[datetime | None, bool]:
    st = ev.get('start') or {}
    if 'dateTime' in st:
        return datetime.fromisoformat(st['dateTime']).replace(tzinfo=None), False
    if st.get('date'):
        return datetime.fromisoformat(st['date']), True
    return None, False


def _is_lesson(ev: dict) -> bool:
    return ((ev.get('extendedProperties') or {}).get('private') or {}).get('tau_src') == 'lessons' \
        or ev.get('_cal_kind') == 'lessons'


def leads_for(ev: dict, d: dict) -> list[int]:
    if ev.get('id') in d['override']:
        return d['override'][ev['id']]
    if not d['settings'].get('on', True):
        return []
    start, all_day = _start(ev)
    if ((ev.get('extendedProperties') or {}).get('private') or {}).get('ssa_src') == 'obligation':
        return []                                     # 20/11 a deadline event: agent/deadline_ladder speaks for it
    if EXAM_RX.search(ev.get('summary') or '') or (ev.get('summary') or '').lstrip().startswith(('❌', '✅')):
        return []                                     # 20/10: "⏰ בעוד 20 דקות — ❌ בוטל — …" ×3 in one day
    from agent import office_hours
    if office_hours.is_office_hours(ev):              # 02/11: the lecturer's Zoom hours — 30 min before, if free
        return [office_hours.LEAD_MIN]
    if all_day:          # a birthday / trip / flight needs the evening before (a gift, packing) — the rest: the brief
        return [240] if PREP_RX.search(ev.get('summary') or '') else []
    return [d['settings'].get('lessons', LESSON_LEAD)] if _is_lesson(ev) else [d['settings'].get('lead', DEFAULT_LEAD)]


def _events(now: datetime, fetch=None) -> list[dict]:
    if fetch is None and _time.time() - _cache['at'] < 300:
        return _cache['events']
    if fetch is None:
        from integrations import gcal
        if not gcal.configured():
            return []

        def fetch():
            out = []
            for i in range(3):                       # today, tomorrow, the day after (a "day before" reminder)
                out += gcal.events_on((now + timedelta(days=i)).date())
            return out
        evs = fetch()
        _cache.update(at=_time.time(), events=evs)
        return evs
    return fetch()


PRIMER = ROOT / 'data' / 'primer_sent.json'


def primer_sent(start: datetime, path: Path | None = None) -> bool:
    """scripts/pre_lecture_primer.py already sent this lesson's "⏰ עוד מעט" (its key: 'YYYY-MM-DD:course:kind:HH:MM').
    The owner 03/11: reminders "בדיוק שעה לפני" — the primer is that one; on 03/11 every class got it and a second ping 40 min later."""
    try:
        sent = json.loads((path or PRIMER).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    day, hhmm = f'{start:%Y-%m-%d}:', f':{start:%H:%M}'
    return any(k.startswith(day) and k.endswith(hhmm) for k in sent)


def _site_moved(ev: dict) -> bool:
    """The university site has another room for it: then the 20-minute reminder carries news and still goes out."""
    try:
        from agent import lesson_sync
        return bool(lesson_sync.site_place(ev))
    except Exception:
        return False


def _cancelled(ev: dict, path: Path | None = None) -> bool:
    try:
        from agent import schedule
        return schedule.cancelled(ev, path=(path.parent / 'schedule_overrides.json') if path else None)
    except Exception:
        return False


def due(now: datetime | None = None, fetch=None, path: Path | None = None, primer_path: Path | None = None) -> list[str]:
    """Reminder texts due now (each sent once). Includes the 21:30 heads-up for tomorrow's early events."""
    now = now or datetime.now()
    d = _load(path)
    out = []
    primer_path = primer_path or (path.parent / 'primer_sent.json' if path else None)    # a test's store: its own folder
    events = _events(now, fetch)
    from agent import daytools, office_hours
    closed = daytools._closed_items(None) if events else []
    for ev in events:
        start, all_day = _start(ev)
        if not start or (start < now - timedelta(minutes=5) and not (all_day and start.date() == now.date())):
            continue
        if daytools._closed_event(ev, closed):             # 05/11: The owner closed this — no reminder for its calendar entry
            continue
        if _is_lesson(ev) and _cancelled(ev, path):        # 16/11 the university cancelled it (agent/schedule)
            continue
        for lead in leads_for(ev, d):
            at = start - timedelta(minutes=lead)
            key = f"{ev.get('id')}|{start.isoformat()}|{lead}"
            if at <= now < at + timedelta(minutes=WINDOW_MIN) and key not in d['sent']:
                d['sent'][key] = now.isoformat(timespec='seconds')
                if _is_lesson(ev) and lead == d['settings'].get('lessons', LESSON_LEAD) and ev.get('id') not in d['override'] \
                        and primer_sent(start, primer_path) and not _site_moved(ev):
                    continue        # 16/11 the "⏰ עוד מעט: … · room" primer went out an hour before: a second ping is noise
                if office_hours.is_office_hours(ev):
                    if office_hours.busy_with(ev, events):     # "אם אני פנוי אצטרף" — busy → no reminder at all
                        continue
                    zoom = office_hours.zoom_of(ev)
                    out.append(text_for(ev, start, all_day, lead, now) + '\n✅ אתה פנוי בשעה הזו'
                               + (f'\n🔗 {zoom}' if zoom else ''))
                    continue
                out.append(text_for(ev, start, all_day, lead, now))
        early = (not all_day and start.date() == (now + timedelta(days=1)).date() and start.hour < 9
                 and leads_for(ev, d) and min(leads_for(ev, d)) < 1440)
        if early and now.hour == 21 and now.minute >= 30:
            key = f"{ev.get('id')}|{start.isoformat()}|eve"
            if key not in d['sent']:
                d['sent'][key] = now.isoformat(timespec='seconds')
                out.append(f"🌙 *מחר מוקדם:* {start:%H:%M} — {ev.get('summary', '')}" + _where(ev))
    if d['settings'].get('leave'):                     # 19/11 opt-in: "כדאי לצאת עכשיו" before a walk across the campus
        try:
            out += leave_now(now, events, d)
        except Exception as e:
            print(f'  ⚠️ leave-now: {e!r}'[:200])
    if out:
        _save(d, path, now)
    return out


def leave_now(now: datetime, events: list[dict], d: dict, timetable=None) -> list[str]:
    """The next session is in another building and the break is shorter than the walk (agent/campus): one line at the
    minute to leave. Only with settings['leave'] (off by default: "תזכיר לי לצאת בין שיעורים" turns it on)."""
    from agent import campus, proactive
    if timetable is None:                              # the my.tau cache, read at most every 5 minutes
        if _time.time() - _tt_cache['at'] > 300:
            from agent import fastpath
            _tt_cache.update(at=_time.time(), rows=fastpath.sessions(now))
        timetable = _tt_cache['rows']
    evs = proactive.with_timetable(now, list(events), sessions=timetable)
    timed = sorted(((a, b, ev) for ev in evs for a, b, allday in [proactive._span(ev)]
                    if a and not allday and a.date() == now.date() and proactive._countable(ev)), key=lambda x: x[0])
    out = []
    for e1, b1, e2, a2, hop in proactive._hops(timed):
        at = campus.leave_at(a2, hop)
        key = f"leave|{e2.get('id')}|{a2.isoformat()}"
        if at <= now < at + timedelta(minutes=5) and key not in d['sent']:
            d['sent'][key] = now.isoformat(timespec='seconds')
            out.append(f"🚶 *כדאי לצאת עכשיו* — {a2:%H:%M} {proactive._short(e2)} ב{hop['to']} "
                       f"(~{hop['walk']} דק׳ הליכה מ{hop['frm']}).")
    return out


def _where(ev: dict) -> str:
    loc = (ev.get('location') or '').strip()
    if _is_lesson(ev):                                      # a lesson: the site's room wins, and no Waze to a classroom
        try:
            from agent import lesson_sync
            site = lesson_sync.site_place(ev)
        except Exception:
            site = ''
        if site:
            return f"\n📍 *{site}* (לפי אתר האוניברסיטה — ביומן כתוב: {loc or 'בלי מקום'})"
        return f"\n📍 {loc}" if loc else ''
    if not loc:
        return ''
    nav = '' if re.search(r'חדר|אודיטוריום|כית[הו]|zoom|meet\.google|אוניברסיט', loc, re.I) else \
        f"\n🚗 https://waze.com/ul?q={quote(loc)}&navigate=yes"
    return f"\n📍 {loc}{nav}"


def _lead_words(lead: int) -> str:
    return {15: 'בעוד רבע שעה', 20: 'בעוד 20 דקות', 30: 'בעוד חצי שעה', 60: 'בעוד שעה', 90: 'בעוד שעה וחצי',
            120: 'בעוד שעתיים', 1440: 'מחר'}.get(lead) or (f'בעוד {lead} דקות' if lead < 60 else
                                                            f'בעוד {lead // 60} שעות' if lead < 1440 else f'בעוד {lead // 1440} ימים')


def text_for(ev: dict, start: datetime, all_day: bool, lead: int, now: datetime) -> str:
    title = ev.get('summary', '')
    if all_day:
        when = 'היום' if start.date() == now.date() else 'מחר' if start.date() == (now + timedelta(days=1)).date() \
            else f'{start:%d/%m}'
        return f"⏰ *{when}:* {title}" + _where(ev)
    words = _lead_words(lead) if lead < 1440 else ('מחר' if start.date() == (now + timedelta(days=1)).date()
                                                   else _lead_words(lead))
    return f"⏰ *{words}* · {start:%H:%M} — {title}" + _where(ev)
