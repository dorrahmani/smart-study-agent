"""
integrations/tau_portal.py — the owner's personal exam schedule, straight from the university (my.tau.ac.il).

"באיזה שעה המבחן?" (The owner or the partner) → a live check of the university's personal area: headless login with the
same TAU SSO the Moodle collectors use, then the calendar JSON the page itself loads. No LLM at all — the
answer is built only from what the university returned. If the check fails it falls back to the last
successful check (and says when it was); with nothing known it says so — it never guesses a time or room.

Also cross-checks against Google Calendar: exams the university has that the owner's calendar is missing.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

from config.paths import DATA_DIR

CACHE = DATA_DIR / 'tau_calendar.json'
FRESH_SECONDS = 30 * 60          # a check from the last 30 min is "now" — don't log in again per message
HOME = 'https://my.tau.ac.il/TAU_Student/'
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
# what owner/partner say → words in the university's course titles
ALIASES = {
    'חדו"א': 'חשבון דיפרנציאלי', 'חדוא': 'חשבון דיפרנציאלי', 'חדו״א': 'חשבון דיפרנציאלי', 'אינפי': 'חשבון דיפרנציאלי',
    'לינארית': 'אלגברה לינארית', 'אלגברה': 'אלגברה לינארית', 'בדידה': 'בדידה', 'מבוא': 'מבוא מורחב',
    'מדמ"ח': 'מבוא מורחב', 'מדעי המחשב': 'מבוא מורחב', 'מיקרו': 'מיקרו כלכלה', 'מאקרו': 'מאקרו כלכלה',
    'הסתברות': 'הסתברות', 'סטטיסטיקה': 'הסתברות', 'הנדסת תוכנה': 'תוכנה', 'תוכנה': 'תוכנה', 'ג\'אווה': 'תוכנה', 'java': 'תוכנה',   # 'תוכנה 1' → 'מבוא להנדסת תוכנה'
}


# ── fetching (headless browser, no LLM) ────────────────────────────────────

def _events_from(obj, out: list) -> None:
    """Walk any OutSystems JSON and collect calendar-event dicts (they carry 'start' + 'title')."""
    if isinstance(obj, dict):
        if 'start' in obj and 'title' in obj and obj.get('title'):
            out.append(obj)
            return
        for v in obj.values():
            _events_from(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _events_from(v, out)


def _norm(e: dict) -> dict:
    title = (e.get('title') or '').strip()
    start = (e.get('start') or '').strip()
    hhmm = ''
    if m := re.search(r'שעת התחלה\s*(\d{1,2}:\d{2})', title):
        hhmm = m.group(1).zfill(5)
    elif re.search(r' \d{2}:\d{2}', start) and not start.endswith('00:00:00'):
        hhmm = start[11:16]
    room = (e.get('Room') or '').strip()
    return {
        'course': re.sub(r'\s*-\s*שעת התחלה.*$', '', title).strip(),
        'date': start[:10], 'time': hhmm,
        'kind': (e.get('LessonTypeHeb') or e.get('TagDescHeb') or '').strip(),
        'is_exam': (e.get('EventType') == 'exam') or 'בחינה' in (e.get('LessonTypeHeb') or ''),
        'building': (e.get('BuildingNameHeb') or '').strip(),
        'room': '' if room.strip('0') == '' else room,
        'course_number': e.get('CourseNumber') or '',
        'group': (e.get('Group') or '').strip(),                 # "01" — which lecture/tutorial group is the owner's
        'end_time': (e.get('end') or '')[11:16] if re.search(r' \d{2}:\d{2}', e.get('end') or '') else '',
        'lecturer': ' '.join(x for x in ((e.get('LecturerDegreeHeb') or '').strip(), (e.get('LecturerFirstNameHeb') or '').strip(),
                                         (e.get('LecturerLastNameHeb') or '').strip()) if x),
    }


def _portal(want, click: str, timeout_ms: int = 40000) -> list:
    """Log in, open the personal area, click `click` (a menu item / link text) and return the JSON bodies of
    the data calls whose URL satisfies `want(url)`. Raises on failure."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from playwright.sync_api import sync_playwright
    from collectors.announcement_scraper import _login
    bodies: list = []

    def on_resp(r):
        if 'screenservices' in r.url and want(r.url):
            try:
                bodies.append(r.json())
            except Exception:
                pass

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        try:
            page = b.new_context(locale='he-IL').new_page()
            page.on('response', on_resp)
            if not _login(page):
                raise RuntimeError('university login failed')
            page.goto(HOME, timeout=timeout_ms)
            page.wait_for_load_state('networkidle', timeout=timeout_ms)
            try:
                page.get_by_text('Got it').first.click(timeout=3000)
            except Exception:
                pass
            page.get_by_text(click).first.click(timeout=15000)
            page.wait_for_load_state('networkidle', timeout=timeout_ms)
            page.wait_for_timeout(3000)
        finally:
            b.close()
    return bodies


