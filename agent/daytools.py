"""
agent/daytools.py — small, deterministic helpers the agent kept getting wrong (06/10 stress test):

  parse_day("אני פנוי ביום שישי?")      → the next Friday
  parse_day("המשתמש פנוי ב-25 לאוקטובר?")  → 2026-10-25
  looks_like_time_answer("ב-18:00")     → True  (only such replies may continue a calendar draft)
  day_overview(day)                      → calendar + lectures (my timetable) + homework due + reminders
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAYS = {'ראשון': 6, 'שני': 0, 'שלישי': 1, 'רביעי': 2, 'חמישי': 3, 'שישי': 4, 'שבת': 5}
MONTHS = {'ינואר': 1, 'פברואר': 2, 'מרץ': 3, 'מרס': 3, 'אפריל': 4, 'מאי': 5, 'יוני': 6, 'יולי': 7, 'אוגוסט': 8,
          'ספטמבר': 9, 'אוקטובר': 10, 'נובמבר': 11, 'דצמבר': 12}
CODE_DAYS = {'sun': 6, 'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5}


def _future(d: date, today: date) -> date:
    return d if d >= today - timedelta(days=1) else d.replace(year=d.year + 1)


def parse_day(text: str, today: date | None = None) -> date | None:
    """A single day mentioned in Hebrew, or None."""
    t = text or ''
    today = today or date.today()
    if 'מחרתיים' in t:
        return today + timedelta(days=2)
    if re.search(r'(?<!\w)[לוב]?מחר(?!\w)', t):            # 02/11 "מה הלוז שלי למחר" got the whole week
        return today + timedelta(days=1)
    if re.search(r'(?<!\w)[לו]?היום(?!\w)|(?<!\w)[לו]?הערב(?!\w)', t):
        return today
    m = re.search(r'(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)', t)
    if m:
        dd, mm, yy = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            y = int(yy) + (2000 if yy and len(yy) == 2 else 0) if yy else today.year
            d = date(y, mm, dd)
            return d if yy else _future(d, today)
        except ValueError:
            pass
    m = re.search(r'(?<!\d)(\d{1,2})\s*(?:ב|ל)?(' + '|'.join(MONTHS) + ')', t)
    if m:
        try:
            return _future(date(today.year, MONTHS[m.group(2)], int(m.group(1))), today)
        except ValueError:
            pass
    m = re.search(r'(?:ביום|יום|ב)\s*(ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)(?!\w)', t)
    if m:
        target = DAYS[m.group(1)]
        ahead = (target - today.weekday()) % 7
        if 'הבא' in t[m.end():m.end() + 8] and ahead == 0:
            ahead = 7
        return today + timedelta(days=ahead)
    return None


TIME_ANSWER = re.compile(r'\d{1,2}[:.]\d{2}|(?<!\d)\d{1,2}[./]\d{1,2}|בשעה|(?<!\w)ב-?\d{1,2}(?!\d)|בבוקר|בערב|'
                         r'בצהריים|בלילה|כל היום|(?<!\w)(?:ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)(?!\w)|מחר|היום|'
                         r'שעה|דקות|' + '|'.join(MONTHS))


def looks_like_time_answer(text: str) -> bool:
    """Only a short reply about time/date may continue a pending calendar draft."""
    t = (text or '').strip()
    return len(t) <= 60 and bool(TIME_ANSWER.search(t))


def _portal_lessons(day: date) -> list[str] | None:
    """That day's lessons from the my.tau cache (current rooms). None if the cache doesn't reach that day."""
    try:
        from integrations import tau_portal
        from agent import timetable
        events, _, _ = tau_portal.schedule(live=False)
    except Exception:
        return None
    if not events or not any(e['date'] >= day.isoformat() for e in events):
        return None
    out = []
    for e in timetable.lessons(events, day):
        if e['date'] == day.isoformat():
            end = f"–{e['end_time']}" if e.get('end_time') else ''
            place = timetable.place(e)
            out.append((e['time'], f"{e['time']}{end} {e['kind']} ב{timetable.short(e['course'])}" + (f' · {place}' if place else '')
                        + (f" · {e['lecturer']}" if e.get('lecturer') else '') + _portal_override(e, day)))
    return [x for _, x in sorted(out)]


def _portal_override(e: dict, day: date) -> str:
    """17/11 a my.tau lesson the university changed (agent/schedule) → " — ❌ מבוטל (הודעה במודל): …" on its line too
    (it was only on calendar lines; without the lessons in the calendar the cancelled tutorial was listed as usual)."""
    try:
        from agent import schedule
        return schedule.suffix(schedule.find(day, f"{e.get('course', '')} – {e.get('kind', '')}", e.get('time', '')))
    except Exception:
        return ''


def lectures_on(day: date) -> list[str]:
    """This semester's sessions on that day — from the university site (13/10: the config still had last month's room,
    'בניין לדוגמה 436' vs the site's 'בניין לדוגמה 514'); the config's my_sessions only as a fallback. Semester only."""
    from config.features import courses_config
    from config.paths import ACTIVE_COURSES, COURSE_NAMES
    cfg = courses_config()                          # {} in assistant-only mode → no lectures, never a crash
    if not cfg:
        return []
    sched = ((cfg.get('settings') or {}).get('schedule') or {})
    try:
        if not (date.fromisoformat(str(sched.get('semester_start'))) <= day <= date.fromisoformat(str(sched.get('semester_end')))):
            return []
    except ValueError:
        return []
    if str(day) in {str(x) for x in sched.get('no_class_dates') or []}:
        return []
    portal = _portal_lessons(day)
    if portal is not None:
        return portal
    courses = cfg.get('courses', cfg)
    out = []
    for ck in ACTIVE_COURSES:
        for s in (courses.get(ck) or {}).get('my_sessions') or []:
            if CODE_DAYS.get(s.get('day')) == day.weekday():
                kind = 'הרצאה' if s.get('type') == 'lecture' else 'תרגול'
                out.append((s.get('start', ''), f"{s.get('start')}–{s.get('end')} {kind} ב{COURSE_NAMES.get(ck, ck)}"
                            + (f" · {s['location']}" if s.get('location') else '')))
    return [x for _, x in sorted(out)]


def _ended(it: dict, after: datetime | None) -> bool:
    """A timed event that is over by `after` (naive Israel time)."""
    end = (it.get('end') or {}).get('dateTime') or (it.get('start') or {}).get('dateTime')
    if not after or not end:
        return False
    try:
        from zoneinfo import ZoneInfo
        e = datetime.fromisoformat(end.replace('Z', '+00:00'))
        if e.tzinfo:
            e = e.astimezone(ZoneInfo('Asia/Jerusalem'))
        return e.replace(tzinfo=None) <= after
    except ValueError:
        return False


def _lesson_over(line: str, day: date, after: datetime | None) -> bool:
    """A lectures_on() line ("08:00–10:00 הרצאה ב…") that is over by `after`. No end time → its start."""
    m = re.match(r'\s*(\d{1,2}:\d{2})(?:[–-](\d{1,2}:\d{2}))?', line)
    if not after or not m:
        return False
    h, mi = map(int, (m.group(2) or m.group(1)).split(':'))
    return datetime(day.year, day.month, day.day, h, mi) <= after


def _closed_items(chat: str | None) -> list[dict]:
    try:
        from agent import closure
        return closure.items(chat)
    except Exception:
        return []


def _closed_event(ev: dict, closed: list[dict]) -> bool:
    """05/11: a task's "✅ …" marker, or a calendar entry about something the owner closed (the library return was in the
    calendar twice) — the day's list doesn't bring it back. The event itself isn't touched."""
    from agent import closure
    title = ev.get('summary') or ''
    if ((ev.get('extendedProperties') or {}).get('private') or {}).get('ssa_src') == 'task' \
            and title.lstrip().startswith(('✅', '❌')):
        return True
    if not closed or ev.get('_cal_kind') == 'lessons' or title.lstrip().startswith(('✅', '❌')):
        return False
    st = ev.get('start') or {}
    return bool(closure.match(closed, title, when=(st.get('dateTime') or st.get('date') or '')[:10], mode='task'))


