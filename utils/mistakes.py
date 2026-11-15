"""
utils/mistakes.py — the mistake notebook ("מחברת טעויות").

Every mistake, wherever it happens — a graded practice exercise, homework the
checker marked wrong, a wrong quiz answer — lands here once, and becomes a
flashcard (generators/sm2) so it comes back after 1 day, then 6, … until it
sticks. A mistake counts as fixed once its card was answered right twice in a row.

    record('probability', 'practice', topic='התפלגות בינומית',
           question='...', wrong='...', correct='...')
    notebook_md('probability')          # the notebook, for the PDF
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / 'data' / 'mistakes.json'
SOURCE_HE = {'practice': 'סט תרגול', 'homework': 'שיעורי בית', 'quiz': 'חידון', 'flashcard': 'כרטיסייה'}


def _load(path: Path | None = None) -> list[dict]:
    try:
        return json.loads((path or PATH).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []


def _save(items: list[dict], path: Path | None = None) -> None:
    p = path or PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(p)


def _norm(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '').strip())[:600]


def record(course_key: str, source: str, *, topic: str, question: str, wrong: str, correct: str,
           path: Path | None = None, make_card: bool = True) -> str | None:
    """Add a mistake (deduplicated on course + question + wrong). Returns its id, or None if a duplicate."""
    question, wrong, correct, topic = _norm(question), _norm(wrong), _norm(correct), _norm(topic)[:80]
    if not (question and correct):
        return None
    mid = 'm' + hashlib.sha1(f'{course_key}|{question}|{wrong}'.encode('utf-8')).hexdigest()[:10]
    items = _load(path)
    if any(m['id'] == mid for m in items):
        return None
    card_q = f'🔁 {topic}: {question}' if topic else f'🔁 {question}'
    items.append({'id': mid, 'ts': datetime.now().isoformat(timespec='seconds'), 'course_key': course_key,
                  'source': source, 'topic': topic, 'question': question, 'wrong': wrong,
                  'correct': correct, 'card_q': card_q})
    _save(items, path)
    if make_card:
        try:
            from generators import sm2
            ans = correct + (f'\n\n(הטעות שלך הייתה: {wrong})' if wrong else '')
            sm2.add_cards([{'q': card_q, 'a': ans, 'course_key': course_key, 'source_stem': 'מחברת טעויות',
                            'topic': topic, 'mistake_id': mid, 'difficulty': 3}])
        except Exception as e:                         # the notebook entry matters more than the card
            print(f'  ⚠️ mistake card failed: {e}')
    return mid


def is_fixed(m: dict, deck: list[dict]) -> bool:
    card = next((c for c in deck if c.get('mistake_id') == m['id'] or c.get('q') == m.get('card_q')), None)
    return bool(card and int(card.get('repetitions') or 0) >= 2)


def notebook(course_key: str, path: Path | None = None) -> tuple[list[dict], list[dict]]:
    """(open, fixed) mistakes of a course, newest first."""
    try:
        from generators import sm2
        deck = sm2._load()
    except Exception:
        deck = []
    items = sorted((m for m in _load(path) if m.get('course_key') == course_key),
                   key=lambda m: m.get('ts', ''), reverse=True)
    open_, fixed = [], []
    for m in items:
        (fixed if is_fixed(m, deck) else open_).append(m)
    return open_, fixed


def notebook_md(course_key: str, path: Path | None = None) -> str | None:
    from config.paths import COURSE_NAMES
    open_, fixed = notebook(course_key, path)
    if not open_ and not fixed:
        return None
    course = COURSE_NAMES.get(course_key, course_key)
    out = [f'# 📕 מחברת טעויות — {course}',
           f'{len(open_)} פתוחות · {len(fixed)} תוקנו (נענו נכון פעמיים ברצף בכרטיסיות)\n']
    by_topic: dict[str, list[dict]] = {}
    for m in open_:
        by_topic.setdefault(m.get('topic') or 'כללי', []).append(m)
    for topic, ms in sorted(by_topic.items(), key=lambda kv: -len(kv[1])):
        out.append(f'## {topic} ({len(ms)})')
        for m in ms:
            out.append(f'### {SOURCE_HE.get(m["source"], m["source"])} · {m["ts"][:10]}')
            out.append(f'**השאלה:** {m["question"]}')
            if m.get('wrong'):
                out.append(f'**❌ מה עשיתי:** {m["wrong"]}')
            out.append(f'**✅ הנכון:** {m["correct"]}\n')
    if fixed:
        out.append(f'## ✅ תוקנו ({len(fixed)})')
        out += [f'- {m.get("topic") or ""}: {m["question"][:120]}' for m in fixed[:30]]
    return '\n'.join(out)
