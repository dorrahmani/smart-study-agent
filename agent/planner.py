"""
agent/planner.py — goals, work shifts and the daily / weekly task page (The owner 19/10: "דף משימות יומי ושבועי",
"שתלמד מתי אני עובד", "מטרות שלנו (כולל להנות עם חברים ולהתאמן)").

    goals   🏋️ להתאמן · 🍻 לבלות עם חברים · 📚 ללמוד — a weekly count each; "מטרה: להתאמן 4 פעמים בשבוע" changes one,
            "התאמנתי" / "יצאתי עם חברים" / "למדתי" counts one (calendar events of the week that match count too)
    work    the weekly shifts — told once ("אני עובד בראשון ורביעי 16-22"), or learned by agent/learn.py
    page    daily_block → the morning brief's "📋 המשימות להיום"; weekly_block → Sunday's "📋 השבוע שלך"
            ("מה המשימות שלי היום / השבוע", "מה המטרות שלי" answer on demand)

No LLM here except parse_work (one flash call, only when the owner describes his shifts). Storage: data/agent_plan.json.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'agent_plan.json'
MODEL = 'gemini-3.8-flash'
DAYS_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']          # by date.weekday()

DEFAULT_GOALS = [
    {'id': 'train', 'emoji': '🏋️', 'name': 'להתאמן', 'per_week': 3,
     'rx': r'אימון|התאמנתי|חדר כושר|ריצה|רצתי|כושר|gym|שחייה|שחיתי|כדורגל|כדורסל|טניס|פאדל|יוגה|פילאטיס'},
    {'id': 'friends', 'emoji': '🍻', 'name': 'לבלות עם חברים', 'per_week': 1,
     'rx': r'חברים|חבר\'ה|יצאתי עם|נפגשתי עם|ערב עם|מסיבה|בירה|בילוי|בילינו|יום הולדת'},
    {'id': 'study', 'emoji': '📚', 'name': 'ללמוד (מעבר לשיעורים)', 'per_week': 4,
     'rx': r'למדתי|חזרה על|תרגלתי|פתרתי'},
]


def _load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or STORE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save(d: dict, path: Path | None = None) -> None:
    p = path or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(p)


def _me(d: dict, chat: str) -> dict:
    u = d.setdefault(chat, {})
    if 'goals' not in u:
        u['goals'] = [dict(g) for g in DEFAULT_GOALS]
    u.setdefault('done', [])
    u.setdefault('work', [])
    return u


def week_start(day: date) -> date:
    """Sunday — the Israeli week."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


# ── goals ───────────────────────────────────────────────────────────────────

GOAL_SET_RX = re.compile(r'^\s*(?:מטרה|יעד|המטרה שלי)\s*[:：]?\s*(.+?)\s+(\d{1,2})\s*(?:פעמים|פעם|אימונים|ימים)?\s*'
                         r'(?:ב|לכל\s*)?שבוע\s*[.!]?\s*$')
GOAL_DEL_RX = re.compile(r'^\s*(?:תמחק|תוריד|בטל)\s+(?:את\s+)?(?:ה)?מטרה\s+(.+?)\s*$')
DONE_RX = re.compile(r'(?:^|\s|ש)(?:התאמנתי|הייתי ב(?:אימון|חדר כושר)|רצתי|שחיתי|עשיתי (?:אימון|ספורט)|'
                     r'(?:יצאתי|נפגשתי|הייתי|ישבתי)(?:\s+\S+)?\s+עם\s+(?:ה)?(?:חברים|חבר\'ה)|בילינו|למדתי|תרגלתי)\b')
GOALS_Q_RX = re.compile(r'מה (?:ה)?מטרות (?:שלי|שלנו)|(?:ה)?מטרות (?:שלי|שלנו) (?:השבוע|לשבוע)|איך אני (?:עם|עומד ב)(?:ה)?מטרות')
TASKS_Q_RX = re.compile(r'(?:מה|אילו|איזה) (?:ה)?משימות (?:שלי |לי )?(?:ל)?(?:היום|מחר|השבוע|לשבוע)|דף (?:ה)?משימות|'
                        r'מה (?:אני צריך|עליי|יש לי) לעשות (?:היום|מחר|השבוע)|(?:תכנן|תתכנן) לי את (?:ה)?(?:יום|שבוע)')


def goals(chat: str, path: Path | None = None) -> list[dict]:
    return _me(_load(path), chat)['goals']