def day_overview(day: date, chat: str | None = None, hide_private: bool = False, after: datetime | None = None) -> str:
    """Everything about one day: calendar, lectures, homework due, own reminders.
    after: "מה נשאר לי היום" — what already ended is left out (03/11 eval: the 08:00–12:00 lessons came at 15:05)."""
    from integrations import gcal
    from integrations.gcal import WEEKDAY_HE
    parts = []
    try:
        items = gcal.events_on(day) if gcal.configured() else []
    except Exception as e:
        print(f'  ⚠️ day overview calendar failed: {e}')
        items = []
    try:
        from utils.deadlines import submitted_title
    except Exception:
        submitted_title = None
    closed = _closed_items(chat)
    ours = {((it.get('extendedProperties') or {}).get('private') or {}).get('oid') for it in items} - {None}
    for it in items:
        if _ended(it, after) or _closed_event(it, closed):
            continue
        st = it.get('start') or {}
        t = datetime.fromisoformat(st['dateTime']).strftime('%H:%M') if 'dateTime' in st else 'כל היום'
        line = f"• {t} — {gcal._summary(it, hide_private)}"
        if submitted_title and submitted_title(it.get('summary') or ''):    # 03/11 eval: "הגשת תרגיל 2" — already in
            line += ' — ✅ כבר הגשת'
        if it.get('_cal_kind') == 'lessons' and not hide_private:   # a lesson: where (the site's room if it changed)
            try:
                from agent import lesson_sync
                site = lesson_sync.site_place(it)
            except Exception:
                site = ''
            room = site or (it.get('location') or '').split(',')[0].strip()
            if room:
                line += f" · 📍 {room}" + (' (חדר חדש לפי האתר)' if site else '')
        if not hide_private:                               # 16/11 cancelled / moved by the university (agent/schedule)
            from agent import schedule
            line += schedule.event_suffix(it)
        parts.append(line)
    synced = any(((it.get('extendedProperties') or {}).get('private') or {}).get('tau_src') == 'lessons'
                 or it.get('_cal_kind') == 'lessons' for it in items)
    if not hide_private and not synced:                    # lessons already in the calendar → not twice
        parts += [f'• 🎓 {x}' for x in lectures_on(day) if not _lesson_over(x, day, after)]
    if not hide_private:
        try:
            from utils.deadlines import pending
            for a in pending(horizon_days=60):
                if a['due'].date() == day and not (after and a['due'] <= after) and a['key'] not in ours:
                    from config.paths import COURSE_NAMES
                    parts.append(f"• 📝 הגשה {a['due']:%H:%M}: {COURSE_NAMES.get(a.get('course_key'), '')} — {a.get('title', '')}")
        except Exception:
            pass
        if chat:
            try:
                from agent.life import _load
                for r in _load()['reminders']:
                    if r.get('chat') == chat and r.get('active') and r['due'][:10] == day.isoformat():
                        parts.append(f"• ⏰ {r['due'][11:16]} תזכורת: {r['text']}")
            except Exception:
                pass
    head = f"🗓️ *יום {WEEKDAY_HE[day.weekday()]} {day:%d/%m}*" + (f" — מ-{after:%H:%M} והלאה" if after else '')
    return head + ('\n' + '\n'.join(parts) if parts else
                   ('\n✅ זהו, לא נשאר לך כלום להיום.' if after else '\n✅ פנוי — אין כלום ביומן.'))
