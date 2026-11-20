"""
agent/deadline_ladder.py — a deadline is pushed harder the closer it gets, until it's submitted (20/11). No LLM.

The owner 20/11: "תוודא כבר שאני מגיש". One explicit ladder for everything in agent/obligations' store:

    d7   7 days before        🗓️ a heads-up: start this week          (not for Software 1's quizzes: they have their
    d3   3 days before        📌 not submitted yet, book the time       own every-2-days reminder in obligations)
    d1   the day before       ⏰ tonight is the time to finish
    dm   the morning of it    ☀️ today, before the evening              (08:00 on, when 6+ hours are still left)
    h6   6 hours before       🚨 now                                     ← may break the quiet hours
    h2   2 hours before       🚨🚨 submit now, even if not perfect       ← may break the quiet hours

    one engine per stage   utils/deadlines (scripts/deadline_watch, with its live Moodle re-check) already says d3 / d1
              / h6 for every item it knows (Moodle, lecturer sites, a deadline he stated): for those the ladder only adds
              d7, dm and h2 and records the rest as said. Exam registrations and estimated closes get the rest here.
    stops     the moment the item is closed: Moodle shows it submitted, ✅ הגשתי, "הגשתי את …" (agent/obligations).
    budget    ≤ DAY_CAP of d7/d3/d1/dm a day, one message a tick (several items due → one list); h6 / h2 always.
              Outside quiet hours it runs in the worker's batch (agent/catchup holds a d7 while he's away); inside
              them only h6 / h2 (urgent_outs). An item first seen late gets only its most urgent stage.
    away      an item due while he's away (agent/away) says when to finish it by instead.

Also here: a deadline he states ("תוסיף הגשה משימה ב׳ בתוכנה 1 ל-6/12") becomes a tracked obligation at once
(data/assignments/manual.json, read by utils/deadlines → agent/obligations → his calendar) — no card, no "תרצה שאוסיף?".
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'deadline_ladder.json'
PENDING = ROOT / 'data' / 'stated_pending.json'
STAGES = ['d7', 'd3', 'd1', 'dm', 'h6', 'h2']            # least → most urgent
ENGINE = {'d3', 'd1', 'h6'}                              # utils/deadlines says these for the items it knows
URGENT = {'h6', 'h2'}
DAY_CAP = 3
MORNING = 8
REFRESH_EVERY = timedelta(minutes=10)
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
_last_refresh: dict = {'at': None}


# ── the ladder (pure) ───────────────────────────────────────────────────────

def stage_now(due: datetime, now: datetime, est: bool = False) -> str | None:
    """The most urgent stage that applies now, or None (more than 7 days left, or already past)."""
    left = (due - now).total_seconds() / 3600
    if left <= 0:
        return None
    if left <= 2 and not est:
        return 'h2'
    if left <= 6 and not est:
        return 'h6'
    if due.date() == now.date() and now.hour >= MORNING and not est:
        return 'dm'
    if left <= 26:
        return 'd1'
    if left <= 72:
        return 'd3'
    if left <= 168:
        return 'd7'
    return None


def _when(due: datetime, now: datetime) -> str:
    d = (due.date() - now.date()).days
    day = 'היום' if d == 0 else 'מחר' if d == 1 else f'ביום {WEEKDAY_HE[due.weekday()]} {due:%d/%m}'
    return f'{day} ב-{due:%H:%M}'


def text_for(stage: str, rec: dict, now: datetime, away_note: str = '') -> str:
    from agent import obligations as ob
    due = ob._dt(rec['due'])
    what = f"{ob._course_name(rec.get('course', ''))} — *{rec.get('title', '')}*"
    verb = 'נסגר' if rec.get('kind') == 'quiz' else 'עד' if rec.get('kind') == 'exam_reg' else 'הגשה'
    est = ' (בערך)' if rec.get('due_est') else ''
    hrs = max(1, round((due - now).total_seconds() / 3600))
    line = {
        'd7': f"🗓️ בעוד שבוע: {what} — {verb} {_when(due, now)}{est}. שווה להתחיל כבר השבוע.",
        'd3': f"📌 עוד 3 ימים: {what} — {verb} {_when(due, now)}{est}. עוד לא ראיתי שסיימת — תשריין לזה זמן מחר.",
        'd1': f"⏰ {what} — {verb} {_when(due, now)}{est}. עוד לא הוגש. הערב זה הזמן לסיים.",
        'dm': f"☀️ היום: {what} — {verb} ב-{due:%H:%M}. עוד לא הוגש. תסגור את זה לפני הערב.",
        'h6': f"🚨 עוד {hrs} שעות ({due:%H:%M}): {what}. עוד לא הוגש — עכשיו.",
        'h2': f"🚨🚨 שעתיים אחרונות: {what} נסגר ב-{due:%H:%M}. תגיש עכשיו, גם אם זה לא מושלם.",
    }[stage]
    url = rec.get('url') or ''
    return line + (f"\n{away_note}" if away_note else '') + (f"\n👉 {url}" if url else '')


def plan(store: dict, state: dict, now: datetime, engine_ids: set[str], urgent_only: bool = False) -> list[dict]:
    """[{oid, stage, send, rec}] — every item whose stage moved; send=False = said by the other engine / superseded.
    Pure. state = {oid: {'due': iso, 'sent': [stages]}}."""
    from agent import obligations as ob
    out = []
    for r in ob.open_items(store, now):
        due = ob._dt(r.get('due'))
        if not due or ob.status(r, now) != 'open' or r.get('kind') == 'exam':
            continue
        st = state.get(r['id']) or {}
        sent = list(st.get('sent') or []) if st.get('due') == r['due'] else []
        stage = stage_now(due, now, bool(r.get('due_est')))
        if not stage or stage in sent:
            continue
        send = True
        if r['id'] in engine_ids and stage in ENGINE:
            send = False                                  # utils/deadlines says this one
        elif stage == 'd7' and ob.is_priority(r):
            send = False                                  # its own every-2-days reminder already runs
        elif urgent_only and stage not in URGENT:
            continue                                      # quiet hours: wait for the morning
        out.append({'oid': r['id'], 'stage': stage, 'send': send, 'rec': r})
    return out


def mark(state: dict, oid: str, due: str, stage: str) -> None:
    st = state.get(oid) or {}
    sent = list(st.get('sent') or []) if st.get('due') == due else []
    upto = STAGES.index(stage)
    state[oid] = {'due': due, 'sent': sorted(set(sent) | set(STAGES[:upto + 1]), key=STAGES.index)}


# ── live ────────────────────────────────────────────────────────────────────

def _load(path: Path | None) -> dict:
    try:
        d = json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    d.setdefault('items', {})
    d.setdefault('days', {})
    return d


def _save(d: dict, path: Path | None) -> None:
    p = path or STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def _engine_ids() -> set[str]:
    try:
        from utils import deadlines as dl
        return {dl.key_of(a) for a in dl.load_items()}
    except Exception:
        return set()


def _buttons(rec: dict, kind: str, now: datetime) -> list[tuple[str, str]]:
    """'sec:odone:<offer>' — the obligation buttons agent/proactive already answers (✅ closes it everywhere)."""
    from agent import obligations as ob, proactive
    st = proactive._load()
    btns = proactive._buttons({'kind': kind, 'offer': ob._offer(rec),
                               'opts': [('✅ הגשתי' if rec.get('kind') != 'quiz' else '✅ עשיתי', 'odone'),
                                        ('🔕 לא רלוונטי', 'oirr')]}, st, now)
    proactive._save(st, None, now)
    return [b for b in btns if not b[1].startswith('sec:mute:')]


def _store(now: datetime, chat: str | None) -> dict:
    """The obligations store, refreshed at most every REFRESH_EVERY (the refresh also keeps his calendar in line)."""
    from agent import obligations as ob
    last = _last_refresh.get('at')
    if last is None or now - last >= REFRESH_EVERY or now < last:
        _last_refresh['at'] = now
        return ob.refresh(now, chat)
    return ob.load()


def tick(now: datetime | None = None, urgent_only: bool = False, store: dict | None = None,
         path: Path | None = None, chat: str | None = None, engine_ids: set[str] | None = None,
         buttons=None) -> list[tuple[str, object]]:
    """→ [(chat, Out)] — at most one message (the items whose stage came, most urgent first). Never raises."""
    try:
        from config.features import study_enabled
        if not study_enabled():                           # release: the obligations it pushes are the student domain's
            return []
        from agent.core import Out
        now = now or datetime.now()
        if chat is None:
            from whatsapp import outbox
            chat = outbox.owner_chat()
        if not chat:
            return []
        store = store if store is not None else _store(now, chat)
        d = _load(path)
        todo = plan(store, d['items'], now, _engine_ids() if engine_ids is None else engine_ids, urgent_only)
        if not todo:
            return []
        day = now.date().isoformat()
        speak = [x for x in todo if x['send']]
        calm = [x for x in speak if x['stage'] not in URGENT]
        if calm and d['days'].get(day, 0) >= DAY_CAP:     # today's budget is spent: they wait (urgent ones never)
            speak = [x for x in speak if x['stage'] in URGENT]
            todo = [x for x in todo if not x['send'] or x['stage'] in URGENT]
        for x in todo:
            mark(d['items'], x['oid'], x['rec']['due'], x['stage'])
        if speak:
            speak.sort(key=lambda x: (-STAGES.index(x['stage']), x['rec']['due']))
            if any(x['stage'] not in URGENT for x in speak):
                d['days'][day] = d['days'].get(day, 0) + 1
        d['days'] = {k: v for k, v in d['days'].items() if k >= (now.date() - timedelta(days=14)).isoformat()}
        _save(d, path)
        if not speak:
            return []
        notes = _away_notes(store, now)
        texts = [text_for(x['stage'], x['rec'], now, notes.get(x['oid'], '')) for x in speak[:5]]
        body = texts[0] if len(texts) == 1 else '\n\n'.join(texts) + '\n\n(הכפתורים על הראשון.)'
        top = speak[0]['rec']
        btns = (buttons or _buttons)(top, 'oblig_remind', now)
        print(f"  ⏳ ladder: {', '.join(x['oid'] + '/' + x['stage'] for x in speak)}")
        return [(chat, Out('buttons', body, buttons=btns) if btns else Out('text', body))]
    except Exception as e:
        print(f'  ⚠️ ladder tick: {e!r}'[:200])
        return []


def urgent_outs(now: datetime | None = None) -> list:
    """Quiet hours: only the last 6 hours before a deadline may wake him."""
    return tick(now, urgent_only=True)


def _away_notes(store: dict, now: datetime) -> dict[str, str]:
    try:
        from agent import away
        return away.finish_notes(store, now)
    except Exception as e:
        print(f'  ⚠️ ladder away: {e!r}'[:200])
        return {}


# ── a deadline he states → a tracked obligation ─────────────────────────────

MANUAL_KIND = r'(?P<kind>הגשה|הגשת|הגשות|דדליין|מטלה|מטלת|תרגיל(?:\s+בית)?|משימה|משימת|עבודה|עבודת|ממ"ן|ממ״ן|פרויקט|בוחן)'
ADD_RX = re.compile(r'^\s*(?:ו?(?:תוסיף|תוסיפי|תכניס|תכניסי|תרשום|תרשמי|רשום|תשים|תשימי|תקבע|תקבעי))\s+'
                    r'(?:לי\s+)?(?:(?:ליומן|לרשימה|למטלות|ביומן)\s+)?(?:את\s+)?(?:ה)?' + MANUAL_KIND + r'(?![א-ת])(?P<rest>.*)$', re.S)
TIME_RX = re.compile(r'(?:בשעה|עד השעה|עד|ב-?)\s*(\d{1,2})[:.](\d{2})')
CUT_RX = re.compile(r'(?:\s|^)(?:ב|ל|עד|מ)-?\s*\d|\s(?:ב|ל|עד)?(?:יום|מחר|היום|ראשון|שני|שלישי|רביעי|חמישי|שישי)(?![א-ת])'
                    r'|\s(?:תזכיר|תזכירי|ותזכיר|תוודא|ותוודא|תדאג|שבוע לפני|ליומן|בבקשה)|[,.\n]')
WORK_RX = re.compile(r'(?:ב|ל|של\s+ה?)?קורס\s+(?:מה|של\s+ה|ב)?עבודה|(?:ב|ל)?עבודה(?![א-ת])')
STUDY_RX = re.compile(r'משימה|משימת|תרגיל|מטלה|מטלת|ממ["״]ן|בוחן|assignment|homework|במודל', re.I)
DONE_RX = re.compile(r'^\s*(?:כבר\s+)?(?:הגשתי|סיימתי\s+את|שלחתי\s+את)\s+(?:את\s+)?(?P<what>.{2,60}?)\s*[.!]*\s*$')


def _manual_path() -> Path:
    from utils import deadlines as dl
    return dl.manual_path()


def _course_of(text: str) -> str:
    """An active course named in the words, 'קורס עבודה' for the work course, '' when neither."""
    try:
        from agent import factcheck
        ck = factcheck._ck_in(text, factcheck._course_words())
        if ck:
            return ck
    except Exception:
        pass
    return 'קורס עבודה' if WORK_RX.search(text or '') else ''


def _title_of(kind: str, rest: str, course: str) -> str:
    part = CUT_RX.split(' ' + rest, maxsplit=1)[0]
    try:                                                  # the course's own words out of the title
        from agent import factcheck
        for w in sorted(factcheck._course_words().get(course, set()), key=len, reverse=True):
            part = re.sub(r'(?<![א-ת])[בלה]?' + re.escape(w) + r'(?:\s+(?:1|2|א|ב|אחד|שתיים))?', ' ', part)
    except Exception:
        pass
    part = WORK_RX.sub(' ', part)
    part = re.sub(r'(?<=[א-ת])["״]', '׳', part)          # "ב״" (one letter) → "ב׳"
    part = re.sub(r'\s+', ' ', part).strip(' -—:')
    if kind in ('הגשה', 'הגשת', 'הגשות', 'דדליין'):
        return part or 'הגשה'
    k = {'מטלת': 'מטלה', 'משימת': 'משימה', 'עבודת': 'עבודה', 'ממ״ן': 'ממ"ן'}.get(kind, kind)
    return f'{k} {part}'.strip()


def parse_add(text: str, now: datetime) -> dict | None:
    """"תוסיף הגשה משימה ב׳ בתוכנה 1 ב-6 לדצמבר" → {title, course, due, time_said}; None when it isn't one of these.
    A missing course or date comes back empty (the caller asks)."""
    m = ADD_RX.match(text or '')
    if not m or len(text) > 220:
        return None
    from agent.daytools import parse_day
    rest = m.group('rest') or ''
    course = _course_of(text)
    if not course and (m.group('kind') in ('משימה', 'משימת', 'עבודה', 'עבודת', 'פרויקט')
                       or not STUDY_RX.search(f"{m.group('kind')} {rest}")):
        return None                  # "תוסיף משימה לקנות חלב" / "הגשת דוח לביטוח לאומי": a task / the calendar, as before
    day = parse_day(rest, now.date())
    tm = TIME_RX.search(rest)
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm and int(tm.group(1)) < 24 else (23, 59)
    due = datetime(day.year, day.month, day.day, hh, mm).isoformat(timespec='minutes') if day else ''
    return {'title': _title_of(m.group('kind'), rest, course)[:80], 'course': course, 'due': due,
            'time_said': bool(tm), 'text': text[:200]}


def add_stated(chat: str, p: dict, now: datetime | None = None, path: Path | None = None) -> tuple[str, dict]:
    """Into data/assignments/manual.json → ('added'|'updated'|'exists', item). Same course + title → its date moves."""
    from utils import deadlines as dl
    now = now or datetime.now()
    pth = path or _manual_path()
    try:
        rows = json.loads(pth.read_text(encoding='utf-8'))
        rows = rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        rows = []
    norm = lambda s: re.sub(r'[\s"״׳\'\-]+', '', s or '')
    for a in rows:
        if a.get('course_key') == p['course'] and norm(a.get('title')) == norm(p['title']) and not a.get('removed'):
            if a.get('due_date') == p['due']:
                return 'exists', a
            a['due_date'], a['updated'] = p['due'], now.isoformat(timespec='minutes')
            _write(pth, rows)
            return 'updated', a
    for a in dl.load_items():                             # a real source already has this course on that day
        if a.get('course_key') == p['course'] and str(a.get('due_date') or '')[:10] == p['due'][:10] \
                and a.get('source') != 'manual' and a.get('kind') != 'quiz':
            return 'exists', a
    a = {'assign_id': 'manual-' + secrets.token_hex(3), 'course_key': p['course'], 'title': p['title'],
         'due_date': p['due'], 'kind': 'assign', 'source': 'manual', 'url': '', 'submitted': False,
         'stated': p.get('text', ''), 'chat': chat, 'created': now.isoformat(timespec='minutes')}
    rows.append(a)
    _write(pth, rows)
    return 'added', a


def _write(p: Path, rows: list) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def _pending(chat: str, value: dict | None = None, clear: bool = False, path: Path | None = None) -> dict | None:
    p = path or PENDING
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    if value is None and not clear:
        v = d.get(chat)
        if v and datetime.now() - datetime.fromisoformat(v['ts']) < timedelta(minutes=20):
            return v
        return None
    if clear:
        d.pop(chat, None)
    else:
        d[chat] = {**value, 'ts': datetime.now().isoformat(timespec='seconds')}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    return None


def _confirm(how: str, a: dict, now: datetime, chat: str) -> str:
    from agent import obligations as ob, deadline_cal
    from utils import deadlines as dl
    due = dl._due(a)
    name = f"{ob._course_name(a.get('course_key', ''))} — {a.get('title', '')}"
    when = f"יום {WEEKDAY_HE[due.weekday()]} {due:%d/%m} {due:%H:%M}"
    if how == 'exists' and a.get('source') != 'manual':
        head = f"👍 כבר יש לי את זה: *{name}* — {when}."
    elif how == 'exists':
        head = f"👍 כבר רשום: *{name}* — {when}."
    elif how == 'updated':
        head = f"📅 עדכנתי את המועד: *{name}* — {when}."
    else:
        head = f"✅ רשמתי: *{name}* — הגשה {when}" + ('' if a.get('_time_said') else ' (לא אמרת שעה — שמתי 23:59)') + '.'
    cal = ''
    try:
        store = ob.refresh(now, chat)                      # → his calendar (agent/deadline_cal) right now
        if deadline_cal.synced(dl.key_of(a)):
            cal = '\n📅 נמצא ביומן שלך.'
        elif deadline_cal.enabled():
            cal = '\n📅 ייכנס ליומן בסנכרון הקרוב.'
        _ = store
    except Exception as e:
        print(f'  ⚠️ stated → calendar: {e!r}'[:200])
    left = (due - now).total_seconds() / 3600
    stages = [s for s, h in (('שבוע לפני', 168), ('3 ימים לפני', 72), ('יום לפני', 26)) if left > h] \
        + ['בבוקר של היום', '6 ושעתיים לפני']
    return head + cal + f"\n⏳ אזכיר {', '.join(stages)} — עד שתגיד \"הגשתי\"."


def route(text: str, chat: str, now: datetime | None = None) -> str | None:
    """"תוסיף הגשה…" / the answer to its one question / "הגשתי את משימה ב׳" (a stated one). → the reply, or None."""
    now = now or datetime.now()
    t = (text or '').strip()
    if not t:
        return None
    p = parse_add(t, now)
    if p is None:
        pend = _pending(chat)
        if pend and len(t) <= 40 and not ADD_RX.match(t) and '?' not in t \
                and not re.match(r'\s*(?:מה|מתי|איפה|איך|למה|האם|כמה|מי)(?![א-ת])', t):
            p = dict(pend)
            p.pop('ts', None)
            if not p['course']:
                p['course'] = _course_of(t)
            if not p['due']:
                from agent.daytools import parse_day
                day = parse_day(t, now.date())
                tm = TIME_RX.search(t)
                if day:
                    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (23, 59)
                    p['due'] = datetime(day.year, day.month, day.day, hh, mm).isoformat(timespec='minutes')
                    p['time_said'] = bool(tm)
            if not (p['course'] and p['due']):
                _pending(chat, clear=True)
                return None                                # not the answer: the message goes on as usual
        else:
            return _done(t, chat, now)
    if not p['course'] or not p['due']:
        _pending(chat, p)
        if not p['course']:
            try:
                from config.paths import ACTIVE_COURSES, COURSE_NAMES
                names = ' / '.join(COURSE_NAMES.get(c, c) for c in ACTIVE_COURSES)
            except Exception:
                names = ''
            return f"📝 באיזה קורס {p['title']}?" + (f" ({names}, או \"קורס עבודה\")" if names else '')
        return f"📝 עד מתי {p['title']}? (למשל: 6/12 23:59)"
    _pending(chat, clear=True)
    if p['due'] < now.isoformat(timespec='minutes'):
        return f"🤔 {p['due'][8:10]}/{p['due'][5:7]} כבר עבר. לאיזה תאריך?"
    how, a = add_stated(chat, p, now)
    a = {**a, '_time_said': p.get('time_said')}
    return _confirm(how, a, now, chat)


def _done(t: str, chat: str, now: datetime) -> str | None:
    """"הגשתי את משימה ב׳" — closes a stated / open obligation when exactly one fits the words."""
    m = DONE_RX.match(t)
    if not m:
        return None
    from agent import obligations as ob, closure
    store = ob.load()
    what = m.group('what')
    ck = _course_of(what)
    nums = ob._nums(what)
    hits = []
    for r in ob.open_items(store, now):
        if ck and r.get('course') != ck:
            continue
        if nums and not (nums & ob._nums(r.get('title', ''))):
            continue
        if closure.same(ob.alias(r), what) or closure.same(r.get('title', ''), what):
            hits.append(r)
    if len(hits) != 1:
        return None
    r = ob.close(chat, hits[0]['id'], 'done', now=now)
    if not r:
        return None
    try:
        ob.refresh(now, chat)                              # → "✅ הוגש" in his calendar now
    except Exception:
        pass
    return f"✅ מעולה — סימנתי ש*{r.get('title', '')}* ({ob._course_name(r.get('course', ''))}) הוגש. לא אזכיר יותר."