def set_goal(chat: str, what: str, per_week: int, path: Path | None = None) -> str:
    d = _load(path)
    u = _me(d, chat)
    what = what.strip(' .')
    g = next((g for g in u['goals'] if re.search(g['rx'], what) or g['name'] in what or what in g['name']), None)
    if g is None:
        gid = re.sub(r'\W+', '_', what)[:20] or f'g{len(u["goals"])}'
        g = {'id': gid, 'emoji': '🎯', 'name': what, 'per_week': per_week, 'rx': re.escape(what)}
        u['goals'].append(g)
    g['per_week'] = max(0, min(int(per_week), 14))
    _save(d, path)
    return f"🎯 עודכן: {g['emoji']} {g['name']} — {g['per_week']} פעמים בשבוע."


def drop_goal(chat: str, what: str, path: Path | None = None) -> str:
    d = _load(path)
    u = _me(d, chat)
    keep = [g for g in u['goals'] if not (re.search(g['rx'], what) or what.strip() in g['name'])]
    if len(keep) == len(u['goals']):
        return f'לא מצאתי מטרה כזו. המטרות: ' + ', '.join(g['name'] for g in u['goals'])
    u['goals'] = keep
    _save(d, path)
    return '🗑️ הורדתי את המטרה.'


def log_done(chat: str, text: str, day: date | None = None, path: Path | None = None) -> str:
    """"התאמנתי" → +1 for the matching goal this week. → the reply ('' when no goal matches)."""
    day = day or date.today()
    if re.search(r'\bאתמול\b', text or ''):
        day -= timedelta(days=1)
    d = _load(path)
    u = _me(d, chat)
    hit = [g for g in u['goals'] if re.search(g['rx'], text or '')]
    if not hit:
        return ''
    for g in hit:
        u['done'].append({'g': g['id'], 'd': day.isoformat()})
    u['done'] = u['done'][-400:]
    _save(d, path)
    prog = progress(chat, day, path=path)
    return '💪 רשמתי! ' + ' · '.join(f"{g['emoji']} {prog.get(g['id'], 0)}/{g['per_week']}" for g in hit) + ' השבוע.'


def _cal_week(start: date, days: int = 7) -> dict[date, list[dict]]:
    """The calendar per day (best-effort; {} without a calendar)."""
    out: dict[date, list[dict]] = {}
    try:
        from integrations import gcal
        if not gcal.configured():
            return {}
        for i in range(days):
            day = start + timedelta(days=i)
            out[day] = gcal.events_on(day)
    except Exception as e:
        print(f'  ⚠️ planner calendar failed: {e!r}'[:200])
    return out


def progress(chat: str, day: date | None = None, cal: dict | None = None, path: Path | None = None) -> dict[str, int]:
    """{goal id: times this week} — what the owner reported + this week's past calendar events that match the goal."""
    day = day or date.today()
    ws = week_start(day)
    u = _me(_load(path), chat)
    n: dict[str, int] = {}
    seen_days: set[tuple] = set()
    for r in u['done']:
        if ws.isoformat() <= r['d'] <= day.isoformat():
            n[r['g']] = n.get(r['g'], 0) + 1
            seen_days.add((r['g'], r['d']))
    for dd, items in (cal or {}).items():
        if not ws <= dd <= day:
            continue
        for it in items:
            if it.get('_cal_kind') == 'lessons':
                continue
            for g in u['goals']:
                if re.search(g['rx'], it.get('summary') or '') and (g['id'], dd.isoformat()) not in seen_days:
                    n[g['id']] = n.get(g['id'], 0) + 1
                    seen_days.add((g['id'], dd.isoformat()))
    return n


def goals_line(chat: str, day: date | None = None, cal: dict | None = None, path: Path | None = None,
               study: bool = True) -> str:
    prog = progress(chat, day, cal, path)
    gs = [g for g in goals(chat, path) if g.get('per_week') and (study or g['id'] != 'study')]
    return ' · '.join(f"{g['emoji']} {g['name'].split(' (')[0]} {prog.get(g['id'], 0)}/{g['per_week']}" for g in gs)


# ── work shifts ─────────────────────────────────────────────────────────────

WORK_RX = re.compile(r'(?:אני|אנחנו)\s+(?:לא\s+)?עובד(?:ת|ים)?\s+(?:ב|כל|ביום|בימי|בין|מ|רק|בכלל|כרגע)|המשמרות שלי|'
                     r'יש לי משמרת|(?:^|\s)משמרות?\s+(?:ב|כל|ביום|בימי)|^\s*אני לא עובד')
