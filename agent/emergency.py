"""
agent/emergency.py — the owner's chat when Gemini can't answer (19/11, improvement 18).

18/11: $39.33 of the $50 cap on the 18th — the lock may really trigger this month. Then (or in a Gemini outage / 429)
the bot must not go quiet, wait 45 s on Ollama or say "⚠️ משהו נכשל (ClientError)":

    reason()   'budget'  the month is locked (utils/budget) and the subscription can't take the turn either
               'down'    the breaker is open (utils/llm_health: outage errors) and the subscription can't take it
               None      try as usual (the subscription answers plain chat free even in a locked month)
    run()      one owner turn inside llm_health.turn(block=reason is not None). With a reason every model call fails
               at once, so the deterministic handlers (fast path, reminders, closures, buttons, grades…) still answer
               and only a turn that needed a model is caught. Such a turn — or one whose model call just failed with an
               outage error — gets the data answer closest to the question (loose(): his day / week / deadlines / next
               session / reminders) and the honest line: in full once per episode, short after that.
               The first answer after the episode ends starts with "✅ חזרתי לעבוד כרגיל" (only if he was told).
Recovery is automatic: the lock clears itself on the 1st; the breaker lets the next turn try Gemini after its cooldown.
State: data/emergency.json (the current episode: kind, since, told). No model, nothing sent from here.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'emergency.json'

WORKS = 'בינתיים אני עונה מהנתונים: הלו״ז (היום / מחר / השבוע), מה מחכה לי, ההגשות הקרובות, איפה ומתי השיעור, ותזכורות.'
FULL = {'budget': '⏸️ התשובות החכמות שלי מושבתות עד ה-1 בחודש — הגענו לתקרת התקציב החודשית של Gemini.\n' + WORKS,
        'down': '⏸️ Gemini לא עונה כרגע, אז התשובות החכמות מושבתות לזמן קצר. אני מנסה שוב לבד.\n' + WORKS}
SHORT = {'budget': '⏸️ את זה אענה רק מה-1 בחודש (תקרת התקציב). אפשר לשאול על הלו״ז, הגשות, מה מחכה לי או השיעור הבא.',
         'down': '⏸️ את זה אענה כש-Gemini יחזור. אפשר לשאול על הלו״ז, הגשות, מה מחכה לי או השיעור הבא.'}
BACK = '✅ חזרתי לעבוד כרגיל.'


def _sub_ok() -> bool:
    if os.environ.get('SSA_BOT_VIA_GEMINI_SUB') != '1':
        return False
    try:
        from utils import gemini_sub
        return gemini_sub.available()
    except Exception:
        return False


def reason(now: datetime | None = None, lock=None, is_open=None, sub_ok=None) -> str | None:
    from utils import budget, llm_health
    sub = (sub_ok or _sub_ok)()
    if (lock or budget.budget_lock_reason)() and not sub:
        return 'budget'
    if (is_open or llm_health.is_open)() and not sub:
        return 'down'
    return None


def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STATE).read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict, path: Path | None = None) -> None:
    p = path or STATE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(p)
    except OSError:
        pass


def honest(kind: str, now: datetime | None = None, path: Path | None = None) -> str:
    """The line for a turn that needed a model: in full once per episode, short after."""
    now = now or datetime.now()
    d = _load(path)
    ep = d.get('episode') or {}
    if ep.get('kind') != kind or not ep.get('told'):
        d['episode'] = {'kind': kind, 'since': ep.get('since') if ep.get('kind') == kind else now.isoformat(timespec='seconds'),
                        'told': now.isoformat(timespec='seconds')}
        d.setdefault('log', []).append({'ts': now.isoformat(timespec='seconds'), 'kind': kind})
        d['log'] = d['log'][-50:]
        _save(d, path)
        return FULL[kind]
    return SHORT[kind]


def recovered(path: Path | None = None) -> str:
    """'' or the one "back" line, when an episode he was told about is over (and closes it)."""
    d = _load(path)
    if not (d.get('episode') or {}).get('told'):
        return ''
    d['episode'] = {}
    _save(d, path)
    return BACK


# ── the nearest data answer ─────────────────────────────────────────────────

_DATA_Q = re.compile(r'לוז|לו"ז|לו״ז|יש לי|מתוכנן|ביומן|תוכני|מה קורה|פנוי')
_DUE = re.compile(r'הגש|דדליי|מטל[הות]|להגיש|תרגיל בית|עבודה להגשה')
_SESSION = re.compile(r'(?<![א-ת])[הב]?(?:שיעור|הרצאה|תרגול|תרגיל)(?![א-ת])')
_WHEREWHEN = re.compile(r'איפה|מתי|באיזה (?:חדר|בניין|שעה)|יש\s')
_REMINDERS = re.compile(r'תזכורות|מה (?:אתה )?(?:צריך )?להזכיר')


def loose(text: str, chat: str, now: datetime | None = None) -> str | None:
    """The data answer nearest to a question the fast path didn't take whole ("מה יש לי מחר בערב?") — or None."""
    from agent import fastpath
    now = now or datetime.now()
    t = fastpath.norm(text)
    if not t or len(t) > 200:
        return None
    try:
        if _REMINDERS.search(t):
            from agent import life
            return life.list_reminders(chat)
        if 'מחכה לי' in t:
            return fastpath.waiting_answer(chat, now)
        if _DUE.search(t) and not _SESSION.search(t.replace('תרגיל בית', '')):
            return fastpath.deadline_answer(chat, now, 'השבוע' if 'השבוע' in t else 'מחר' if 'מחר' in t else
                                            'היום' if 'היום' in t else '')
        if (m := _SESSION.search(t)) and _WHEREWHEN.search(t):
            from agent import factcheck
            kind = re.sub(r'^[הב]', '', m.group(0))
            course = t if factcheck._ck_in(t, factcheck._course_words()) else ''
            return fastpath.next_answer(now, kind, course)
        if _DATA_Q.search(t) or re.search(r'היום|מחר|מחרתיים|השבוע', t):
            if 'השבוע' in t:
                return fastpath.week_answer(chat, now, 'הבא' in t)
            day = 'מחרתיים' if 'מחרתיים' in t else 'מחר' if 'מחר' in t else 'היום' if 'היום' in t else ''
            if m := re.search(r'(?:ב|ל)?יום\s+(?:ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)', t):
                day = m.group(0)
            if day:
                return fastpath.day_answer(chat, day, now)
    except Exception as e:
        print(f'  ⚠️ emergency loose answer: {e!r}'[:200])
    return None


