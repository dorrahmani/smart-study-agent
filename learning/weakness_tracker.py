"""
learning/weakness_tracker.py — Track which topics/concepts the user is weak on.

Signal sources:
  - User asks for a video/quiz/podcast/explanation on topic X → +0.15
  - User fails a quiz question on concept C → +0.20
  - Time decay: each week, scores decay by 30% (so old weaknesses fade)

Storage: data/weakness_scores.json
    {
        "concepts": {"recursion": 0.65, "big_o": 0.32},
        "course_topics": {"intro_to_cs::while_loops": 0.4},
        "last_decay": "2026-04-28"
    }

API:
    log_request(course_key, topic_or_concept, weight=0.15)
    log_quiz_failure(concept, weight=0.20)
    decay_if_stale()
    get_score(course_key, topic_or_concept) -> float
    get_top_weaknesses(n=5) -> list[(label, score)]
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Optional

from config.paths import WEAKNESS_PATH


_DEFAULT = {'concepts': {}, 'course_topics': {}, 'last_decay': None}


def _load() -> dict:
    if WEAKNESS_PATH.exists():
        try:
            d = json.loads(WEAKNESS_PATH.read_text(encoding='utf-8'))
            for k in _DEFAULT:
                d.setdefault(k, _DEFAULT[k])
            return d
        except Exception:
            pass
    return dict(_DEFAULT)


def _save(d: dict) -> None:
    WEAKNESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEAKNESS_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                             encoding='utf-8')


def _bump(d: dict, key: str, store: str, weight: float) -> None:
    cur = d[store].get(key, 0.0)
    new = min(1.0, cur + weight)
    d[store][key] = round(new, 4)


def log_request(course_key: str, topic_or_concept: str, weight: float = 0.15) -> None:
    d = _load()
    decay_if_stale(d, save=False)
    _bump(d, topic_or_concept, 'concepts', weight)
    if course_key:
        _bump(d, f'{course_key}::{topic_or_concept}', 'course_topics', weight)
    _save(d)


def log_quiz_failure(concept: str, weight: float = 0.20) -> None:
    d = _load()
    decay_if_stale(d, save=False)
    _bump(d, concept, 'concepts', weight)
    _save(d)


def decay_if_stale(d: Optional[dict] = None, save: bool = True) -> None:
    """If a week passed since last_decay, multiply all scores by 0.7."""
    own_load = d is None
    if own_load:
        d = _load()
    last = d.get('last_decay')
    today = datetime.now().date()
    do_decay = False
    if not last:
        do_decay = True
    else:
        try:
            last_d = datetime.strptime(last, '%Y-%m-%d').date()
            if (today - last_d) >= timedelta(days=7):
                do_decay = True
        except ValueError:
            do_decay = True
    if do_decay:
        for store in ('concepts', 'course_topics'):
            for k in list(d[store].keys()):
                v = d[store][k] * 0.7
                if v < 0.05:
                    del d[store][k]
                else:
                    d[store][k] = round(v, 4)
        d['last_decay'] = today.isoformat()
        if save and own_load:
            _save(d)


def get_score(course_key: str, topic_or_concept: str) -> float:
    d = _load()
    a = d['concepts'].get(topic_or_concept, 0.0)
    b = d['course_topics'].get(f'{course_key}::{topic_or_concept}', 0.0) if course_key else 0.0
    return max(a, b)


def get_top_weaknesses(n: int = 5) -> list[tuple[str, float]]:
    d = _load()
    decay_if_stale(d)
    merged: dict[str, float] = dict(d['concepts'])
    for k, v in d['course_topics'].items():
        merged[k] = max(merged.get(k, 0.0), v)
    return sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:n]
