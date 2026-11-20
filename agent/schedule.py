"""
agent/schedule.py — what is really on HIS timetable on a given day: the base timetable + the changes the university
announced (16/11).

16/11 20:18 "מה הלוז שלי מחר" listed "11:00 הסתברות וסטטיסטיקה (תרגיל, קב׳ 02)" — the TA had cancelled this week's
tutorials in Moodle that morning (it was in the 13:00 digest); a minute later the bot itself said "התרגולים השבוע
מבוטלים… טעות שלי". The calendar change waited for a ✅ nobody saw, and every schedule answer read the calendar alone.

    base        the calendar's lessons / courses.yaml my_sessions (integrations/univ_relevance.sessions)
    overrides   data/schedule_overrides.json — one row per session-date a university update changes: cancelled / room /
                moved, with the update it came from. Written by code only: from a triaged update that the relevance rule
                says is HIS session (mail_tick), and by the backfill (scripts/backfill_schedule_overrides.py).
    readers     gcal.day_text / week_text, daytools.day_overview (the morning message, "מה יש לי מחר"), event_reminders
                (no ping / "מחר מוקדם" for a cancelled class), the pre-class primer (no primer for a cancelled class).
Nothing here sends anything or writes the calendar; an update is someone else's text — read for dates and kinds only.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'schedule_overrides.json'
KEEP_DAYS = 45

CANCEL_RX = re.compile(r'בוטל|מבוטל|יבוטל|מתבטל|ביטול|לא\s+(?:יתקיים|תתקיים|יהיה|יתקיימו)|cancel|no\s+(?:lecture|recitation|class)', re.I)
ROOM_RX = re.compile(r'מיקום|חדר|כיתה|אולם|בניין|room|hall|location', re.I)
MOVE_RX = re.compile(r'יעבור\s+ל(?:יום|שעה)|הוזז|יוזז|נדחה|במועד\s+אחר|moved|postponed|rescheduled', re.I)
WEEK_RX = re.compile(r'השבוע|this\s+week', re.I)
KIND_HE = {'lecture': 'שיעור', 'tutorial': 'תרגיל'}


# ── the store ───────────────────────────────────────────────────────────────

def load(path: Path | None = None) -> list[dict]:
    try:
        rows = json.loads((path or STORE).read_text(encoding='utf-8'))
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _save(rows: list[dict], path: Path | None = None, today: date | None = None) -> None:
    p = path or STORE
    cut = ((today or date.today()) - timedelta(days=KEEP_DAYS)).isoformat()
    rows = [r for r in rows if str(r.get('date', '')) >= cut]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(p)


def _status(text: str) -> str:
    if CANCEL_RX.search(text):
        return 'cancelled'
    if MOVE_RX.search(text):
        return 'moved'
    if ROOM_RX.search(text):
        return 'room'
    return ''


def _week(ref: date) -> list[date]:
    """The rest of ref's week (Sunday to Saturday, the Israeli week), from ref on."""
    sunday = ref - timedelta(days=(ref.weekday() + 1) % 7)
    return [d for d in (sunday + timedelta(days=i) for i in range(7)) if d >= ref]


SPLIT_RX = re.compile(r'[\n.!?;]+\s*|\s+(?=ו(?:ביטול|בוטל|מבוטל|לא\s+(?:תתקיים|יתקיים)|החדר|המיקום|השיעור|ההרצאה|התרגול))')


def _anchored(r: dict, seg: str) -> bool:
    return bool(r['dates'] or r['days'] or WEEK_RX.search(seg))


def plan(ck: str, text: str, ref: date, sess: dict | None = None) -> list[dict]:
    """An update's change → the session-dates it touches: [{date, ck, kind, start, status}]. Pure. Sentence by sentence:
    the day a change names is read from ITS sentence (16/11 "Lecture on Monday 16.11 … וביטול ההרצאה ביום רביעי" — the
    Wednesday one is cancelled, not his Monday); only a sentence that names no day borrows the first line's (the subject).
    [] when the day can't be told (then nothing is assumed — the update is still in the digest)."""
    from integrations import univ_relevance as ur
    ss = ((ur.sessions() if sess is None else sess).get(ck)) or [] if ck else []
    if not ss:
        return []
    segs = [x.strip() for x in SPLIT_RX.split(text or '') if x and x.strip()]
    first = segs[0] if segs else ''
    out, seen = [], set()
    for seg in segs:
        status = _status(seg)
        if not status:
            continue
        r = ur.read(seg, ref)
        if not _anchored(r, seg):
            r0 = ur.read(first, ref)
            if not _anchored(r0, first):
                continue
            r = {**r, 'dates': r0['dates'], 'days': r0['days'], 'kinds': r['kinds'] or r0['kinds']}
            seg = f'{seg} {first}'
        pool = [s for s in ss if not r['kinds'] or s['kind'] in r['kinds']]
        if r['groups']:
            pool = [s for s in pool if s.get('group') in r['groups']] or ([] if not r['all'] else pool)
        if r['hours']:
            pool = [s for s in pool if any(ur._overlap(h, s) for h in r['hours'])] or pool
        if r['dates']:
            days = [d for d in r['dates'] if d >= ref - timedelta(days=1)]
        elif WEEK_RX.search(seg):
            days = _week(ref)
        else:
            days = [ref + timedelta(days=(wd - ref.weekday()) % 7) for wd in sorted(r['days'])]
        for d in days:
            for s in pool:
                k = (d, s['kind'], s['start'])
                if s['wd'] == d.weekday() and k not in seen:
                    seen.add(k)
                    out.append({'date': d.isoformat(), 'ck': ck, 'kind': s['kind'],
                                'start': f"{s['start'] // 60:02d}:{s['start'] % 60:02d}", 'status': status})
    return out


