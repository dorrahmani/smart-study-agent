"""
utils/llm_health.py — is Gemini reachable right now, and what a chat turn may spend while it isn't (19/11, emergency mode).

18/11: $39.33 of the $50 cap on the 18th, so the budget lock may really trigger this month. Until now a locked month, a
429 or a Gemini outage reached the owner as "⚠️ משהו נכשל (ClientError)" — or a 45 s wait on the local Ollama first.

    breaker   every paid call through the budget guard reports here (note()). Two outage errors in a row within
              10 minutes, or one quota error (429 / RESOURCE_EXHAUSTED), open it for a cooldown (2 min, doubling to
              15). After the cooldown the next chat turn tries Gemini again (half-open): a success closes it, a failure
              opens it for twice as long. State in data/llm_health.json, so a worker restart keeps it.
    turn()    agent/emergency wraps an owner chat turn in it. With block=True every model call in the turn fails at
              once (gate(): LLMPaused; gemini_sub.ask(): None) instead of waiting for a timeout, and the turn records
              that it needed one (t['blocked']) — then emergency answers from the data or says why it can't.
    ollama    the local qwen fallback (bot/brain.classify, rag/chat) is skipped inside a chat turn when Gemini is out,
              and anywhere when the model isn't loaded (a cold load is ~22 s on the VM's CPU, then 45 s timeouts).
No model is called here; nothing here sends anything.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'llm_health.json'
FAILS_TO_OPEN = 2
FAIL_WINDOW = 600                      # two outage errors this close together = an outage
COOLDOWN_MIN, COOLDOWN_MAX = 120, 900
TURN: ContextVar[dict | None] = ContextVar('ssa_llm_turn', default=None)
_lock = threading.Lock()

QUOTA_RX = re.compile(r'RESOURCE_EXHAUSTED|\b429\b|quota|rate.?limit', re.I)
OUTAGE_RX = re.compile(r'RESOURCE_EXHAUSTED|UNAVAILABLE|DEADLINE_EXCEEDED|INTERNAL|overloaded|\b(?:429|500|502|503|504)\b|'
                       r'quota|rate.?limit|ServerError|ConnectError|ConnectTimeout|ReadTimeout|TimeoutException|'
                       r'timed out|Temporary failure in name resolution|Connection (?:reset|refused|aborted)|'
                       r'RemoteProtocolError|ServiceUnavailable', re.I)


def is_outage(e: BaseException) -> bool:
    """A Gemini error that says "the service isn't answering now", not "your request is wrong" (400) or the lock."""
    from utils.budget import BudgetLocked
    if isinstance(e, BudgetLocked):
        return False
    code = getattr(e, 'code', None) or getattr(e, 'status_code', None)
    if isinstance(code, int):
        return code in (429, 500, 502, 503, 504)
    return isinstance(e, (TimeoutError, ConnectionError)) or bool(OUTAGE_RX.search(f'{type(e).__name__} {e}'))


# ── the breaker ─────────────────────────────────────────────────────────────

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
        tmp = p.with_name(f'{p.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(p)
    except OSError:
        pass


def note(ok: bool, err: BaseException | None = None, path: Path | None = None, now: float | None = None) -> None:
    """One paid call's outcome. Only outage errors count against Gemini; a bad request says nothing about it."""
    now = now or time.time()
    t = TURN.get()
    if not ok and err is not None and not is_outage(err):
        return
    with _lock:
        d = _load(path)
        if ok:
            if d.get('fails') or d.get('open_until'):
                d.update(fails=0, open_until=0, cooldown=0, since=0, recovered=now)
                _save(d, path)
            if t is not None:
                t['ok'] = True
            return
        if t is not None:
            t['failed'] = True
        recent = now - float(d.get('last_fail') or 0) < FAIL_WINDOW
        d['fails'] = (int(d.get('fails') or 0) + 1) if recent else 1
        d['last_fail'] = now
        d['last_error'] = f'{type(err).__name__}: {err}'[:200] if err else ''
        if d['fails'] >= FAILS_TO_OPEN or (err is not None and QUOTA_RX.search(f'{err}')):
            was = int(d.get('cooldown') or 0)                   # a failed retry after a cooldown: twice as long
            d['cooldown'] = min(COOLDOWN_MAX, was * 2) if was else COOLDOWN_MIN
            d['open_until'] = now + d['cooldown']
            if not d.get('since'):
                d['since'] = now
        _save(d, path)


def is_open(path: Path | None = None, now: float | None = None) -> bool:
    """Gemini is in its cooldown: don't try it this turn."""
    return float(_load(path).get('open_until') or 0) > (now or time.time())


def status(path: Path | None = None) -> dict:
    return _load(path)


# ── a chat turn ─────────────────────────────────────────────────────────────

@contextmanager
def turn(block: bool = False):
    """Around one owner chat turn → a dict: blocked (a model call was refused), failed (an outage error), ok."""
    t = {'block': bool(block), 'blocked': False, 'failed': False, 'ok': False}
    tok = TURN.set(t)
    try:
        yield t
    finally:
        TURN.reset(tok)


def blocking() -> bool:
    """This turn may not call a model. Marks the turn as having needed one."""
    t = TURN.get()
    if t is not None and t.get('block'):
        t['blocked'] = True
        return True
    return False


def mark_blocked() -> None:
    t = TURN.get()
    if t is not None:
        t['blocked'] = True


def in_turn() -> bool:
    return TURN.get() is not None


# ── the local Ollama fallback ───────────────────────────────────────────────

OLLAMA = 'http://127.0.0.1:11434'


def ollama_ok(model: str = 'qwen2.5:7b', probe=None) -> bool:
    """May a caller wait on the local model now? Not in a chat turn whose Gemini is out (The owner would wait 22-45 s for a
    reply that then needs Gemini anyway), and only when the model is already loaded (warm ~3 s; cold ~22 s)."""
    t = TURN.get()
    if t is not None and (t.get('block') or t.get('failed') or t.get('blocked')):
        return False
    try:
        if probe is not None:
            loaded = probe()
        else:
            import urllib.request
            with urllib.request.urlopen(f'{OLLAMA}/api/ps', timeout=1.5) as r:
                loaded = [m.get('name', '') for m in (json.loads(r.read().decode('utf-8')).get('models') or [])]
        return any(str(n).split(':')[0] == model.split(':')[0] for n in loaded)
    except Exception:
        return False
