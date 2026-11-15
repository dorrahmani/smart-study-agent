# -*- coding: utf-8 -*-
"""generators/coverage_map.py — Syllabus-vs-taught coverage map.

For a course, compare the FULL intended curriculum against what was ACTUALLY
taught this semester (from real lecture/tutorial summaries), and mark each
topic ✅ taught / ⚠️ partial / ❌ not taught. Material was cut mid-semester
(war), so this flags which old exam topics are no longer relevant.

Robust to bad syllabus PDFs: uses Gemini's knowledge of the standard course
curriculum, augmented by the official syllabus / full lecturer notes when they
extract legibly (with a reversed-Hebrew guard). One Gemini call.
"""
from __future__ import annotations
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

COURSE_HE = {
    'intro_to_cs': 'מבוא מורחב למדעי המחשב', 'calculus_2': 'חשבון דיפרנציאלי ואינטגרלי 2',
    'discrete_math_2': 'מתמטיקה בדידה 2', 'linear_algebra_2': 'אלגברה לינארית 2',
    'microeconomics': 'מיקרו כלכלה א',
}


def _maybe_fix_reversed_hebrew(text: str) -> str:
    """Some RTL PDFs extract Hebrew in visual (reversed) order. Detect via
    common words vs their reverses; if reversed, flip each line."""
    fwd = text.count(' של ') + text.count(' את ') + text.count('הרצאה') + text.count('סילבוס')
    rev = text.count(' לש ') + text.count(' תא ') + text.count('האצרה') + text.count('סובליס')
    if rev > fwd and rev >= 3:
        return '\n'.join(line[::-1] for line in text.splitlines())
    return text


def _read_pdf(path: Path) -> str:
    try:
        from generators.summarizer import AISummarizer
        t = AISummarizer().read_pdf(str(path)) or ''
    except Exception:
        t = ''
    return _maybe_fix_reversed_hebrew(t)


def _syllabus_text(course_key: str, max_chars: int = 7000) -> tuple[str, str]:
    """Return (text, source_label). Prefers official syllabus, then full
    lecturer notes. Empty text if nothing legible."""
    src = ROOT / 'data' / 'moodle' / course_key / 'סילבוס.pdf'
    if src.exists():
        t = _read_pdf(src)
        if len(t) > 400:
            return t[:max_chars], 'סילבוס רשמי'
    try:
        import yaml
        cfg = yaml.safe_load(open(ROOT / 'config' / 'courses.yaml', encoding='utf-8'))
        sp = (cfg.get('courses', {}).get(course_key, {}) or {}).get('sources', {}).get('static_pdf', '')
        if sp:
            p = Path(sp) if Path(sp).is_absolute() else (ROOT / sp)
            if p.exists():
                t = _read_pdf(p)
                if len(t) > 400:
                    return t[:max_chars], 'רשימות המרצה המלאות'
    except Exception:
        pass
    return '', ''


def _covered_corpus(course_key: str, max_chars: int = 16000) -> str:
    base = ROOT / 'data' / 'summaries' / course_key
    lines = []
    for sub in ('lectures', 'tutorials'):
        d = base / sub
        if not d.exists():
            continue
        for md in sorted(d.glob('*.md')):
            if md.stat().st_size < 2000:
                continue
            try:
                txt = md.read_text(encoding='utf-8', errors='replace')
            except Exception:
                continue
            kind = 'תרגול' if sub == 'tutorials' else 'הרצאה'
            lines.append(f'\n### [{kind}] {md.stem}')
            for ln in txt.splitlines():
                s = ln.strip()
                if 'TL;DR' in s:
                    lines.append(re.sub(r'^>\s*📝?\s*\*?\*?TL;DR:?\*?\*?\s*', 'תקציר: ', s)[:300])
                elif s.startswith('## ') and 'הפרופסור' not in s and 'מקורות' not in s:
                    lines.append('• ' + s.lstrip('# ').strip()[:120])
    return '\n'.join(lines)[:max_chars]


