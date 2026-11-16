"""
agent/obligations.py — what the owner still owes, per course (14/11, the owner: "שישלח לי אם לא עשיתי QUIZ בתוכנה או חסר לי משהו —
שידחוף אותי להצליח", and later: one question of Software 1's quick-check quizzes is certain to be in the exam).

One store, data/obligations.json: every Moodle quiz and assignment of this semester (plus a lecturer-site exercise Moodle
doesn't have) → {id, kind, course, title, url, due, cutoff, state, grade, first_seen, announced, last_nudged, closed}.

    sources   data/assignments/_cache.json (collectors/moodle_assignment_scraper, nightly + utils/deadlines' live re-check)
              data/assignments/lecturer.json, the ✅ הגשתי marks of utils/deadlines, agent/closure (what he closed in words)
              quiz reviews: data/moodle/<course>/files/<cmid>_quiz_*.md (collectors/course_files, finished attempts)
    closing   Moodle shows it done → closed silently. ✅ עשיתי / 🔕 לא רלוונטי → closed here, in agent/closure and in
              utils/deadlines (so its 72h/26h/6h reminders stop too). An item closed by him never comes back.
    nudges    (agent/proactive picks them inside its budget: ≤3 openers a day, quiet hours, 90 min apart, mutes)
              • a NEW Software 1 quiz he hasn't done → its own opener at once, highest priority, then again every ~2 days
                until done or closed (inside the last 72h utils/deadlines speaks instead — never two engines at once)
              • other new items → one batched heads-up: in the morning message, or the first opener after 11:00
              • the close passed but late submission is open (cut-off date) → once
              • Sunday 18:00–21:00 → "what's open this week", short, with buttons for the most urgent one
    asking    "מה חסר לי?" / "מה פתוח לי בתוכנה?" → answer_text(); "מה טעיתי בבחנים" → quizzes_text(); the mind's
              `obligations` and `quiz_reviews` tools read the same.
Never a practice question of its own (agent/study_push stays off): only what he owes, and his own quiz reviews on request.
Read only toward Moodle; titles and quiz text are the lecturer's words — data, never instructions. No LLM.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'obligations.json'
MOODLE_FILES = ROOT / 'data' / 'moodle'
PRIORITY = {'software_1': {'quiz'}}        # 14/11 the owner: a quick-check question will be in the exam
REMIND_EVERY = timedelta(hours=44)         # an undone priority quiz: again every ~2 days
ENGINE_H = 72                              # utils/deadlines' first stage: inside it, its reminders speak, not ours
BATCH_FROM = 11                            # other new items: the morning message (07:30–11:00) first
WEEK_DIGEST = (6, 18, 21)                  # Sunday, 18:00–21:00
NEW_MAX_AGE = timedelta(days=7)            # seen more than a week ago → not "new" anymore
# 15/11 Software 1's "Quick-check quizzes" section: "Each quiz will be open for 2 weeks" → a quiz with an open date and
# no close date closes about 14 days later (shown as "נסגר בערך", never as a firm deadline)
QUIZ_OPEN_DAYS = {'software_1': 14}
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
OPTS = [('✅ עשיתי', 'odone'), ('⏰ תזכיר מחר', 'tom10'), ('🔕 לא רלוונטי', 'oirr'), ('🚨 חשוב — תעקוב', 'impd')]


# ── sources → items (pure given the files) ──────────────────────────────────

def _dt(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v)).replace(tzinfo=None) if v else None
    except ValueError:
        return None


def _course_name(ck: str) -> str:
    try:
        from config.paths import COURSE_NAMES
        return COURSE_NAMES.get(ck, ck)
    except Exception:
        return ck


def _active() -> list[str]:
    try:
        from config.paths import ACTIVE_COURSES
        return list(ACTIVE_COURSES)
    except Exception:
        return []


SOLUTION_RX = re.compile(r'פתרון|solution', re.I)


def _for_me(a: dict) -> bool:
    """15/11 "הודעות שלא רלוונטיות אליי אל תשלח": an item whose title / section names another group, day or tutor
    ("הגשה — קבוצה 16", "Quiz — Wednesday recitation") isn't his to owe. Only the rule (integrations/univ_relevance)."""
    try:
        from integrations import univ_relevance
        return univ_relevance.for_me(a.get('course_key'), f"{a.get('title') or ''} {a.get('section') or ''}")
    except Exception:
        return True


def collect(cache: Path | None = None, lecturer: Path | None = None, dl_state: dict | None = None,
            courses: list[str] | None = None) -> list[dict]:
    """Every quiz / assignment of this semester's courses, with its state now. A PDF exercise ('resource') only when it
    has a due date (Micro B's 12 homework PDFs have none and no submission: nothing to owe)."""
    from utils import deadlines as dl
    try:
        raw = json.loads((cache or dl.CACHE).read_text(encoding='utf-8')).get('assignments') or []
    except (OSError, ValueError, AttributeError):
        raw = []
    raw = [a for a in raw if isinstance(a, dict)]
    site = [a for a in dl.load_items(cache, lecturer) if a.get('source') == 'lecturer_site']
    st = dl.load_state() if dl_state is None else dl_state
    courses = _active() if courses is None else courses
    out, seen = [], set()
    for a in raw + site:
        kind = a.get('kind') or 'assign'
        if courses and a.get('course_key') not in courses:
            continue
        if kind not in ('assign', 'quiz') and not (kind == 'resource' and a.get('due_date')):
            continue
        if SOLUTION_RX.search(a.get('title') or ''):
            continue
        if not _for_me(a):                                  # 15/11 another group's submission box / quiz
            continue
        oid = dl.key_of(a)
        if oid in seen:
            continue
        seen.add(oid)
        qs = a.get('quiz_state')
        # done = the finished attempt / the submission (Moodle's "Done" completion badge is not the test: 15/11 his
        # Lecture 4 attempt was finished while the badge still offered "Mark as done")
        state = 'done' if dl.is_done(a, st) else 'in_progress' if qs == 'in_progress' else 'open'
        due, est = a.get('due_date') or '', False
        if not due and kind == 'quiz' and a.get('start_date') and a.get('course_key') in QUIZ_OPEN_DAYS:
            o = _dt(a['start_date'])
            if o:
                due, est = (o + timedelta(days=QUIZ_OPEN_DAYS[a['course_key']])).isoformat(timespec='minutes'), True
        out.append({'id': oid, 'kind': kind, 'course': a.get('course_key', ''), 'title': (a.get('title') or '').strip(),
                    'url': a.get('url') or '', 'due': due, 'cutoff': a.get('cutoff_date') or '',
                    'opens': a.get('start_date') or '', 'state': state, 'grade': a.get('grade') or '',
                    'manual': bool((st.get(oid) or {}).get('manual_done')) and not a.get('submitted'),
                    'due_est': est, 'unmarked': state == 'done' and a.get('completion') == 0})
    return out


