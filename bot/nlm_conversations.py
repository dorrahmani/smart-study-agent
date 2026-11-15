"""bot/nlm_conversations.py — Per-user conversation_id persistence for NotebookLM.

Stores conversation_id per (chat_id, course_key) with TTL.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
CONV_PATH = ROOT / 'data' / 'nlm_conversations.json'

TTL_MINUTES = 30


def _load() -> dict:
    if not CONV_PATH.exists(): return {}
    try:
        return json.loads(CONV_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save(d: dict) -> None:
    CONV_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONV_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                         encoding='utf-8')


def _key(chat_id: int, course_key: str, scope: str) -> str:
    return f'{chat_id}::{course_key}::{scope}'


def get(chat_id: int, course_key: str, scope: str) -> Optional[str]:
    """Returns conversation_id if not expired, else None."""
    d = _load()
    rec = d.get(_key(chat_id, course_key, scope))
    if not rec: return None
    try:
        last = datetime.fromisoformat(rec.get('updated', ''))
    except Exception:
        return None
    age_min = (datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)).total_seconds() / 60
    if age_min > TTL_MINUTES:
        return None
    return rec.get('conversation_id')


def update(chat_id: int, course_key: str, scope: str, conversation_id: str) -> None:
    d = _load()
    d[_key(chat_id, course_key, scope)] = {
        'conversation_id': conversation_id,
        'updated': datetime.now(timezone.utc).isoformat(),
    }
    # Garbage collect expired entries opportunistically
    now = datetime.now(timezone.utc)
    expired = []
    for k, v in d.items():
        try:
            last = datetime.fromisoformat(v.get('updated', '')).replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() / 60 > TTL_MINUTES * 4:
                expired.append(k)
        except Exception:
            expired.append(k)
    for k in expired: d.pop(k, None)
    _save(d)


def reset(chat_id: int, course_key: Optional[str] = None) -> int:
    """Clear conversations for this user. If course_key given, only that course.
    Returns number of conversations cleared."""
    d = _load()
    prefix = f'{chat_id}::'
    if course_key:
        prefix = f'{chat_id}::{course_key}::'
    to_remove = [k for k in d if k.startswith(prefix)]
    for k in to_remove: d.pop(k, None)
    _save(d)
    return len(to_remove)
