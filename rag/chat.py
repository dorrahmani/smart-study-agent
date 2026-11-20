"""
rag/chat.py — Pure function: question → answer + sources.

This is intentionally a stateless function so it can be called from:
  - Dashboard tab (now)
  - WhatsApp webhook (future)
  - CLI (now: python -m rag.chat "question")
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

from rag.vault_indexer import search


# ─── LaTeX → Unicode (safety net for Telegram display) ─────────────
_LATEX_MAP = {
    r'\lambda': 'λ', r'\mu': 'μ', r'\alpha': 'α', r'\beta': 'β',
    r'\gamma': 'γ', r'\delta': 'δ', r'\epsilon': 'ε', r'\theta': 'θ',
    r'\sigma': 'σ', r'\phi': 'φ', r'\psi': 'ψ', r'\omega': 'ω',
    r'\Sigma': 'Σ', r'\Delta': 'Δ', r'\Phi': 'Φ', r'\Omega': 'Ω',
    r'\subseteq': '⊆', r'\subset': '⊂', r'\supseteq': '⊇', r'\supset': '⊃',
    r'\in': '∈', r'\notin': '∉', r'\emptyset': '∅', r'\infty': '∞',
    r'\mathbb{R}': 'ℝ', r'\mathbb{N}': 'ℕ', r'\mathbb{Z}': 'ℤ',
    r'\mathbb{Q}': 'ℚ', r'\mathbb{C}': 'ℂ',
    r'\leq': '≤', r'\geq': '≥', r'\neq': '≠', r'\approx': '≈',
    r'\to': '→', r'\Rightarrow': '⇒', r'\Leftrightarrow': '⇔',
    r'\forall': '∀', r'\exists': '∃', r'\neg': '¬', r'\land': '∧', r'\lor': '∨',
    r'\sqrt': '√', r'\pm': '±', r'\times': '×', r'\cdot': '·', r'\div': '÷',
    r'\nabla': '∇', r'\partial': '∂', r'\sum': 'Σ', r'\int': '∫',
    r'\setminus': '\\',
}
_SUPER_MAP = str.maketrans('0123456789n+-=()i', '⁰¹²³⁴⁵⁶⁷⁸⁹ⁿ⁺⁻⁼⁽⁾ⁱ')
_SUB_MAP   = str.maketrans('0123456789n+-=()i', '₀₁₂₃₄₅₆₇₈₉ₙ₊₋₌₍₎ᵢ')

def _render_matrix(body: str, bracket: str = '[') -> str:
    """Render LaTeX matrix body (rows separated by \\, cols by &) as plain text.
    Example input: ' 1 & 1 \\ 1 & 0 '
    Output:
       [1  1]
       [1  0]
    """
    open_b, close_b = ({'[':'[]','(':'()','|':'||'}.get(bracket,'[]'))
    # Split rows on \\ (or \\\\) — also accept literal '\\' that lost the leading backslash
    import re as _re
    rows = _re.split(r'\\\\|\\\s|(?<!\w)\\\\(?!\w)', body)
    if len(rows) < 2:
        rows = _re.split(r'\s+\\\\?\s+|\s+\\\\\s+', body)
    # Last-resort split on bare \\
    if len(rows) < 2 and '\\\\' in body:
        rows = body.split('\\\\')
    if len(rows) < 2:
        rows = body.split('\\')  # absolute fallback
    cells: list[list[str]] = []
    for r in rows:
        r = r.strip()
        if not r: continue
        cols = [c.strip() for c in r.split('&')]
        cells.append(cols)
    if not cells: return body
    n_cols = max(len(r) for r in cells)
    # Pad
    for r in cells:
        while len(r) < n_cols:
            r.append('')
    # Compute column widths
    widths = [max(len(r[i]) for r in cells) for i in range(n_cols)]
    lines = []
    for r in cells:
        inner = '  '.join(c.center(widths[i]) for i, c in enumerate(r))
        lines.append(f'{open_b}{inner}{close_b}')
    return '\n' + '\n'.join(lines) + '\n'


def _latex_to_unicode(text: str) -> str:
    """Strip $...$ wrappers and convert common LaTeX commands to Unicode.
    Special-cases matrices (pmatrix, bmatrix, matrix) into readable ASCII grids."""
    import re as _re
    # ── Matrices (pmatrix/bmatrix/matrix), with or without backslashes ──
    # Pattern matches: \begin{pmatrix}...\end{pmatrix} OR begin{pmatrix}...end{pmatrix}
    def _mat_repl(m):
        kind = m.group(1)
        body = m.group(2)
        bracket = {'pmatrix':'(', 'bmatrix':'[', 'matrix':'[', 'vmatrix':'|'}.get(kind, '[')
        return _render_matrix(body, bracket=bracket)
    text = _re.sub(
        r'\\?begin\{(p|b|v)?matrix\}(.*?)\\?end\{(?:p|b|v)?matrix\}',
        lambda m: _render_matrix(m.group(2), bracket={'p':'(','b':'[','v':'|'}.get(m.group(1) or 'b','[')),
        text, flags=_re.DOTALL,
    )
    # \frac{a}{b} → (a)/(b) — easier to read than nested div
    text = _re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', r'(\1)/(\2)', text)
    text = _re.sub(r'\\dfrac\{([^{}]+)\}\{([^{}]+)\}', r'(\1)/(\2)', text)
    # \sqrt{x} → √x or √(x)
    text = _re.sub(r'\\sqrt\{([^{}]+)\}', r'√(\1)', text)
    # \mathrm{X} → X
    text = _re.sub(r'\\mathrm\{([^}]*)\}', r'\1', text)
    text = _re.sub(r'\\text\{([^}]*)\}', r'\1', text)
    # \mathbb{X} handled in map; also handle generic case
    text = _re.sub(r'\\mathbb\{([A-Z])\}',
                   lambda m: {'R':'ℝ','N':'ℕ','Z':'ℤ','Q':'ℚ','C':'ℂ'}.get(m.group(1), m.group(1)), text)
    # apply command map (longest first to avoid prefix conflicts)
    for cmd in sorted(_LATEX_MAP, key=len, reverse=True):
        text = text.replace(cmd, _LATEX_MAP[cmd])
    # ^{...} → superscript
    def sup(m):
        s = m.group(1)
        try: return s.translate(_SUPER_MAP)
        except: return '^' + s
    text = _re.sub(r'\^\{([^}]+)\}', sup, text)
    text = _re.sub(r'\^([A-Za-z0-9])', lambda m: m.group(1).translate(_SUPER_MAP) if m.group(1).isalnum() else m.group(0), text)
    # _{...} → subscript
    def sub(m):
        s = m.group(1)
        try: return s.translate(_SUB_MAP)
        except: return '_' + s
    text = _re.sub(r'_\{([^}]+)\}', sub, text)
    text = _re.sub(r'_([A-Za-z0-9])', lambda m: m.group(1).translate(_SUB_MAP) if m.group(1).isalnum() else m.group(0), text)
    # remove $ delimiters (display + inline)
    text = text.replace('$$', '').replace('$', '')
    # Common leftover patterns from broken LaTeX
    text = text.replace('\\,', ' ').replace('\\;', ' ').replace('\\!', '')
    text = text.replace('\\quad', '   ').replace('\\qquad', '      ')
    # clean leftover backslash-letter chains (unknown commands)
    text = _re.sub(r'\\([a-zA-Z]+)', r'\1', text)
    # Collapse triple+ blank lines
    text = _re.sub(r'\n{3,}', '\n\n', text)
    return text

SYSTEM = (
    "אתה עוזר לימוד פרטי לסטודנט באוניברסיטה (מבוא מורחב, חדו\"א 2, "
    "לינארית 2, בדידה 2, מיקרו א'). תקבל שאלה + קטעי מקור מסיכומי הרצאות.\n\n"
    "═══════════════════════════════════\n"
    "פורמט תשובה — חובה לעקוב:\n"
    "═══════════════════════════════════\n\n"
    "**עבור שאלה על מושג מתמטי/הגדרה** — השתמש בפורמט הזה:\n\n"
    "📌 *<שם המושג>*\n\n"
    "<2-3 משפטי הגדרה ברורים>\n\n"
    "▸ *תכונות עיקריות:*\n"
    "• <תכונה 1>\n"
    "• <תכונה 2>\n"
    "• <תכונה 3>\n\n"
    "💡 *דוגמה:*\n"
    "<דוגמה ברורה — אם רלוונטי, צטט מההרצאה הספציפית>\n\n"
    "📚 *מהיכן בקורס:*\n"
    "<ציין הרצאה/תרגול ספציפיים שדנו בזה — לפי שם וקורס>\n\n"
    "**עבור \"באיזה הרצאה למדנו על X\"** — פורמט נקי:\n\n"
    "📌 *X*\n\n"
    "<משפט פותח קצר על המושג>\n\n"
    "▸ *הרצאות רלוונטיות:*\n\n"
    "📖 *<תאריך> — <כותרת ההרצאה>* (קורס: <שם הקורס בעברית>)\n"
    "<הסבר 2-3 משפטים על מה נדון בהרצאה זו לגבי המושג>\n\n"
    "📖 *<תאריך> — <כותרת>* (קורס: ...)\n"
    "<הסבר>\n\n"
    "**חשוב — שמות קורסים בעברית:** intro_to_cs → מבוא מורחב; calculus_2 → חדו\"א 2; discrete_math_2 → בדידה 2; linear_algebra_2 → לינארית 2; microeconomics → מיקרו א'.\n\n"
    "**עבור שאלה על מטלות/ציונים/מועד** — רשימה ממוקדת בעברית עם תאריכים.\n\n"
    "**עבור בקשה לחלוקה לנושאים** (\"תחלק לנושאים\", \"רשימת הנושאים בקורס\", \"מה למדנו ב-X\")\n"
    "— פורמט סדור לפי **סדר כרונולוגי**:\n\n"
    "📚 *נושאי <שם הקורס>* (לפי סדר לימוד)\n\n"
    "*1. <שם נושא ראשון>* — <תאריכים בערך, למשל 14/04–21/04>\n"
    "   • <נקודה משנית 1>\n"
    "   • <נקודה משנית 2>\n\n"
    "*2. <שם נושא שני>* — <תאריכים>\n"
    "   • <נקודה משנית 1>\n"
    "   • <נקודה משנית 2>\n\n"
    "...(וכן הלאה)...\n\n"
    "**חוקים לחלוקה לנושאים:**\n"
    "  - אל תחזיר רשימת הרצאות בודדות — קבץ לפי **נושא תיאורטי**\n"
    "  - הצג בסדר *כרונולוגי* — מהמוקדם לאחרון (לפי תאריך ההרצאה הראשונה בנושא)\n"
    "  - 4-8 נושאי-על לקורס, לא 20\n"
    "  - לכל נושא: 2-4 תתי-נקודות בקיצור\n"
    "  - אל תפרט הסברים מלאים — רק שלד התוכן\n"
    "  - אל תוסיף 'היי! אני כאן לעזור' לתשובה זו\n\n"
    "**עבור ברכה לבדה** (\"היי\"/\"שלום\" — *רק* כשזו ההודעה כולה, בלי שאלה אחריה):\n"
    "תגובה קצרה: \"היי! אני כאן לעזור עם החומר.\" — וזהו, בלי דבר נוסף.\n"
    "**אסור** להתחיל תשובות אחרות במילים האלו!\n\n"
    "═══════════════════════════════════\n"
    "כללי תוכן (חשוב!):\n"
    "═══════════════════════════════════\n\n"
    "1. **ענה לשאלה — אל תסכם מקורות.** אם המקורות לא קשורים — אמור: \"אין לי על זה מידע בסיכומים\".\n"
    "1a. **קריטי — סינון מקורות:** אם מקור הוא מקורס שלא קשור לשאלה (למשל שאלה על חדו\"א ומקור ממיקרו) — **התעלם ממנו לחלוטין** ולא תצטט אותו. צטט רק מקורות שבאמת תרמו לתשובה.\n"
    "1b. **אם השאלה מתייחסת לקורס ספציפי** (למשל \"בחדו\\\"א\") — סנן את התשובה רק לקורס הזה.\n"
    "1c. **תן הסבר מלא:** אם השאלה דורשת הבנה, אל תקצר מדי — 5-12 משפטים. אבל לא דחוסים — חלק לסעיפים, השתמש בפורמט.\n"
    "2. **אסור LaTeX!** התשובה בטלגרם — לא ירונדר. השתמש ב-Unicode מתמטי בלבד:\n"
    "   • אותיות יווניות: λ μ α β γ δ ε θ σ φ ψ ω Σ Δ\n"
    "   • קבוצות: ⊆ ⊂ ⊇ ⊃ ∈ ∉ ∅ ∞ ℝ ℕ ℤ ℚ ℂ\n"
    "   • יחסים: ≤ ≥ ≠ ≈ → ⇒ ⇔ ∀ ∃\n"
    "   • פעולות: √ ± × · ÷ ∇ ∂ ∑ ∫\n"
    "   • חזקות/תחתי: ⁰¹²³⁴⁵⁶⁷⁸⁹ⁿⁱ ₀₁₂₃ₙᵢ\n"
    "   • לעולם לא: $...$, \\\\command, ^{...}, _{...}\n"
    "   • דוגמה: \"A ⊆ ℝⁿ, λᵢ ∈ ℂ\" (תקין). \"$A \\\\subseteq \\\\mathbb{R}^n$\" (אסור!).\n"
    "2a. **מטריצות** — ציור ב-ASCII עם סוגריים, **שורה לכל שורה**, מיושר:\n"
    "   ```\n"
    "   A = [ 1  1 ]\n"
    "       [ 1  0 ]\n"
    "   ```\n"
    "   לעולם לא: `begin{pmatrix} 1 & 1 \\\\\\\\ 1 & 0 end{pmatrix}` — זה גורם זוועה!\n"
    "2b. **שברים** — `(a)/(b)` ולא `\\frac{a}{b}`. שורש — `√(x)` ולא `\\sqrt{x}`.\n"
    "2c. **חישובים ארוכים** — כל שלב בשורה נפרדת, יישור עם רווחים:\n"
    "   ```\n"
    "   det(A) = 1·0 - 1·1\n"
    "          = -1\n"
    "   ```\n"
    "3. **דגש (bold) ב-Telegram:** *מילה* (כוכבית אחת מכל צד), לא **מילה**.\n"
    "4. **בלי [[wikilinks]] בתוך הטקסט** (טלגרם לא מבין). ציטוטים יבואו בנפרד למטה.\n"
    "5. ענה בעברית. קצר ויפה."
)


def _rerank_with_gemini(question: str, hits: list[dict],
                        top_n: int = 5, model: str = 'gemini-2.5-flash-lite') -> list[dict]:
    """Re-rank retrieved chunks by relevance to the question using a quick LLM call.
    Saves the original score in 'retrieval_score' and replaces 'score' with rerank score (0-10).
    Returns top_n by rerank score; never returns fewer than the input list.

    Cost-saver: if top result already has very high embed score (>0.85), skip rerank entirely.
    """
    if len(hits) <= top_n:
        return hits
    # Skip rerank when retrieval is already confident — saves a Gemini call per question.
    top_score = float(hits[0].get('score', 0) or hits[0].get('rrf_score', 0) or 0)
    if top_score > 0.85:
        return hits[:top_n]
    google_key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
    if not google_key:
        return hits[:top_n]
    try:
        from google import genai
        from google.genai import types as genai_types
        client = genai.Client(api_key=google_key)
        # Build numbered snippet list
        snippets = []
        for i, h in enumerate(hits):
            snippets.append(f"[{i}] ({h.get('course_key','?')}) {h.get('title','?')}: "
                            f"{(h.get('text','') or '')[:400]}")
        prompt = (
            f"שאלה: {question}\n\n"
            f"להלן {len(hits)} קטעים. דרג כל אחד בציון 0-10 לפי הרלוונטיות לשאלה.\n"
            f"החזר JSON בלבד: [{{\"i\": 0, \"score\": 8}}, ...]. ללא הסבר.\n\n"
            + '\n'.join(snippets)
        )
        resp = client.models.generate_content(
            model=model, contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=600, temperature=0.0,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                response_mime_type='application/json',
            ),
        )
        try:
            from utils.usage_tracker import log_gemini
            log_gemini(resp, model, op='rag.rerank')
        except Exception: pass
        import json as _json
        scored = _json.loads((resp.text or '[]').strip())
        # Map back to original hits
        ranked: list[tuple[float, dict]] = []
        for item in scored if isinstance(scored, list) else []:
            try:
                i = int(item['i']); s = float(item['score'])
            except Exception: continue
            if 0 <= i < len(hits):
                h = dict(hits[i])
                h['retrieval_score'] = h.get('score', 0)
                h['score'] = round(s, 2)
                ranked.append((s, h))
        if not ranked: return hits[:top_n]
        ranked.sort(key=lambda x: -x[0])
        return [h for _, h in ranked[:top_n]]
    except Exception as e:
        print(f'  rerank fallback: {e}', file=sys.stderr)
        return hits[:top_n]


def _detect_cross_course(question: str) -> bool:
    """Detect if the user's question implies searching across all courses."""
    import re
    q = question.strip()
    patterns = [
        r'בכל הקורסים', r'בכל המקצועות', r'across courses', r'across all',
        r'איפה השתמשנו ב', r'איפה למדנו על', r'באילו קורסים',
    ]
    return any(re.search(p, q, re.IGNORECASE) for p in patterns)