# ── the store ───────────────────────────────────────────────────────────────

def load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STORE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d.get('items'), dict):
        d['items'] = {}
    return d


def save(d: dict, path: Path | None = None) -> None:
    p = path or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def _nums(t: str) -> set[str]:
    return set(re.findall(r'\d+', t or ''))


def alias(rec: dict) -> str:
    """The words the owner would use for it: "Lecture 5 Quiz" (Software 1) → "תוכנה 1 בוחן קוויז הרצאה 5 Lecture 5 Quiz"."""
    title = rec.get('title', '')
    heb = 'בוחן קוויז בחן' if rec.get('kind') == 'quiz' else 'מטלה תרגיל שיעורי בית'
    n = sorted(_nums(title))
    return f"{_course_name(rec.get('course', ''))} {heb} {'הרצאה ' + n[0] if n and rec.get('kind') == 'quiz' else ''} {title}"


def closed_in_words(rec: dict, closed: list[dict]) -> dict | None:
    """He said it's done / irrelevant ("עשיתי את הבוחן של הרצאה 5") after it appeared. A numbered item needs its number."""
    from agent import closure
    want = _nums(rec.get('title', ''))
    for c in closed or []:
        if c.get('origin') == 'obligation' or (c.get('ts') or '') < (rec.get('first_seen') or '')[:16]:
            continue
        text = c.get('text', '')
        if want and not (want & closure._nums(text)):
            continue
        if closure.same(alias(rec), text):
            return c
    return None


def sync(items: list[dict], store: dict, now: datetime, closed: list[dict] | None = None) -> bool:
    """Merge what the sources say now into the store. → changed. The first sync ever announces nothing (no burst of
    "new" on deploy); an item already done or already past when first seen is never new either."""
    first = not store.get('init')
    have = store['items']
    stamp = now.isoformat(timespec='minutes')
    changed = first
    live = set()
    for it in items:
        live.add(it['id'])
        rec = have.get(it['id'])
        if rec is None:
            due = _dt(it['due'])
            rec = {**it, 'first_seen': stamp, 'announced': first or it['state'] == 'done' or bool(due and due <= now),
                   'last_nudged': '', 'closed': '', 'closed_by': ''}
            have[it['id']] = rec
            changed = True
        else:
            if it['grade'] and not rec.get('grade') and it['kind'] != 'quiz':     # a grade was published
                store.setdefault('news', []).append({'kind': 'grade', 'key': f"{it['course']}:{hw_num(it['title'])}",
                                                     'id': it['id'], 'grade': it['grade'], 'ts': stamp})
            for k in ('kind', 'course', 'title', 'url', 'due', 'cutoff', 'opens', 'state', 'grade', 'manual',
                      'due_est', 'unmarked'):
                if k in it and rec.get(k) != it[k]:
                    rec[k] = it[k]
                    changed = True
            if rec.pop('gone', None):
                changed = True
        if it['state'] == 'done' and not rec.get('closed'):
            rec.update(closed=stamp, closed_by='moodle' if not it.get('manual') else 'owner')
            changed = True
        elif it['state'] != 'done' and rec.get('closed_by') == 'moodle':
            rec.update(closed='', closed_by='')          # Moodle took it back (a removed submission): open again
            changed = True
        if not rec.get('closed') and (c := closed_in_words(rec, closed or [])):
            rec.update(closed=stamp, closed_by='words', closed_why=c.get('why', 'done'))
            changed = True
    for oid, rec in have.items():
        if oid not in live and not rec.get('gone'):
            rec['gone'] = True                           # left Moodle (or the course ended) — kept, never shown
            changed = True
    if first:
        store['init'] = stamp
    return changed


def track_coursework(store: dict, now: datetime, catalog: dict[str, dict]) -> bool:
    """A solution / feedback file that wasn't there before → a news item (the first look records what exists, silently).
    catalog = {course: coursework(course)}."""
    stamp = now.isoformat(timespec='minutes')
    sols = {f'{ck}:{n}' for ck, cw in catalog.items() for n, e in cw.items() if e['solution']}
    fbs = {f'{ck}:{n}' for ck, cw in catalog.items() for n, e in cw.items() if e['feedback']}
    shs = {f'{ck}:{n}' for ck, cw in catalog.items() for n, e in cw.items() if e['sheet']}
    if not store.get('cw_init'):
        store.update(cw_init=stamp, solutions=sorted(sols), feedbacks=sorted(fbs), sheets=sorted(shs))
        return True
    news = store.setdefault('news', [])
    new_s, new_f = sols - set(store.get('solutions') or []), fbs - set(store.get('feedbacks') or [])
    owed = {f"{r['course']}:{hw_num(r['title'])}" for r in store['items'].values()}   # those come as 🆕 obligations
    new_h = shs - set(store.get('sheets') or []) - owed
    news += [{'kind': 'sheet', 'key': k, 'ts': stamp} for k in sorted(new_h)]
    news += [{'kind': 'solution', 'key': k, 'ts': stamp} for k in sorted(new_s)]
    news += [{'kind': 'feedback', 'key': k, 'ts': stamp} for k in sorted(new_f)]
    store['solutions'] = sorted(sols | set(store.get('solutions') or []))
    store['feedbacks'] = sorted(fbs | set(store.get('feedbacks') or []))
    store['sheets'] = sorted(shs | set(store.get('sheets') or []))
    cut = (now - timedelta(days=14)).isoformat()
    store['news'] = [n for n in news if n.get('ts', '') >= cut]
    return bool(new_s or new_f or new_h)


