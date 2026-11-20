"""
agent/load_radar.py — the deadline load radar (19/11, secretary item 13). Deterministic, no LLM.

Looks 10 days ahead. A week (Sunday–Saturday, the Israeli week) with 3+ deadlines — homework, quizzes, exam
registrations still open in agent/obligations' store, plus the university's exam dates — gets ONE heads-up with the
list, once per week-at-risk (data/load_radar.json). Only what he owes and when: an obligation nudge, never a study
question or a practice offer. Daytime only (09:00–21:00), and the worker's batch runs outside quiet hours anyway.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'load_radar.json'
AHEAD_DAYS = 10
MIN_LOAD = 3
HOURS = (9, 21)
WEEKDAY_HE = ['ב׳', 'ג׳', 'ד׳', 'ה׳', 'ו׳', 'ש׳', 'א׳']


def week_start(d: date) -> date:
    """The Sunday that opens d's week."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def deadlines(store: dict, now: datetime) -> list[dict]:
    """[{at, what, course, kind}] — every open dated obligation and every exam from now to AHEAD_DAYS on. Pure."""
    from agent import obligations as ob
    end = now + timedelta(days=AHEAD_DAYS)
    out = []
    for r in ob.open_items(store, now):
        at = ob._dt(r.get('due'))
        if not at or not now <= at <= end or ob.status(r, now) != 'open':
            continue
        out.append({'at': at, 'what': r.get('title', ''), 'course': ob._course_name(r.get('course', '')),
                    'kind': ob._kind_he(r), 'est': bool(r.get('due_est'))})
    for ck, xs in (store.get('exams') or {}).items():
        for x in xs or []:
            try:
                at = datetime.fromisoformat(f"{x['date']}T{x.get('time') or '09:00'}")
            except (KeyError, ValueError):
                continue
            if now <= at <= end:
                moed = f"מועד {x['moed']}'" if x.get('moed') else 'מבחן'
                out.append({'at': at, 'what': moed, 'course': ob._course_name(ck), 'kind': 'מבחן', 'est': False})
    return sorted(out, key=lambda x: x['at'])


def heavy_weeks(items: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for x in items:
        by.setdefault(week_start(x['at'].date()).isoformat(), []).append(x)
    return {w: xs for w, xs in by.items() if len(xs) >= MIN_LOAD}


def text_for(week: str, xs: list[dict], now: datetime) -> str:
    w0 = date.fromisoformat(week)
    w1 = w0 + timedelta(days=6)
    when = 'השבוע' if w0 <= now.date() <= w1 else 'השבוע הבא' if w0 - now.date() <= timedelta(days=7) else 'בשבוע'
    lines = [f"• {WEEKDAY_HE[x['at'].weekday()]} {x['at']:%d/%m}"
             + (f" {x['at']:%H:%M}" if (x['at'].hour, x['at'].minute) != (23, 59) else '')
             + f" — {x['course']}: {x['what']} ({x['kind']}{', בערך' if x.get('est') else ''})" for x in xs[:10]]
    return (f"📊 *שבוע עמוס בדרך* — {when} ({w0:%d/%m}–{w1:%d/%m}) יש {len(xs)} הגשות/מבחנים:\n" + '\n'.join(lines)
            + '\n\nשווה לסגור משהו מזה מוקדם.')


def _load(path: Path | None) -> dict:
    try:
        return json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def tick(now: datetime | None = None, store: dict | None = None, path: Path | None = None) -> list[str]:
    """→ the heads-up texts to send now (each week at risk once). Marks them flagged."""
    now = now or datetime.now()
    if not HOURS[0] <= now.hour < HOURS[1]:
        return []
    if store is None:
        from agent import obligations
        store = obligations.load()
    st = _load(path)
    flagged = st.setdefault('flagged', {})
    out = []
    for week, xs in sorted(heavy_weeks(deadlines(store, now)).items()):
        if week in flagged:
            continue
        flagged[week] = now.isoformat(timespec='minutes')
        out.append(text_for(week, xs, now))
    if out:
        cut = (now.date() - timedelta(days=60)).isoformat()
        st['flagged'] = {w: v for w, v in flagged.items() if w >= cut}
        p = path or STATE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')
    return out