def fetch(timeout_ms: int = 40000) -> list[dict]:
    """Log in and read the university calendar (home + full calendar). Raises on failure."""
    raw: list = []
    for body in _portal(lambda u: 'Calendar' in u or 'Schedules' in u, 'ליומן המלא', timeout_ms):
        _events_from(body, raw)
    seen, events = set(), []
    for e in map(_norm, raw):
        k = (e['course'], e['date'], e['time'])
        if e['date'] and k not in seen:
            seen.add(k)
            events.append(e)
    if not events:
        raise RuntimeError('the university calendar came back empty')
    return sorted(events, key=lambda e: (e['date'], e['time']))


def _save(events: list[dict]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({'fetched_at': datetime.now().isoformat(timespec='seconds'), 'events': events},
                                ensure_ascii=False, indent=1), encoding='utf-8')
    try:
        os.chmod(CACHE, 0o600)
    except OSError:
        pass


def _cached() -> dict | None:
    try:
        return json.loads(CACHE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


CHANGES = DATA_DIR / 'tau_exam_changes.json'


def exam_changes(old: list[dict], new: list[dict], today: str | None = None) -> list[dict]:
    """Exams the university moved between two checks: per course, a (date, time) that disappeared paired with
    one that appeared. Only future exams; a course that only gained or only lost an exam isn't a "move"."""
    today = today or date.today().isoformat()
    out = []
    for course in {e['course'] for e in new if e['is_exam']}:
        o = {(e['date'], e['time']) for e in old if e['is_exam'] and e['course'] == course and e['date'] >= today}
        n = {(e['date'], e['time']) for e in new if e['is_exam'] and e['course'] == course and e['date'] >= today}
        gone, came = sorted(o - n), sorted(n - o)
        for (od, ot), (nd, nt) in zip(gone, came):
            out.append({'type': 'move', 'course': course, 'old': {'date': od, 'time': ot}, 'new': {'date': nd, 'time': nt}})
    return out


def room_changes(old: list[dict], new: list[dict], today: str | None = None) -> list[dict]:
    """An exam's room was published or changed (same course + date, new place not empty)."""
    today = today or date.today().isoformat()
    before = {(e['course'], e['date']): e for e in old if e['is_exam']}
    out = []
    for e in new:
        o = before.get((e['course'], e['date']))
        if not e['is_exam'] or not o or e['date'] < today or not (e['room'] or e['building']):
            continue
        if (o['building'], o['room']) != (e['building'], e['room']):
            out.append({'type': 'room', 'course': e['course'], 'date': e['date'], 'time': e['time'],
                        'old_place': ' '.join(x for x in (o['building'], o['room'] and f"חדר {o['room']}") if x),
                        'place': ' '.join(x for x in (e['building'], e['room'] and f"חדר {e['room']}") if x)})
    return out


def _record_changes(ch: list[dict]) -> None:
    if not ch:
        return
    try:
        cur = json.loads(CHANGES.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cur = []
    cur += [dict(c, seen=datetime.now().isoformat(timespec='seconds')) for c in ch]
    CHANGES.parent.mkdir(parents=True, exist_ok=True)
    CHANGES.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding='utf-8')


def take_changes() -> list[dict]:
    """Pending moves (each reported once)."""
    try:
        cur = json.loads(CHANGES.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    CHANGES.write_text('[]', encoding='utf-8')
    return cur


def schedule(live: bool = True, fetcher=None) -> tuple[list[dict] | None, str | None, bool]:
    """(events, fetched_at, is_fresh). Checks the site unless a check from the last 30 min exists.
    A live check also records exams the university moved since the previous check."""
    c = _cached()
    if c and (datetime.now() - datetime.fromisoformat(c['fetched_at'])).total_seconds() < FRESH_SECONDS:
        return c['events'], c['fetched_at'], True
    if live:
        try:
            ev = (fetcher or fetch)()
            if c:
                _record_changes(exam_changes(c['events'], ev) + room_changes(c['events'], ev))
            _save(ev)
            return ev, datetime.now().isoformat(timespec='seconds'), True
        except Exception as e:
            print(f'[tau_portal] live check failed: {e}', file=sys.stderr)
    if c:
        return c['events'], c['fetched_at'], False
    return None, None, False


# ── answering (only from what the university returned) ─────────────────────

def course_hint(text: str) -> str:
    t = text.lower()
    for k, v in sorted(ALIASES.items(), key=lambda kv: -len(kv[0])):     # 'מבוא להנדסת תוכנה' ≠ 'מבוא' (the old intro)
        if k.lower() in t:
            return v
    return ''


def _line(e: dict) -> str:
    d = date.fromisoformat(e['date'])
    left = (d - date.today()).days
    when = 'היום' if left == 0 else ('מחר' if left == 1 else f'בעוד {left} ימים')
    out = f"📚 *{e['course']}* — {e['kind'] or 'בחינה'}\nיום {WEEKDAY_HE[d.weekday()]} {d:%d/%m} ({when})"
    out += f", שעת התחלה *{e['time']}*" if e['time'] else ' — שעה: לא מופיעה באתר'
    place = ' '.join(x for x in (e['building'], f"חדר {e['room']}" if e['room'] else '') if x)
    return out + (f'\n📍 {place}' if place else '\n📍 חדר: עוד לא פורסם באתר')


def exam_answer(text: str, for_partner: bool = False, fetcher=None, missing_check=None) -> str:
    events, at, fresh = schedule(fetcher=fetcher)
    if events is None:
        return ('לא הצלחתי להיכנס עכשיו לאתר האוניברסיטה, ואין לי בדיקה קודמת — אז אני לא יודע, ולא אנחש.\n'
                'אפשר לבדוק ב-my.tau.ac.il ← יומן.')
    today = date.today().isoformat()
    exams = [e for e in events if e['is_exam'] and e['date'] >= today]
    hint = course_hint(text)
    picked = [e for e in exams if hint and hint in e['course']] if hint else exams
    more = ''
    if not hint and not re.search(r'מבחנים|בחינות', text) and len(picked) > 1:     # 'המבחן' → the next one
        more = f"\n\n(יש עוד {len(picked) - 1} מבחנים — שאל 'באיזה שעה המבחנים?' לכולם)"
        picked = picked[:1]
    head = '🎓 *לפי אתר האוניברסיטה*' + (' (בדקתי עכשיו)' if fresh else
                                          f" — לא הצלחתי להיכנס עכשיו, זה מבדיקה מ-{at[8:10]}/{at[5:7]} {at[11:16]}")
    if hint and not picked:
        others = '\n\n'.join(_line(e) for e in exams[:5])
        return (f'{head}\nאין באתר מבחן שתואם "{hint}".' +
                (f'\nהמבחנים שרשומים שם:\n\n{others}' if others else '\nאין שם מבחנים קרובים בכלל.'))
    if not picked:
        return f'{head}\nאין באתר מבחנים קרובים.'
    out = head + '\n\n' + '\n\n'.join(_line(e) for e in picked[:6]) + more
    if not for_partner and missing_check:
        try:
            miss = missing_check(picked)
        except Exception:
            miss = []
        if miss:
            out += '\n\n⚠️ ביומן גוגל שלך חסרים: ' + ', '.join(f"{e['course']} ({e['date'][8:]}/{e['date'][5:7]})" for e in miss)
    return out


def _same_course(course: str, summary: str) -> bool:
    """'חשבון דיפרנציאלי ואינטגרלי 2ב' vs 'מבחן בחדו"א — מועד ב' → True (via the aliases)."""
    if not re.search(r'מבחן|בחינה|מועד', summary):
        return False
    h = course_hint(summary)
    if h:
        return h in course
    words = {w for w in re.findall(r'[א-ת]{4,}', course)}
    return any(w in summary for w in words)


def gcal_match(exam: dict, day: str | None = None, service=None) -> dict | None:
    """The owner's calendar event for this exam on `day` (default: the exam's date)."""
    from integrations import gcal
    evs = gcal.events_on(date.fromisoformat(day or exam['date']), service=service)
    return next((x for x in evs if _same_course(exam['course'], x.get('summary') or '')), None)


def calendar_issues(exams: list[dict]) -> list[dict]:
    """[{'type': 'missing'|'time', 'exam', 'event'?}] — not in the calendar at all, or there but all-day /
    at another time than the university's start time."""
    from integrations import gcal
    if not gcal.configured():
        return []
    svc = gcal._service()
    out = []
    for e in exams:
        ev = gcal_match(e, service=svc)
        if not ev:
            out.append({'type': 'missing', 'exam': e})
            continue
        st = ev.get('start') or {}
        if e['time'] and ('date' in st or st.get('dateTime', '')[11:16] != e['time']):
            out.append({'type': 'time', 'exam': e, 'event': ev})
    return out


def missing_in_gcal(exams: list[dict]) -> list[dict]:
    """University exams with no exam-looking event on that day in the owner's Google Calendar."""
    from integrations import gcal
    if not gcal.configured():
        return []
    svc = gcal._service()
    miss = []
    for e in exams:
        evs = gcal.events_on(date.fromisoformat(e['date']), service=svc)
        if not any(_same_course(e['course'], x.get('summary') or '') for x in evs):
            miss.append(e)
    return miss


# ── grades (owner only — never shown to the partner) ───────────────────────

GRADES = DATA_DIR / 'tau_grades.json'
NO_DATE = '1900-01-01'


def _grade_row(r: dict) -> dict:
    g = str(r.get('FinalGrade') or '').strip()
    upd = (r.get('UpdateDate') or '')[:19]
    return {
        'course': (r.get('CourseDescription') or '').strip(),
        'number': r.get('CourseNumber') or '', 'sem': r.get('YearAndSemester') or '',
        'sem_heb': f"{r.get('YearHeb') or ''} {r.get('SemesterHeb') or ''}".strip(),
        # a course with no grade yet still comes back with a placeholder — no update date = no grade
        'grade': g if g and not upd.startswith(NO_DATE) else '',
        'due': (r.get('DueInHeb') or '').strip(), 'updated': '' if upd.startswith(NO_DATE) else upd,
        'note': ' '.join(x for x in ((r.get('GradeDescription') or '').strip(), (r.get('GradeNote') or '').strip(),
                                     (r.get('Remarks') or '').strip()) if x),
    }


def fetch_grades(timeout_ms: int = 40000) -> list[dict]:
    rows: list = []
    for body in _portal(lambda u: 'CourseGradesList' in u, 'ציונים', timeout_ms):
        try:
            rows += body['data']['CourseGradesList']['List']
        except (KeyError, TypeError):
            pass
    if not rows:
        raise RuntimeError('the grades page came back empty')
    return [_grade_row(r) for r in rows if r.get('CourseDescription')]


def grade_changes(old: list[dict], new: list[dict]) -> list[dict]:
    """Rows whose grade / exam session / update date changed and now carry a grade."""
    before = {(r['sem'], r['number']): r for r in old}
    out = []
    for r in new:
        o = before.get((r['sem'], r['number']))
        if r['grade'] and (o is None or (o['grade'], o['due'], o['updated']) != (r['grade'], r['due'], r['updated'])):
            out.append(dict(r, old_grade=(o or {}).get('grade', ''), old_due=(o or {}).get('due', '')))
    return out


def _save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def check_grades(fetcher=None) -> list[dict]:
    """Fetch, store, and return what changed since the last check. The very first check is a silent baseline
    (otherwise every old grade would be "new")."""
    rows = (fetcher or fetch_grades)()
    try:
        old = json.loads(GRADES.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        old = None
    _save_json(GRADES, {'fetched_at': datetime.now().isoformat(timespec='seconds'), 'rows': rows})
    return [] if old is None else grade_changes(old['rows'], rows)


def grade_alert_text(ch: dict) -> str:
    due = f" (מועד {ch['due']})" if ch['due'] else ''
    out = f"🎓 *פורסם ציון* — {ch['course']}{due}: *{ch['grade']}*"
    if ch.get('old_grade') and ch['old_grade'] != ch['grade']:
        out += f"\n(קודם: {ch['old_grade']}" + (f", מועד {ch['old_due']}" if ch.get('old_due') else '') + ')'
    if ch.get('note'):
        out += f"\n📝 {ch['note']}"
    return out + '\n(מאתר האוניברסיטה)'


GRADE_Q = re.compile(r'(?:מה|כמה) (?:ה)?ציון|(?:ה)?ציונים שלי|קיבלתי ב|יצא (?:ה)?ציון|פורסם (?:ה)?ציון|הממוצע שלי')


def grades_answer(text: str) -> str:
    """'מה הציון שלי בבדידה?' — from the last check (refreshed by scripts/tau_watch.py), never guessed."""
    try:
        c = json.loads(GRADES.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 'עוד לא בדקתי ציונים באתר האוניברסיטה — הבדיקה הראשונה תרוץ בקרוב. אני לא מנחש ציונים.'
    at = c['fetched_at']
    hint = course_hint(text)
    rows = [r for r in c['rows'] if r['grade'] and (not hint or hint in r['course'])]
    head = f"🎓 *ציונים — לפי אתר האוניברסיטה* (נבדק {at[8:10]}/{at[5:7]} {at[11:16]})"
    if not rows:
        pending = [r for r in c['rows'] if hint and hint in r['course']]
        return head + ('\nעוד אין ציון ב' + pending[0]['course'] + '.' if pending else
                       f'\nלא מצאתי קורס שתואם "{hint}".' if hint else '\nאין ציונים באתר.')
    if not hint:                                    # the latest semester that has grades
        last = max(r['sem'] for r in rows)
        rows = [r for r in rows if r['sem'] == last]
    return head + '\n' + '\n'.join(
        f"• {r['course']}: *{r['grade']}*" + (f" (מועד {r['due']})" if r['due'] else '') + (f" — {r['sem_heb']}" if hint else '')
        for r in rows)


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=True)
    print(exam_answer(' '.join(sys.argv[1:]) or 'מתי המבחנים', missing_check=missing_in_gcal))
