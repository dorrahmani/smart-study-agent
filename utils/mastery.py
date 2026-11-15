"""
utils/mastery.py — how well do I know each topic of a course?

Every answer anywhere is an event: a flashcard grade, a quiz answer, a graded
practice exercise, a homework question. Events carry a free-text topic (an
exercise title, a card question, a lecture name…). Building the map:
  1. one cheap model call groups the raw topics into ≤15 stable course topics
     (cached; existing names are reused so the map doesn't reshuffle weekly),
  2. the score per topic is computed in CODE: recency-weighted mean outcome
     (half-life 21 days), with a minimum number of attempts before judging.

    record('probability', 'התפלגות בינומית', 0.4, 'practice')
    build_map('probability')  →  [{'topic', 'score', 'n', 'status'}], untested topics
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / 'data' / 'mastery_events.jsonl'
CANON_DIR = ROOT / 'data' / 'mastery'
MODEL = 'gemini-3.8-flash'
HALF_LIFE_DAYS = 21
MIN_ATTEMPTS = 2
WINDOW_DAYS = 120

STATUS = [(0.8, '✅', 'שולט'), (0.55, '🟡', 'צריך חיזוק'), (0.0, '🔴', 'חלש')]


def record(course_key: str, topic: str, outcome: float, source: str, path: Path | None = None) -> None:
    """outcome ∈ [0, 1]. Never raises — a learning signal must not break the feature that produced it."""
    try:
        topic = re.sub(r'\s+', ' ', str(topic or '')).strip()[:160]
        if not course_key or not topic:
            return
        p = path or EVENTS
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': datetime.now().isoformat(timespec='seconds'), 'course_key': course_key,
                                'topic': topic, 'outcome': round(max(0.0, min(1.0, float(outcome))), 3),
                                'source': source}, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f'  ⚠️ mastery record failed: {e}')


def events(course_key: str, path: Path | None = None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    out = []
    try:
        with open(path or EVENTS, encoding='utf-8') as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get('course_key') == course_key and \
                        (now - datetime.fromisoformat(e['ts'])).days <= WINDOW_DAYS:
                    out.append(e)
    except OSError:
        pass
    return out


def score(evs: list[dict], now: datetime | None = None) -> float:
    """Recency-weighted mean outcome: an answer 21 days old counts half as much as today's."""
    now = now or datetime.now()
    num = den = 0.0
    for e in evs:
        age = max(0.0, (now - datetime.fromisoformat(e['ts'])).total_seconds() / 86400)
        w = math.pow(0.5, age / HALF_LIFE_DAYS)
        num += w * float(e['outcome'])
        den += w
    return num / den if den else 0.0


def status(s: float, n: int) -> tuple[str, str]:
    if n < MIN_ATTEMPTS:
        return '⚪', 'מעט נתונים'
    for th, icon, label in STATUS:
        if s >= th:
            return icon, label
    return STATUS[-1][1], STATUS[-1][2]


CANON_PROMPT = """אתה מארגן את נושאי הקורס "{course}".
1. "topics": רשימה של 6-15 נושאים מרכזיים בקורס עד עכשיו (שם קצר, 2-5 מילים), לפי מה שנלמד.
   **חובה** להשתמש מחדש בשמות הקיימים כשהם מתאימים: {existing}
2. "map": שייך כל אחד מהתיאורים הגולמיים למטה לאחד הנושאים (בדיוק כפי שנכתב ב-topics).
החזר JSON בלבד: {{"topics": ["..."], "map": {{"<תיאור גולמי>": "<נושא>"}}}}

## מה נלמד בקורס (כותרות מהסיכומים)
{taught}

## תיאורים גולמיים לשיוך
{raws}
"""


def _canon_path(course_key: str, d: Path | None = None) -> Path:
    return (d or CANON_DIR) / f'{course_key}.json'


