"""
agent/factcheck.py — the answer's dates, times, rooms and sessions checked against his data before it goes out
(17/11, the owner's improvement 2). Deterministic: no LLM, nothing blocks, nothing is dropped.

16/11 20:18 "מה הלוז שלי מחר" → "• 11:00 — הסתברות וסטטיסטיקה (תרגיל, קב׳ 02)": Moodle had cancelled it that morning.
A model's answer can always say a wrong thing; this reads what it said, line by line, and compares with the stores:

    cancelled   a session the line lists, on a day the university cancelled it (agent/schedule overrides) and the line
                doesn't say so                         → "⚠️ שים לב: התרגול בהסתברות ב-11:00 (17/11) מבוטל (הודעה במודל)"
    weekday     "יום שלישי 18/11" — 18/11 is a Wednesday  → the weekday on the line is corrected in place
    room        the line's room for a session ≠ the university site's room for it (my.tau cache)  → a short correction
    time        a session of course+kind on a day the site lists at another hour, and none at the line's hour
    deadline    "תרגיל 4 בהסתברות … עד 18/11" ≠ the Moodle close (agent/obligations store)  → a short correction

What it can't place (no day, no course, two courses on one line, a homework "תרגיל 5" vs a tutorial) it leaves alone:
a false correction is worse than none. Every fix is logged to data/fact_fixes.jsonl. check() never raises.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / 'data' / 'fact_fixes.jsonl'
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
DAYS = {'ראשון': 6, 'שני': 0, 'שלישי': 1, 'רביעי': 2, 'חמישי': 3, 'שישי': 4, 'שבת': 5}
KIND_WORD = {'tutorial': 'התרגול', 'lecture': 'השיעור'}
PORTAL_KIND = {'tutorial': 'תרגיל', 'lecture': 'שיעור'}

_B, _E = r'(?<![א-ת\w])', r'(?![א-ת\w])'
TIME_RX = re.compile(r'(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])')
DATE_RX = re.compile(r'(?<![\d/.:])(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?(?![\d/:])')
WD_DATE_RX = re.compile(_B + r'(?:יום\s+)?(ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)[\s,]+(?:ה-?)?(\d{1,2})[/.](\d{1,2})(?:[/.]\d{2,4})?(?![\d/])')
REL_RX = re.compile(_B + r'(?:[לו]?ב?)?(מחרתיים|מחר|היום|הערב)' + _E)
# "תרגיל 5" / "תרגיל בית" / "שיעורי בית" are homework, not a session
KIND_RX = re.compile(_B + r'(?:ה|ב|ו|ל)?(?:(תרגול|תרגיל|תרגולים)(?!\s*(?:מס(?:פר|\')?\s*)?\d)(?!\s+בית)|(שיעור|הרצאה|הרצאות)(?!י))' + _E)
CANCEL_RX = re.compile(r'בוטל|מבוטל|יבוטל|ביטול|❌|לא\s+(?:מתקיים|מתקיימ|יתקיים|תתקיים|יהיה|יתקיימו)|אין\s+(?:תרגול|שיעור|הרצאה)|cancel', re.I)
DEADLINE_RX = re.compile(r'הגשה|להגיש|מגישים|הגשת|דדליין|נסגר|עד\s+(?:ה-?)?\d|due', re.I)
HW_RX = re.compile(_B + r'(?:ה)?(?:תרגיל|מטלה|ממ"ן)\s*(?:מס(?:פר|\')?\s*)?(\d{1,2})' + _E)
ROOM_RX = re.compile(r'(?<![\d/.:])(\d{3})(?![\d/.:])')
NOT_SESSION_RX = re.compile(r'קבלת\s+קהל|שעות\s+קבלה|זום|zoom|מבחן|בחינה|בוחן|מועד|הקלטה|פגישה', re.I)


# ── the data ────────────────────────────────────────────────────────────────

def _course_words() -> dict[str, set[str]]:
    """active course key → the words that name it in a line ("הסתברות", "מיקרו", "תוכנה")."""
    from config.paths import ACTIVE_COURSES, COURSE_ALT_NAMES, COURSE_NAMES
    try:
        from integrations.tau_portal import ALIASES
    except Exception:
        ALIASES = {}
    out = {}
    for ck in ACTIVE_COURSES:
        names = [COURSE_NAMES.get(ck, ck), *COURSE_ALT_NAMES.get(ck, [])]
        ws = {n.split()[0] for n in names if n}
        ws |= {k for k, v in ALIASES.items() if any(v.split()[0] in n for n in names)}
        out[ck] = {w for w in ws if len(w) >= 4}
    return out


def _ck_in(line: str, words: dict[str, set[str]]) -> str | None:
    hit = {ck for ck, ws in words.items() for w in ws if re.search(r'(?<![א-ת])[בהלו]?' + re.escape(w), line)}
    return hit.pop() if len(hit) == 1 else None


def _portal() -> list[dict]:
    try:
        from integrations import tau_portal
        return (tau_portal._cached() or {}).get('events') or []
    except Exception:
        return []


def _portal_ck(e: dict, words: dict[str, set[str]], codes: dict[str, str]) -> str | None:
    num = str(e.get('course_number') or '').strip().zfill(8)
    if num in codes:
        return codes[num]
    return _ck_in(e.get('course') or '', words)


def _kind_of(m: re.Match) -> str:
    return 'tutorial' if m.group(1) else 'lecture'


# ── reading the answer ──────────────────────────────────────────────────────

def _date(dd: int, mm: int, yy: str | None, now: datetime) -> date | None:
    try:
        if yy:
            return date(int(yy) + (2000 if len(yy) == 2 else 0), mm, dd)
        d = date(now.year, mm, dd)                             # an answer's dates are ahead: 28/05 in October is next May
        return d if d >= now.date() - timedelta(days=60) else date(now.year + 1, mm, dd)
    except ValueError:
        return None


def _days_in(line: str, now: datetime) -> list[date]:
    out = []
    for m in DATE_RX.finditer(line):
        if d := _date(int(m.group(1)), int(m.group(2)), m.group(3), now):
            out.append(d)
    for m in REL_RX.finditer(line):
        out.append(now.date() + timedelta(days={'מחרתיים': 2, 'מחר': 1}.get(m.group(1), 0)))
    return out


def _weekday_fixes(text: str, now: datetime, fixes: list) -> str:
    def sub(m: re.Match) -> str:
        d = _date(int(m.group(2)), int(m.group(3)), None, now)
        if not d or DAYS[m.group(1)] == d.weekday():
            return m.group(0)
        right = WEEKDAY_HE[d.weekday()]
        fixes.append({'kind': 'weekday', 'said': m.group(0), 'fixed': right})
        return m.group(0)[:m.start(1) - m.start(0)] + right + m.group(0)[m.end(1) - m.start(0):]
    return WD_DATE_RX.sub(sub, text)


def _context_days(lines: list[str], now: datetime) -> list[date | None]:
    """The day each line is about: its own date / "מחר", else the last header line's, else the answer's only day."""
    every = {d for ln in lines for d in _days_in(ln, now)}
    only = next(iter(every)) if len(every) == 1 else None
    cur, out = None, []
    for ln in lines:
        own = set(_days_in(ln, now))
        if len(own) == 1:
            d = own.pop()
            if not TIME_RX.search(ln) or ln.strip().startswith(('*', '🗓', '📅')) or ln.rstrip().endswith(':'):
                cur = d                                       # a header: the lines under it are that day's
            out.append(d)
        elif own:
            out.append(None)                                  # two days on one line: can't tell
        else:
            out.append(cur or only)
    return out


# ── the checks ──────────────────────────────────────────────────────────────

def _say_day(d: date, now: datetime) -> str:
    left = (d - now.date()).days
    rel = 'היום, ' if left == 0 else 'מחר, ' if left == 1 else ''
    return f'{rel}{d:%d/%m}'


def check(text: str, now: datetime | None = None, rows: list[dict] | None = None, portal: list[dict] | None = None,
          store: dict | None = None) -> tuple[str, list[dict]]:
    """→ (the text, corrected / with a short correction appended, [the fixes]). Any failure → the text as it was."""
    try:
        return _check(text, now or datetime.now(), rows, portal, store)
    except Exception as e:
        print(f'  ⚠️ factcheck: {e!r}'[:200])
        return text, []


def _check(text: str, now: datetime, rows, portal, store) -> tuple[str, list[dict]]:
    if not text or not (TIME_RX.search(text) or DATE_RX.search(text) or REL_RX.search(text)):
        return text, []
    fixes: list[dict] = []
    text = _weekday_fixes(text, now, fixes)
    from agent import schedule
    rows = schedule.load() if rows is None else rows
    portal = _portal() if portal is None else portal
    words = _course_words()
    try:
        from agent.obligations import _course_codes
        codes = _course_codes()
    except Exception:
        codes = {}
    lines = text.split('\n')
    days = _context_days(lines, now)
    notes: list[str] = []
    said_cancel = bool(CANCEL_RX.search(text))
    for ln, day in zip(lines, days):
        if not day:
            continue
        ck = _ck_in(ln, words)
        if not ck:
            continue
        if DEADLINE_RX.search(ln) and (hw := HW_RX.search(ln)):
            notes += _deadline(ln, ck, hw.group(1), day, now, store, fixes)
            continue
        km = KIND_RX.search(ln)
        tm = TIME_RX.search(ln)
        if not tm or not km or NOT_SESSION_RX.search(ln) or DEADLINE_RX.search(ln):
            continue                                          # a session is named as one ("תרגול", "שיעור"), or not checked
        kind = _kind_of(km)
        hm = f'{int(tm.group(1)):02d}:{tm.group(2)}'
        o = schedule.find(day, '', hm, ck=ck, kind=kind, rows=rows)
        if o and o.get('status') == 'cancelled':
            if not CANCEL_RX.search(ln) and not (said_cancel and _cancel_said_for(text, ck)):
                what = KIND_WORD.get(o.get('kind') or kind, 'השיעור')
                src = f" ({o['source']})" if o.get('source') else ''
                notes.append(f'⚠️ שים לב: {what} ב{_short(ck)} ב-{hm} ({_say_day(day, now)}) מבוטל{src}')
                fixes.append({'kind': 'cancelled', 'line': ln.strip()[:160], 'date': day.isoformat(), 'ck': ck, 'start': hm})
            continue
        same = [e for e in portal if e.get('date') == day.isoformat() and _portal_ck(e, words, codes) == ck
                and not e.get('is_exam') and PORTAL_KIND[kind] in (e.get('kind') or '')]
        if not same:
            continue
        at = [e for e in same if (e.get('time') or '')[:5] == hm]
        if at:
            notes += _room(ln, at[0], ck, kind, hm, day, now, o, fixes)
        elif len(same) == 1 and not any((e.get('time') or '')[:5] == hm for e in portal
                                        if e.get('date') == day.isoformat() and _portal_ck(e, words, codes) == ck):
            right = same[0]['time'][:5]
            notes.append(f'⚠️ שים לב: לפי מערכת השעות {KIND_WORD[kind]} ב{_short(ck)} ({_say_day(day, now)}) ב-{right}, לא ב-{hm}')
            fixes.append({'kind': 'time', 'line': ln.strip()[:160], 'date': day.isoformat(), 'ck': ck, 'said': hm, 'right': right})
    notes = list(dict.fromkeys(notes))
    if notes:
        text = text.rstrip() + '\n\n' + '\n'.join(notes)
    for f in fixes:
        log(f['kind'], f)
    return text, fixes


def _cancel_said_for(text: str, ck: str) -> bool:
    """The answer already says, on some line, that this course's session is cancelled."""
    words = _course_words().get(ck, set())
    return any(CANCEL_RX.search(ln) and any(w in ln for w in words) for ln in text.split('\n'))


