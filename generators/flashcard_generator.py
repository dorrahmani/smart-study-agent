"""generators/flashcard_generator.py — Auto-generate flashcards from summaries.

Reads a summary .md, extracts 3-6 Q&A pairs via Gemini, returns list of dicts.

Each card: {question, answer, source_stem, course_key, difficulty (1-5)}.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def cards_from_summary(md_path: Path, course_key: str = '') -> list[dict]:
    """Extract Q&A flashcards from a summary file via Gemini."""
    body = md_path.read_text(encoding='utf-8', errors='replace')
    if len(body) < 1000:
        return []
    # Truncate to keep prompt small
    if len(body) > 15000:
        body = body[:15000]

    prompt = (
        "אתה יוצר כרטיסיות לימוד (flashcards) לסטודנט. מהסיכום הבא — "
        "צור 4-6 שאלות+תשובות שמסקרות את הנושאים החשובים ביותר. כל שאלה צריכה לחייב "
        "שליפה מהזיכרון (לא just 'הגדר X', אלא 'מה הקשר בין X ל-Y?', 'מתי משתמשים ב-X?').\n\n"
        "החזר JSON בלבד (בלי ```), עם המבנה:\n"
        '[{"q": "שאלה", "a": "תשובה (2-4 משפטים)", "difficulty": 1-5}, ...]\n\n'
        f"הסיכום:\n{body}"
    )
    try:
        from google import genai
        from google.genai import types as genai_types
        key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
        if not key: return []
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=2500, temperature=0.4,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                response_mime_type='application/json',
            ),
        )
        try:
            from utils.usage_tracker import log_gemini
            log_gemini(resp, 'gemini-2.5-flash', op='flashcards.extract')
        except Exception: pass
        txt = (resp.text or '').strip()
        # Strip markdown fences if any
        txt = re.sub(r'^```(?:json)?', '', txt).strip()
        txt = re.sub(r'```$', '', txt).strip()
        items = json.loads(txt)
        out = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict): continue
            q = (it.get('q') or '').strip()
            a = (it.get('a') or '').strip()
            if not q or not a: continue
            out.append({
                'q': q, 'a': a,
                'difficulty': int(it.get('difficulty', 3)),
                'source_stem': md_path.stem,
                'course_key': course_key,
            })
        return out
    except Exception as e:
        print(f'flashcard extract failed for {md_path.name}: {e}')
        return []


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('usage: python -m generators.flashcard_generator <path.md>')
        sys.exit(1)
    p = Path(sys.argv[1])
    cards = cards_from_summary(p)
    for c in cards:
        print(f"[{c['difficulty']}] Q: {c['q']}")
        print(f"      A: {c['a']}")
        print()
