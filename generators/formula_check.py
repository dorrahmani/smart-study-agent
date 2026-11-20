"""
generators/formula_check.py — flag summary formulas the lecture doesn't support.

A wrong formula learned the night before an exam is worse than a missing one.
After a summary is written, a cheap model gets the formulas from the summary
plus the transcript (and board text) and lists ONLY formulas that contradict
the source or have no basis in it. They are appended as a short "check against
the recording" section; a clean result adds a one-line ✅.

Never blocks a summary: any failure returns '' and the summary ships as is.
"""
from __future__ import annotations

import json
import re

MODEL = 'gemini-3.8-flash'
MIN_FORMULAS = 3          # e.g. Software 1 lectures: nothing to check
MAX_FORMULAS = 200        # one call; the formula list is short next to the transcript

_DISPLAY = re.compile(r'\$\$(.+?)\$\$', re.S)
_INLINE = re.compile(r'(?<!\$)\$([^$\n]{2,200}?)\$(?!\$)')

PROMPT = """אתה בודק עובדות קפדן. לפניך נוסחאות שנכתבו בסיכום של הרצאה, ומקור ההרצאה (תמלול + תוכן הלוח).
החזר JSON בלבד: {{"suspicious": [{{"i": <מספר הנוסחה>, "reason": "<עד 15 מילים בעברית>"}}]}}
כלול נוסחה רק אם היא:
- סותרת את מה שנאמר או נכתב בהרצאה (סימן הפוך, מקדם שגוי, תנאי חסר/שגוי, חזקה שגויה), או
- טענה מתמטית מהותית שאין לה שום בסיס במקור ונראית שגויה.
אל תכלול: ניסוח או סימון שונה לאותה משמעות, נוסחאות סטנדרטיות נכונות, השלמות נכונות שהסיכום הוסיף.
אם הכול תקין — {{"suspicious": []}}.

## הנוסחאות בסיכום
{formulas}

## מקור ההרצאה
{source}
"""


def extract_formulas(md: str) -> list[str]:
    seen, out = set(), []
    for rx in (_DISPLAY, _INLINE):
        for m in rx.finditer(md):
            f = ' '.join(m.group(1).split())
            if len(f) >= 3 and f not in seen and any(c in f for c in '=<>\\^_'):
                seen.add(f)
                out.append(f)
    return out[:MAX_FORMULAS]


def _on_subscription(prompt: str) -> str | None:
    """The check on the subscription (agy): its JSON, or None → the API. An answer that isn't the expected JSON → None."""
    import os
    if os.getenv('SSA_FORMULA_VIA_SUB', '1') == '0':
        return None
    try:
        from utils import gemini_sub
        out = gemini_sub.ask(prompt, json_mode=True, timeout=300)
        if out is None or not isinstance(json.loads(out).get('suspicious', []), list):
            return None
        from utils.usage_tracker import log_usage
        log_usage(service='gemini-subscription', model=f'agy-{gemini_sub.MODEL}', op='formula_check.sub',
                  extra={'chars_in': len(prompt)})
        return out
    except Exception:
        return None


def check_formulas(summary: str, transcript: str, board_text: str, client, model: str = MODEL) -> str:
    """Markdown section to append ('' when not applicable or on any error)."""
    formulas = extract_formulas(summary or '')
    if len(formulas) < MIN_FORMULAS:
        return ''
    try:
        from google.genai import types as T
        source = (transcript or '')[:120000] + ('\n\n## לוח\n' + board_text[:20000] if board_text else '')
        listing = '\n'.join(f'{i}. ${f}$' for i, f in enumerate(formulas, 1))
        prompt = PROMPT.format(formulas=listing, source=source)
        raw = _on_subscription(prompt)                   # 19/11 the owner's Gemini Pro subscription first ($0)
        if raw is None:
            resp = client.models.generate_content(
                model=model, contents=prompt,
                config=T.GenerateContentConfig(max_output_tokens=2000, temperature=0.0,
                                               response_mime_type='application/json',
                                               thinking_config=T.ThinkingConfig(thinking_budget=0)))
            from utils.usage_tracker import log_gemini
            log_gemini(resp, model, op='formula_check')
            raw = resp.text or ''
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
        sus = json.loads(raw).get('suspicious') or []
    except Exception as e:
        print(f"⚠️  בדיקת נוסחאות נכשלה ({e}) — ממשיך בלי")
        return ''
    rows = []
    for s in sus:
        try:
            i = int(s.get('i'))
            if 1 <= i <= len(formulas):
                rows.append(f'- ${formulas[i - 1]}$ — {str(s.get("reason", "")).strip()[:120]}')
        except (TypeError, ValueError):
            continue
    if not rows:
        return f'\n\n---\n✅ *בדיקת נוסחאות אוטומטית: {len(formulas)} נוסחאות נבדקו מול התמלול והלוח — לא נמצאו סתירות.*\n'
    return ('\n\n---\n## ⚠️ בדיקת נוסחאות — לבדוק מול ההקלטה\n'
            f'נבדקו {len(formulas)} נוסחאות מול התמלול והלוח. אלה לא תאמו את המקור או שלא נמצא להן בסיס:\n'
            + '\n'.join(rows[:10]) + '\n')
