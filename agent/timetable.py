"""
agent/timetable.py — the owner's lessons from the university site (my.tau.ac.il) kept in his Google Calendar.

The university calendar only reaches ~a month ahead and already reflects cancellations (no tutorials on
election day 27/10) and rooms. So this is a ROLLING sync, not one recurring event: after the owner's one ✅,
every day it adds the lessons now inside the window, updates rooms, and removes lessons that vanished
from the site — reporting room changes / time changes / cancellations to him.

Safety: it only ever touches events it created (private tag tau_src=lessons); deletions only inside the
window the site actually returned, only future dates, and never when the site returned suspiciously few
lessons (a half-broken fetch must not wipe the week).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

LESSON_KINDS = ('שיעור', 'תרגיל', 'מעבדה', 'סדנה', 'סמינר')
TAG, SRC = 'tau_src', 'lessons'
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']


def lessons(events: list[dict], today: date | None = None) -> list[dict]:
    t = (today or date.today()).isoformat()
    return [e for e in events if not e['is_exam'] and e['time'] and e['date'] >= t
            and any(k in (e.get('kind') or '') for k in LESSON_KINDS)]


def key(e: dict) -> str:
    return f"{e.get('course_number') or e['course']}|{e['kind']}|{e['date']}|{e['time']}"


def short(course: str) -> str:
    """'מאקרו כלכלה: צמיחה כלכלית, …' → 'מאקרו כלכלה'; "מיקרו כלכלה ב' - כלכלת רווחה…" → "מיקרו כלכלה ב'"."""
    return re.split(r'\s*[:]\s*|\s+-\s+', course)[0].strip()


def staff(events: list[dict]) -> list[str]:
    """03/11 ("מי מתרגל בהסתברות" → the bot named the lecturer as the TA): who teaches each of the owner's own groups."""
    seen: dict[str, dict[str, str]] = {}
    for e in events or []:
        if e.get('is_exam') or not e.get('lecturer') or not any(k in (e.get('kind') or '') for k in LESSON_KINDS):
            continue
        role = 'מרצה' if 'שיעור' in e['kind'] else 'מתרגל/ת'
        grp = f" (קב' {e['group']})" if e.get('group') else ''
        seen.setdefault(short(e['course']), {}).setdefault(role, e['lecturer'] + grp)
    return [f"• {c} — " + ' · '.join(f'{r}: {n}' for r, n in sorted(rs.items(), key=lambda x: x[0] != 'מרצה'))
            for c, rs in seen.items()]


def place(e: dict) -> str:
    return ', '.join(x for x in (e.get('building', ''), f"חדר {e['room']}" if e.get('room') else '') if x)


def usual_place(events: list[dict], course_word: str, weekday: int, start: str, today: date | None = None) -> str:
    """03/11 eval: config/courses.yaml rooms are stale (probability דאך 005 → צ'ק פוינט 001, software TA בניין לדוגמה 486 →
    קפלון 118). The room of that weekly slot from the university site: its next meeting (else the latest one); ''
    when the site doesn't have the slot."""
    t = (today or date.today()).isoformat()
    slot = [e for e in events or [] if not e.get('is_exam') and (e.get('time') or '')[:5] == (start or '')[:5].zfill(5)
            and course_word and course_word in e.get('course', '') and (e.get('building') or e.get('room'))
            and date.fromisoformat(e['date']).weekday() == weekday]
    nxt = sorted((e for e in slot if e['date'] >= t), key=lambda e: e['date'])
    e = nxt[0] if nxt else (max(slot, key=lambda e: e['date']) if slot else None)
    return place(e) if e else ''


def to_event(e: dict) -> dict:
    h, m = map(int, e['time'].split(':'))
    start = datetime.fromisoformat(f"{e['date']}T{h:02d}:{m:02d}")
    end = e.get('end_time') or (start + timedelta(hours=2)).strftime('%H:%M')
    icon = '📘' if 'שיעור' in e['kind'] else '✏️'
    return {'title': f"{icon} {short(e['course'])} — {e['kind']}", 'start': f"{e['date']}T{e['time']}",
            'end': f"{e['date']}T{end}", 'location': place(e),
            'notes': ' · '.join(x for x in (e.get('lecturer', ''), 'מערכת השעות מאתר האוניברסיטה — מתעדכן אוטומטית') if x),
            'private': {TAG: SRC, 'tau_key': key(e)}, 'color': '9' if 'שיעור' in e['kind'] else '2',
            'no_reminders': True}


