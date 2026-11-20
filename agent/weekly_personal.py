"""
agent/weekly_personal.py — one short message a week about the owner's own week (17/11, the owner's improvement 6).

What he closed, what got pushed off, what's coming in the next 7 days, and his streaks. Built from the stores only —
no LLM, no network: data/obligations.json (Moodle homework/quizzes, agent/obligations), data/tasks.json (agent/tasks),
data/closed_items.json (agent/closure: what he closed in words), data/agent_conversations.jsonl (his "🌙 מחר" /
"⏰ עוד שעה" taps), data/agent_life.json and data/proactive_seen.json (to name what a tap pushed off).
Never a study question, never practice: only what he did and what he owes (feedback_no_unsolicited_study).

Sent by scripts/weekly_bot_report.py (the "Weekly Bot Report" cron, Sunday ~09:00 Israel — the start of his week),
as its own message before the bot report, once per ISO week (data/weekly_personal_sent.json), never in quiet hours.
    venv/bin/python3 scripts/weekly_bot_report.py --personal-dry     # print it, send nothing, mark nothing
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / 'data'
SENT = D / 'weekly_personal_sent.json'
POSTPONE_RX = re.compile(r'^(rmd:snooze|task:tomorrow|sec:(?:tom10|at1h)):(\w+)')
MAX_LINES = 6
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']


def _json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _dt(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v)[:19]) if v else None
    except ValueError:
        return None


def _jsonl(p: Path) -> list[dict]:
    try:
        out = []
        for ln in p.read_text(encoding='utf-8').splitlines()[-3000:]:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
        return out
    except OSError:
        return []


def _course(ck: str) -> str:
    try:
        from config.paths import COURSE_NAMES
        return COURSE_NAMES.get(ck, ck)
    except Exception:
        return ck


def _short(s: str, n: int = 60) -> str:
    s = re.sub(r'\s+', ' ', s or '').strip().strip('*')
    return s if len(s) <= n else s[:n - 1].rstrip() + '…'


def _day(t: datetime, now: datetime) -> str:
    d = (t.date() - now.date()).days
    return 'היום' if d == 0 else 'מחר' if d == 1 else f'{WEEKDAY_HE[t.weekday()]} {t:%d/%m}'


def _obl_label(r: dict) -> str:
    kind = 'בוחן' if r.get('kind') == 'quiz' else 'מטלה'
    return f"{_course(r.get('course', ''))}: {_short(r.get('title', ''), 40)} ({kind})"


def _missed(r: dict, now: datetime) -> bool:
    due = _dt(r.get('due'))
    return bool(due and due <= now and not r.get('closed') and r.get('state') != 'done' and not r.get('gone'))


def _done(r: dict) -> bool:
    return bool(r.get('closed') or r.get('state') == 'done') and r.get('closed_why', 'done') != 'irrelevant'


def collect(now: datetime | None = None, chat: str = '', paths: dict | None = None) -> dict:
    now = now or datetime.now()
    p = {'obligations': D / 'obligations.json', 'tasks': D / 'tasks.json', 'closed': D / 'closed_items.json',
         'convo': D / 'agent_conversations.jsonl', 'life': D / 'agent_life.json',
         'proactive': D / 'proactive_seen.json', 'grades': D / 'feedback', **(paths or {})}
    since, ahead = now - timedelta(days=7), now + timedelta(days=7)
    store = _json(p['obligations'], {})
    items = [r for r in (store.get('items') or {}).values() if isinstance(r, dict) and not r.get('gone')]
    done, pushed, coming, names = [], [], [], set()

    for r in items:                                     # Moodle: closed this week (not what was done long before)
        c, due = _dt(r.get('closed')), _dt(r.get('due'))
        if c and since <= c <= now and _done(r):
            if r.get('closed_by') == 'moodle' and due and due < since:
                continue                                # found done when the tracker started, due weeks ago
            done.append((c, _obl_label(r)))
            names.add(_short(r.get('title', ''), 40))
        elif _missed(r, now) and due and due >= since:
            late = _dt(r.get('cutoff'))
            pushed.append(f"⏳ {_obl_label(r)} — עבר המועד" + (f" (אפשר באיחור עד {_day(late, now)})"
                                                              if late and late > now else ''))
        elif not _done(r) and due and now < due <= ahead:
            coming.append((due, f"{_obl_label(r)} — {_day(due, now)} {due:%H:%M}"))

    tasks = [t for t in (_json(p['tasks'], {}).get('tasks') or []) if not chat or t.get('chat') == chat]
    for t in tasks:
        c = _dt(t.get('closed'))
        if t.get('status') == 'done' and c and since <= c <= now:
            done.append((c, _short(t.get('text', ''))))
            names.add(_short(t.get('text', '')))
        due = _dt(t.get('due'))
        if t.get('status') == 'open' and due:
            if now < due + timedelta(days=1) and due <= ahead:
                coming.append((due, f"{_short(t.get('text', ''))} — עד {_day(due, now)}"))
            elif due < now - timedelta(days=1) and due >= since:
                pushed.append(f"⏳ {_short(t.get('text', ''))} — היה עד {due:%d/%m}, עדיין פתוח")

    for c in _json(p['closed'], []) if isinstance(_json(p['closed'], []), list) else []:
        t = _dt(c.get('ts'))                            # what he closed in words (not the obligations' own records)
        if t and since <= t <= now and c.get('why') == 'done' and c.get('origin') == 'words' \
                and (not chat or c.get('chat') == chat):
            label = _short(c.get('text', ''), 50)
            if label not in names:
                done.append((t, label))

    pushed += postponed(now, since, p, tasks)
    for ck, xs in (store.get('exams') or {}).items():
        for x in xs or []:
            t = _dt(f"{x.get('date')}T{x.get('time') or '09:00'}")
            if t and now < t <= ahead:
                coming.append((t, f"🎯 מבחן ב{_course(ck)} — {_day(t, now)}" + (f" {x['time']}" if x.get('time') else '')))
    return {'since': since, 'now': now, 'done': [x for _, x in sorted(done)], 'pushed': pushed,
            'coming': [x for _, x in sorted(coming)], 'streaks': streaks(items, now),
            'grades': graded(p['grades'], since, now)}


def graded(d: Path, since: datetime, now: datetime) -> list[str]:
    """Grades / grader feedback that reached agent/grades' log this week (data/feedback/<course>.json). What the first
    run recorded silently (its backfill) isn't "this week". Only the grade; the grader's words are in their own message."""
    init = _dt(_json(Path(d) / '_state.json', {}).get('init'))
    out = []
    try:
        files = sorted(Path(d).glob('*.json'))
    except OSError:
        return []
    for f in files:
        if f.name.startswith('_'):
            continue
        for r in (_json(f, {}).get('items') or {}).values():
            t = _dt(r.get('updated'))
            if not isinstance(r, dict) or not t or not since <= t <= now or (init and t <= init + timedelta(minutes=5)):
                continue
            try:
                from agent.grades import grade_label
                label = grade_label(r)
            except Exception:
                label = r.get('label') or ''
            if label:
                out.append((t, f"{_course(r.get('course', ''))}: {_short(r.get('title', ''), 40)} — {label}"))
    return [x for _, x in sorted(out)]


def postponed(now: datetime, since: datetime, p: dict, tasks: list[dict]) -> list[str]:
    """His "🌙 מחר" / "⏰ עוד שעה" / "⏰ תזכיר מחר" taps this week, named, one line per thing."""
    life = {r.get('id'): r.get('text', '') for r in (_json(p['life'], {}).get('reminders') or [])}
    offers = {k: v.get('title', '') for k, v in ((_json(p['proactive'], {}) or {}).get('offers') or {}).items()}
    task_text = {t.get('id'): t.get('text', '') for t in tasks}
    count: dict[str, int] = {}
    for r in _jsonl(p['convo']):
        t = _dt(r.get('ts'))
        if r.get('role') != 'owner' or r.get('kind') != 'button' or not t or not since <= t <= now:
            continue
        m = POSTPONE_RX.match(str(r.get('in') or ''))
        if not m:
            continue
        what = {'rmd:snooze': life, 'task:tomorrow': task_text}.get(m.group(1), offers).get(m.group(2)) or 'תזכורת'
        count[_short(what, 50)] = count.get(_short(what, 50), 0) + 1
    return [f"⏭️ {k}" + (f" (דחית {n} פעמים)" if n > 1 else '') for k, n in count.items()]


def _week_start(d: date) -> date:
    return d - timedelta(days=(d.weekday() + 1) % 7)       # Sunday


def streaks(items: list[dict], now: datetime) -> list[str]:
    """Weeks in a row with nothing missed (a week with nothing due doesn't break it), and Software 1's quizzes done in
    a row (the ones whose close passed)."""
    out = []
    weeks: dict[date, list[dict]] = {}
    for r in items:
        due = _dt(r.get('due'))
        if due and due <= now and r.get('closed_why') != 'irrelevant':
            weeks.setdefault(_week_start(due.date()), []).append(r)
    n = 0
    for w in sorted(weeks, reverse=True):
        if any(not _done(r) for r in weeks[w]):
            break
        n += 1
    if n >= 2:
        out.append(f'🔥 {n} שבועות ברצף בלי לפספס אף הגשה או בוחן')
    quizzes = sorted((r for r in items if r.get('course') == 'software_1' and r.get('kind') == 'quiz'
                      and (_done(r) or (_dt(r.get('due')) or now) <= now)), key=lambda r: r.get('due') or '')
    q = 0
    for r in reversed(quizzes):
        if not _done(r):
            break
        q += 1
    if q >= 2:
        out.append(f'⭐ {q} בחני תוכנה 1 ברצף')
    return out


def build_text(r: dict) -> str:
    lines = [f"🗓️ *השבוע שלך* ({r['since']:%d/%m}–{r['now']:%d/%m})", '']
    if r['done']:
        lines.append(f"✅ *סגרת {len(r['done'])}:*")
        lines += [f'• {x}' for x in r['done'][:MAX_LINES]]
        if len(r['done']) > MAX_LINES:
            lines.append(f"• ועוד {len(r['done']) - MAX_LINES}")
    else:
        lines.append('✅ השבוע לא נסגר כלום ברשימות שלי')
    if r.get('grades'):
        lines += ['', '*📊 משובים שקיבלת:*'] + [f'• {x}' for x in r['grades'][:MAX_LINES]]
    if r['pushed']:
        lines += ['', '*נדחה / פתוח:*'] + [f'• {x}' for x in r['pushed'][:MAX_LINES]]
    lines += ['', '*🔜 ב-7 הימים הבאים:*']
    lines += [f'• {x}' for x in r['coming'][:MAX_LINES]] if r['coming'] else ['• אין הגשות או מבחנים ברשימות']
    if len(r['coming']) > MAX_LINES:
        lines.append(f"• ועוד {len(r['coming']) - MAX_LINES}")
    if r['streaks']:
        lines += [''] + r['streaks']
    return '\n'.join(lines)


def _week_key(d: date | None = None) -> str:
    y, w, _ = (d or date.today()).isocalendar()
    return f'{y}-W{w:02d}'


def send_once(chat: str, send, now: datetime | None = None, force: bool = False, sent_path: Path | None = None,
              text: str | None = None) -> str:
    """'sent' | 'already' | 'quiet' | 'failed'. Once per ISO week, never in quiet hours."""
    now = now or datetime.now()
    p = sent_path or SENT
    sent = _json(p, {})
    week = _week_key(now.date())
    if sent.get(week) and not force:
        return 'already'
    from utils.notify import in_quiet_hours
    if in_quiet_hours(now) and not force:
        return 'quiet'
    if not send(text if text is not None else build_text(collect(now, chat))):
        return 'failed'
    sent[week] = now.isoformat(timespec='seconds')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(sorted(sent.items())[-30:]), ensure_ascii=False, indent=1), encoding='utf-8')
    return 'sent'
