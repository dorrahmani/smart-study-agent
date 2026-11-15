"""
agent/lesson_sync.py — the university site vs the owner's "לימודי תשפ"ז" calendar: a changed room, a moved lesson, a
cancelled one → a ready-made calendar change (agent/core._edit_offer) that waits for his ✅.

The owner 15/10: "if a lecturer says a lesson was cancelled / moved / changed room — tell me and change the calendar".
Mail and Moodle are read by integrations/tau_mail (lesson_change); this is the third source — my.tau's own
timetable (data/tau_calendar.json, refreshed 3×/day by scripts/tau_watch.py). No LLM: plain matching.

Conservative on purpose: a calendar lesson counts as moved/cancelled only if the site lists that very course+kind
on other days (the site doesn't list every exercise group — the probability exercise is missing entirely).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

CAMPUS = 'אוניברסיטת תל אביב'
CANCELLED = '❌ בוטל — '
_STOP = {'מבוא', 'ו', 'של', 'ל', 'ב', 'א', 'ב׳', "ב'", 'א׳', "א'", 'שיעור', 'תרגיל', 'קב׳'}
_KINDS = ('שיעור', 'תרגיל')


def _words(s: str) -> set[str]:
    s = re.sub(r'\(.*?\)', ' ', s or '')
    out = set()
    for w in re.findall(r'[\w׳\'"]+', s):
        w = w.strip('\'"׳')
        if len(w) > 1 and w not in _STOP and not w.isdigit():
            out.add(w)
            if w[0] in 'לבהמו' and len(w) > 3:              # "להנדסת" ~ "הנדסת"
                out.add(w[1:])
    return out


def _cal_parts(ev: dict) -> tuple[str, str, str]:
    """'תוכנה 1 – תרגיל (קב׳ 03)' → ('תוכנה 1', 'תרגיל', '03')."""
    s = ev.get('summary') or ''
    kind = next((k for k in _KINDS if k in s), '')
    m = re.search(r'קב[׳\']?\s*(\d+)', s)
    return re.split(r'\s[–-]\s', s)[0].strip(), kind, (m.group(1).zfill(2) if m else '')


def _same_course(ev: dict, p: dict) -> bool:
    course, kind, group = _cal_parts(ev)
    if not kind or p.get('kind') != kind:
        return False
    if group and p.get('group') and group != p['group']:
        return False
    a, b = _words(course), _words(p.get('course', ''))
    if a & b:
        return True
    try:                                                    # 'תוכנה 1' ↔ 'מבוא להנדסת תוכנה' via config/paths
        from config.paths import COURSE_ALT_NAMES, COURSE_NAMES
        for ck, name in COURSE_NAMES.items():
            names = [name, *COURSE_ALT_NAMES.get(ck, [])]
            if any(_words(n) & a for n in names) and any(_words(n) & b for n in names):
                return True
    except Exception:
        pass
    return False


def place(p: dict) -> str:
    return ' '.join(x for x in (p.get('building', ''), p.get('room', '')) if x).strip()


def _room_differs(location: str, p: dict) -> bool:
    if not p.get('room') and not p.get('building'):
        return False                                        # the site doesn't say — nothing to compare
    loc = location or ''
    same_building = not p.get('building') or bool(_words(p['building']) & _words(loc))
    if not same_building:
        return True
    # 19/10: "לב אודיטוריום 009" (site) vs "אולם לב (Lev Auditorium)" (calendar, from a Moodle post) is the same hall —
    # only a DIFFERENT room number in the calendar counts as a change
    nums = re.findall(r'\b\d{2,4}\b', loc)
    return bool(p.get('room')) and bool(nums) and p['room'] not in nums


def _start(ev: dict) -> datetime | None:
    st = (ev.get('start') or {}).get('dateTime')
    return datetime.fromisoformat(st) if st else None


def diff(portal: list[dict], lessons: list[dict], today: date, days: int = 14) -> list[dict]:
    """→ plans in agent/calendar_edit.plan's shape ({'event', 'title', 'location', 'date', 'start', 'end', 'explain'})."""
    portal = [p for p in portal if not p.get('is_exam') and p.get('time') and p.get('kind') in _KINDS]
    if not portal:
        return []
    last = max(p['date'] for p in portal)
    horizon = min((today + timedelta(days=days)).isoformat(), last)
    used, plans = set(), []
    for ev in lessons:
        s = _start(ev)
        if not s or (ev.get('summary') or '').startswith(CANCELLED):
            continue
        d, t = s.date().isoformat(), s.strftime('%H:%M')
        if not today.isoformat() <= d <= horizon:
            continue
        mine = [i for i, p in enumerate(portal) if _same_course(ev, p)]
        if not mine:
            continue                                        # the site doesn't track this lesson at all
        exact = next((i for i in mine if portal[i]['date'] == d and portal[i]['time'] == t and i not in used), None)
        if exact is not None:
            used.add(exact)
            p = portal[exact]
            if _room_differs(ev.get('location', ''), p):
                plans.append({'event': ev, 'title': None, 'location': f'{place(p)}, {CAMPUS}', 'date': None,
                              'start': None, 'end': None, 'explain': f'באתר האוניברסיטה החדר הוא {place(p)}'})
            continue
        same_day = next((i for i in mine if portal[i]['date'] == d and i not in used), None)
        if same_day is not None:                            # same day, another hour
            used.add(same_day)
            p = portal[same_day]
            plans.append({'event': ev, 'title': None, 'location': None, 'date': None, 'start': p['time'],
                          'end': p.get('end_time') or None, 'explain': f"באתר האוניברסיטה השעה היא {p['time']}"})
            continue
        # not on that day: a lesson of this course on the site on a day the calendar doesn't have → moved; else cancelled
        cal_days = {(_start(e) or datetime.min).date().isoformat() for e in lessons if _same_course(e, portal[mine[0]])}
        week = (s - timedelta(days=s.weekday() + 1)).date()   # Sunday-based week
        moved = next((i for i in mine if i not in used and portal[i]['date'] not in cal_days
                      and week <= date.fromisoformat(portal[i]['date']) < week + timedelta(days=7)), None)
        if moved is not None:
            used.add(moved)
            p = portal[moved]
            plans.append({'event': ev, 'title': None, 'location': None, 'date': p['date'], 'start': p['time'],
                          'end': p.get('end_time') or None, 'explain': f"באתר האוניברסיטה זה עבר ל-{p['date']} {p['time']}"})
        else:
            plans.append({'event': ev, 'title': CANCELLED + (ev.get('summary') or ''), 'location': None, 'date': None,
                          'start': None, 'end': None, 'explain': 'השיעור כבר לא מופיע באתר האוניברסיטה'})
    return plans