def preview_text(events: list[dict], today: date | None = None) -> str:
    ls = lessons(events, today)
    if not ls:
        return 'באתר האוניברסיטה עוד אין לך שיעורים בחודש הקרוב.'
    first = date.fromisoformat(ls[0]['date'])
    week = [e for e in ls if date.fromisoformat(e['date']) < first + timedelta(days=7)]
    lines, cur = [f"🗓️ *מערכת השעות — שבוע ראשון* (מאתר האוניברסיטה)"], None
    for e in week:
        if e['date'] != cur:
            d = date.fromisoformat(e['date'])
            lines.append(f"\n*יום {WEEKDAY_HE[d.weekday()]} {d:%d/%m}*")
            cur = e['date']
        end = f"–{e['end_time']}" if e.get('end_time') else ''
        lines.append(f"• {e['time']}{end} {short(e['course'])} ({e['kind']}) — {place(e) or 'חדר לא פורסם'}")
    return '\n'.join(lines)


def sync(events: list[dict], today: date | None = None, service=None) -> dict:
    """Bring the calendar in line with the site's lessons. → {'added', 'room', 'time', 'cancelled'}."""
    from integrations import gcal
    today = today or date.today()
    ls = lessons(events, today)
    res = {'added': [], 'room': [], 'time': [], 'cancelled': []}
    if not ls:
        return res
    hi = date.fromisoformat(max(e['date'] for e in events))
    svc = service or gcal._service()
    ours = {}
    for ev in gcal.list_tagged(TAG, SRC, today, hi, service=svc):
        k = ((ev.get('extendedProperties') or {}).get('private') or {}).get('tau_key')
        if k:
            ours[k] = ev
    for e in ls:
        ev = ours.pop(key(e), None)
        if ev is None:
            gcal.insert_event(to_event(e), service=svc)
            res['added'].append(e)
        elif place(e) and (ev.get('location') or '') != place(e):
            gcal.set_location(ev, place(e), service=svc)
            res['room'].append((ev.get('location') or '', e))
    # what's left of ours vanished from the site → cancelled (or moved to another time)
    gone = [ev for k, ev in ours.items() if today.isoformat() <= k.split('|')[2] <= hi.isoformat()]
    if gone and len(ls) < 3:                    # a near-empty fetch must not wipe the calendar
        return res
    added_by_slot = {(e.get('course_number') or e['course'], e['kind'], e['date']): e for e in res['added']}
    for ev in gone:
        k = ev['extendedProperties']['private']['tau_key'].split('|')
        gcal.delete_event(ev['id'], service=svc)
        moved = added_by_slot.get((k[0], k[1], k[2]))
        if moved:
            res['time'].append((k[3], moved))
            res['added'].remove(moved)
        else:
            res['cancelled'].append(ev)
    return res


def changes_text(res: dict) -> str:
    """The changes worth a message (new lessons as the window rolls forward are added silently)."""
    lines = []
    for old, e in res['room']:
        lines.append(f"📍 *שינוי חדר* — {short(e['course'])} ({e['kind']}) {e['date'][8:]}/{e['date'][5:7]}: "
                     f"*{place(e)}*" + (f" (היה: {old})" if old else ''))
    for old_time, e in res['time']:
        lines.append(f"🕐 *שינוי שעה* — {short(e['course'])} ({e['kind']}) {e['date'][8:]}/{e['date'][5:7]}: "
                     f"{old_time} → *{e['time']}*")
    for ev in res['cancelled']:
        k = ev['extendedProperties']['private']['tau_key'].split('|')
        lines.append(f"🚫 *בוטל* — {ev.get('summary', '')} {k[2][8:]}/{k[2][5:7]} {k[3]} (ירד מאתר האוניברסיטה, הסרתי מהיומן)")
    return ('🗓️ *עדכונים במערכת השעות*\n' + '\n'.join(lines)) if lines else ''