def add(ck: str, text: str, ref: date, source: str, note: str = '', path: Path | None = None,
        sess: dict | None = None, now: datetime | None = None) -> list[dict]:
    """Record what an update changes in his sessions. → the new rows (one per session-date, never twice)."""
    rows = load(path)
    have = {(r['date'], r['ck'], r['kind'], r['start']) for r in rows}
    new = []
    for p in plan(ck, text, ref, sess):
        k = (p['date'], p['ck'], p['kind'], p['start'])
        if k in have:
            continue
        have.add(k)
        new.append({**p, 'source': (source or '')[:80], 'note': re.sub(r'\s+', ' ', note or text).strip()[:200],
                    'added': (now or datetime.now()).isoformat(timespec='seconds')})
    if new:
        _save(rows + new, path, ref)
    return new


# ── reading it ──────────────────────────────────────────────────────────────

def _course_words(ck: str) -> set[str]:
    from agent.lesson_sync import _words
    try:
        from config.paths import COURSE_ALT_NAMES, COURSE_NAMES
        names = [COURSE_NAMES.get(ck, ck), *COURSE_ALT_NAMES.get(ck, [])]
    except Exception:
        names = [ck]
    out = set()
    for n in names:
        out |= _words(n)
    return out


def _kind_of(title: str) -> str:
    return 'tutorial' if re.search(r'תרגיל|תרגול', title or '') else 'lecture' if re.search(r'שיעור|הרצאה', title or '') else ''


def find(day: date, title: str, start: str = '', ck: str = '', kind: str = '', rows: list[dict] | None = None,
         path: Path | None = None) -> dict | None:
    """The override for this session on this day, or None. A calendar lesson is matched by its title's course words,
    kind and start time; a timetable item by its course key."""
    from agent.lesson_sync import _words
    rows = load(path) if rows is None else rows
    kind = kind or _kind_of(title)
    tw = _words(re.sub(r'^❌ בוטל — ', '', title or '').split('–')[0])
    for r in rows:
        if r.get('date') != day.isoformat():
            continue
        if start and r.get('start') and r['start'] != start[:5].zfill(5):
            continue
        if kind and r.get('kind') and r['kind'] != kind:
            continue
        if ck and r.get('ck') == ck or (not ck and tw and tw & _course_words(r.get('ck', ''))):
            return r
    return None


def _event_parts(ev: dict) -> tuple[date | None, str]:
    st = (ev.get('start') or {}).get('dateTime') or ''
    try:
        dt = datetime.fromisoformat(st)
    except ValueError:
        return None, ''
    return dt.date(), dt.strftime('%H:%M')


def for_event(ev: dict, rows: list[dict] | None = None, path: Path | None = None) -> dict | None:
    """A calendar event that is one of his lessons, changed by an update → its override."""
    day, hm = _event_parts(ev)
    if not day or not _kind_of(ev.get('summary') or ''):
        return None
    return find(day, ev.get('summary') or '', hm, rows=rows, path=path)


def cancelled(ev: dict, rows: list[dict] | None = None, path: Path | None = None) -> bool:
    o = for_event(ev, rows, path)
    return bool(o and o.get('status') == 'cancelled')


def suffix(o: dict | None) -> str:
    """How a changed session is shown on its line: " — ❌ מבוטל (הודעה במודל): …"."""
    if not o:
        return ''
    head = {'cancelled': '❌ מבוטל', 'room': '📍 שינוי מיקום', 'moved': '🔀 הוזז'}.get(o.get('status'), 'שינוי')
    src = f" ({o['source']})" if o.get('source') else ''
    note = f": {o['note']}" if o.get('note') else ''
    return f' — {head}{src}{note}'


def event_suffix(ev: dict, path: Path | None = None) -> str:
    """For the calendar readers; '' on any failure — a schedule answer never breaks on this."""
    try:
        return suffix(for_event(ev, path=path))
    except Exception as e:
        print(f'  ⚠️ schedule override: {e!r}'[:200])
        return ''


def source_of(r: dict) -> str:
    """'הודעה במודל' / 'מייל מהמתרגלת' — where the change was announced (for the line he sees)."""
    if r.get('source') == 'moodle' or 'moodle' in str(r.get('from', '')).lower() or 'מודל' in str(r.get('from', '')):
        return 'הודעה במודל'
    try:
        from integrations import tau_mail
        who = tau_mail.sender_name(r.get('from', ''))
    except Exception:
        who = ''
    return f'מייל מ{who}' if who else 'מייל'


def from_update(r: dict, ref: date | None = None, path: Path | None = None, sess: dict | None = None) -> list[dict]:
    """A triaged university update (tau_mail.gate kept it: his session or the whole course) → overrides. Only with a
    lesson change and a course the relevance rule pinned; an 'unsure' update never changes his schedule."""
    change = (r.get('lesson_change') or r.get('rule_change') or '').strip()
    ck = r.get('course_key') or ''
    if not change or not ck or r.get('relevance') == 'unsure':
        return []
    ref = ref or (date.fromisoformat(r['ref']) if r.get('ref') else date.today())
    subj = re.sub(r'^\s*\d{6,}\s*-\s*', '', r.get('subject') or '')
    return add(ck, f'{subj}\n{change}', ref, source_of(r), note=change, path=path, sess=sess)
