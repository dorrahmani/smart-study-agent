"""
utils/budget.py — the monthly budget lock, shared by every paid step.

scripts/budget_check.py writes data/budget_lock.json when monthly spend passes
the hard cap. Until 09/2026 only the summarizer honoured it, so paid audio
transcription (Whisper / gpt-4o-transcribe) kept spending after the lock.

07/11 audit: the lock lived in each call site and most had none, so text from anyone could spend past it. install_guard()
wraps generate_content once in a process: every API call there passes one gate. The owner's own message passes as it always did
(the sites he reaches check for themselves); everyone else, and background work, stops at the lock; the partner and contacts also
have a daily count. Subscription calls cost nothing and never get here: gemini_sub wraps this wrapper, so install this first.
19/11 (emergency mode, agent/emergency): The owner's own API calls stop at the lock too — the subscription above still answers
his plain chat free, and what needs the paid API waits for the 1st. Every call's outcome feeds utils/llm_health.
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parent.parent / 'data' / 'budget_lock.json'
CALLS_PATH = LOCK_PATH.parent / 'role_calls.json'
ROLE_CAPS = {'partner': 150, 'contact': 60}          # API calls a day, per role (a message takes 1-3)
ACTOR: ContextVar[str | None] = ContextVar('budget_actor', default=None)
_default_actor = 'system'
_count_lock = threading.Lock()


class BudgetLocked(RuntimeError):
    """No API call now. An Exception, so what already survives a failed call survives this."""


class LLMPaused(BudgetLocked):
    """19/11 emergency mode: this chat turn may not call a model (Gemini is out or the month is locked)."""


@contextmanager
def acting_for(role: str | None):
    """Whose message this work answers. Nothing set = background work, which isn't the owner speaking."""
    tok = ACTOR.set(role or 'contact')
    try:
        yield
    finally:
        ACTOR.reset(tok)


def _take_one(role: str, cap: int, path: Path | None = None, today: str | None = None) -> bool:
    """One more call of this role today, or False. An unreadable count starts again: a cap, not a security boundary."""
    p, day = path or CALLS_PATH, today or date.today().isoformat()
    with _count_lock:
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
            n = d.get(day) if isinstance(d, dict) else None
            n = n if isinstance(n, dict) else {}
        except (OSError, ValueError):
            n = {}
        if int(n.get(role) or 0) >= cap:
            return False
        n[role] = int(n.get(role) or 0) + 1
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps({day: n}), encoding='utf-8')
        os.replace(tmp, p)
    return True


def gate(actor: str | None = None) -> None:
    """Before a paid call. Raises BudgetLocked, or returns."""
    from utils import llm_health
    if llm_health.blocking() or llm_health.in_turn() and llm_health.is_open():   # 19/11 emergency mode: fail now,
        llm_health.mark_blocked()                                                # not after a timeout / more retries
        raise LLMPaused('emergency mode: no model calls this turn')
    who = actor or ACTOR.get() or _default_actor
    reason = budget_lock_reason()
    if reason:
        # 19/11: The owner's own turns stop at the lock too (the cap is hard; $39.33 on the 18th). The subscription sits
        # above this guard and still answers his plain chat for free; what needs the paid API waits for the 1st.
        llm_health.mark_blocked()
        raise BudgetLocked(f'locked for {who}: {reason}')
    if who == 'owner':
        return
    cap = ROLE_CAPS.get(who)
    if cap is not None and not _take_one(who, cap):
        raise BudgetLocked(f'{who}: daily cap of {cap} calls')


def _guarded(original):
    def guarded(self, *a, **kw):
        gate()
        from utils import llm_health
        try:
            r = original(self, *a, **kw)
        except Exception as e:                        # 19/11 an outage error opens the breaker (utils/llm_health)
            llm_health.note(False, e)
            raise
        llm_health.note(True)
        return r

    guarded._ssa_budget = True
    return guarded