def build_coverage_map(course_key: str) -> str:
    course_he = COURSE_HE.get(course_key, course_key)
    covered = _covered_corpus(course_key)
    if len(covered) < 200:
        return f'⚠️ אין מספיק סיכומים ל-{course_he} כדי לבנות מפת כיסוי.'
    syl_text, syl_src = _syllabus_text(course_key)
    if syl_text:
        syl_block = (f'להלן גם ה{syl_src} (השתמש בו כמקור עיקרי לרשימת הנושאים המתוכננת; '
                     f'ייתכנו שיבושי כיוון בטקסט — הסק את הנושאים לפי ההקשר):\n{syl_text}')
    else:
        syl_block = ('(אין סילבוס קריא — הסתמך על הידע שלך על תכנית הלימודים הסטנדרטית '
                     'של הקורס הזה באוניברסיטה ישראלית.)')

    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        return '⚠️ אין מפתח Gemini זמין.'
    prompt = (
        f'אתה בונה *מפת כיסוי* לקורס *{course_he}* לקראת המבחן.\n'
        'רקע קריטי: הסמסטר *ירד חומר בגלל המלחמה*, אז חלק מהנושאים המתוכננים '
        '*לא נלמדו בפועל*. המטרה: לזהות אילו נושאים מתכנית הקורס נלמדו ואילו לא, '
        'כדי שהסטודנט ידע אילו שאלות במבחנים ישנים כבר לא רלוונטיות.\n\n'
        'קבע את *רשימת הנושאים המתוכננת* לפי הידע שלך על הקורס הסטנדרטי + הסילבוס למטה. '
        'אז השווה מול *מה שבאמת נלמד* (מהסיכומים).\n\n'
        f'{syl_block}\n\n'
        'הפק טבלת כיסוי מסודרת לפי סדר הקורס. לכל נושא מרכזי, שורה אחת:\n'
        '✅ <נושא> — נלמד\n'
        '⚠️ <נושא> — חלקית (הוזכר אך לא לעומק)\n'
        '❌ <נושא> — כנראה לא נלמד (ירד)\n\n'
        'כללים:\n'
        '- ✅ רק אם יש עדות ממשית ב"מה שנלמד". נושא מתוכנן שלא מופיע בסיכומים → ❌.\n'
        '- *אל תשתמש בטבלת markdown* (לא נתמכת בטלגרם) — שורה רגילה אחת לכל נושא, '
        'שמתחילה באייקון (✅/⚠️/❌).\n'
        '- בסוף: שורת "📊 נלמדו X מתוך Y נושאים מרכזיים" + אזהרה קצרה: '
        'נושאי ❌ עלולים להופיע במבחנים ישנים אך אינם רלוונטיים — לוודא מול המרצה.\n'
        '- עברית ברורה, בלי LaTeX מסובך. תמציתי — שורה לכל נושא.\n\n'
        f'=== מה שבאמת נלמד הסמסטר ===\n{covered}'
    )
    try:
        from google import genai
        from google.genai import types as gt
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=gt.GenerateContentConfig(
                max_output_tokens=2500, temperature=0.1,
                thinking_config=gt.ThinkingConfig(thinking_budget=0)))
        try:
            from utils.usage_tracker import log_gemini
            log_gemini(resp, 'gemini-2.5-flash', op='coverage_map')
        except Exception:
            pass
        out = (resp.text or '').strip()
        if not out:
            return '⚠️ לא הצלחתי לבנות את המפה.'
        note = '' if syl_text else '\n\n_(מבוסס על תכנית הקורס הסטנדרטית + מה שנלמד — כדאי להצליב מול הסילבוס הרשמי)_'
        return f'🗺️ *מפת כיסוי — {course_he}*\n_(מתוכנן מול מה שנלמד בפועל)_\n\n{out}{note}'
    except Exception as e:
        return f'⚠️ שגיאת Gemini: {e}'


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(ROOT))
    try:
        from utils.env import _load_env_dotfile
        _load_env_dotfile()
    except Exception:
        pass
    ck = sys.argv[1] if len(sys.argv) > 1 else 'microeconomics'
    print(build_coverage_map(ck))
