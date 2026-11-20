"""
utils/deadlines.py — homework reminders that know whether you already submitted.

Source: data/assignments/_cache.json (Moodle scrape, nightly) — and, right before
reminding, a LIVE re-check of just the assignments due soon (refresh_live), so a
submission made this afternoon silences this evening's reminder.

Per assignment, at most:
  d3   ≤72h left   📌 heads-up              (+ ✅ הגשתי · 🏋️ סט תרגול לקראתה)
  d1   ≤26h left   ⏰ tomorrow
  h6   ≤6h left    🚨 urgent (bypasses quiet hours)
  ack  submitted   ✅ "ראיתי שהגשת" — only if we had reminded
  moved            📅 due date changed → stages reset
  missed           ⚠️ once, ≤12h after a deadline passed without a submission
A stage that is already past when first seen is skipped (no "3 days left" at 20h).
"✅ הגשתי" (mark_done) covers work submitted outside Moodle and resource-type items
whose submission status Moodle doesn't show.

plan_actions() is pure (items, state, now) → actions, so every case is testable.
"""
from __future__ import annotations

import hashlib
import re
import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / 'data' / 'assignments' / '_cache.json'
STATE = ROOT / 'data' / 'deadline_state.json'

STAGES = [('d3', 72), ('d1', 26), ('h6', 6)]       # name, hours-before threshold (least → most urgent)
MISSED_WINDOW_H = 12
LIVE_HORIZON_H = 80
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']


# ── data ────────────────────────────────────────────────────────────────────

def key_of(a: dict) -> str:
    aid = str(a.get('assign_id') or '').strip()
    if not aid:
        aid = hashlib.sha1(str(a.get('title', '')).encode('utf-8')).hexdigest()[:8]
    return f"{a.get('course_key', '?')}:{aid}"


def _due(a: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(a.get('due_date'))).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


LECTURER = ROOT / 'data' / 'assignments' / 'lecturer.json'      # exercises posted on a lecturer's own site (19/10)
ANNOUNCED = ROOT / 'data' / 'assignments' / 'announced.json'    # 19/11 an assignment / quiz a mail or Moodle post named
                                                                # (agent/obligation_ingest) before the Moodle scrape saw it
MANUAL = ROOT / 'data' / 'assignments' / 'manual.json'          # deadlines the owner stated himself (20/11)


def _ex_num(a: dict) -> str:
    m = re.search(r'(?:תרגיל|מטלה|ex|hw)\D{0,8}(\d{1,2})', str(a.get('title', '')), re.I)
    return m.group(1) if m else ''


def manual_path(cache: Path | None = None) -> Path:
    """data/assignments/manual.json — beside the cache (a test's tmp cache → its own manual file)."""
    return Path(cache or CACHE).with_name('manual.json')


def num_key(a: dict) -> tuple:
    """(course, kind, numbers in the title): "Assignment 2" (a mail) and "Assignment 2 - Submission" (Moodle) are one item."""
    kind = 'quiz' if a.get('kind') == 'quiz' else 'assign'
    return (a.get('course_key'), kind, tuple(sorted(set(re.findall(r'\d+', str(a.get('title', '')))))))