# ── exams (15/11): only what the university's own calendar (my.tau, integrations/tau_portal's cache) says ──
# On 15/11 the cache covers 15/11–14/12 and holds no exam and no registration window: nothing is added until it does.
# Moodle has no exam dates for these courses. Nothing here is guessed: a date/time is the university's; "מועד א/ב" is
# its own words when present, else (two exams of one course) the earlier is א — marked moed_inferred.

MOED_RX = re.compile(r"מועד\s*['׳]?\s*([אבג])")
REG_RX = re.compile(r'הרשמה|רישום')
REG_FOR_RX = re.compile(r'בחינ|מבחן|מועד')


def _course_codes() -> dict[str, str]:
    """course number (my.tau) → course key, from config/courses.yaml."""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / 'config' / 'courses.yaml').read_text(encoding='utf-8'))
        cs = cfg.get('courses', cfg)
        return {str(v.get('course_code')).strip().zfill(8): k for k, v in cs.items()
                if isinstance(v, dict) and v.get('course_code')}
    except Exception:
        return {}


def exams_from(events: list[dict], codes: dict[str, str], today: str) -> tuple[dict[str, list[dict]], list[dict]]:
    """(exams per course key, registration-window obligation items) from tau_portal events. Pure."""
    per: dict[str, list[dict]] = {}
    regs = []
    for e in events or []:
        ck = codes.get(str(e.get('course_number') or '').zfill(8))
        text = f"{e.get('course', '')} {e.get('kind', '')}"
        if not ck or not e.get('date'):
            continue
        if REG_RX.search(text) and REG_FOR_RX.search(text):
            m = MOED_RX.search(text)
            regs.append({'id': f"{ck}:examreg:{e['date']}", 'kind': 'exam_reg', 'course': ck,
                         'title': 'הרשמה לבחינה' + (f" — מועד {m.group(1)}'" if m else ''), 'url': 'https://my.tau.ac.il',
                         'due': f"{e['date']}T{e.get('end_time') or e.get('time') or '23:59'}", 'cutoff': '',
                         'opens': '', 'state': 'open', 'grade': '', 'manual': False})
        elif e.get('is_exam') and e['date'] >= today:
            m = MOED_RX.search(text)
            per.setdefault(ck, []).append({'date': e['date'], 'time': e.get('time') or '', 'moed': m.group(1) if m else '',
                                           'moed_inferred': False, 'room': e.get('room') or '',
                                           'building': e.get('building') or ''})
    for ck, xs in per.items():
        xs.sort(key=lambda x: (x['date'], x['time']))
        if len(xs) == 2 and not any(x['moed'] for x in xs):
            xs[0].update(moed='א', moed_inferred=True)
            xs[1].update(moed='ב', moed_inferred=True)
    return per, regs


def _tau_events() -> list[dict]:
    try:
        from integrations import tau_portal
        return (tau_portal._cached() or {}).get('events') or []
    except Exception:
        return []


def exam_line(x: dict) -> str:
    moed = f"מועד {x['moed']}" if x.get('moed') else 'מבחן'
    where = ' · '.join(v for v in (x.get('building'), x.get('room')) if v)
    return f"🎯 {moed}: {x['date'][8:10]}/{x['date'][5:7]}" + (f" {x['time']}" if x.get('time') else '') \
        + (f" ({where})" if where else '')


def refresh(now: datetime | None = None, chat: str | None = None, path: Path | None = None,
            items: list[dict] | None = None, write: bool = True, catalog: dict | None = None) -> dict:
    """Read the sources, merge, save (when something changed). Never raises: a broken source = the last store."""
    now = now or datetime.now()
    store = load(path)
    try:
        if items is None:
            items = collect()
            try:
                per, regs = exams_from(_tau_events(), _course_codes(), now.date().isoformat())
                items = items + [r for r in regs if not _active() or r['course'] in _active()]
                if per != (store.get('exams') or {}):
                    store['exams'] = per
                    if write:
                        save(store, path)
            except Exception as e:
                print(f'  ⚠️ obligations exams: {e!r}'[:200])
        closed = []
        try:
            from agent import closure
            closed = closure.items(chat)
        except Exception:
            pass
        changed = sync(items, store, now, closed)
        catalog = {ck: coursework(ck) for ck in _active()} if catalog is None else catalog
        changed = track_coursework(store, now, catalog) or changed
        if changed and write:
            save(store, path)
    except Exception as e:
        print(f'  ⚠️ obligations: {e!r}'[:200])
    return store


# ── reading the store ───────────────────────────────────────────────────────

def is_priority(rec: dict) -> bool:
    return rec.get('kind') in PRIORITY.get(rec.get('course', ''), set())


def status(rec: dict, now: datetime) -> str:
    """'closed' | 'open' | 'late' (the close passed, the cut-off didn't) | 'missed' (both passed) | 'gone'."""
    if rec.get('gone'):
        return 'gone'
    if rec.get('closed') or rec.get('state') == 'done':
        return 'closed'
    due, cut = _dt(rec.get('due')), _dt(rec.get('cutoff'))
    if due and due <= now:
        return 'late' if cut and cut > now else 'missed'
    return 'open'


def open_items(store: dict, now: datetime, course: str | None = None) -> list[dict]:
    """Still owed (open or late), most urgent first: a dated one by its close, then undated, priority quizzes first."""
    out = [r for r in store['items'].values() if status(r, now) in ('open', 'late') and (not course or r['course'] == course)]
    return sorted(out, key=lambda r: (r.get('due') or '9999', not is_priority(r), r.get('title', '')))


def _when(t: datetime, now: datetime) -> str:
    d = (t.date() - now.date()).days
    day = 'היום' if d == 0 else 'מחר' if d == 1 else f'יום {WEEKDAY_HE[t.weekday()]} {t:%d/%m}'
    return f'{day} {t:%H:%M}'


def _kind_he(rec: dict) -> str:
    return {'quiz': 'בוחן', 'exam_reg': 'my.tau'}.get(rec.get('kind'), 'מטלה')


