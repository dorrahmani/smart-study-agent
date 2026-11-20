"""
agent/fastpath.py — the owner's everyday schedule questions answered straight from the data, no Gemini (17/11, improvement 1).

"מה הלוז שלי מחר", "מה יש לי השבוע", "מתי ההגשה הבאה", "מה מחכה לי", "איפה השיעור", "מתי השיעור הבא" each went
through the agent loop (one Gemini call or more, 4-7s, ~$0.015) — and 16/11 the model left a cancelled tutorial in.
The answer to each is a read of the stores that every other answer uses anyway:

    day / week   daytools.day_overview / gcal.week_text: the calendar + his lessons with the university's changes
                 (agent/schedule: "❌ מבוטל"), homework due, his reminders
    deadlines    agent/obligations (Moodle + lecturer sites), what's still open, soonest first
    sessions     the my.tau cache (rooms as the site has them) with the changes on top

Only a message that IS one of these questions, whole (a short full match), comes here; anything with more in it
("מה יש לי מחר בערב חוץ מהפאדל?") goes to the agent as before. answer() → the reply, or None (not one of these, or the
data couldn't be read) — None never answers anything.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
_WD = r'(?:ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)'
DAYREF = (rf'(?:ל|ב)?(?:היום|מחר|מחרתיים)|(?:ב|ל)?יום\s+{_WD}(?:\s+(?:הבא|הקרוב|הזה))?|(?:ב|ל){_WD}(?:\s+(?:הבא|הקרוב))?'
          r'|(?:ב|ל|ה)?-?\d{1,2}[./]\d{1,2}')
WHAT = (r'(?:יש\s+לי|יש|מתוכנן(?:\s+לי)?|קורה|הלוז(?:\s+שלי)?|הלו"ז(?:\s+שלי)?|הלוח\s+זמנים(?:\s+שלי)?|התוכניות(?:\s+שלי)?|'
        r'התכנית(?:\s+שלי)?|ביומן|בלוז|בלו"ז|יש\s+(?:לי\s+)?ביומן|יש\s+(?:לי\s+)?בלוז|יש\s+(?:לי\s+)?בלו"ז)')
TAIL = r'(?:\s+(?:ביומן|בלוז|בלו"ז|שלי))?'
DAY_Q = re.compile(rf'^ו?מה\s+{WHAT}\s+(?P<d>{DAYREF}){TAIL}$|^(?:ה)?(?:לוז|לו"ז)\s+(?:של\s+)?(?P<d2>{DAYREF})$|'
                   rf'^מה\s+(?P<left>נשאר)\s+לי\s+(?:ל|ב)?(?P<d3>היום)$')
WEEK_Q = re.compile(rf'^ו?מה\s+{WHAT}\s+(?P<w>השבוע|לשבוע|בשבוע\s+הבא|לשבוע\s+הבא|השבוע\s+הבא){TAIL}$|'
                    r'^(?:ה)?(?:לוז|לו"ז)\s+(?:של\s+)?(?P<w2>השבוע|לשבוע|השבוע\s+הבא)$')
DEADLINE_Q = re.compile(r'^(?:מתי|מה)\s+(?:ה)?(?:הגשה|דדליין|מטלה)\s+(?:ה)?(?:הבאה|הקרובה|הבא|הקרוב)(?:\s+שלי)?$|'
                        r'^(?:מה|מתי)\s+(?:ה)?(?:הגשות|דדליינים|מטלות)\s+(?:ה)?(?:הבאות|הקרובות|הבאים|הקרובים)(?:\s+שלי)?$|'
                        r'^מה\s+יש\s+לי\s+להגיש(?:\s+(?P<when>השבוע|היום|מחר))?$|^מה\s+(?:אני\s+)?צריך\s+להגיש(?:\s+(?P<when2>השבוע|היום|מחר))?$')
WAITING_Q = re.compile(r'^(?:ו?מה)\s+(?:עוד\s+)?מחכה\s+לי(?:\s+(?P<d>היום|מחר|השבוע))?$')
_KIND = r'(?P<kind>שיעור|הרצאה|תרגול|תרגיל)'
_COURSE = r'(?:\s+(?:ב|של)\s*(?P<course>[א-ת"\'׳ ]{3,22}?))?'
WHERE_Q = re.compile(rf'^איפה\s+(?:יש\s+לי\s+)?(?:ה|את\s+ה)?{_KIND}{_COURSE}(?:\s+(?P<d>הבא|עכשיו|{DAYREF}))?{_COURSE.replace("course", "course2")}$')
# 16/11 20:19 "יש תרגול בהסתברות?" — with a course only ("יש תרגול?" alone could be anything)
_COURSE_REQ = r'\s+(?:ב|של)\s*(?P<course>[א-ת"\'׳ ]{3,22}?)'
IS_Q = re.compile(rf'^(?:האם\s+)?(?:יש|יש\s+לי|מתי)\s+(?:ה)?{_KIND}(?:\s+(?P<d>{DAYREF}))?{_COURSE_REQ}(?:\s+(?P<d2>{DAYREF}))?$')
NEXT_Q = re.compile(rf'^(?:מתי|מה|איזה|איפה)\s+(?:ה)?{_KIND}\s+(?:ה)?(?:הבא|הקרוב)(?:\s+שלי)?{_COURSE}$')
NOW_Q = re.compile(r'^מה\s+(?:יש\s+לי|קורה)\s+(?:עכשיו|מעכשיו)$|^מה\s+(?:עוד\s+)?נשאר\s+לי\s+(?:ל|ב)?היום$')


def norm(text: str) -> str:
    t = (text or '').strip()
    t = re.sub(r'[״”“]', '"', t)
    t = re.sub(r'(?<=[א-ת])[\'׳](?=[א-ת])', '', t)
    t = re.sub(r'[?!.,…\s🙂😊🙏]+$', '', t)
    t = re.sub(r'^(?:היי|הי|אהלן|יו|בוקר טוב|ערב טוב)[,!\s]+', '', t)
    t = re.sub(r'\bלו\s*"?\s*ז\b', 'לוז', t)
    t = t.replace('לו"ז', 'לוז')
    return re.sub(r'\s+', ' ', t).strip()


def intent(text: str) -> tuple[str, dict] | None:
    """The question this message is, whole — or None."""
    t = norm(text)
    if not t or len(t) > 60 or '\n' in t:
        return None
    if NOW_Q.match(t):
        return 'day', {'d': 'היום', 'left': True}
    if m := DAY_Q.match(t):
        return 'day', {'d': m.group('d') or m.group('d2') or m.group('d3'), 'left': bool(m.group('left'))}
    if m := WEEK_Q.match(t):
        return 'week', {'next': 'הבא' in (m.group('w') or m.group('w2') or '')}
    if m := DEADLINE_Q.match(t):
        return 'deadline', {'when': m.group('when') or m.group('when2') or ''}
    if m := WAITING_Q.match(t):
        return 'waiting', {'d': m.group('d') or ''}
    if m := WHERE_Q.match(t):
        return 'where', {'kind': m.group('kind'), 'course': (m.group('course') or m.group('course2') or '').strip(),
                         'd': m.group('d') or ''}
    if m := IS_Q.match(t):
        return 'where', {'kind': m.group('kind'), 'course': m.group('course').strip(), 'd': m.group('d') or m.group('d2') or ''}
    if m := NEXT_Q.match(t):
        return 'next', {'kind': m.group('kind') or '', 'course': (m.group('course') or '').strip()}
    return None


# ── answers ─────────────────────────────────────────────────────────────────

def _day_of(ref: str, today: date) -> date | None:
    from agent.daytools import parse_day
    ref = re.sub(r'^ל(?=יום|ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)', 'ב', ref.strip())
    return parse_day(ref if ref.startswith(('ב', 'יום')) or re.match(r'\d', ref) else ref, today)


def _open_deadlines(chat: str, now: datetime, until: datetime | None = None) -> list[dict]:
    from agent import obligations
    store = obligations.refresh(now, chat or None)
    rows = [r for r in obligations.open_items(store, now) if r.get('due')]
    rows = [r for r in rows if obligations.status(r, now) == 'open']
    if until:
        rows = [r for r in rows if datetime.fromisoformat(r['due']) <= until]
    return rows


def _course_of(r: dict) -> str:
    from agent.obligations import _course_name
    return _course_name(r.get('course', ''))


def _deadline_lines(rows: list[dict], now: datetime, n: int = 3) -> list[str]:
    from agent.obligations import line
    return [f"{line(r, now, link=False)} · {_course_of(r)}" for r in rows[:n]]


def day_answer(chat: str, ref: str, now: datetime, left: bool = False) -> str | None:
    from agent.daytools import day_overview
    day = _day_of(ref, now.date())
    if not day or not 0 <= (day - now.date()).days <= 60:
        return None
    return day_overview(day, chat, after=now if (left and day == now.date()) else None)


def week_answer(chat: str, now: datetime, nxt: bool = False) -> str | None:
    from integrations import gcal
    if not gcal.configured():
        return None
    start = now.date()
    if nxt:
        start = start + timedelta(days=(6 - start.weekday()) % 7 or 7)
    text = gcal.week_text(start, 7)
    try:
        end = datetime(start.year, start.month, start.day) + timedelta(days=7)
        dl = [r for r in _open_deadlines(chat, now, end) if datetime.fromisoformat(r['due']).date() >= start]
        if dl:
            text += '\n\n📝 *הגשות בשבוע הזה*\n' + '\n'.join(_deadline_lines(dl, now, 6))
    except Exception as e:
        print(f'  ⚠️ fastpath deadlines: {e!r}'[:200])
    return text


def deadline_answer(chat: str, now: datetime, when: str = '') -> str | None:
    until = None
    if when == 'היום':
        until = now.replace(hour=23, minute=59)
    elif when == 'מחר':
        until = (now + timedelta(days=1)).replace(hour=23, minute=59)
    elif when == 'השבוע':
        until = (now + timedelta(days=7)).replace(hour=23, minute=59)
    rows = _open_deadlines(chat, now, until)
    if when == 'מחר':
        rows = [r for r in rows if datetime.fromisoformat(r['due']).date() == now.date() + timedelta(days=1)]
    if not rows:
        return f"✅ אין הגשה פתוחה{' ' + when if when else ''} — הכול מוגש או שאין תאריך במודל."
    head = '📝 *ההגשה הבאה:*' if not when else f'📝 *להגיש {when}:*'
    lines = _deadline_lines(rows, now, 1 if not when else 6)
    if not when and len(rows) > 1:
        lines += ['', 'אחריה:'] + _deadline_lines(rows[1:], now, 2)
    return head + '\n' + '\n'.join(lines)


def waiting_answer(chat: str, now: datetime, ref: str = '') -> str | None:
    if ref == 'השבוע':
        return week_answer(chat, now)
    if ref == 'מחר':
        return day_answer(chat, 'מחר', now)
    from agent.daytools import day_overview
    parts = [day_overview(now.date(), chat, after=now)]
    if now.hour >= 18:                                         # in the evening, tomorrow is what's waiting
        parts.append(day_overview(now.date() + timedelta(days=1), chat))
    rows = _open_deadlines(chat, now, (now + timedelta(days=14)).replace(hour=23, minute=59))
    if rows:
        parts.append('📝 *ההגשות הקרובות*\n' + '\n'.join(_deadline_lines(rows, now, 4)))
    return '\n\n'.join(parts)


# his sessions (my.tau cache + the university's changes)

def sessions(now: datetime, day: date | None = None) -> list[dict]:
    """[{at, end, course, ck, kind, place, override}] from now (or that whole day), soonest first."""
    from agent import factcheck, schedule, timetable
    from integrations import tau_portal
    events = (tau_portal._cached() or {}).get('events') or []
    rows = schedule.load()
    words = factcheck._course_words()
    try:
        from agent.obligations import _course_codes
        codes = _course_codes()
    except Exception:
        codes = {}
    out = []
    for e in timetable.lessons(events, day or now.date()):
        try:
            at = datetime.fromisoformat(f"{e['date']}T{e['time']}")
            end = datetime.fromisoformat(f"{e['date']}T{e['end_time']}") if e.get('end_time') else at + timedelta(hours=2)
        except (KeyError, ValueError):
            continue
        if day and at.date() != day or (not day and end <= now):
            continue
        ck = factcheck._portal_ck(e, words, codes)
        kind = 'tutorial' if 'תרגיל' in (e.get('kind') or '') else 'lecture'
        o = schedule.find(at.date(), '', e['time'][:5], ck=ck or '', kind=kind, rows=rows) if ck else None
        out.append({'at': at, 'end': end, 'course': timetable.short(e.get('course') or ''), 'ck': ck, 'kind': e.get('kind') or '',
                    'place': timetable.place(e), 'override': o})
    return sorted(out, key=lambda x: x['at'])


def _when(at: datetime, now: datetime) -> str:
    left = (at.date() - now.date()).days
    day = 'היום' if left == 0 else 'מחר' if left == 1 else f'ביום {WEEKDAY_HE[at.weekday()]} {at:%d/%m}'
    return f'{day} ב-{at:%H:%M}'


def _session_line(s: dict, now: datetime) -> str:
    from agent import schedule
    o = s.get('override')
    now_on = s['at'] <= now < s['end']
    head = f"🎓 *{s['kind']} ב{s['course']}* — {'עכשיו (עד ' + s['end'].strftime('%H:%M') + ')' if now_on else _when(s['at'], now)}"
    if o and o.get('status') == 'cancelled':
        return head + schedule.suffix(o)
    return head + (f"\n📍 {s['place']}" if s['place'] else '\n📍 החדר לא מופיע באתר') + (schedule.suffix(o) if o else '')


def _filter(rows: list[dict], kind: str, course: str) -> list[dict] | None:
    from agent import factcheck
    if course:
        ck = factcheck._ck_in(course, factcheck._course_words())
        if not ck:
            return None                                        # a course it can't name for sure → the agent
        rows = [s for s in rows if s['ck'] == ck]
    k = {'תרגול': 'תרגיל', 'תרגיל': 'תרגיל', 'הרצאה': 'שיעור', 'שיעור': ''}.get(kind, '')
    return [s for s in rows if not k or k in s['kind']]


def where_answer(now: datetime, kind: str, course: str = '', ref: str = '') -> str | None:
    day = None
    if ref and ref not in ('הבא', 'עכשיו'):
        day = _day_of(ref, now.date())
        if not day:
            return None
    rows = _filter(sessions(now, day), kind, course)
    if rows is None:
        return None
    if not day and ref != 'הבא':                              # "איפה השיעור" → today's next (or now) — else the next one
        today = [s for s in rows if s['at'].date() == now.date()]
        rows = today or rows
    if not rows:
        return '🎓 לא מצאתי שיעור כזה במערכת שלך' + (f' ב{_day_of(ref, now.date()):%d/%m}' if day else ' בימים הקרובים') + '.'
    if day:
        return '\n'.join(_session_line(s, now) for s in rows[:5])
    held = [s for s in rows if not (s.get('override') or {}).get('status') == 'cancelled']
    first = rows[0]
    out = _session_line(first, now)
    if first is not (held[0] if held else None) and held:
        out += '\n\nהבא שמתקיים:\n' + _session_line(held[0], now)
    return out


def next_answer(now: datetime, kind: str = '', course: str = '') -> str | None:
    rows = _filter(sessions(now), kind or 'שיעור', course)
    if rows is None:
        return None
    if not rows:
        return '🎓 אין שיעורים במערכת שלך בימים הקרובים (לפי אתר האוניברסיטה).'
    out = _session_line(rows[0], now)
    held = [s for s in rows if (s.get('override') or {}).get('status') != 'cancelled']
    if held and held[0] is not rows[0]:
        out += '\n\nהבא שמתקיים:\n' + _session_line(held[0], now)
    return out


def answer(text: str, chat: str, now: datetime | None = None) -> tuple[str, str] | None:
    """→ (intent, reply) or None. Never raises."""
    it = intent(text)
    if not it:
        return None
    now = now or datetime.now()
    name, p = it
    try:
        if name == 'day':
            said = day_answer(chat, p['d'], now, p['left'])
        elif name == 'week':
            said = week_answer(chat, now, p['next'])
        elif name == 'deadline':
            said = deadline_answer(chat, now, p['when'])
        elif name == 'waiting':
            said = waiting_answer(chat, now, p['d'])
        elif name == 'where':
            said = where_answer(now, p['kind'], p['course'], p['d'])
        else:
            said = next_answer(now, p['kind'], p['course'])
    except Exception as e:
        print(f'  ⚠️ fastpath {name}: {e!r}'[:200])
        return None
    return (name, said) if said else None