def site_place(ev: dict, portal: list[dict] | None = None) -> str:
    """The room the university site gives for this calendar lesson, when it differs from the calendar's; else ''.
    15/10: a reminder must not send the owner to last week's room just because he hasn't ✅'d the update yet."""
    s = _start(ev)
    if not s:
        return ''
    if portal is None:
        try:
            from integrations import tau_portal
            portal = tau_portal.schedule(live=False)[0] or []
        except Exception:
            return ''
    d, t = s.date().isoformat(), s.strftime('%H:%M')
    p = next((p for p in portal if p.get('date') == d and p.get('time') == t and _same_course(ev, p)), None)
    return place(p) if p and _room_differs(ev.get('location', ''), p) else ''


def key(p: dict) -> str:
    """One offer per change — a ❌ is never asked again for the same thing. A new room is keyed by the lesson SERIES
    (title + old room + new room): 18/10 the probability room was asked 3 times — once ✅'d for one lecture, the
    next week's lecture of the same series looked like a new change."""
    ev = p['event']
    if p.get('location') and not any(p.get(f) for f in ('title', 'date', 'start')):
        return '|'.join(['room', ev.get('summary', ''), ev.get('location', '') or '', p['location']])
    return '|'.join(str(x) for x in (ev.get('id'), p.get('title'), p.get('location'), p.get('date'), p.get('start')))


def check(today: date | None = None, service=None) -> list[dict]:
    """The live run: cache + calendar → plans. [] when the site cache is stale (>36h) or the calendar isn't set up."""
    from integrations import gcal, tau_portal
    today = today or date.today()
    if not any(c.get('kind') == 'lessons' and c.get('write') for c in gcal.read_calendars()):
        return []
    events, fetched, _ = tau_portal.schedule(live=False)
    try:
        if fetched and datetime.now() - datetime.fromisoformat(str(fetched)[:19]) > timedelta(hours=36):
            return []
    except ValueError:
        pass
    from agent import calendar_edit
    lessons = [e for e in calendar_edit.upcoming(days=16, service=service) if e.get('_cal_kind') == 'lessons']
    return diff(events or [], lessons, today)
