"""
agent/hetime.py — Hebrew time words → an Israel wall-clock datetime, in code (17/11, the owner's improvement 3).

Every "תזכיר לי … מחר בבוקר" went through a Gemini call (agent.reminder_parse) just to read the time, and a model can be
off by a day ("ביום ראשון" on a Saturday night). The common phrases have one meaning; this reads them:

    parse("מחרתיים בערב", now)            → day+2 20:00
    parse("ביום ראשון הבא", now)          → the coming Sunday (today is Sunday → next week's), no hour
    parse("עוד שעתיים", now)              → now + 2 real hours (across a DST change too)
    parse("בעוד שבוע", now)               → now's date + 7, no hour
    parse("ב-8 בבוקר", now)               → 08:00, today if still ahead, else tomorrow
    parse("סוף השבוע", now)               → this week's Friday (Fri/Sat → today), no hour
    parse("לפני השיעור", now, lessons=…)  → 30 minutes before his next session that isn't cancelled

Returns When(at, has_day, has_time, spans) or None; None also when the words are ambiguous ("ב-3 בלילה" after "מחר",
"ב-5" with no part of day): the caller then asks the model, as before. Pure: `now` is naive Israel time (the process
runs with TZ=Asia/Jerusalem), the result is naive Israel time, and real-time arithmetic ("עוד שעתיים") goes through UTC
so a DST change never moves it by an hour. A wall-clock time that doesn't exist (the spring-forward hour) moves forward.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Jerusalem')
DAYS = {'ראשון': 6, 'שני': 0, 'שלישי': 1, 'רביעי': 2, 'חמישי': 3, 'שישי': 4, 'שבת': 5}
MONTHS = {'ינואר': 1, 'פברואר': 2, 'מרץ': 3, 'מרס': 3, 'אפריל': 4, 'מאי': 5, 'יוני': 6, 'יולי': 7, 'אוגוסט': 8,
          'ספטמבר': 9, 'אוקטובר': 10, 'נובמבר': 11, 'דצמבר': 12}
NUM = {'אחת': 1, 'אחד': 1, 'שתיים': 2, 'שתים': 2, 'שניים': 2, 'שלוש': 3, 'שלושה': 3, 'ארבע': 4, 'ארבעה': 4,
       'חמש': 5, 'חמישה': 5, 'שש': 6, 'שישה': 6, 'שבע': 7, 'שבעה': 7, 'שמונה': 8, 'תשע': 9, 'תשעה': 9,
       'עשר': 10, 'עשרה': 10, 'אחת עשרה': 11, 'אחת-עשרה': 11, 'שתים עשרה': 12, 'שתיים עשרה': 12}
PART_DEFAULT = {'בוקר': 9, 'צהריים': 13, 'אחה"צ': 16, 'ערב': 20, 'לילה': 22}
BEFORE_MIN = 30                        # "לפני השיעור" → half an hour before it

_B = r'(?<![א-ת\w])'                     # a word edge that also holds for Hebrew
_E = r'(?![א-ת\w])'
_NUMW = '|'.join(sorted(NUM, key=len, reverse=True))
_HOUR = rf'(\d{{1,2}})(?:[:.](\d{{2}}))?|({_NUMW})'
_PART = r'(?:ב|אחר\s+ה|אחרי\s+ה|לפנות\s+)?(בוקר|צהריים|צהרים|ערב|לילה)|(אחה"צ|אחה״צ)'
_FRAC = r'(?:\s*(וחצי|ורבע|ועשרים|ועשר|וחמישה|וחמש))?'

RX_REL = re.compile(_B + r'(?:בעוד|עוד|תוך|אחרי)\s+(?:(\d{1,3}|' + _NUMW + r')\s+)?'
                    r'(דקה|דקות|רבע\s+שעה|חצי\s+שעה|שעה|שעתיים|שעות|יום|יומיים|ימים|שבוע|שבועיים|שבועות|חודש|חודשיים)' + _E)
RX_DAYWORD = re.compile(_B + r'(?:[לו]?ב?)?(מחרתיים|מחר|היום|הערב|הלילה|הבוקר)' + _E)
RX_WEEKDAY = re.compile(_B + r'(?:ו?ב?יום\s+|ו?ב|ו?ל)(ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)' + r'(?:\s+(הבא|הקרוב|הזה))?' + _E)
RX_WEEKEND = re.compile(_B + r'(?:ב|ל)?(?:סוף\s+(?:ה)?שבוע|סופ"ש|סופ״ש|סופש)(?:\s+(הבא|הזה|הקרוב))?' + _E)
RX_NEXTWEEK = re.compile(_B + r'(?:ב|ל)(?:שבוע\s+הבא|שבוע\s+הבע"ל)' + _E)
RX_DATE = re.compile(r'(?<![\d:])(?:(?<![א-ת])[בלה]-?)?(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d:])')
RX_MDATE = re.compile(_B + r'(?:ב-?|ה-?)?(\d{1,2})\s*(?:ב|ל)?(' + '|'.join(MONTHS) + r')' + _E)
RX_CLOCK = re.compile(r'(?<![\d/.])(?:בשעה\s+|ב-?\s?|ל-?)?(\d{1,2}):(\d{2})(?![\d/])')
RX_HOUR = re.compile(_B + r'(?:בשעה\s+|ב-?\s?|ל-?)(' + r'\d{1,2}|' + _NUMW + r')' + _FRAC
                     + r'(?:\s+(?:' + _PART + r'))?' + _E)
RX_PART = re.compile(_B + _PART + _E)
RX_BEFORE = re.compile(_B + r'(?:רגע\s+)?לפני\s+(?:ה)?(שיעור|הרצאה|תרגול|תרגיל)(?:\s+(?:ב|של)\s*([א-ת"\'׳״0-9 ]{2,25}?))?'
                       r'(?=$|[\s,.!?]|' + _E + ')')
RX_REPEAT = re.compile(_B + r'כל\s+(?:יום|בוקר|ערב|שבוע|חודש|ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת|שעה)|'
                       r'מדי\s+(?:יום|שבוע)|every', re.I)


@dataclass
class When:
    at: datetime                     # naive Israel wall-clock time
    has_day: bool = False
    has_time: bool = False
    spans: list[tuple[int, int]] = field(default_factory=list)   # where the time words are in the text
    note: str = ''                   # what it is ("לפני השיעור בהסתברות 11:00")

    def date(self) -> date:
        return self.at.date()


# ── wall clock ↔ real time ──────────────────────────────────────────────────

def local_now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def real(dt: datetime) -> datetime:
    """A naive Israel wall-clock time → aware UTC. The ambiguous fall-back hour reads as the first (summer) one."""
    return dt.replace(tzinfo=TZ, fold=0).astimezone(timezone.utc)


def wall(utc: datetime) -> datetime:
    return utc.astimezone(TZ).replace(tzinfo=None)


def exists(dt: datetime) -> datetime:
    """The spring-forward gap (02:00–02:59 that night) doesn't exist on a clock: → the same minutes an hour later."""
    back = wall(real(dt))
    return back if back != dt else dt


