"""
extractors/concept_extractor.py — Extract canonical 'concepts[]' from a note.

Uses Gemini Flash (cheap) to normalize a list of topics + opening text into
3-8 lowercase snake_case concept IDs that can serve as cross-course hubs.

Result is cached per-note (mtime-based) in data/concept_cache.json so we
never pay for the same note twice.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from config.paths import DATA_DIR

# Load .env (the project uses python-dotenv elsewhere)
try:
    from dotenv import load_dotenv as _load_env
    _load_env(DATA_DIR.parent / '.env', override=True)
except Exception:
    pass

CACHE_PATH = DATA_DIR / 'concept_cache.json'

SYSTEM = (
    "אתה עוזר חילוץ מושגים אקדמיים. תקבל רשימת נושאים וטקסט פתיחה של סיכום הרצאה, "
    "ותחזיר 3-8 מושגי-יסוד מנורמלים. כל מושג: lowercase, snake_case, באנגלית או טרנסליטרציה. "
    "אם מושג עלול להתבלבל בין דיסציפלינות, הוסף סיומת __math / __cs / __econ / __stats."
)
PROMPT = """נושאים: {topics}

טקסט פתיחה (עד 800 תווים):
{opening}

החזר JSON בלבד בפורמט:
{{"concepts": ["concept_a", "concept_b__math", ...]}}

חשוב: 3-8 מושגי-יסוד יציבים שיכולים לחבר בין הרצאות שונות (לא ספציפי מדי, לא כללי מדי)."""


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def _save_cache(c: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding='utf-8')


_KEY_RE = re.compile(r'[^a-z0-9_]+')


def _normalize(c) -> str:
    if not isinstance(c, str):
        c = str(c) if c is not None else ''
    c = c.lower().strip()
    c = c.replace(' ', '_').replace('-', '_')
    c = _KEY_RE.sub('_', c).strip('_')
    return c


def extract_concepts(note_path: Path, topics: list[str], opening: str,
                     course_key: str = '', use_api: bool = True) -> list[str]:
    """Returns a list of normalized concept IDs.

    Cached per (path, mtime). Falls back to topic-derived concepts if no API key
    or use_api=False.
    """
    cache = _load_cache()
    key = str(note_path)
    try:
        mtime = note_path.stat().st_mtime
    except OSError:
        mtime = 0.0

    cached = cache.get(key)
    if cached and cached.get('mtime') == mtime:
        return cached.get('concepts', [])

    concepts: list[str] = []
    if use_api and (os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')):
        concepts = _call_gemini(topics, opening, course_key) or []

    if not concepts:
        # Fallback: derive simple concepts from topics
        concepts = _fallback(topics, course_key)

    cache[key] = {'mtime': mtime, 'concepts': concepts}
    _save_cache(cache)
    return concepts


def _call_gemini(topics: list[str], opening: str, course_key: str) -> Optional[list[str]]:
    key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types as _gt
    except ImportError:
        return None
    topics_clean = [str(t) if not isinstance(t, str) else t for t in (topics or [])]
    topics_str = ', '.join(topics_clean[:12]) if topics_clean else '(אין)'
    opening = (opening or '')[:800]
    prompt = SYSTEM + '\n\n' + PROMPT.format(topics=topics_str, opening=opening)
    try:
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model='gemini-2.5-flash-lite',  # 10/2026: list extraction, ~3x cheaper than flash
            contents=prompt,
            config=_gt.GenerateContentConfig(
                max_output_tokens=400, temperature=0.0,
                thinking_config=_gt.ThinkingConfig(thinking_budget=0),
                response_mime_type='application/json',
            ),
        )
        from utils.usage_tracker import log_gemini as _lg; _lg(resp, 'gemini-2.5-flash-lite', op='concept_extractor')
        raw = (resp.text or '').strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)
        items = data.get('concepts') or []
        return [_normalize(c) for c in items if c]
    except Exception as e:
        print(f'[concept_extractor] Gemini failed: {e}', file=sys.stderr)
        return None


_COURSE_SUFFIX = {
    'calculus_2':       '__math',
    'discrete_math_2':  '__math',
    'linear_algebra_2': '__math',
    'microeconomics':   '__econ',
    'intro_to_cs':      '__cs',
}


def _fallback(topics: list[str], course_key: str) -> list[str]:
    """Without API: just normalize the first 6 topics with course suffix."""
    suffix = _COURSE_SUFFIX.get(course_key, '')
    out = []
    seen = set()
    for t in topics[:6]:
        norm = _normalize(t)
        if not norm or len(norm) < 2:
            continue
        # Add course suffix only if there's ambiguity risk (very short normalized form)
        if len(norm) <= 6 and suffix:
            norm = norm + suffix
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out
