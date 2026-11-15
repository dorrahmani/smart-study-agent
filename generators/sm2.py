"""generators/sm2.py — SM-2 spaced repetition scheduler.

Card state: {ease (float, default 2.5), interval (days), repetitions, last_review, next_review}
Grade 0-5 from user; algorithm computes the next interval.

Persists deck at data/flashcards_deck.json.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECK_PATH = ROOT / 'data' / 'flashcards_deck.json'


def _load() -> list[dict]:
    if not DECK_PATH.exists(): return []
    try:
        return json.loads(DECK_PATH.read_text(encoding='utf-8'))
    except Exception:
        return []


def _save(deck: list[dict]) -> None:
    DECK_PATH.parent.mkdir(parents=True, exist_ok=True)
    DECK_PATH.write_text(json.dumps(deck, ensure_ascii=False, indent=2),
                         encoding='utf-8')


def add_cards(new_cards: list[dict]) -> int:
    """Add new cards (skip duplicates by question text). Returns count added."""
    deck = _load()
    existing = {c.get('q') for c in deck}
    today = date.today().isoformat()
    added = 0
    for c in new_cards:
        if c.get('q') in existing: continue
        deck.append({
            **c,
            'id':           f"c{len(deck)+added+1:06d}",
            'ease':         2.5,
            'interval':     0,
            'repetitions':  0,
            'last_review':  None,
            'next_review':  today,  # due immediately
        })
        added += 1
    _save(deck)
    return added


def due_cards(limit: int = 3, courses: set | None = None) -> list[dict]:
    """Return cards due today or earlier. Mixes the order so user doesn't memorise sequence.
    `courses`: only cards of these course_keys (None = all)."""
    deck = _load()
    today_s = date.today().isoformat()
    due = [c for c in deck if (c.get('next_review') or '0000-01-01') <= today_s
           and (courses is None or c.get('course_key') in courses)]
    random.shuffle(due)
    return due[:limit]


# grade (0-5) → mastery outcome (0-1): "hard but right" is not mastery yet
GRADE_OUTCOME = {0: 0.0, 1: 0.0, 2: 0.2, 3: 0.6, 4: 0.85, 5: 1.0}


def update_card(card_id: str, grade: int) -> dict | None:
    """SM-2 update. grade ∈ [0, 5]:
        0-2 = wrong (reset)
        3   = correct but hard
        4   = correct
        5   = correct, easy
    Returns the updated card or None if not found."""
    deck = _load()
    for c in deck:
        if c.get('id') != card_id: continue
        ease = float(c.get('ease', 2.5))
        reps = int(c.get('repetitions', 0))
        if grade < 3:
            # Reset — see again tomorrow
            reps = 0
            interval = 1
        else:
            if reps == 0:   interval = 1
            elif reps == 1: interval = 6
            else:           interval = round(c.get('interval', 1) * ease)
            reps += 1
        # Update ease
        ease = max(1.3, ease + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)))
        next_dt = date.today() + timedelta(days=interval)
        c['ease']         = round(ease, 3)
        c['interval']     = interval
        c['repetitions']  = reps
        c['last_review']  = date.today().isoformat()
        c['next_review']  = next_dt.isoformat()
        c['last_grade']   = grade
        _save(deck)
        from utils.mastery import record as _mastery
        _mastery(c.get('course_key', ''), c.get('topic') or str(c.get('q', ''))[:120],
                 GRADE_OUTCOME.get(grade, grade / 5), 'flashcard')
        return c
    return None


def stats() -> dict:
    deck = _load()
    today_s = date.today().isoformat()
    due_now = sum(1 for c in deck if (c.get('next_review') or '0000-01-01') <= today_s)
    seen = sum(1 for c in deck if (c.get('repetitions') or 0) > 0)
    by_course: dict[str, int] = {}
    for c in deck:
        ck = c.get('course_key', '?')
        by_course[ck] = by_course.get(ck, 0) + 1
    return {
        'total':       len(deck),
        'due_today':   due_now,
        'seen':        seen,
        'never_seen':  len(deck) - seen,
        'by_course':   dict(sorted(by_course.items(), key=lambda x: -x[1])),
    }


def grade_with_gemini(card_q: str, card_a: str, user_answer: str) -> tuple[int, str]:
    """Use Gemini to grade a user's answer vs the canonical answer.
    Returns (grade 0-5, short feedback in Hebrew).
    """
    import os
    try:
        from google import genai
        from google.genai import types as genai_types
        key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
        if not key:
            return (3, 'אין מפתח API — לא הצלחתי לדרג. נחשיב כתשובה בינונית.')
        client = genai.Client(api_key=key)
        prompt = (
            "אתה מורה פרטי שבודק תשובה של סטודנט. דרג את התשובה שלו ביחס לתשובה הנכונה.\n\n"
            "החזר JSON בלבד (בלי ```), במבנה:\n"
            '{"grade": 0-5, "feedback": "משפט הסבר קצר בעברית"}\n\n'
            "סולם:\n"
            "0-1 = לגמרי לא נכון / לא ענה\n"
            "2 = ניסה אבל פיספס את העיקר\n"
            "3 = נכון בעיקר, חסר פירוט\n"
            "4 = נכון ומלא\n"
            "5 = נכון, מלא, ועם תובנה נוספת\n\n"
            f"שאלה: {card_q}\n"
            f"תשובה נכונה: {card_a}\n"
            f"תשובת הסטודנט: {user_answer}"
        )
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=300, temperature=0.2,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                response_mime_type='application/json',
            ),
        )
        try:
            from utils.usage_tracker import log_gemini
            log_gemini(resp, 'gemini-2.5-flash', op='flashcards.grade')
        except Exception: pass
        txt = (resp.text or '').strip()
        d = json.loads(txt)
        g = int(d.get('grade', 3))
        g = max(0, min(5, g))
        fb = (d.get('feedback') or '').strip() or 'בסדר.'
        return (g, fb)
    except Exception as e:
        return (3, f'שגיאת הערכה: {e}. נחשיב כבינוני.')