def canonicalize(course_key: str, raws: list[str], taught: str = '', client=None,
                 canon_dir: Path | None = None) -> tuple[list[str], dict]:
    """(canonical topics, raw → topic). Only unseen raw topics go to the model; cached."""
    p = _canon_path(course_key, canon_dir)
    try:
        c = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        c = {'topics': [], 'map': {}}
    topics, mapping = list(c.get('topics') or []), dict(c.get('map') or {})
    todo = [r for r in dict.fromkeys(raws) if r not in mapping]
    refresh = bool(taught) and c.get('taught_sig') != str(len(taught))
    if not todo and not refresh and topics:
        return topics, mapping
    from config.paths import COURSE_NAMES
    try:
        from google.genai import types as T
        if client is None:
            import google.genai as genai
            client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
        resp = client.models.generate_content(
            model=MODEL, contents=CANON_PROMPT.format(
                course=COURSE_NAMES.get(course_key, course_key),
                existing=', '.join(topics) or '(אין עדיין)', taught=taught[-15000:] or '(לא ידוע)',
                raws='\n'.join(f'- {r}' for r in todo[:200]) or '(אין)'),
            config=T.GenerateContentConfig(max_output_tokens=6000, temperature=0.0,
                                           response_mime_type='application/json',
                                           thinking_config=T.ThinkingConfig(thinking_level='low')))
        from utils.usage_tracker import log_gemini
        log_gemini(resp, MODEL, op='mastery_canon')
        data = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', (resp.text or '').strip()))
    except Exception as e:
        print(f'  ⚠️ mastery canonicalize failed: {e}')
        return topics, mapping
    new_topics = [str(t).strip() for t in data.get('topics') or [] if str(t).strip()][:15]
    topics = list(dict.fromkeys(topics + new_topics))[:20]
    for raw, t in (data.get('map') or {}).items():
        if str(t).strip() in topics:
            mapping[str(raw)] = str(t).strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({'topics': topics, 'map': mapping, 'taught_sig': str(len(taught))},
                            ensure_ascii=False, indent=1), encoding='utf-8')
    return topics, mapping


def build_map(course_key: str, client=None, now: datetime | None = None,
              events_path: Path | None = None, canon_dir: Path | None = None) -> tuple[list[dict], list[str]]:
    """(rows sorted weakest first, topics with no attempts yet)."""
    evs = events(course_key, events_path, now)
    try:
        from generators.study_focus import covered_topics
        taught, _ = covered_topics(course_key)
    except Exception:
        taught = ''
    topics, mapping = canonicalize(course_key, [e['topic'] for e in evs], taught, client, canon_dir)
    grouped: dict[str, list[dict]] = {}
    for e in evs:
        t = mapping.get(e['topic'])
        if t:
            grouped.setdefault(t, []).append(e)
    rows = []
    for t, es in grouped.items():
        s = score(es, now)
        icon, label = status(s, len(es))
        rows.append({'topic': t, 'score': round(s, 2), 'n': len(es), 'icon': icon, 'label': label,
                     'sources': sorted({e['source'] for e in es})})
    rows.sort(key=lambda r: (r['icon'] == '⚪', r['score']))
    untested = [t for t in topics if t not in grouped]
    return rows, untested


def weak_topics(course_key: str, limit: int = 5, **kw) -> list[str]:
    rows, _ = build_map(course_key, **kw)
    return [f"{r['topic']} ({int(r['score'] * 100)}%, {r['n']} ניסיונות)"
            for r in rows if r['icon'] in ('🔴', '🟡')][:limit]


def map_text(course_key: str, **kw) -> str:
    """Telegram-markdown mastery map."""
    from config.paths import COURSE_NAMES
    course = COURSE_NAMES.get(course_key, course_key)
    rows, untested = build_map(course_key, **kw)
    if not rows and not untested:
        return f'🗺️ אין עדיין נתונים ל{course}. המפה נבנית מתשובות בכרטיסיות, בחידון, בסטי תרגול ובשיעורי בית.'
    out = [f'*🗺️ מפת שליטה — {course}*']
    for r in rows:
        pct = '' if r['icon'] == '⚪' else f" — {int(r['score'] * 100)}%"
        out.append(f"{r['icon']} {r['topic']}{pct} ({r['n']} ניסיונות)")
    if untested:
        out.append('\n⚪ *עוד לא נבדק:* ' + ' · '.join(untested))
    weakest = next((r for r in rows if r['icon'] in ('🔴', '🟡')), None)
    if weakest:
        out.append(f'\n💡 הכי כדאי עכשיו: "סט תרגול על {weakest["topic"]} ב{course}"')
    out.append('\n_✅ ≥80% · 🟡 55-80% · 🔴 מתחת · ⚪ פחות מ-2 ניסיונות. תשובות אחרונות שוקלות יותר._')
    return '\n'.join(out)
