"""
utils/summary_feedback.py — one feedback loop for summaries (dashboard + Telegram).

Ratings live in data/ratings.json, keyed "<course>/<lectures|tutorials>/<file.md>"
(the dashboard's existing format): {"rating": "up"|"down", "notes": str,
"feedback_pending": bool}. Until 10/2026 only main.py (manual runs) read pending
notes — the nightly pipeline never did, so feedback was silently ignored.
Now generate_summary() pulls a course's pending notes into the next summary of
that course and marks them consumed.

Telegram buttons carry a short id (callback_data max 64 bytes): data/rating_index.json
maps it back to the rating key.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RATINGS = ROOT / 'data' / 'ratings.json'
INDEX = ROOT / 'data' / 'rating_index.json'

UP_NOTE = 'הסיכום הקודם בקורס הזה היה מצוין (👍). שמור על אותו סגנון, רמת פירוט ומבנה.'


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    except Exception:
        return {}


def _save(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def rating_key(course_key: str, sub: str, filename: str) -> str:
    return f'{course_key}/{sub}/{filename}'


def short_id(key: str) -> str:
    """Stable 10-char id for Telegram callback_data; remembered in the index."""
    sid = hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]
    idx = _load(INDEX)
    if idx.get(sid) != key:
        idx[sid] = key
        _save(INDEX, idx)
    return sid


def key_for(sid: str) -> str | None:
    return _load(INDEX).get(sid)


def record(key: str, rating: str | None = None, note: str | None = None,
           ratings_path: Path = RATINGS) -> dict:
    """Set a rating and/or note; anything recorded becomes pending for the
    course's next summary."""
    r = _load(ratings_path)
    cur = dict(r.get(key, {}))
    if rating in ('up', 'down'):
        cur['rating'] = rating
        if rating == 'up' and not note:
            cur['notes'] = UP_NOTE
    if note:
        cur['notes'] = note.strip()[:1000]
    cur['feedback_pending'] = True
    cur['updated'] = datetime.now().isoformat(timespec='seconds')
    r[key] = cur
    _save(ratings_path, r)
    return cur


def pending_feedback(course_key: str, ratings_path: Path = RATINGS) -> str:
    """All pending notes for a course, newest last, as one instruction block."""
    if not course_key:
        return ''
    r = _load(ratings_path)
    items = [(v.get('updated', ''), k, v) for k, v in r.items()
             if k.startswith(f'{course_key}/') and v.get('feedback_pending') and v.get('notes')]
    items.sort()
    lines = []
    for _, k, v in items[-5:]:
        mark = '👍' if v.get('rating') == 'up' else ('👎' if v.get('rating') == 'down' else '📝')
        lines.append(f'- {mark} ({k.split("/")[-1][:60]}): {v["notes"]}')
    return '\n'.join(lines)


def mark_consumed(course_key: str, ratings_path: Path = RATINGS) -> int:
    r = _load(ratings_path)
    n = 0
    for k, v in r.items():
        if k.startswith(f'{course_key}/') and v.get('feedback_pending'):
            v['feedback_pending'] = False
            n += 1
    if n:
        _save(ratings_path, r)
    return n