def line(rec: dict, now: datetime, link: bool = True) -> str:
    """• ⭐ Lecture 5 Quiz (בוחן) — נסגר יום ב' 23/11 23:59 · באמצע 👉 <url>"""
    due, cut = _dt(rec.get('due')), _dt(rec.get('cutoff'))
    verb = {'quiz': 'נסגר', 'exam_reg': 'עד'}.get(rec.get('kind'), 'הגשה')
    if status(rec, now) == 'late':
        when = f'עבר המועד — אפשר עוד באיחור עד {_when(cut, now)}'
    elif due and rec.get('due_est'):
        when = f'{verb} בערך ב{_when(due, now)} (פתוח שבועיים)'
    else:
        when = f'{verb} {_when(due, now)}' if due else 'בלי תאריך סגירה'
    extra = ' · התחלת ולא סיימת' if rec.get('state') == 'in_progress' else ''
    star = '⭐ ' if is_priority(rec) else ''
    return f"• {star}{rec.get('title', '')} ({_kind_he(rec)}) — {when}{extra}" + (f"\n  👉 {rec['url']}" if link and rec.get('url') else '')


# ── quiz reviews (his finished attempts, saved by collectors/course_files) ──

QSTATE_OK = {'correct', 'נכון', 'נכונה'}


def parse_review(md: str) -> dict:
    """A <cmid>_quiz_<name>.md → {status, questions: [{num, text, chosen, right, state, wrong}]}."""
    head, *blocks = re.split(r'\n## שאלה ', '\n' + (md or ''))
    status_m = re.search(r'^- (?:Status|מצב): (.+)$', head, re.M)
    qs = []
    for b in blocks:
        first, _, body = b.partition('\n')
        num = re.match(r'(\S+)', first)
        st = re.search(r'^מצב: (.+)$', body, re.M)
        state = st.group(1).strip() if st else ''
        chosen = [re.sub(r'\s*[✅❌]\s*$', '', x).strip() for x in re.findall(r'^- \[x\] (.+)$', body, re.M)]
        right = re.search(r'^\*\*(.+?)\*\*\s*$', body, re.M)
        text = re.split(r'\n- \[[ x]\] ', body, maxsplit=1)[0]
        text = re.sub(r'\n{2,}', '\n', text).strip()
        qs.append({'num': num.group(1) if num else '?', 'text': text, 'chosen': chosen,
                   'right': right.group(1).strip() if right else '', 'state': state,
                   'wrong': bool(state) and state.lower() not in QSTATE_OK})
    return {'status': status_m.group(1).strip() if status_m else '', 'questions': qs}


def quiz_reviews(course: str = 'software_1', root: Path | None = None) -> dict[str, dict]:
    """{cmid: {title, file, n, wrong: [question…]}} for every saved review of the course."""
    d = (root or MOODLE_FILES) / course / 'files'
    out = {}
    for p in sorted(d.glob('*_quiz_*.md')) if d.exists() else []:
        cmid, _, name = p.stem.partition('_quiz_')
        try:
            r = parse_review(p.read_text(encoding='utf-8'))
        except OSError:
            continue
        out[cmid] = {'title': name, 'file': str(p), 'n': len(r['questions']), 'status': r['status'],
                     'wrong': [q for q in r['questions'] if q['wrong']]}
    return out


def _quiz_order(t: str) -> tuple:
    n = sorted(int(x) for x in _nums(t))
    return (n[0] if n else 999, t)


def quizzes_text(course: str = 'software_1', wrong_only: bool = False, root: Path | None = None,
                 store: dict | None = None, now: datetime | None = None) -> str:
    """"תראה לי את הבחנים בתוכנה" / "מה טעיתי בבחנים" — his own finished attempts, on his request. Read only."""
    now = now or datetime.now()
    revs = quiz_reviews(course, root)
    name = _course_name(course)
    store = store if store is not None else load()
    todo = [r for r in open_items(store, now, course) if r.get('kind') == 'quiz']
    out = []
    if not revs:
        out.append(f'אין לי עדיין סקירה של בוחן שסיימת ב{name}.')
    else:
        total = sum(r['n'] for r in revs.values())
        bad = sum(len(r['wrong']) for r in revs.values())
        out.append(f'🧪 *הבחנים ב{name}* — {len(revs)} שסיימת, {total} שאלות, '
                   + ('בלי אף טעות ✅' if not bad else f'{bad} טעויות'))
        for cmid, r in sorted(revs.items(), key=lambda kv: _quiz_order(kv[1]['title'])):
            out.append(f"• {r['title']}: {r['n']} שאלות · " + ('הכול נכון ✅' if not r['wrong'] else f"❌ {len(r['wrong'])} טעויות"))
        if wrong_only or bad:
            for cmid, r in sorted(revs.items(), key=lambda kv: _quiz_order(kv[1]['title'])):
                for q in r['wrong']:
                    text = q['text'] if len(q['text']) <= 600 else q['text'][:600] + '…'
                    out.append(f"\n*{r['title']} — שאלה {q['num']}* ({q['state']})\n{text}\n"
                               f"ענית: {', '.join(q['chosen']) or '—'}" + (f"\n{q['right']}" if q['right'] else ''))
        if wrong_only and not bad:
            out.append('\nלא מצאתי שאלה שטעית בה בניסיון האחרון של אף בוחן.')
        out.append('\n_(לפי הניסיון האחרון שסיימת בכל בוחן; המודל לא מראה ציון לבחנים האלה.)_')
    if todo:
        out.append('\nעוד לא עשית:\n' + '\n'.join(line(r, now) for r in todo))
    return '\n'.join(out)


def wrong_counts(course: str = 'software_1', root: Path | None = None) -> dict[str, tuple[int, int]]:
    """{obligation id: (wrong, questions)} — the dashboard's and the answer's per-quiz count."""
    return {f'{course}:{cmid}': (len(r['wrong']), r['n']) for cmid, r in quiz_reviews(course, root).items()}


# ── coursework: every exercise sheet, its solution, his feedback (14/11 the owner: "גם כל שיעורי בית בכלל") ──

