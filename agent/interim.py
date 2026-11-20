"""
agent/interim.py — at most one short "רגע, בודק…" line per turn, and only when the turn will clearly take long.

17/11 a course question now waits up to 45s for the course's NotebookLM notebook (agent/course_lookup.NOTEBOOK_SECONDS).
Before that the turn could send several "working" lines (the worker's '📎 קורא…', the loop's '🔎 בודק רגע…' after 7s,
'🎓 שואל את NotebookLM…') or none at all. The worker opens a turn for the owner's private chat (begin); whoever is about to do
the slow thing calls say(); the first one wins and every later one in the same turn is silent. Outside a turn (tests, the
daily jobs, a group) say() sends nothing.
"""
from __future__ import annotations

import contextvars
import threading

COURSE = 'רגע, בודק בחומר של הקורס… 📚'

_TURN: contextvars.ContextVar[dict | None] = contextvars.ContextVar('ssa_interim_turn', default=None)
_LOCK = threading.Lock()


def begin(chat: str) -> dict:
    """A new turn for this chat. → its state (a timer thread that has no copy of the context passes it to say)."""
    st = {'chat': chat, 'sent': ''}
    _TURN.set(st)
    return st


def end() -> None:
    _TURN.set(None)


def current() -> dict | None:
    return _TURN.get()


def sent() -> str:
    st = _TURN.get()
    return (st or {}).get('sent', '')


def say(text: str, state: dict | None = None, send=None) -> bool:
    """Send `text` now if this turn hasn't sent its one line yet. → True when it went out. Never raises."""
    st = state if state is not None else _TURN.get()
    if not st or not text or '@' not in (st.get('chat') or '') or st['chat'].endswith('@g.us'):
        return False
    with _LOCK:
        if st.get('sent'):
            return False
        st['sent'] = text
    try:
        if send is None:
            from whatsapp import outbox
            send = outbox.send_text
        send(st['chat'], text)
        return True
    except Exception as e:
        print(f'  ⚠️ interim line: {e!r}'[:160])
        return False