# ── one turn ────────────────────────────────────────────────────────────────

def _keeps(outs: list) -> bool:
    """A turn that prepared something for his ✅ (buttons) or a file keeps its answer."""
    return any(getattr(o, 'kind', '') in ('buttons', 'document') for o in outs or [])


def run(msg, route, now: datetime | None = None, path: Path | None = None, why=None) -> list:
    """route() → its outs, or — when the turn needed a model it couldn't have — the data answer + the honest line."""
    from agent.core import Out
    from utils import llm_health
    from utils.budget import BudgetLocked
    kind = (why or reason)()
    with llm_health.turn(block=kind is not None) as t:
        try:
            outs = route()
            err = None
        except Exception as e:
            if not (isinstance(e, BudgetLocked) or llm_health.is_outage(e) or t['blocked'] or t['failed']):
                raise
            outs, err = [], e
    degraded = t['blocked'] or (t['failed'] and not t['ok']) or err is not None
    if not degraded:
        back = recovered(path) if kind is None else ''
        if back and outs and outs[0].kind in ('text', 'buttons'):
            outs[0] = Out(outs[0].kind, f'{back}\n\n{outs[0].text}', outs[0].path, outs[0].buttons)
        return outs
    if outs and _keeps(outs) and err is None:
        return outs
    if kind is None:                                  # it failed in this very turn
        from utils import budget
        kind = 'budget' if budget.budget_lock_reason() else 'down'
    print(f'  ⏸️ emergency mode ({kind}): a turn that needed a model')
    data = loose(msg.text or '', msg.sender, now) if msg.kind == 'text' else None
    line = honest(kind, now, path)
    return [Out('text', f'{data}\n\n{line}' if data else line)]


def status_line(path: Path | None = None) -> str:
    """For the health report: '' when all is well."""
    from utils import llm_health
    k = reason()
    st = llm_health.status()
    if not k:
        return ''
    since = st.get('since')
    when = f" מאז {datetime.fromtimestamp(since):%H:%M}" if k == 'down' and since else ''
    return {'budget': '⏸️ מצב חירום: תקרת התקציב — עונה רק מהנתונים עד ה-1.',
            'down': f'⏸️ מצב חירום: Gemini לא זמין{when} — עונה רק מהנתונים.'}[k]
