"""
agent/deadline_cal.py — every deadline the owner owes, in his own Google Calendar, kept up to date by itself (20/11).

The owner 20/11: all the deadlines in his calendar, automatically, without a ✅ card each time. The standing authorisation is
config/assistant.yaml `auto_deadlines_to_calendar: true` (default false: config/assistant.example.yaml). Every other
calendar write keeps its card.

    what      every item of agent/obligations' store with a due date (Moodle assignments / quizzes, lecturer sites, a
              deadline he stated, exam registrations) + the university's exam dates (store['exams'])
    event     "📝 הגשה: <course> — <item>", 30 minutes ending at the due time, the Moodle link in the description,
              popups a day and 3 hours before. Tagged ssa_src=obligation (+ oid) in his PRIMARY calendar only.
    changes   the due date moved → the event moves · submitted (Moodle, ✅, "הגשתי") → "✅ הוגש · …", no popups ·
              gone from Moodle / "לא רלוונטי" → "❌ בוטל · …", no popups. NOTHING is ever deleted.
    once      data/deadline_calendar.json: oid → {event id, fingerprint}. An item whose event is unchanged costs no
              call; a new one gets a stable id (gcal.deadline_event_id), so two processes can't make two events.
              An event of his for the same deadline on that day ("הגשת משימה ב׳ בתוכנה 1") is taken over, not doubled.
              An event he deleted himself stays deleted.
    guard     more than MAX_GONE items leaving at once (a broken scrape, the semester's end) → none is marked ❌.
Runs after each obligations refresh (agent/obligations.refresh → after_refresh). No LLM, no message to anyone.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'config' / 'assistant.yaml'
MAP = ROOT / 'data' / 'deadline_calendar.json'
TAG, VALUE = 'ssa_src', 'obligation'
CAL = 'primary'                                  # his own calendar — never anything else
LENGTH = timedelta(minutes=30)
POPUPS = (1440, 180)                             # a day before, 3 hours before
MAX_GONE = 3
PAST = timedelta(days=1)                         # an item already past when first seen gets no event
MOODLE = 'https://moodle.tau.ac.il'


def enabled(path: Path | None = None) -> bool:
    try:
        import yaml
        d = yaml.safe_load((path or CONFIG).read_text(encoding='utf-8')) or {}
        return d.get('auto_deadlines_to_calendar') is True
    except Exception:
        return False


def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or MAP).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d.get('events'), dict):
        d['events'] = {}
    return d


def _save(d: dict, path: Path | None = None) -> None:
    p = path or MAP
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def synced(oid: str, path: Path | None = None) -> bool:
    """This obligation has its event in the calendar (fastpath / the day's list don't say it twice)."""
    e = _load(path)['events'].get(oid) or {}
    return bool(e.get('id')) and not e.get('owner_deleted')


def course_link(ck: str) -> str:
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / 'config' / 'courses.yaml').read_text(encoding='utf-8')) or {}
        cid = str((((cfg.get('courses') or {}).get(ck) or {}).get('sources') or {}).get('moodle', {}).get('course_id') or '')
        if cid.isdigit():
            return f'{MOODLE}/course/view.php?id={cid}'
    except Exception:
        pass
    return f'{MOODLE}/my/'


# ── what the calendar should hold (pure) ────────────────────────────────────

def _state(rec: dict, now: datetime) -> str:
    from agent import obligations as ob
    if rec.get('gone') or rec.get('closed_why') == 'irrelevant':
        return 'cancelled'
    return 'done' if ob.status(rec, now) == 'closed' else 'open'


def event_for(rec: dict, state: str) -> dict:
    """The event body for one obligation in a state. Pure."""
    from agent import obligations as ob
    due = ob._dt(rec.get('due'))
    course = ob._course_name(rec.get('course', ''))
    title = (rec.get('title') or '').strip()
    if rec.get('kind') == 'exam':
        base = f"מבחן: {course} — {title}"
        mark = '🎯 '
    else:
        base = f"{'הרשמה' if rec.get('kind') == 'exam_reg' else 'הגשה'}: {course} — {title}"
        mark = '📝 '
    summary = {'open': mark + base, 'done': '✅ הוגש · ' + base, 'cancelled': '❌ בוטל · ' + base}[state][:200]
    link = rec.get('url') or course_link(rec.get('course', ''))
    when = f"{due:%d/%m/%Y %H:%M}" + (' (בערך — לפי "פתוח שבועיים")' if rec.get('due_est') else '')
    desc = (f"{link}\n\nמועד: {when}\n"
            + {'open': 'כשתגיש — האירוע יסומן ✅ לבד.', 'done': '✅ הוגש.', 'cancelled': '❌ בוטל / לא רלוונטי.'}[state]
            + '\n(נוסף אוטומטית מרשימת המטלות — Smart Study)')
    start = due - LENGTH
    return {'summary': summary, 'description': desc,
            'start': {'dateTime': start.strftime('%Y-%m-%dT%H:%M:00'), 'timeZone': 'Asia/Jerusalem'},
            'end': {'dateTime': due.strftime('%Y-%m-%dT%H:%M:00'), 'timeZone': 'Asia/Jerusalem'},
            'reminders': {'useDefault': False,
                          'overrides': [{'method': 'popup', 'minutes': m} for m in POPUPS] if state == 'open' else []},
            'extendedProperties': {'private': {TAG: VALUE, 'oid': rec['id'][:200]}}}


def _exam_recs(store: dict) -> list[dict]:
    out = []
    for ck, xs in (store.get('exams') or {}).items():
        for x in xs or []:
            if not x.get('date'):
                continue
            t = x.get('time') or '09:00'
            moed = f"מועד {x['moed']}'" if x.get('moed') else 'מבחן'
            where = ' · '.join(v for v in (x.get('building'), x.get('room')) if v)
            # 'due' = the exam's start + 30 min, so the 30-minute event (ending at 'due') begins at the exam's hour
            at = datetime.fromisoformat(f"{x['date']}T{t}") + LENGTH
            out.append({'id': f"{ck}:exam:{x['date']}", 'kind': 'exam', 'course': ck,
                        'title': moed + (f" ({where})" if where else ''), 'due': at.isoformat(timespec='minutes'),
                        'url': 'https://my.tau.ac.il'})
    return out


def _fp(body: dict) -> str:
    return hashlib.sha1(json.dumps(body, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:16]


def plan(store: dict, now: datetime, mapping: dict) -> list[tuple[str, str, dict]]:
    """[(oid, 'create'|'update', body)] — what to write. Pure (store + mapping + now)."""
    from agent import obligations as ob
    have = mapping.get('events') or {}
    recs = {r['id']: r for r in (store.get('items') or {}).values() if r.get('id') and ob._dt(r.get('due'))}
    exams = {r['id']: r for r in _exam_recs(store)}
    want: dict[str, tuple[dict, str]] = {}
    for oid, r in recs.items():
        want[oid] = (r, _state(r, now))
    for oid, r in exams.items():
        want[oid] = (r, 'open')
    for oid, e in have.items():                        # an exam that left the university's list: cancelled
        if oid not in want and ':exam:' in oid and e.get('rec'):
            r = e['rec']
            if (ob._dt(r.get('due')) or now) > now:
                want[oid] = (r, 'cancelled')
    # 20/11 a deadline he stated, and then Moodle / a mail brought the same one: the event moves over, not ❌ + new
    for oid, (r, st) in list(want.items()):
        if st != 'cancelled' or oid not in have or have[oid].get('moved_to') or not r.get('gone'):
            continue
        day = (r.get('due') or '')[:10]
        twin = next((o for o, (r2, s2) in want.items() if o != oid and o not in have and s2 != 'cancelled'
                     and r2.get('course') == r.get('course') and (r2.get('due') or '')[:10] == day
                     and r2.get('kind') != 'quiz'), None)
        if twin:
            have[twin] = {**have[oid], 'fp': ''}
            have[oid] = {**have[oid], 'moved_to': twin}
    out = []
    newly_gone = []
    for oid, (r, st) in want.items():
        e = have.get(oid) or {}
        if e.get('owner_deleted') or e.get('moved_to'):
            continue
        due = ob._dt(r.get('due'))
        if not e.get('id'):
            if st != 'open' or due < now - PAST:
                continue                               # nothing to put in for a past / closed / cancelled item
            out.append((oid, 'create', event_for(r, st)))
            continue
        if st == 'cancelled' and e.get('state') != 'cancelled':
            if due < now - PAST:
                continue                               # a past item leaving Moodle isn't news
            newly_gone.append(oid)
        body = event_for(r, st)
        if _fp(body) != e.get('fp'):
            out.append((oid, 'update', body))
    if len(newly_gone) > MAX_GONE:                     # a broken scrape: never a wave of ❌
        print(f'  ⚠️ deadline calendar: {len(newly_gone)} items left at once — not marking them cancelled')
        out = [x for x in out if x[0] not in newly_gone]
    return out


# ── the calendar side ───────────────────────────────────────────────────────

def _status(e) -> str:
    return str(getattr(getattr(e, 'resp', None), 'status', None) or getattr(e, 'status_code', None) or '')


def _words(s: str) -> set[str]:
    ws = set(re.findall(r'[א-תA-Za-z0-9]{2,}', s or ''))
    return {w[1:] if w[:1] in 'בלהו' and len(w) > 3 else w for w in ws}


def _his_twin(svc, rec: dict, body: dict) -> dict | None:
    """An event of his on that day for this same deadline (made through a ✅ card before this existed). Only an
    untagged one in the primary calendar whose title says it's a submission and names the course or the item."""
    from integrations import gcal
    from agent import obligations as ob
    due = ob._dt(rec.get('due'))
    day0 = datetime(due.year, due.month, due.day)
    tz = gcal._tz()
    items = svc.events().list(calendarId=CAL, timeMin=day0.replace(tzinfo=tz).isoformat(),
                              timeMax=(day0 + timedelta(days=1)).replace(tzinfo=tz).isoformat(),
                              singleEvents=True, maxResults=50).execute().get('items') or []
    course_w = _words(ob._course_name(rec.get('course', ''))) - {'1', '2'}
    item_w = _words(rec.get('title', '')) - {'Quiz', 'Lecture', 'Assignment'}
    for it in items:
        priv = (it.get('extendedProperties') or {}).get('private') or {}
        title = it.get('summary') or ''
        if priv or it.get('status') == 'cancelled' or title.lstrip().startswith(('✅', '❌')):
            continue
        if not re.search(r'הגש|מטל|תרגיל|משימ|בוחן|הרשמה|מבחן|deadline|assignment', title, re.I):
            continue
        if _words(title) & (course_w | item_w):
            return it
    return None