HW_RX = re.compile(r'תרגיל|מטלה|מטלות|שיעורי בית|ש"ב|ש״ב|homework|\bhw\s*\d|assignment|(?:^|[_\s])ex_?\d', re.I)
SOL_RX = re.compile(r'פתרון|פתרונות|solution|\bsol\b', re.I)
NOT_HW_RX = re.compile(r'מועד|מבחן|בחינה|exam|quiz|בוחן|^\s*תרגול|סילבוס|syllab|installing|setting up', re.I)
NUM_RX = re.compile(r'(?:תרגיל(?:\s+בית)?|מטלה|שיעורי\s+בית|ש["״]ב|homework|hw|assignment|ex)[\s_#.\-]*0*(\d{1,2})(?!\d)', re.I)
DOC_EXT = ('.pdf', '.md', '.docx', '.pptx', '.zip', '.py', '.txt', '.ipynb')


def hw_num(title: str) -> str:
    m = NUM_RX.search(title or '')
    return m.group(1) if m else ''


def coursework(course: str, root: Path | None = None) -> dict[str, dict]:
    """{exercise number: {'sheet': [paths], 'solution': [paths], 'feedback': [paths], 'titles': [...]}} — from the Moodle
    files (collectors/course_files), the assignment pages (data/assignments/<course>, scripts/download_assignments),
    a lecturer's site (data/moodle/<course>/homework) and the saved feedback. Files only; nothing is fetched here."""
    base = root or ROOT
    files = base / 'data' / 'moodle' / course / 'files'
    out: dict[str, dict] = {}

    def add(num: str, kind: str, path: Path, title: str):
        if not num or not path.exists() or path.suffix.lower() not in DOC_EXT:
            return
        e = out.setdefault(num, {'sheet': [], 'solution': [], 'feedback': [], 'titles': []})
        if str(path) not in e[kind]:
            e[kind].append(str(path))
        if title not in e['titles']:
            e['titles'].append(title)
    try:
        idx = json.loads((files / 'index.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        idx = {}
    for e in idx.values():
        t = e.get('title', '')
        if not e.get('file') or e.get('module') == 'quiz' or NOT_HW_RX.search(t) or not HW_RX.search(t):
            continue
        add(hw_num(t), 'solution' if SOL_RX.search(t) else 'sheet', base / e['file'], t)
    for p in sorted((base / 'data' / 'assignments' / course).glob('*_assignment*')) if (base / 'data' / 'assignments' / course).exists() else []:
        t = p.name.split('_assignment')[0]
        add(hw_num(t) or hw_num(p.stem), 'sheet', p, t)
    hw_dir = base / 'data' / 'moodle' / course / 'homework'
    for p in sorted(hw_dir.iterdir()) if hw_dir.exists() else []:
        add(hw_num(p.stem), 'solution' if SOL_RX.search(p.stem) else 'sheet', p, p.stem)
    for p in sorted(files.glob('*_feedback_*')) if files.exists() else []:
        add(hw_num(p.name.split('_feedback_', 1)[1]), 'feedback', p, p.stem)
    for e in out.values():                                   # a PDF first: that's the file to send
        for k in ('sheet', 'solution', 'feedback'):
            e[k].sort(key=lambda s: (not s.lower().endswith('.pdf'), s))
    return out


def coursework_paths(course: str, root: Path | None = None) -> tuple[list[str], list[str]]:
    """(PDFs, text files) of every sheet / solution / feedback — what the course's NotebookLM notebook should hold.
    Not wired into the NotebookLM sync here (another owner's code): see the report of 14/11."""
    pdfs, texts = [], []
    for e in coursework(course, root).values():
        for k in ('sheet', 'solution', 'feedback'):
            for p in e[k]:
                (pdfs if p.lower().endswith('.pdf') else texts if p.lower().endswith('.md') else []).append(p)
    return pdfs, texts


def _rec_for(store: dict, course: str, num: str) -> dict | None:
    """The obligation of exercise N in the course (an assignment / exercise, not a quiz)."""
    hits = [r for r in store['items'].values() if r['course'] == course and r['kind'] != 'quiz'
            and not r.get('gone') and hw_num(r['title']) == num]
    return sorted(hits, key=lambda r: r.get('due') or '')[-1] if hits else None


def _status_line(rec: dict | None, now: datetime) -> str:
    if not rec:
        return 'אין לו פעילות הגשה במודל (קובץ בלבד).'
    due = _dt(rec.get('due'))
    s = status(rec, now)
    if s == 'closed':
        how = 'סימנת שהגשת' if rec.get('closed_by') in ('owner', 'words') else 'הוגש'
        return f"✅ {how}" + (f" · ציון {rec['grade']}" if rec.get('grade') else ' · עוד לא נבדק')
    if s == 'late':
        return f"⌛ המועד עבר — אפשר עוד באיחור עד {_when(_dt(rec['cutoff']), now)}"
    if s == 'missed':
        return f"⚠️ המועד עבר ({due:%d/%m %H:%M}) ולא ראיתי הגשה"
    return f"⏳ עוד לא הגשת" + (f" — הגשה {_when(due, now)}" if due else '')


def sheet_answer(course: str, num: str, want: str = 'sheet', now: datetime | None = None, store: dict | None = None,
                 root: Path | None = None) -> tuple[str, list[str]]:
    """"תראה לי את תרגיל 3 במיקרו" / "יצא פתרון לתרגיל 2?" → (text, files to send). Read only."""
    now = now or datetime.now()
    store = store if store is not None else load()
    cw = coursework(course, root).get(num)
    rec = _rec_for(store, course, num)
    name = _course_name(course)
    if not cw and not rec:
        have = sorted(coursework(course, root), key=int)
        return (f'לא מצאתי את תרגיל {num} ב{name}.' + (f" יש: {', '.join(have)}." if have else ''), [])
    head = f"📝 *{name} — תרגיל {num}*" + (f" ({rec['title']})" if rec and rec.get('title') else '')
    lines = [head, _status_line(rec, now)]
    if rec and rec.get('url'):
        lines.append(f"👉 {rec['url']}")
    sols = (cw or {}).get('solution') or []
    fbs = (cw or {}).get('feedback') or []
    if want == 'solution':
        lines.append('✅ יצא פתרון — מצרף.' if sols else '❌ עדיין לא ראיתי פתרון רשמי במודל או באתר המרצה.')
        return '\n'.join(lines), sols[:1]
    lines.append(('✅ יש פתרון רשמי ("יצא פתרון לתרגיל ' + num + '?" — ואשלח)') if sols else 'פתרון: עדיין לא פורסם.')
    if fbs:
        lines.append('💬 יש משוב מהבודק — מצרף.')
    sheets = (cw or {}).get('sheet') or []
    if not sheets:
        lines.append('את הקובץ של התרגיל עצמו אין לי (רק בעמוד במודל).')
    return '\n'.join(lines), sheets[:1] + fbs[:1]


# ── "מה חסר לי?" ────────────────────────────────────────────────────────────

def answer_text(chat: str = '', course: str | None = None, now: datetime | None = None, store: dict | None = None,
                others: bool = True, root: Path | None = None, only: str | None = None) -> str:
    """Per course: what's still owed (with the Moodle link), what's done. Without a course: + open tasks, university
    mails that asked for something, exams in the next 14 days. only='hw': homework only (no quizzes, no extras).
    Read only (the store is refreshed from the files first)."""
    now = now or datetime.now()
    live_store = store is None
    store = refresh(now, chat or None) if store is None else store
    said_badge = False
    courses = [course] if course else (_active() or sorted({r['course'] for r in store['items'].values()}))
    keep = (lambda r: r.get('kind') != 'quiz') if only == 'hw' else (lambda r: True)
    others = others and only is None
    out, any_open = [], False
    for ck in courses:
        items = [r for r in open_items(store, now, ck) if keep(r)]
        done = [r for r in store['items'].values() if r['course'] == ck and status(r, now) == 'closed' and keep(r)]
        if not items and not course and not done:
            continue
        head = f'*{_course_name(ck)}*'
        if items:
            any_open = True
            block = head + '\n' + '\n'.join(line(r, now) for r in items)
        else:
            block = head + ' — אין כלום פתוח ✅'
        quizzes = [r for r in done if r.get('kind') == 'quiz']
        if quizzes and ck in PRIORITY:
            wc = wrong_counts(ck, root)
            bad = sum(wc.get(r['id'], (0, 0))[0] for r in quizzes)
            block += f"\n✅ {len(quizzes)} בחנים הושלמו" + (f' · {bad} טעויות ("מה טעיתי בבחנים")' if bad else ' · בלי טעויות')
        elif done and items:
            block += f'\n✅ הושלמו: {len(done)}'
        exams = [x for x in (store.get('exams') or {}).get(ck, []) if x['date'] >= now.date().isoformat()]
        if exams and course:
            block += '\n' + '\n'.join(exam_line(x) + (' _(לפי הסדר ביומן)_' if x.get('moed_inferred') else '')
                                      for x in exams)
        for r in done:                     # 15/11 finished, but Moodle's badge still says "Mark as done": once, here only
            if r.get('kind') == 'quiz' and r.get('unmarked') and not r.get('unmarked_said'):
                block += (f"\nℹ️ {r['title']}: הניסיון הסתיים וזה מה שקובע — רק כפתור ה-\"Mark as done\" במודל לא נלחץ "
                          "(אם חשוב לך שיהיה ירוק, לחץ עליו שם; אני לא לוחץ בשבילך).")
                r['unmarked_said'] = now.isoformat(timespec='minutes')
                said_badge = True
        out.append(block)
    if others and not course:
        extra = []
        try:
            from agent import tasks
            ts = tasks.open_tasks(chat) if chat else []
            extra += [f"• 📌 {t.get('text', '')}" + (f" (עד {t['due']})" if t.get('due') else '') for t in ts[:8]]
        except Exception:
            pass
        try:                                           # a university mail that asked for something (register / fill / pay)
            from agent import closure, proactive
            closed = closure.items(chat or None)
            for m in proactive._mails(now):
                if proactive.mail_candidate(m, now.date()) and \
                        not closure.match(closed, f"{m['subject']} {m['action']}", m.get('who', ''), when=m['day']):
                    extra.append(f"• 📬 {m['action'].rstrip('.')} ({m['who'][:30]}, {m['day']:%d/%m})")
        except Exception:
            pass
        try:
            from integrations import tau_portal
            events, _, _ = tau_portal.schedule(live=False)
            for e in events or []:
                if e.get('is_exam') and now.date().isoformat() <= e['date'] <= (now + timedelta(days=14)).date().isoformat():
                    extra.append(f"• 🎯 מבחן ב{e['course']} — {e['date'][8:10]}/{e['date'][5:7]} {e.get('time') or ''}")
        except Exception:
            pass
        if extra:
            out.append('*עוד דברים פתוחים:*\n' + '\n'.join(extra))
    if said_badge and live_store:
        try:
            d = load()
            for oid, r in store['items'].items():
                if r.get('unmarked_said') and oid in d['items']:
                    d['items'][oid]['unmarked_said'] = r['unmarked_said']
            save(d)
        except Exception:
            pass
    if not out:
        return '✅ אין לך כרגע שום מטלה או בוחן פתוחים במודל.'
    title = f'📋 *מה פתוח לך ב{_course_name(course)}:*' if course else '📋 *מה עוד פתוח לך:*'
    if not any_open and course:
        title = f'✅ *ב{_course_name(course)} אין כלום פתוח.*'
    return title + '\n\n' + '\n\n'.join(out)


# ── the nudges (pure: store + now → proactive notes) ───────────────────────

def _offer(rec: dict) -> dict:
    o = {'title': f"{_course_name(rec['course'])} — {rec.get('title', '')}"[:120], 'oid': rec['id'], 'allday': False}
    if rec.get('due'):
        o['start'] = rec['due'][:16]
    return o


def _close_txt(rec: dict, now: datetime) -> str:
    due = _dt(rec.get('due'))
    if due and rec.get('due_est'):
        return f"נסגר בערך ב{_when(due, now)}"
    return f"נסגר {_when(due, now)}" if due else 'אין לו תאריך סגירה'


def notes(now: datetime, store: dict) -> list[dict]:
    """proactive-shaped notes ({key, kind, text, prio, offer, opts, ids}). Pure."""
    out = []
    items = open_items(store, now)
    batch = []
    for r in items:
        due = _dt(r.get('due'))
        engine = bool(due) and not r.get('due_est') and (due - now) <= timedelta(hours=ENGINE_H)   # utils/deadlines has it
        # (an estimated close isn't in utils/deadlines' items, so it never silences us)
        fresh = not r.get('announced') and (now - (_dt(r.get('first_seen')) or now)) <= NEW_MAX_AGE
        if status(r, now) == 'late':
            if not r.get('late_said'):
                out.append({'key': f"oblig|late|{r['id']}", 'kind': 'oblig_late', 'prio': 0 if is_priority(r) else 1,
                            'ids': [r['id']], 'offer': _offer(r), 'opts': OPTS,
                            'text': f"⌛ {_course_name(r['course'])} — *{r['title']}*: המועד עבר ולא ראיתי שהגשת, "
                                    f"אבל אפשר עוד באיחור עד {_when(_dt(r['cutoff']), now)}.\n👉 {r['url']}"})
            continue
        if is_priority(r):
            if fresh:
                out.append({'key': f"oblig|new|{r['id']}", 'kind': 'oblig_new', 'prio': 0, 'ids': [r['id']],
                            'offer': _offer(r), 'opts': OPTS,
                            'text': f"🧪 *בוחן חדש ב{_course_name(r['course'])}: {r['title']}* — עוד לא עשית אותו "
                                    f"({_close_txt(r, now)}).\nשאלה מהבחנים האלה נכנסת למבחן — שווה לעשות אותו עכשיו, "
                                    f"כשהשיעור טרי.\n👉 {r['url']}"})
            elif r.get('announced') and not engine:
                last = _dt(r.get('last_nudged')) or _dt(r.get('first_seen')) or now
                if now - last >= REMIND_EVERY:
                    started = ' התחלת ולא סיימת.' if r.get('state') == 'in_progress' else ''
                    out.append({'key': f"oblig|r|{r['id']}|{now:%Y-%m-%d}", 'kind': 'oblig_remind', 'prio': 0,
                                'ids': [r['id']], 'offer': _offer(r), 'opts': OPTS,
                                'text': f"🧪 עדיין פתוח: *{r['title']}* ב{_course_name(r['course'])} "
                                        f"({_close_txt(r, now)}).{started}\nהבחנים האלה הם הכי קרובים למבחן שיש. "
                                        f"עשית? לחץ ✅.\n👉 {r['url']}"})
            continue
        if fresh and not engine:
            batch.append(r)
    if batch and now.hour >= BATCH_FROM:
        one = batch[0] if len(batch) == 1 else None
        out.append({'key': 'oblig|new|' + '+'.join(sorted(r['id'] for r in batch))[:160], 'kind': 'oblig_new', 'prio': 1,
                    'ids': [r['id'] for r in batch], 'offer': _offer(one) if one else {'title': 'מטלות חדשות'},
                    'opts': OPTS if one else [('👍 ראיתי', 'ok')],
                    'text': '🆕 *חדש במודל:*\n' + '\n'.join(f"{line(r, now, link=bool(one))}" for r in batch)
                            + ('' if one else '\n\nהקישורים ב"מה חסר לי".')})
    news = [n for n in store.get('news') or [] if not n.get('said')]
    if news:
        def news_line(n):
            ck, _, num = n['key'].partition(':')
            what = f"תרגיל {num} ב{_course_name(ck)}" if num else _course_name(ck)
            if n['kind'] == 'solution':
                return f'📗 יצא פתרון ל{what} ("יצא פתרון לתרגיל {num}?" ואשלח)'
            if n['kind'] == 'sheet':
                return f'📄 יצא {what} ("תראה לי את תרגיל {num}" ואשלח)'
            if n['kind'] == 'grade':
                return f"💯 קיבלת ציון ב{what}: {n.get('grade', '')}"
            return f'💬 יש משוב מהבודק על {what} ("תראה לי את תרגיל {num}")'
        out.append({'key': 'oblig|news|' + '+'.join(f"{n['kind']}:{n['key']}" for n in news)[:160], 'kind': 'oblig_news',
                    'prio': 1, 'ids': [], 'news': [f"{n['kind']}:{n['key']}" for n in news], 'opts': [('👍 ראיתי', 'ok')],
                    'offer': {'title': 'עדכונים במודל'}, 'text': '\n'.join(news_line(n) for n in news[:6])})
    wd, h0, h1 = WEEK_DIGEST
    if now.weekday() == wd and h0 <= now.hour < h1:
        week = [r for r in items if not r.get('due') or is_priority(r) or _dt(r['due']) <= now + timedelta(days=7)]
        if week:
            top = week[0]
            out.append({'key': f'oblig|week|{now:%Y-%m-%d}', 'kind': 'oblig_week', 'prio': 2, 'ids': [],
                        'offer': _offer(top),
                        'opts': [(f"✅ עשיתי: {top['title'][:30]}", 'odone'), ('⏰ תזכיר מחר', 'tom10'),
                                 ('🔕 לא רלוונטי', 'oirr')],
                        'text': '🗓️ *מה פתוח לך השבוע:*\n' + '\n'.join(line(r, now, link=False) for r in week[:8])
                                + (f'\nועוד {len(week) - 8}.' if len(week) > 8 else '')
                                + '\n\nהכפתורים על הראשון ברשימה. קישורים: "מה חסר לי".'})
    return out


def mark_said(note: dict, now: datetime | None = None, path: Path | None = None) -> None:
    """A note went out (agent/proactive.tick): its items are announced / nudged now."""
    if not str(note.get('kind', '')).startswith('oblig') or not (note.get('ids') or note.get('news')):
        return
    now = now or datetime.now()
    d = load(path)
    for n in d.get('news') or []:
        if f"{n['kind']}:{n['key']}" in (note.get('news') or []):
            n['said'] = now.isoformat(timespec='minutes')
    for oid in note['ids']:
        r = d['items'].get(oid)
        if r:
            r['announced'] = True
            r['last_nudged'] = now.isoformat(timespec='minutes')
            if note['kind'] == 'oblig_late':
                r['late_said'] = True
    save(d, path)


def take_new(now: datetime | None = None, path: Path | None = None, store: dict | None = None,
             write: bool = True) -> list[str]:
    """The morning message's "🆕 חדש במודל" lines: new items that are not a priority quiz (those get their own opener).
    Marks them announced."""
    now = now or datetime.now()
    d = store if store is not None else load(path)
    new = [r for r in open_items(d, now) if not r.get('announced') and not is_priority(r)
           and now - (_dt(r.get('first_seen')) or now) <= NEW_MAX_AGE]
    for r in new:
        r['announced'] = True
        r['last_nudged'] = now.isoformat(timespec='minutes')
    if new and write:
        save(d, path)
    return [f"{_course_name(r['course'])}: " + line(r, now, link=False)[2:] for r in new]


def close(chat: str, oid: str, why: str = 'done', now: datetime | None = None, path: Path | None = None,
          closed_path: Path | None = None, dl_path: Path | None = None) -> dict | None:
    """✅ עשיתי / 🔕 לא רלוונטי: closed here, in agent/closure (never comes back) and in utils/deadlines (its reminders
    stop). → the item, or None if unknown."""
    from agent import closure
    from utils import deadlines as dl
    now = now or datetime.now()
    d = load(path)
    r = d['items'].get(oid)
    if not r:
        return None
    r.update(closed=now.isoformat(timespec='minutes'), closed_by='owner', closed_why=why)
    save(d, path)
    closure.record(chat, alias(r), why, origin='obligation', now=now, path=closed_path)
    try:
        dl.mark_done(oid, dl_path)
    except Exception as e:
        print(f'  ⚠️ obligations → deadlines: {e!r}'[:200])
    return r


# ── words ───────────────────────────────────────────────────────────────────

MISSING_RX = re.compile(r'^\s*(?:ו?מה|יש|איזה|אילו|תגיד לי מה)\s+(?:לי\s+)?(?:עוד\s+)?(?:משהו\s+)?'
                        r'(?:חסר|חסרים|פתוח|פתוחים|פתוחות|נשאר|נשארו|לא עשיתי|לא הגשתי|מטלות|בחנים)'
                        r'(?:\s+(?:פתוחים|פתוחות|שנשארו))?'
                        r'(?:\s+לי)?(?:\s+(?:לעשות|להגיש|במודל|השבוע|עוד))*(?:\s+[בל]?\S+(?:\s+\d)?)?\s*[?!.]*\s*$')
DAY_RX = re.compile(r'היום|מחר|הערב|בערב|בבוקר|ביומן|בלו"ז|בלו״ז|לקנות|בבית|בעבודה')   # the day's plan / life, not Moodle
QUIZ_WORD_RX = re.compile(r'בחנים|הבחנים|בוחן|הבוחן|קוויז|הקוויזים|quiz', re.I)
QUIZ_SHOW_RX = re.compile(r'תראה|הראה|תציג|טעיתי|טעויות|איך הלך לי')
NOT_QUIZ_RX = re.compile(r'סילבוס|מבחן|כתוב|תן לי|תכין|תייצר|תרגול')     # the syllabus' "בוחן", a practice request


HW_WORD = r'(?:תרגיל(?:\s+בית)?|תרגילי\s+בית|שיעורי\s+(?:ה)?בית|ש["״]ב|מטל(?:ה|ות)|הגשות|ההגשות|hw|assignment)'
HW_LIST_RX = re.compile(r'^\s*(?:ו?מה|אילו|איזה|יש)\s+(?:לי\s+)?(?:עוד\s+)?(?:ה)?' + HW_WORD +
                        r'(?:\s+(?:ה)?(?:פתוחים|פתוחות|שנשארו|שלא הגשתי|שעוד לא הגשתי|שלי))+(?:\s+[בל]?\S+)?\s*[?!.]*\s*$', re.I)
SHEET_RX = re.compile(r'(?:תראה|תשלח|שלח|תביא|הראה|תציג|יש לך|אפשר)\s+(?:לי\s+)?(?:את\s+)?(?:ה)?' + HW_WORD +
                      r'\s*(?:מספר\s*)?(\d{1,2})(?!\d)', re.I)
SOLUTION_RX = re.compile(r'פתרון|פתרונות', re.I)
SOLUTION_ASK_RX = re.compile(r'(?:יצא|יצאו|פורסם|פרסמו|עלה|העלו|יש|תשלח|שלח|תביא|תראה)\s+(?:כבר\s+)?(?:לי\s+)?(?:את\s+)?(?:ה)?'
                             r'(?:פתרון|פתרונות)\s+(?:ה?רשמי\s+)?(?:של\s+|ל|ל?ה)?' + HW_WORD + r'?\s*(\d{1,2})(?!\d)', re.I)


def _course_for_num(num: str, course: str | None, store: dict) -> list[str]:
    if course:
        return [course]
    return [ck for ck in _active() if num in coursework(ck) or _rec_for(store, ck, num)]


def route(text: str, chat: str, course: str | None = None):
    """Deterministic: → the answer (str), or (text, [files to send]), or None when the message isn't one of these."""
    t = (text or '').strip()
    if not t or len(t) > 80:
        return None
    try:                                                   # 15/11 "מה סיננת?" — the university updates not sent to him
        from integrations import univ_relevance
        if univ_relevance.WHAT_RX.match(t):
            return univ_relevance.filtered_text()
    except ImportError:
        pass
    if QUIZ_WORD_RX.search(t) and QUIZ_SHOW_RX.search(t) and not NOT_QUIZ_RX.search(t):
        return quizzes_text(course or 'software_1', wrong_only=bool(re.search(r'טעי', t)))
    m = SOLUTION_ASK_RX.search(t) or (SHEET_RX.search(t) if not re.search(r'תן לי|על\s', t) else None)
    if m:
        num, want = m.group(1), 'solution' if SOLUTION_RX.search(t) else 'sheet'
        store = refresh(chat=chat or None)
        cks = _course_for_num(num, course, store)
        if len(cks) != 1:
            names = ', '.join(_course_name(c) for c in cks) or ', '.join(_course_name(c) for c in _active())
            return f'באיזה קורס? ({names})'
        return sheet_answer(cks[0], num, want, store=store)
    if HW_LIST_RX.match(t) and not DAY_RX.search(t):
        return answer_text(chat, course, only='hw')
    if MISSING_RX.match(t) and not DAY_RX.search(t):
        return answer_text(chat, course)
    return None