def plus(now: datetime, delta: timedelta) -> datetime:
    """now + a real duration ("עוד שעתיים"), through UTC: 00:30 on 25/10/2026 + 2h is 01:30, not 02:30."""
    return wall(real(now) + delta)


def _num(s: str) -> int | None:
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    return NUM.get(s)


# ── the pieces ──────────────────────────────────────────────────────────────

def _relative(t: str, now: datetime):
    m = RX_REL.search(t)
    if not m:
        return None
    n = _num(m.group(1)) if m.group(1) else None
    unit = re.sub(r'\s+', ' ', m.group(2))
    if m.group(1) and n is None:
        return None
    if unit in ('דקה', 'דקות'):
        d, timed = timedelta(minutes=n or 1), True
    elif unit == 'רבע שעה':
        d, timed = timedelta(minutes=15), True
    elif unit == 'חצי שעה':
        d, timed = timedelta(minutes=30), True
    elif unit == 'שעה':
        d, timed = timedelta(hours=n or 1), True
    elif unit == 'שעתיים':
        d, timed = timedelta(hours=2), True
    elif unit == 'שעות':
        if n is None:
            return None
        d, timed = timedelta(hours=n), True
    elif unit == 'יום':
        d, timed = timedelta(days=n or 1), False
    elif unit == 'יומיים':
        d, timed = timedelta(days=2), False
    elif unit == 'ימים':
        if n is None:
            return None
        d, timed = timedelta(days=n), False
    elif unit == 'שבוע':
        d, timed = timedelta(days=7 * (n or 1)), False
    elif unit == 'שבועיים':
        d, timed = timedelta(days=14), False
    elif unit == 'שבועות':
        if n is None:
            return None
        d, timed = timedelta(days=7 * n), False
    elif unit == 'חודש':
        d, timed = timedelta(days=30 * (n or 1)), False
    elif unit == 'חודשיים':
        d, timed = timedelta(days=60), False
    else:
        return None
    if timed:
        if d > timedelta(hours=72):
            return None
        return plus(now, d).replace(second=0, microsecond=0), True, (m.start(), m.end())
    return (now + d).date(), False, (m.start(), m.end())