def _write(svc, oid: str, how: str, body: dict, rec: dict, e: dict) -> dict:
    """One create / update → the mapping entry. Only CAL, only insert / get / update — never delete."""
    from integrations import gcal
    ev = svc.events()
    if how == 'create':
        twin = None
        try:
            twin = _his_twin(svc, rec, body)
        except Exception as ex:
            print(f'  ⚠️ deadline calendar twin: {ex!r}'[:160])
        if twin:
            full = {**twin, **body}
            got = ev.update(calendarId=CAL, eventId=twin['id'], body=full).execute()
            return {'id': got.get('id') or twin['id'], 'adopted': True}
        eid = gcal.deadline_event_id(oid)
        try:
            got = ev.insert(calendarId=CAL, body={**body, 'id': eid}).execute()
            return {'id': got.get('id') or eid}
        except Exception as ex:
            if _status(ex) != '409' and '409' not in str(ex):
                raise
            how, e = 'update', {'id': eid}             # it exists (another process, or the old opt-in mirror)
    cur = ev.get(calendarId=CAL, eventId=e['id']).execute()
    if cur.get('status') == 'cancelled':               # he deleted it himself: it stays deleted
        return {'id': e['id'], 'owner_deleted': True}
    full = {k: v for k, v in cur.items() if k not in ('start', 'end')}
    full.update(body)
    ev.update(calendarId=CAL, eventId=e['id'], body=full).execute()
    return {'id': e['id'], **({'adopted': True} if e.get('adopted') else {})}