OLD_COURSE_RX = re.compile(r'סמסטר\s+(?:קודם|שעבר)|שנה\s+שעברה|מיקרו\s*(?:כלכלה\s*)?א|חדו|חשבון\s+דיפרנציאלי|אינפי|בדידה|'
                           r'לינארית|מבוא\s+מורחב|מדמ"?ח', re.I)


def this_semester_first(question: str, hits: list[dict]) -> list[dict]:
    """03/11 eval: "מה למדו בהרצאה האחרונה במיקרו" (no course given) was answered from micro A, 28/06. With no course,
    the current semester's courses win; a finished course only when the question names it (or no current hit)."""
    try:
        from config.paths import ACTIVE_COURSES
    except Exception:
        return hits
    if OLD_COURSE_RX.search(question or ''):
        return hits
    now = [h for h in hits if h.get('course_key') in ACTIVE_COURSES]
    return now or hits


def chat(question: str, k: int = 5, model: str = 'gemini-2.5-flash',
         course_filter: str | None = None, rerank: bool = True) -> dict[str, Any]:
    """
    Returns {
        'answer': str,
        'sources': list[dict],   # each: {title, course_key, stem, score, snippet}
        'used_api': bool,
        'scope': str,            # 'all' or course_key
    }

    course_filter: optional course_key. If set, retrieval is scoped to one course.
    rerank: if True (default), retrieve 2x k then use Gemini to re-rank to top-k.
    """
    # Cross-course mode: if user mentions "all courses" / "in which courses",
    # force course_filter=None so we span everything.
    if _detect_cross_course(question):
        course_filter = None
    # Retrieve more chunks if we're going to rerank
    k_inner = (k * 3) if rerank else k
    hits = search(question, k=k_inner, course_filter=course_filter)
    if not course_filter and hits:
        hits = this_semester_first(question, hits)
    if rerank and hits:
        hits = _rerank_with_gemini(question, hits, top_n=k, model=model)
    scope = course_filter or 'all'
    if not hits:
        msg = 'לא מצאתי שום קטע רלוונטי בסיכומים.'
        if course_filter:
            msg = f'לא מצאתי קטע רלוונטי במוח {course_filter} — נסה היקף "כל המוחות".'
        return {'answer': msg, 'sources': [], 'used_api': False, 'scope': scope}

    # Prefer GEMINI_API_KEY (working for summaries) over GOOGLE_API_KEY
    google_key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
    if not google_key:
        # Offline fallback: return the snippets directly
        lines = ['📚 לא הופעל GOOGLE_API_KEY — מציג את הקטעים הרלוונטיים ביותר:\n']
        for h in hits:
            lines.append(f"\n**{h['title']}** — `{h['course_key']}` — [[{h['stem']}]]")
            lines.append(f"> {h['text'][:300]}...")
        return {'answer': '\n'.join(lines), 'sources': hits,
                'used_api': False, 'scope': scope}

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        return {'answer': 'התקן google-genai: pip install google-genai',
                'sources': hits, 'used_api': False, 'scope': scope}

    context_parts = []
    for h in hits:
        context_parts.append(
            f"מקור: [[{h['stem']}]] (קורס {h['course_key']}, תאריך {h.get('date','')})\n"
            f"{h['text']}\n"
        )
    # If cross-course mode (or hits span >1 course), instruct LLM to GROUP BY COURSE
    courses_in_hits = {h.get('course_key') for h in hits if h.get('course_key')}
    cross_course_hint = ''
    if len(courses_in_hits) > 1:
        cross_course_hint = (
            "\n**חוצה-קורסים:** המקורות מתפרשים על מספר קורסים. "
            "ארגן את התשובה לפי קורסים — תת-כותרת לכל קורס "
            "(`📘 מבוא מורחב:`, `📐 חדו\"א 2:`, וכו'), ותחתיה ההתייחסות הספציפית לקורס.\n"
        )
    user_msg = (
        f"{SYSTEM}\n"
        f"{cross_course_hint}\n"
        f"שאלה: {question}\n\n"
        f"מקורות זמינים:\n\n" + '\n---\n'.join(context_parts)
    )
    import time
    answer = None
    last_err = None
    client = genai.Client(api_key=google_key)
    for attempt, (m, sleep_s) in enumerate(
        [(model, 5), (model, 15), (model, 30), ('gemini-3.1-pro-preview', 0)], 1
    ):
        try:
            # thinking_budget=0 invalid for pro models — only set for flash.
            _cfg = dict(max_output_tokens=2048, temperature=0.3)
            if 'flash' in m.lower():
                _cfg['thinking_config'] = genai_types.ThinkingConfig(thinking_budget=0)
            resp = client.models.generate_content(
                model=m,
                contents=user_msg,
                config=genai_types.GenerateContentConfig(**_cfg),
            )
            txt = (resp.text or '').strip()
            if txt:
                answer = txt
                try:
                    from utils.usage_tracker import log_gemini
                    log_gemini(resp, m, op='rag.chat')
                except Exception: pass
                break
        except Exception as e:
            last_err = e
            from utils import llm_health
            # 19/11 emergency mode: 5+15+30 s of sleeps kept the owner waiting 50 s in an outage. In a chat turn, once the
            # breaker is open (two outage errors) or the turn may not call a model, stop; else at most a short pause.
            if llm_health.in_turn() and (llm_health.is_open() or (llm_health.TURN.get() or {}).get('block')):
                break
            if '503' in str(e) and sleep_s:
                time.sleep(min(sleep_s, 2) if llm_health.in_turn() else sleep_s)
                continue
            break
    from utils import llm_health
    if not answer and llm_health.ollama_ok():        # 19/11 only a loaded model, never inside a chat turn with Gemini out
        # Last resort: try local Ollama (qwen2.5:7b) — slow but free, works offline.
        # Better than just dumping raw snippets at the user.
        try:
            import urllib.request as _ur, json as _json
            req = _ur.Request(
                'http://127.0.0.1:11434/api/generate',
                data=_json.dumps({
                    'model': 'qwen2.5:7b',
                    'prompt': user_msg,
                    'stream': False,
                    'options': {'temperature': 0.3, 'num_predict': 1500},
                }).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            )
            with _ur.urlopen(req, timeout=120) as resp_l:
                data_l = _json.loads(resp_l.read().decode('utf-8'))
                local_txt = (data_l.get('response') or '').strip()
            if local_txt:
                answer = '⚙️ _(תשובה ממודל מקומי — Gemini היה עמוס)_\n\n' + local_txt
        except Exception:
            pass

    if not answer:
        answer = f'⚠️ גם Gemini וגם המודל המקומי לא הצליחו. הקטעים הרלוונטיים:\n\n' + \
                 '\n'.join(f'• [[{h["stem"]}]]\n  {h["text"][:200]}...' for h in hits[:3])
    else:
        # Safety net: convert any LaTeX that slipped through
        answer = _latex_to_unicode(answer)
    return {'answer': answer, 'sources': hits, 'used_api': True, 'scope': scope}


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    if len(sys.argv) < 2:
        print('usage: python -m rag.chat "your question"')
        sys.exit(1)
    q = ' '.join(sys.argv[1:])
    res = chat(q)
    print(res['answer'])
    print('\n--- sources ---')
    for s in res['sources']:
        print(f"  [{s['score']}] [[{s['stem']}]] ({s['course_key']})")