def _day(t: str, today: date):
    """→ (date, span, part) for the one day the text names; 'ambiguous' when it names two different days."""
    found = []
    for m in RX_DAYWORD.finditer(t):
        w = m.group(1)
        d = today + timedelta(days={'מחרתיים': 2, 'מחר': 1}.get(w, 0))
        part = {'הערב': 'ערב', 'הלילה': 'לילה', 'הבוקר': 'בוקר'}.get(w, '')
        found.append((d, (m.start(), m.end()), part))
    for m in RX_WEEKDAY.finditer(t):
        target = DAYS[m.group(1)]
        ahead = (target - today.weekday()) % 7
        if m.group(2) == 'הבא' and ahead == 0:
            ahead = 7
        found.append((today + timedelta(days=ahead), (m.start(), m.end()), ''))
    for m in RX_WEEKEND.finditer(t):
        ahead = (4 - today.weekday()) % 7 if today.weekday() not in (4, 5) else 0
        if m.group(1) == 'הבא':
            ahead = (4 - today.weekday()) % 7 + (7 if today.weekday() in (4, 5) else 0) or 7
        found.append((today + timedelta(days=ahead), (m.start(), m.end()), ''))
    for m in RX_NEXTWEEK.finditer(t):
        ahead = (6 - today.weekday()) % 7 or 7                 # the coming Sunday
        found.append((today + timedelta(days=ahead), (m.start(), m.end()), ''))
    for m in RX_DATE.finditer(t):
        dd, mm, yy = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            y = (int(yy) + (2000 if len(yy) == 2 else 0)) if yy else today.year
            d = date(y, mm, dd)
        except ValueError:
            continue
        if not yy and d < today - timedelta(days=1):
            d = d.replace(year=d.year + 1)
        found.append((d, (m.start(), m.end()), ''))
    for m in RX_MDATE.finditer(t):
        try:
            d = date(today.year, MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            continue
        if d < today - timedelta(days=1):
            d = d.replace(year=d.year + 1)
        found.append((d, (m.start(), m.end()), ''))
    if not found:
        return None
    days = {d for d, _, _ in found}
    if len(days) > 1:
        return 'ambiguous'
    part = next((p for _, _, p in found if p), '')
    return found[0][0], [s for _, s, _ in found], part


def _hour24(h: int, part: str) -> int | None:
    """An hour as said + its part of day → 0–23, or None when it can't be told."""
    if part in ('צהריים', 'צהרים'):
        return h + 12 if 1 <= h <= 6 else h if h in (11, 12) else None
    if part in ('אחה"צ', 'אחה״צ'):
        return h + 12 if 1 <= h <= 7 else None
    if part == 'ערב':
        return h + 12 if 4 <= h <= 11 else None
    if part == 'לילה':
        return h + 12 if 8 <= h <= 11 else 0 if h == 12 else None     # "ב-2 בלילה": which night? → the model
    if part == 'בוקר':
        return h if 4 <= h <= 11 else 0 if h == 12 else None
    return None


def _time(t: str, day_part: str = ''):
    """→ (hour, minute, spans) or None; 'ambiguous' for an hour that needs a part of day it doesn't have."""
    spans = []
    m = RX_CLOCK.search(t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return 'ambiguous'
        spans.append((m.start(), m.end()))
        p = RX_PART.search(t, m.end())
        if p and p.start() - m.end() <= 3 and h <= 12:
            part = p.group(1) or p.group(2)
            h2 = _hour24(h, part) if h < 12 or part != 'בוקר' else h
            if h2 is not None:
                h = h2
            spans.append((p.start(), p.end()))
        elif h <= 12 and day_part in ('ערב', 'לילה') and 4 <= h <= 11:
            h += 12
        return h, mi, spans
    m = RX_HOUR.search(t)
    if m:
        h = _num(m.group(1))
        if h is None or not 0 <= h <= 23:
            return 'ambiguous'
        mi = {'וחצי': 30, 'ורבע': 15, 'ועשרים': 20, 'ועשר': 10, 'וחמישה': 5, 'וחמש': 5}.get(m.group(2) or '', 0)
        part = m.group(3) or m.group(4) or day_part
        spans.append((m.start(), m.end()))
        if h >= 13:
            return h, mi, spans
        if part:
            h2 = _hour24(h, part)
            if h2 is None:
                return 'ambiguous'
            return h2, mi, spans
        if h == 12 or ('בשעה' in m.group(0) and 8 <= h <= 11):
            return h, mi, spans                                # "ב-12" is noon; "בשעה 10" the morning one
        return 'ambiguous'                                     # "ב-8" / "ב-5" bare: morning or evening — the model asks
    m = RX_PART.search(t)
    if m:
        part = m.group(1) or m.group(2)
        spans.append((m.start(), m.end()))
        return PART_DEFAULT.get('צהריים' if part == 'צהרים' else 'אחה"צ' if part == 'אחה״צ' else part), 0, spans
    if day_part:
        return PART_DEFAULT[day_part], 0, spans
    return None


# ── his sessions, for "לפני השיעור" ─────────────────────────────────────────

def lessons_ahead(now: datetime) -> list[dict]:
    """His sessions from now on (the my.tau cache), cancelled ones left out: [{at, course, kind}]."""
    try:
        from integrations import tau_portal
        from agent import schedule, timetable
        events = (tau_portal._cached() or {}).get('events') or []
        rows = schedule.load()
    except Exception:
        return []
    out = []
    for e in timetable.lessons(events, now.date()):
        try:
            at = datetime.fromisoformat(f"{e['date']}T{e['time']}")
        except (ValueError, KeyError):
            continue
        if at <= now:
            continue
        o = schedule.find(at.date(), f"{e.get('course', '')} – {e.get('kind', '')}", e['time'], rows=rows)
        if o and o.get('status') == 'cancelled':
            continue
        out.append({'at': at, 'course': e.get('course', ''), 'kind': e.get('kind', '')})
    return sorted(out, key=lambda x: x['at'])


def _course_match(course_words: str, name: str) -> bool:
    w = re.sub(r'^(?:ב|של)\s*', '', (course_words or '').strip())
    if not w:
        return True
    try:
        from integrations import tau_portal
        hint = tau_portal.course_hint(w) or w
    except Exception:
        hint = w
    return hint.split()[0] in name


def _before_lesson(t: str, now: datetime, day: date | None, lessons):
    m = RX_BEFORE.search(t)
    if not m:
        return None
    kind = {'שיעור': '', 'הרצאה': 'שיעור', 'תרגול': 'תרגיל', 'תרגיל': 'תרגיל'}[m.group(1)]
    rows = lessons(now) if callable(lessons) else (lessons or [])
    pick = [x for x in rows if x['at'] > now + timedelta(minutes=BEFORE_MIN)
            and (not kind or kind in (x.get('kind') or ''))
            and (not m.group(2) or _course_match(m.group(2), x.get('course') or ''))
            and (day is None or x['at'].date() == day)]
    if not pick:
        return 'ambiguous'
    x = pick[0]
    at = x['at'] - timedelta(minutes=BEFORE_MIN)
    note = f"לפני ה{x.get('kind') or 'שיעור'} ב{re.split(r'[:(]|\s+-\s+', x.get('course') or '')[0].strip()} ב-{x['at']:%H:%M}"
    return at, (m.start(), m.end()), note


# ── the whole phrase ────────────────────────────────────────────────────────

def _blank(t: str, spans) -> str:
    for a, b in spans:
        t = t[:a] + ' ' * (b - a) + t[b:]
    return t


def parse(text: str, now: datetime | None = None, lessons=lessons_ahead) -> When | None:
    """The time a Hebrew phrase names, or None (nothing found, or it can't be told for sure)."""
    t = (text or '').strip()
    if not t:
        return None
    now = (now or local_now()).replace(second=0, microsecond=0)
    today = now.date()
    if RX_REPEAT.search(t):
        return None                                            # "כל יום ב-8" is a repeat: the model reads those
    rel = _relative(t, now)
    if rel:
        val, timed, span = rel
        rest = _blank(t, [span])
        if timed:
            if _day(rest, today) or _time(rest):
                return None                                    # "בעוד שעה מחר" — two answers
            return When(val, True, True, [span])
        d = _day(rest, today)
        if d:
            return None
        tm = _time(rest)
        if tm == 'ambiguous':
            return None
        if tm:
            h, mi, sp = tm
            return When(exists(datetime(val.year, val.month, val.day, h, mi)), True, True, [span] + sp)
        return When(datetime(val.year, val.month, val.day, 9, 0), True, False, [span])
    d = _day(t, today)
    if d == 'ambiguous':
        return None
    day, dspans, dpart = d if d else (None, [], '')
    t = _blank(t, dspans)                                      # "ב25 לנובמבר בשעה 10": 25 is the day, not an hour
    before = _before_lesson(t, now, day, lessons)
    if before == 'ambiguous':
        return None
    if before:
        at, span, note = before
        return When(at, True, True, dspans + [span], note)
    tm = _time(t, dpart)
    if tm == 'ambiguous':
        return None
    if day is None and tm is None:
        return None
    if tm is None:
        return When(datetime(day.year, day.month, day.day, 9, 0), True, False, dspans)
    h, mi, tspans = tm
    if day is None:
        at = datetime(today.year, today.month, today.day, h, mi)
        if at <= now:
            at += timedelta(days=1)                            # "ב-8 בבוקר" at 15:00 → tomorrow
        return When(exists(at), False, True, tspans)
    return When(exists(datetime(day.year, day.month, day.day, h, mi)), True, True, dspans + tspans)


def strip(text: str, w: When) -> str:
    """The text without its time words: what's left is the thing itself ("לקנות חלב")."""
    t = text
    for a, b in sorted(w.spans, reverse=True):
        t = t[:a] + ' ' + t[b:]
    return re.sub(r'\s+', ' ', t).strip(' ,.-–')


WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']


def say(dt: datetime, now: datetime | None = None) -> str:
    """'מחר (רביעי 18/11) ב-08:00' — how a time is said back to him."""
    now = now or local_now()
    left = (dt.date() - now.date()).days
    head = 'היום' if left == 0 else 'מחר' if left == 1 else 'מחרתיים' if left == 2 else f'יום {WEEKDAY_HE[dt.weekday()]}'
    return f"{head} ({WEEKDAY_HE[dt.weekday()]} {dt:%d/%m}) ב-{dt:%H:%M}" if left in (0, 1, 2) else f"{head} {dt:%d/%m} ב-{dt:%H:%M}"