def install_guard(default_actor: str = 'system') -> None:
    """Once per process, before gemini_sub.install(). default_actor='owner' only where nobody else can reach (Telegram)."""
    global _default_actor
    _default_actor = default_actor
    from google.genai import models as _models
    if not getattr(_models.Models.generate_content, '_ssa_budget', False):
        _models.Models.generate_content = _guarded(_models.Models.generate_content)


def budget_lock_reason(lock_path: Path = LOCK_PATH) -> str | None:
    """Reason string if spending is locked for the CURRENT month, else None.
    A lock from a previous month is stale (budgets reset on the 1st): it is
    removed so spending resumes automatically."""
    lock = Path(lock_path)
    if not lock.exists():
        return None
    try:
        d = json.loads(lock.read_text(encoding='utf-8'))
        lock_month = d.get('month', '')
        if lock_month and lock_month != datetime.utcnow().strftime('%Y-%m'):
            try:
                lock.unlink()
            except Exception:
                pass
            return None
        return d.get('reason', 'budget locked')
    except Exception:
        return 'budget lock file exists'


# ── budget pace (18/11) ─────────────────────────────────────────────────────
# 18/11: $39.33 of the $50 cap on the 18th. The lock stops everything at $50; before that, work nothing waits for (the
# study site's pages) stays off the paid API while the month is spending faster than cap / days. The owner's turns, lecture
# summaries and transcription are not deferred by this.
MONTH_CAP_USD = float(os.environ.get('SSA_MONTH_CAP_USD', '50'))      # the default; a month of its own in CAPS_PATH
CAPS_PATH = LOCK_PATH.parent.parent / 'config' / 'budget_caps.json'
_PACE: dict = {'at': 0.0, 'spent': 0.0, 'month': ''}


def month_cap(month: str | None = None, default: float | None = None, path: Path | None = None) -> float:
    """19/11 the hard cap of a month ('YYYY-MM'): config/budget_caps.json {"months": {"2026-12": 20}}, else `default`
    (scripts/budget_check.HARD_LIMIT_USD), else SSA_MONTH_CAP_USD / 50. A cap set below what the month already spent
    locks at the next budget check, so a lower cap goes on a month that hasn't started."""
    month = month or datetime.utcnow().strftime('%Y-%m')
    try:
        d = json.loads((path or CAPS_PATH).read_text(encoding='utf-8'))
        v = (d.get('months') or {}).get(month)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    except (OSError, ValueError, AttributeError):
        pass
    return float(default if default is not None else MONTH_CAP_USD)


def month_spend(ledger: Path | None = None, now: datetime | None = None) -> float:
    """This month's ledger total (UTC month, as scripts/budget_check.py counts it). Cached 10 minutes in a process."""
    import time
    now = now or datetime.utcnow()
    month = now.strftime('%Y-%m')
    if ledger is None and _PACE['month'] == month and time.time() - _PACE['at'] < 600:
        return _PACE['spent']
    path = ledger or LOCK_PATH.parent / 'usage.jsonl'
    total = 0.0
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for ln in f:
                if ln.startswith('{"ts": "' + month):
                    try:
                        total += float(json.loads(ln).get('cost_usd') or 0)
                    except ValueError:
                        pass
    except OSError:
        return 0.0
    if ledger is None:
        _PACE.update(at=time.time(), spent=total, month=month)
    return total


def ahead_of_pace(now: datetime | None = None, cap: float | None = None, spent: float | None = None) -> bool:
    """True when this month's spend so far is more than its share of the cap (cap × the part of the month gone).
    Unreadable ledger → False: this only defers optional work, it is not the lock."""
    import calendar
    now = now or datetime.utcnow()
    cap = month_cap(now.strftime('%Y-%m')) if cap is None else cap
    try:
        spent = month_spend(now=now) if spent is None else spent
    except Exception:
        return False
    days = calendar.monthrange(now.year, now.month)[1]
    gone = (now.day - 1 + (now.hour * 3600 + now.minute * 60) / 86400) / days
    return spent > cap * max(gone, 1 / days)

