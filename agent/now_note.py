"""
agent/now_note.py — "what's going on with the owner now": a short rolling note the assistant sees every turn.

03/11 (The owner: "כמה שיותר AGI, שאני ארגיש שאני מדבר עם בן אדם עם מוח"): a person remembers that this morning you said
you're not taking the exam, that you asked to fix the warm-up times, that this week is about the probability homework.
life.facts() holds the permanent things; this holds the current ones — decisions, open threads, requests — for a week.
No LLM call of its own: items come from what already happened (an exam decision, a feature request sent to Claude,
the assistant's own "thread" field in the call it makes anyway, the nightly review).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / 'data' / 'agent_now.json'
KEEP_DAYS = 7
MAX_ITEMS = 12
LABEL = {'decision': 'החלטה', 'correction': 'תיקון', 'thread': 'פתוח', 'request': 'ביקש', 'review': 'מהסקירה'}


def _load(path: Path | None) -> dict:
    try:
        return json.loads((path or PATH).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def add(chat: str, text: str, kind: str = 'thread', path: Path | None = None, now: float | None = None) -> None:
    """One line (deduped: the same text replaces its older copy); the newest MAX_ITEMS within KEEP_DAYS stay."""
    text = ' '.join((text or '').split())[:200]
    if not chat or not text:
        return
    now = now or time.time()
    d = _load(path)
    items = [i for i in d.get(chat) or [] if now - i.get('at', 0) < KEEP_DAYS * 86400 and i.get('text') != text]
    items.append({'text': text, 'kind': kind, 'at': now})
    d[chat] = items[-MAX_ITEMS:]
    p = path or PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(p)
    except OSError as e:
        print(f'  ⚠️ now note: {e}')


def text(chat: str, path: Path | None = None, now: float | None = None) -> str:
    """The note for the prompt: '• [03/11 החלטה] לא ניגש לבדידה 2 …' (oldest first), '(אין)' when empty."""
    now = now or time.time()
    items = [i for i in _load(path).get(chat) or [] if now - i.get('at', 0) < KEEP_DAYS * 86400]
    return '\n'.join(f"• [{time.strftime('%d/%m', time.localtime(i['at']))} {LABEL.get(i.get('kind'), '')}] {i['text']}"
                     for i in items) or '(אין)'