def _short(ck: str) -> str:
    from config.paths import COURSE_NAMES
    return COURSE_NAMES.get(ck, ck)


def _room(ln, e, ck, kind, hm, day, now, o, fixes) -> list[str]:
    """The line names a room (3 digits) for this session, and the site's room for it is another one."""
    said = set(ROOM_RX.findall(TIME_RX.sub(' ', DATE_RX.sub(' ', ln))))
    right = str(e.get('room') or '').strip()
    if not said or not right or right in said or (o and o.get('status') == 'room'):
        return []
    if not re.fullmatch(r'\d{3}', right):
        return []
    where = ' '.join(x for x in (e.get('building'), right) if x)
    fixes.append({'kind': 'room', 'line': ln.strip()[:160], 'date': day.isoformat(), 'ck': ck, 'said': sorted(said), 'right': right})
    return [f'⚠️ שים לב: לפי אתר האוניברסיטה {KIND_WORD[kind]} ב{_short(ck)} ב-{hm} ({_say_day(day, now)}) ב{where}']


def _deadline(ln, ck, num, day, now, store, fixes) -> list[str]:
    """"תרגיל 4 … עד 18/11" vs the close Moodle gives (the obligations store)."""
    if not DATE_RX.search(ln) and not REL_RX.search(ln):
        return []
    try:
        from agent import obligations
        store = obligations.load() if store is None else store
    except Exception:
        return []
    hits = [r for r in (store.get('items') or {}).values() if r.get('course') == ck and r.get('kind') != 'quiz'
            and obligations.hw_num(r.get('title') or '') == num and r.get('due') and not r.get('due_est')]
    if len(hits) != 1:
        return []
    try:
        due = datetime.fromisoformat(hits[0]['due'])
    except ValueError:
        return []
    if due.date() == day or (due.hour == 0 and due.minute == 0 and due.date() - timedelta(days=1) == day):
        return []
    fixes.append({'kind': 'deadline', 'line': ln.strip()[:160], 'said': day.isoformat(), 'right': hits[0]['due'], 'ck': ck})
    return [f"⚠️ שים לב: לפי מודל {hits[0].get('title', f'תרגיל {num}')} ב{_short(ck)} להגשה עד "
            f"יום {WEEKDAY_HE[due.weekday()]} {due:%d/%m} ב-{due:%H:%M}"]


# ── the outs and the log ────────────────────────────────────────────────────

def apply(outs: list, now: datetime | None = None) -> list:
    """Every text / buttons Out checked; the same Outs otherwise. Never raises."""
    try:
        from dataclasses import replace
        res = []
        for o in outs:
            if getattr(o, 'kind', '') in ('text', 'buttons') and getattr(o, 'text', ''):
                new, fx = check(o.text, now)
                res.append(replace(o, text=new) if fx else o)
            else:
                res.append(o)
        return res
    except Exception as e:
        print(f'  ⚠️ factcheck apply: {e!r}'[:200])
        return outs


def log(kind: str, detail: dict, path: Path | None = None) -> None:
    try:
        p = path or LOG
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': datetime.now().isoformat(timespec='seconds'), **detail, 'kind': kind},
                               ensure_ascii=False, default=str) + '\n')
    except OSError:
        pass