WORK_HINT_RX = re.compile(r'ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת|\d{1,2}(?::\d{2})?\s*[-–]\s*\d{1,2}|לא עובד')
WORK_Q_RX = re.compile(r'מתי אני עובד|מה (?:ה)?משמרות שלי')

WORK_PROMPT = """המשתמש כתב על המשמרות / שעות העבודה הקבועות שלו. החזר JSON בלבד:
{{"none": true/false, "shifts": [{{"day": "ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת", "start": "HH:MM", "end": "HH:MM", "where": ""}}]}}
- "none": true רק אם כתב שהוא לא עובד בכלל.
- טווח ימים ("ראשון עד חמישי") → משמרת לכל יום. שעה בלי דקות → HH:00. "ערב" בלי שעות → 17:00-22:00, "בוקר" → 08:00-14:00.
- אם לא ניתן להבין ימים — shifts ריק.
הטקסט: {text}"""


def parse_work(text: str, client=None) -> dict:
    if client is None:
        import google.genai as genai
        client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    from google.genai import types as T
    resp = client.models.generate_content(
        model=MODEL, contents=WORK_PROMPT.format(text=text[:800]),
        config=T.GenerateContentConfig(temperature=0, max_output_tokens=1200, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    try:
        from utils.usage_tracker import log_gemini
        log_gemini(resp, MODEL, op='planner.work')
    except Exception:
        pass
    return clean_work(json.loads(resp.text or '{}'))


def clean_work(raw: dict) -> dict:
    """The model's JSON → {'none': bool, 'shifts': [{'wd': weekday, 'start', 'end', 'where'}]} (bad rows dropped)."""
    out = []
    for s in (raw or {}).get('shifts') or []:
        name = (s.get('day') or '').strip()
        if name.startswith('יום '):
            name = name[4:]
        if name not in DAYS_HE:
            continue
        st, en = (s.get('start') or '').strip(), (s.get('end') or '').strip()
        if not (re.fullmatch(r'\d{1,2}:\d{2}', st) and re.fullmatch(r'\d{1,2}:\d{2}', en)):
            continue
        out.append({'wd': DAYS_HE.index(name), 'start': st.zfill(5), 'end': en.zfill(5),
                    'where': (s.get('where') or '').strip()[:40]})
    return {'none': bool((raw or {}).get('none')) and not out, 'shifts': out}


def set_work(chat: str, work: dict, path: Path | None = None) -> str:
    d = _load(path)
    u = _me(d, chat)
    u['work'] = work['shifts']
    u['work_known'] = True
    u['work_set'] = date.today().isoformat()
    _save(d, path)
    try:                                                   # the brain's profile mentions it too (agent/life memory)
        if path is not None:                               # a test run — never the real memory
            raise LookupError('test')
        from agent import life
        ld = life._load()
        mem = [f for f in ld['memory'].get(chat, []) if not f.startswith('משמרות עבודה:')]
        ld['memory'][chat] = mem + [f'משמרות עבודה: {work_text(work["shifts"]) or "לא עובד כרגע"}']
        life._save(ld)
    except LookupError:
        pass
    except Exception as e:
        print(f'  ⚠️ work → memory failed: {e!r}'[:200])
    if work['none']:
        return '💼 רשמתי שאתה לא עובד כרגע. אם זה ישתנה — "אני עובד ב…".'
    return f'💼 רשמתי את המשמרות: {work_text(work["shifts"])}.\nאתכנן סביבן את המשימות והמטרות. שינוי? "אני עובד ב…".'


def work_text(shifts: list[dict]) -> str:
    return ', '.join(f"{DAYS_HE[s['wd']]} {s['start']}–{s['end']}" + (f" ({s['where']})" if s.get('where') else '')
                     for s in sorted(shifts, key=lambda s: ((s['wd'] + 1) % 7, s['start'])))


def work_known(chat: str, path: Path | None = None) -> bool:
    return bool(_me(_load(path), chat).get('work_known'))


def shifts_on(chat: str, day: date, path: Path | None = None) -> list[dict]:
    return [s for s in _me(_load(path), chat)['work'] if s['wd'] == day.weekday()]


# ── the page ────────────────────────────────────────────────────────────────

def _busy_evening(chat: str, day: date, items: list[dict], path: Path | None = None) -> bool:
    """Work or a timed event from 17:00 on → no evening for training / friends."""
    if any(s['end'] > '17:00' for s in shifts_on(chat, day, path)):
        return True
    for it in items:
        t = ((it.get('start') or {}).get('dateTime') or '')[11:16]
        if t and t >= '17:00':
            return True
    return False


def _exams_soon(today: date, days: int = 21, u: dict | None = None, chat: str = '') -> list[tuple[int, str]]:
    """The exams the owner is sitting (not the ones he answered "not taking" — agent/core exam_skip)."""
    out = []
    try:
        from integrations import tau_portal
        from agent import core
        if u is None:
            u = core._load().get(chat, {}) if chat else {}
        events, _, _ = tau_portal.schedule(live=False)
        for e in events or []:
            if e.get('is_exam') and today.isoformat() <= e['date'] <= (today + timedelta(days=days)).isoformat() \
                    and core._taking(u, e):
                out.append(((date.fromisoformat(e['date']) - today).days, e['course']))
    except Exception:
        pass
    return sorted(set(out))


def _todo_items(n: int = 5) -> list[str]:
    try:
        from agent import closure, life
        closed = closure.items()
        return [t for t in life._load()['lists'].get('משימות', []) if not closure.match(closed, t)][:n]
    except Exception:
        return []


def daily_block(chat: str, day: date | None = None, cal: dict | None = None, u: dict | None = None,
                path: Path | None = None, deadlines: bool = True) -> str:
    """"📋 המשימות להיום" — work, what to submit, the goals, open to-dos. 05/11: the study block for the nearest exam and
    the semester's most useful topic only with SSA_STUDY_PUSH=1 (agent/study_push); "מה ללמוד" still answers."""
    from agent import study_push
    push = study_push.on()
    day = day or date.today()
    cal = cal if cal is not None else _cal_week(week_start(day), (day - week_start(day)).days + 1)
    lines = []
    for s in shifts_on(chat, day, path):
        lines.append(f"• 💼 עבודה {s['start']}–{s['end']}" + (f" ({s['where']})" if s.get('where') else ''))
    try:
        if not deadlines:                                  # the morning brief lists them already
            raise LookupError
        from utils.deadlines import pending
        from config.paths import COURSE_NAMES
        for a in pending(now=datetime.combine(day, datetime.min.time()), horizon_days=3):
            left = (a['due'].date() - day).days
            when = 'היום' if left == 0 else 'מחר' if left == 1 else f'עד {a["due"]:%d/%m}'
            lines.append(f"• 📝 {'להגיש' if left == 0 else 'להתקדם ב'} {COURSE_NAMES.get(a.get('course_key'), '')} — "
                         f"{a.get('title', '')} ({when})")
    except Exception:
        pass
    exams = _exams_soon(day, u=u, chat=chat)
    if exams:
        left, course = exams[0]
        mins = 90 if left <= 7 else 45
        if left == 0:                                       # 23/10: "90 דק׳ חזרה (מבחן בעוד 0 ימים)" on the exam day
            lines.append(f'• 🎯 היום המבחן ב{course} — בהצלחה! 🍀')
        elif push:
            lines.append(f"• 📚 {mins} דק׳ חזרה ל{course} (מבחן {'מחר' if left == 1 else f'בעוד {left} ימים'})")
    if push and path is None and not (exams and exams[0][0] <= 7):     # no exam this week → the semester's most useful topic
        try:
            from agent import study_focus
            from collectors.course_files import SEMESTER_START
            if (p := study_focus.today_pick(day, SEMESTER_START)):
                lines.append(study_focus.line(p))
        except Exception as e:
            print(f'  ⚠️ study focus failed: {e!r}'[:200])
    items = (cal or {}).get(day, [])
    prog = progress(chat, day, cal, path)
    days_left = 6 - (day.weekday() + 1) % 7                     # days after today until Saturday
    if not _busy_evening(chat, day, items, path):
        for g in goals(chat, path):
            need = g.get('per_week', 0) - prog.get(g['id'], 0)
            if g['id'] != 'study' and need > 0 and need > days_left // 2:   # behind pace → today is a good day
                lines.append(f"• {g['emoji']} ערב פנוי — {g['name']}? ({prog.get(g['id'], 0)}/{g['per_week']} השבוע)")
                break
    for t in _todo_items():
        lines.append(f'• ☐ {t}')
    head = '📋 *המשימות להיום*'
    gl = goals_line(chat, day, cal, path, study=push)
    if not lines:
        return f'{head}\nאין משימות פתוחות להיום 🎉' + (f'\n🎯 השבוע: {gl}' if gl else '')
    return head + '\n' + '\n'.join(lines) + (f'\n🎯 השבוע: {gl}' if gl else '')


def weekly_block(chat: str, today: date | None = None, cal: dict | None = None, u: dict | None = None,
                 path: Path | None = None) -> str:
    """Sunday: "📋 השבוע שלך" — shifts, deadlines, exams ahead, goal targets and the free evenings to put them in."""
    today = today or date.today()
    ws = week_start(today)
    cal = cal if cal is not None else _cal_week(ws)
    parts = []
    me = _me(_load(path), chat)
    if me['work']:
        parts.append('💼 *משמרות:* ' + work_text(me['work']))
    try:
        from utils.deadlines import pending
        from config.paths import COURSE_NAMES
        dl = [a for a in pending(now=datetime.combine(today, datetime.min.time()), horizon_days=7)]
        if dl:
            parts.append('📝 *הגשות השבוע:*\n' + '\n'.join(
                f"• {DAYS_HE[a['due'].weekday()]} {a['due']:%d/%m} — {COURSE_NAMES.get(a.get('course_key'), '')}: {a.get('title', '')}"
                for a in dl))
    except Exception:
        pass
    exams = _exams_soon(today, 28, u, chat)
    if exams:
        parts.append('🎯 *מבחנים ב-4 השבועות הקרובים:* ' + ', '.join(f'{c} (בעוד {d} ימים)' for d, c in exams))
    free = [ws + timedelta(days=i) for i in range(7)
            if ws + timedelta(days=i) >= today and not _busy_evening(chat, ws + timedelta(days=i), cal.get(ws + timedelta(days=i), []), path)]
    from agent import study_push
    gl = [g for g in goals(chat, path) if g.get('per_week') and (study_push.on() or g['id'] != 'study')]
    if gl:
        lines = [f"• {g['emoji']} {g['name']} — {g['per_week']} פעמים" for g in gl]
        if free:
            lines.append('🌙 ערבים פנויים: ' + ', '.join(f'{DAYS_HE[d.weekday()]}' for d in free)
                         + ' — אפשר לשבץ בהם אימון / חברים.')
        parts.append('🏁 *המטרות לשבוע:*\n' + '\n'.join(lines))
    todo = _todo_items(8)
    if todo:
        parts.append('☐ *משימות פתוחות:* ' + ', '.join(todo))
    return '📋 *השבוע שלך*\n\n' + '\n\n'.join(parts) if parts else ''


def route(chat: str, text: str, path: Path | None = None) -> str | None:
    """Goals / work / task-page phrases → the reply; None otherwise (no LLM unless the owner describes his shifts)."""
    t = (text or '').strip()
    if m := GOAL_SET_RX.match(t):
        return set_goal(chat, m.group(1), int(m.group(2)), path)
    if m := GOAL_DEL_RX.match(t):
        return drop_goal(chat, m.group(1), path)
    if GOALS_Q_RX.search(t):
        gl = goals_line(chat, path=path, cal=_cal_week(week_start(date.today()), (date.today() - week_start(date.today())).days + 1))
        return (f'🎯 *המטרות השבוע:* {gl}\n\nשינוי: "מטרה: להתאמן 4 פעמים בשבוע" · דיווח: "התאמנתי" / "יצאתי עם חברים" / "למדתי"')
    try:
        from agent import study_focus
        if study_focus.ASK_RX.search(t):
            from collectors.course_files import SEMESTER_START
            return study_focus.answer(date.today(), SEMESTER_START)
    except ImportError:
        pass
    if TASKS_Q_RX.search(t):
        today = date.today()
        if re.search(r'שבוע', t):
            return weekly_block(chat, today, path=path) or 'אין לי עדיין מה לתכנן לשבוע הזה.'
        day = today + timedelta(days=1) if 'מחר' in t else today
        return daily_block(chat, day, path=path)
    if WORK_Q_RX.search(t) and not re.search(r'\d', t):
        me = _me(_load(path), chat)
        return (f"💼 המשמרות שלך: {work_text(me['work'])}" if me['work'] else
                '💼 עוד לא סיפרת לי מתי אתה עובד. כתוב למשל "אני עובד בראשון ורביעי 16-22".')
    if DONE_RX.search(t) and len(t) < 60 and '?' not in t and not re.search(r'תסביר|לא הבנתי|איך|מה |למה', t):
        r = log_done(chat, t, path=path)
        if r:
            return r
    return None