def load_items(cache: Path | None = None, lecturer: Path | None = None, announced: Path | None = None) -> list[dict]:
    try:
        items = json.loads((cache or CACHE).read_text(encoding='utf-8')).get('assignments') or []
    except (OSError, ValueError, AttributeError):
        items = []
    items = [a for a in items if isinstance(a, dict)]
    try:                                       # + the lecturer-site exercises, unless Moodle already has that number
        site = json.loads((lecturer or (LECTURER if cache is None else Path(cache).with_name('lecturer.json'))).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        site = []
    moodle_nums = {(a.get('course_key'), _ex_num(a)) for a in items if _ex_num(a)}
    items += [a for a in site if isinstance(a, dict) and (a.get('course_key'), _ex_num(a)) not in moodle_nums]
    try:                                       # + what a mail / announcement said, unless Moodle already has that one
        ann = json.loads((announced or (ANNOUNCED if cache is None else Path(cache).with_name('announced.json'))).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        ann = []
    mo = [a for a in items if a.get('source') != 'announced']
    have = {num_key(a) for a in mo}
    same_day = {(a.get('course_key'), num_key(a)[1], str(a.get('due_date'))[:10]) for a in mo}
    items += [a for a in ann if isinstance(a, dict) and num_key(a) not in have
              and (a.get('course_key'), num_key(a)[1], str(a.get('due_date'))[:10]) not in same_day]
    try:                                       # 20/11 + deadlines the owner stated ("תוסיף הגשה…", agent/deadline_ladder),
        mine = json.loads(manual_path(cache).read_text(encoding='utf-8'))
    except (OSError, ValueError):              # unless a real source already has that course on that day
        mine = []
    days = {(a.get('course_key'), str(a.get('due_date') or '')[:10]) for a in items if a.get('kind') != 'quiz'}
    items += [a for a in mine if isinstance(a, dict) and not a.get('removed')
              and (a.get('course_key'), str(a.get('due_date') or '')[:10]) not in days]
    return [a for a in items if _due(a) and _for_me(a)]


def _for_me(a: dict) -> bool:
    """15/11: no reminder for another group's submission box / quiz (integrations/univ_relevance, rule only)."""
    try:
        from integrations import univ_relevance
        return univ_relevance.for_me(a.get('course_key'), f"{a.get('title') or ''} {a.get('section') or ''}")
    except Exception:
        return True


def load_state(path: Path | None = None) -> dict:
    try:
        return json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_state(st: dict, path: Path | None = None) -> None:
    p = path or STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(p)


def is_done(a: dict, st: dict) -> bool:
    return bool(a.get('submitted')) or bool((st.get(key_of(a)) or {}).get('manual_done'))


def mark_done(key: str, path: Path | None = None) -> bool:
    """'✅ הגשתי' button. True if the key was known (or newly recorded)."""
    st = load_state(path)
    st.setdefault(key, {})['manual_done'] = datetime.now().isoformat(timespec='seconds')
    save_state(st, path)
    if path is None:                                    # the real store → the course site shows it ✅ right away
        try:
            from scripts.build_course_site import refresh_soon
            refresh_soon()
        except Exception:
            pass
    return True


def pending(now: datetime | None = None, horizon_days: int = 7, cache: Path | None = None,
            state_path: Path | None = None) -> list[dict]:
    """Not-submitted assignments due within the horizon, soonest first (for briefings / the bot)."""
    now = now or datetime.now()
    st = load_state(state_path)
    out = []
    for a in load_items(cache):
        due = _due(a)
        if now < due <= now + timedelta(days=horizon_days) and not is_done(a, st):
            out.append({**a, 'due': due, 'key': key_of(a)})
    out.sort(key=lambda a: a['due'])
    return out


def submitted_title(summary: str, items: list[dict] | None = None, state: dict | None = None) -> bool:
    """A calendar event like "✅ הגשת תרגיל 2 — הסתברות וסטטיסטיקה" whose assignment is already submitted
    (03/11 eval: "מה נשאר לי היום" listed HW2 at 22:00 — handed in hours before)."""
    if not re.search(r'הגש', summary or ''):
        return False
    num = _ex_num({'title': summary})
    if not num:
        return False
    from config.paths import COURSE_NAMES
    items = load_items() if items is None else items
    st = load_state() if state is None else state
    for a in items:
        name = COURSE_NAMES.get(a.get('course_key'), '')
        first = (re.findall(r'[א-ת]{3,}', name) or [''])[0]
        if _ex_num(a) == num and first and first in summary:
            return is_done(a, st)
    return False


def summary_line(now: datetime | None = None, horizon_days: int = 7, cache: Path | None = None,
                 state_path: Path | None = None) -> str:
    """The deadlines answer: what's still open (exact Moodle time), else that everything is in + the next open one."""
    from config.paths import COURSE_NAMES
    now = now or datetime.now()
    st = load_state(state_path)
    items = load_items(cache)
    due_soon = [a for a in items if now < _due(a) <= now + timedelta(days=horizon_days)]
    open_ = sorted((a for a in due_soon if not is_done(a, st)), key=_due)
    done = [a for a in due_soon if is_done(a, st)]

    def line(a):
        d = _due(a)
        return f"• {WEEKDAY_HE[d.weekday()]} {d:%d/%m} {d:%H:%M} — {COURSE_NAMES.get(a.get('course_key'), '')}: {a.get('title', '')}"
    if open_:
        out = '⏰ *עוד לא הגשת (השבוע הקרוב):*\n' + '\n'.join(line(a) for a in open_)
        return out + (f"\n\n✅ כבר הגשת: {', '.join(a.get('title', '') for a in done)}" if done else '')
    later = sorted((a for a in items if _due(a) > now + timedelta(days=horizon_days) and not is_done(a, st)), key=_due)
    out = '✅ אין לך הגשות פתוחות בשבוע הקרוב' + (f" (הגשת כבר: {', '.join(a.get('title', '') for a in done)})" if done else '') + '.'
    return out + (f"\nהבאה בתור:\n{line(later[0])}" if later else '')


# ── decisions (pure) ────────────────────────────────────────────────────────

def plan_actions(items: list[dict], state: dict, now: datetime) -> list[dict]:
    """[{'kind': 'remind'|'ack'|'moved'|'missed', 'key', 'item', 'stage'?, 'old_due'?}]"""
    actions = []
    for a in items:
        due = _due(a)
        if not due:
            continue
        k = key_of(a)
        st = state.get(k) or {}
        prev_due = st.get('due')
        if prev_due and prev_due != due.isoformat() and due > now:
            actions.append({'kind': 'moved', 'key': k, 'item': a, 'old_due': prev_due})
            st = {**st, 'sent': [], 'missed': False}            # the new date gets fresh reminders
        if is_done(a, state):
            if st.get('sent') and not st.get('acked'):
                actions.append({'kind': 'ack', 'key': k, 'item': a})
            continue
        if due <= now:
            if st.get('sent') and not st.get('missed') and now - due <= timedelta(hours=MISSED_WINDOW_H):
                actions.append({'kind': 'missed', 'key': k, 'item': a})
            continue
        left_h = (due - now).total_seconds() / 3600
        applicable = [name for name, h in STAGES if left_h <= h]
        if applicable and applicable[-1] not in (st.get('sent') or []):
            actions.append({'kind': 'remind', 'key': k, 'item': a, 'stage': applicable[-1]})
    return actions


def apply_action(state: dict, action: dict) -> None:
    """Record a delivered action in the state."""
    a, k = action['item'], action['key']
    st = state.setdefault(k, {})
    due = _due(a)
    if action['kind'] == 'moved':
        st['sent'], st['missed'] = [], False
    elif action['kind'] == 'remind':
        names = [n for n, _ in STAGES]
        upto = names.index(action['stage'])
        st['sent'] = sorted(set((st.get('sent') or []) + names[:upto + 1]), key=names.index)
    elif action['kind'] == 'ack':
        st['acked'] = True
    elif action['kind'] == 'missed':
        st['missed'] = True
    st['due'] = due.isoformat() if due else st.get('due')
    st['title'], st['course_key'] = a.get('title', ''), a.get('course_key', '')


# ── wording ─────────────────────────────────────────────────────────────────

def _when(due: datetime, now: datetime) -> str:
    days = (due.date() - now.date()).days
    day = 'היום' if days == 0 else ('מחר' if days == 1 else f"ביום {WEEKDAY_HE[due.weekday()]}")
    return f"{day} {due:%d/%m} ב-{due:%H:%M}"


def message(action: dict, now: datetime) -> tuple[str, list, str]:
    """(text, buttons, priority)"""
    from config.paths import COURSE_NAMES
    a = action['item']
    due = _due(a)
    course = COURSE_NAMES.get(a.get('course_key', ''), a.get('course_key', ''))
    title = a.get('title', 'מטלה')
    quiz = a.get('kind') == 'quiz'                    # 14/11: a quiz is done, not submitted; the quiz is the practice
    done_btn = ('✅ עשיתי' if quiz else '✅ הגשתי', f"hw:done:{action['key']}")
    prac_btn = ('🏋️ סט תרגול לקראתה', f"hw:prac:{action['key']}")
    row = [done_btn] if quiz else [done_btn, prac_btn]
    blind = '' if a.get('kind', 'assign') in ('assign', 'quiz') else \
        '\n_(את ההגשה של זו אני לא רואה במודל — לחץ ✅ כשהגשת.)_'
    not_yet = 'עוד לא רואה שעשית אותו.' if quiz else 'עוד לא רואה הגשה.'
    kind = action['kind']
    if kind == 'remind':
        left_h = (due - now).total_seconds() / 3600
        if action['stage'] == 'h6':
            hrs = max(1, round(left_h))
            return (f"🚨 *עוד {hrs} שעות*: {course} — {title} ({due:%H:%M}).\n{not_yet}{blind}",
                    [[done_btn]], 'urgent')
        if action['stage'] == 'd1':
            return (f"⏰ *{_when(due, now)}*: {course} — {title}.\n{not_yet}{blind}",
                    [row], 'normal')
        return (f"📌 {course} — *{title}*: {'נסגר' if quiz else 'הגשה'} {_when(due, now)}.{blind}", [row], 'normal')
    if kind == 'ack':
        return f"✅ ראיתי שהגשת את {title} ב{course}. יפה!", [], 'normal'
    if kind == 'moved':
        try:
            old = datetime.fromisoformat(action['old_due']).strftime('%d/%m %H:%M')
        except (TypeError, ValueError):
            old = '?'
        return f"📅 המועד של {title} ({course}) הוזז: {old} ← {due:%d/%m %H:%M}", [], 'normal'
    return (f"⚠️ {title} ({course}) — הדדליין עבר ({due:%d/%m %H:%M}) ולא ראיתי הגשה.\n"
            f"אם יש הגשה באיחור — עכשיו הזמן. הגשת בדרך אחרת? לחץ ✅.", [[done_btn]], 'normal')


# ── live re-check (Moodle) ──────────────────────────────────────────────────

def refresh_live(items: list[dict], now: datetime, cache: Path | None = None) -> list[dict]:
    """Re-read submission status + due date from Moodle for the assignments due soon
    (and recently passed), so reminders never rely on last night's scrape. Updates the cache
    file in place. On any failure returns the items unchanged."""
    soon = [a for a in items if a.get('url') and a.get('kind', 'assign') in ('assign', 'quiz')   # 14/11 + quizzes
            and not a.get('submitted') and _due(a)
            and -MISSED_WINDOW_H <= (_due(a) - now).total_seconds() / 3600 <= LIVE_HORIZON_H]
    if not soon:
        return items
    try:
        from collectors.moodle_assignment_scraper import login_and_get_session, fetch_assignment_details
        session = login_and_get_session()
        if not session:
            print('  ⚠️ deadlines: Moodle login failed — using last night\'s status')
            return items
        fresh = {}
        for a in soon:
            d = fetch_assignment_details(session, a['url']) or {}
            if d:
                fresh[key_of(a)] = d
    except Exception as e:
        print(f'  ⚠️ deadlines: live refresh failed ({e}) — using last night\'s status')
        return items
    out = []
    for a in items:
        d = fresh.get(key_of(a))
        if d:
            a = {**a, 'submitted': bool(d.get('submitted')), 'due_date': d.get('due_date') or a.get('due_date'),
                 **{k: d[k] for k in ('quiz_state', 'grade', 'graded', 'cutoff_date') if d.get(k) is not None}}
        out.append(a)
    try:                                                  # write back so the bot/briefing agree
        p = cache or CACHE
        c = json.loads(p.read_text(encoding='utf-8'))
        byk = {key_of(a): a for a in out}
        c['assignments'] = [byk.get(key_of(x), x) if isinstance(x, dict) else x for x in c.get('assignments') or []]
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(p)
    except Exception as e:
        print(f'  ⚠️ deadlines: cache write-back failed: {e}')
    if cache is None and any(d.get('submitted') for d in fresh.values()):
        try:                                              # just submitted → the course site says ✅ now, not tonight
            from scripts.build_course_site import build  # (this runs in a short cron process — no background thread)
            build()
        except Exception as e:
            print(f'  ⚠️ site rebuild: {e!r}'[:200])
    return out


def find(key: str, cache: Path | None = None) -> dict | None:
    return next((a for a in load_items(cache) if key_of(a) == key), None)