def sync(store: dict, now: datetime | None = None, service=None, path: Path | None = None) -> dict:
    """Bring the calendar in line with the store. → counts. A write that fails is retried on the next refresh."""
    from agent import obligations as ob
    now = now or datetime.now()
    m = _load(path)
    todo = plan(store, now, m)
    counts = {'created': 0, 'updated': 0, 'failed': 0}
    if not todo:
        if any(e.get('moved_to') for e in m['events'].values()):
            _save(m, path)
        return counts
    if service is None:
        from integrations import gcal
        service = gcal._service()
    recs = {r['id']: r for r in (store.get('items') or {}).values()}
    recs.update({r['id']: r for r in _exam_recs(store)})
    for oid, how, body in todo:
        e = m['events'].get(oid) or {}
        rec = recs.get(oid) or e.get('rec') or {'id': oid}
        try:
            got = _write(service, oid, how, body, rec, e)
        except Exception as ex:
            counts['failed'] += 1
            print(f'  ⚠️ deadline calendar {oid}: {ex!r}'[:200])
            continue
        st = 'cancelled' if body['summary'].startswith('❌') else 'done' if body['summary'].startswith('✅') else 'open'
        m['events'][oid] = {**got, 'fp': _fp(body), 'state': st, 'ts': now.isoformat(timespec='minutes'),
                            'title': body['summary'][:120], 'due': rec.get('due', ''),
                            **({'rec': {k: rec.get(k) for k in ('id', 'kind', 'course', 'title', 'due', 'url')}}
                               if ':exam:' in oid else {})}
        counts['created' if how == 'create' and not got.get('owner_deleted') else 'updated'] += 1
    _save(m, path)
    if counts['created'] or counts['updated']:
        print(f"  📅 deadline calendar: {counts['created']} new, {counts['updated']} updated")
    return counts


def after_refresh(store: dict, now: datetime | None = None) -> dict | None:
    """agent/obligations.refresh's hook: on only with the owner's standing authorisation and a calendar token. Never raises."""
    try:
        if not enabled():
            return None
        from agent import obligations as ob                # a test's own store never reaches the real calendar
        if Path(ob.STORE).resolve() != (ROOT / 'data' / 'obligations.json').resolve():
            return None
        from integrations import gcal
        if not gcal.configured():
            return None
        return sync(store, now)
    except Exception as e:
        print(f'  ⚠️ deadline calendar: {e!r}'[:200])
        return None
