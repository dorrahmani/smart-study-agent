"""
utils/budget.py — the monthly budget lock, shared by every paid step.

scripts/budget_check.py writes data/budget_lock.json when monthly spend passes
the hard cap. Until 09/2026 only the summarizer honoured it, so paid audio
transcription (Whisper / gpt-4o-transcribe) kept spending after the lock.

07/11 audit: the lock lived in each call site and most had none, so text from anyone could spend past it. install_guard()
wraps generate_content once in a process: every API call there passes one gate. The owner's own message passes as it always did
(the sites he reaches check for themselves); everyone else, and background work, stops at the lock; the partner and contacts also
have a daily count. Subscription calls cost nothing and never get here: gemini_sub wraps this wrapper, so install this first.
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
    who = actor or ACTOR.get() or _default_actor
    if who == 'owner':
        return
    reason = budget_lock_reason()
    if reason:
        raise BudgetLocked(f'locked for {who}: {reason}')
    cap = ROLE_CAPS.get(who)
    if cap is not None and not _take_one(who, cap):
        raise BudgetLocked(f'{who}: daily cap of {cap} calls')


def _guarded(original):
    def guarded(self, *a, **kw):
        gate()
        return original(self, *a, **kw)

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
