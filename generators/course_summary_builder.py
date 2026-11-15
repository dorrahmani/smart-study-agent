"""generators/course_summary_builder.py — Generate a comprehensive course summary.

Different from cheatsheet:
  • cheatsheet = 1-page formula reference, dense, no explanations
  • course_summary = 3-5 page comprehensive overview with brief explanations,
    main theorems with conditions+conclusions, one example per major theorem,
    organized chronologically by topic.

Output:
    vault/course_summaries/{course_key}_summary.md
    vault/course_summaries/{course_key}_summary.pdf

Idempotent: only rebuilds when summary list changes (sig hash).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAULT_DIR = ROOT / 'vault'
COURSE_SUMMARIES_DIR = VAULT_DIR / 'course_summaries'
SUMMARIES_DIR = ROOT / 'data' / 'summaries'

COURSE_HE = {
    'intro_to_cs':       'מבוא מורחב למדעי המחשב',
    'calculus_2':        'חדו״א 2',
    'discrete_math_2':   'בדידה 2',
    'linear_algebra_2':  'לינארית 2',
    'microeconomics':    'מיקרו א\'',
}


def _list_course_summaries(course_key: str) -> list[Path]:
    base = SUMMARIES_DIR / course_key
    if not base.exists(): return []
    out: list[Path] = []
    for sub in ('lectures', 'tutorials'):
        d = base / sub
        if not d.exists(): continue
        # Sort chronologically by stem (date-prefixed)
        out.extend(sorted(d.glob('*.md'), key=lambda p: p.stem))
    return out


def _sig(files: list[Path]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda p: p.name):
        try:
            st = f.stat()
            h.update(f'{f.name}:{int(st.st_mtime)}:{st.st_size};'.encode())
        except Exception: pass
    return h.hexdigest()[:16]


def _extract_key_content(text: str) -> str:
    """Pull headings + theorem blocks + definition blocks + 'remember' blocks."""
    blocks: list[str] = []
    # Headings (h2, h3, h4)
    for m in re.finditer(r'(?:^|\n)#{2,4}\s*([^\n]{2,100})', text, flags=re.MULTILINE):
        blocks.append(f'### {m.group(1).strip()}')
    # Theorem/definition/key blocks (icon-led)
    patterns = [
        r'(?:📌|🔢|🔑)\s*\*\*[^\n*]+\*\*[^\n]{5,400}',
        r'(?:^|\n)>\s*🔑\s*\*\*[^\n]+\*\*[^\n]+',
    ]
    for p in patterns:
        for m in re.finditer(p, text, flags=re.MULTILINE):
            s = m.group(0).strip()
            if 15 < len(s) < 800:
                blocks.append(s)
    # LaTeX equations (display)
    for m in re.finditer(r'\$\$[^$]+\$\$', text):
        s = m.group(0).strip()
        if 10 < len(s) < 500:
            blocks.append(s)
    return '\n\n'.join(blocks)


def build_course_summary(course_key: str, force: bool = False) -> Path | None:
    files = _list_course_summaries(course_key)
    if not files:
        return None
    sig = _sig(files)
    out_md = COURSE_SUMMARIES_DIR / f'{course_key}_summary.md'
    if not force and out_md.exists():
        head = out_md.read_text(encoding='utf-8', errors='replace')[:500]
        m = re.search(r'<!--\s*sig:\s*([0-9a-f]+)\s*-->', head)
        if m and m.group(1) == sig:
            return out_md

    # Extract key content + light context from each file
    sections: list[str] = []
    for f in files:
        try:
            body = f.read_text(encoding='utf-8', errors='replace')
            key = _extract_key_content(body)
            if key:
                sections.append(f'## מקור: {f.stem}\n\n{key}')
        except Exception: pass

    if not sections:
        return None

    joined = '\n\n---\n\n'.join(sections)
    if len(joined) > 70000:
        joined = joined[:70000]

    course_he = COURSE_HE.get(course_key, course_key)
    # Build source-attribution list (lecturers + tutorials)
    source_list = '\n'.join(f'  - {f.stem}' for f in files)
    prompt = (
        f"אתה בונה *סיכום-על מקיף* של הקורס *{course_he}* — מסמך לימוד 4-7 עמודים "
        f"לתלמיד שרוצה להבין את **כל הקורס מאפס**. \n\n"
        f"### מטרת המסמך:\n"
        f"קורא שמעולם לא שמע על הקורס אמור לסיים את המסמך הזה ולהבין את כל הידע "
        f"הכללי שצריך — מה הקורס מלמד, מה המושגים העיקריים, איך הם מתחברים, "
        f"איפה הם בלימודים. בלי לוותר על עומק.\n\n"
        f"### הבדל מ-cheatsheet:\n"
        f"זה לא דף נוסחאות יבש. זה ספר-לימוד מקוצר. כל מי שיקרא יבין את הקורס.\n\n"
        f"### חוקים:\n"
        f"1. **ארגן לפי נושאים תיאורטיים** (לא לפי הרצאות), בסדר *כרונולוגי*: מהתחלת הקורס לסופו.\n"
        f"2. לכל **נושא**: פתח בפסקת מבוא (2-3 משפטים) שמסבירה למה הנושא חשוב.\n"
        f"3. אז רשום את ההגדרות, המשפטים והנוסחאות העיקריים — עם תנאים מלאים.\n"
        f"4. לכל משפט עיקרי הוסף **דוגמה אחת קצרה** או יישום (לא הוכחה מלאה).\n"
        f"5. **בלי דוגמאות מפורטות, בלי הוכחות מלאות** — סיכום, לא ספר.\n"
        f"6. שמור על LaTeX קיים — המסמך יתרנדר כ-PDF דרך MathJax.\n"
        f"7. השתמש באייקונים: 📌 הגדרה | 🔢 משפט | ⚡ נוסחה | 💡 דוגמה | 🔑 לזכור | 💎 טיפ זהב.\n"
        f"8. תוצר: 4000-7000 מילים. אם פחות מ-3000 — חסר תוכן, הוסף.\n"
        f"9. **שמות נושאים בעברית**. **אסור LaTeX מסובך** — השתמש ב-Unicode הכי שאפשר.\n\n"
        f"### חובה לכלול:\n\n"
        f"**א. ייחוס מקורות לכל נושא** — בסוף כל סעיף נושא, ציין מאיזה הרצאות/תרגולים נלקח המידע:\n"
        f"   `📚 *מקורות:* הרצאה X (YYYY-MM-DD), תרגול Y (YYYY-MM-DD), ...`\n"
        f"   השתמש בשמות הקבצים שלי בדיוק (מהרשימה למטה).\n\n"
        f"**ב. סעיף 'טיפי זהב למבחנים'** בסוף המסמך — אסוף את כל הטיפים שהמרצים נתנו לאורך הקורס:\n"
        f"   - מה יבוא במבחן ('המרצה הדגיש ש...')\n"
        f"   - טעויות נפוצות שצריך להיזהר מהן\n"
        f"   - טריקים לזהות באיזה כלי להשתמש\n"
        f"   - דברים שחסרים בספרים אבל המרצים סיפרו\n"
        f"   צטט מילולית כשאפשר, ציין מאיזה הרצאה.\n\n"
        f"### תבנית לכל נושא:\n"
        f"```\n"
        f"## <שם הנושא>\n\n"
        f"<פסקת מבוא — למה הנושא חשוב, מה הוא בא לפתור>\n\n"
        f"📌 *הגדרה:* <הגדרה פורמלית עם כל התנאים>\n\n"
        f"🔢 *משפט [שם המשפט]:*\n"
        f"  - הנחות: ...\n"
        f"  - מסקנה: ...\n\n"
        f"💡 *דוגמה:* <דוגמה ממוקדת בשורה-שתיים>\n\n"
        f"🔑 *לזכור:* <נקודה קריטית, מלכודת נפוצה, מה לעולם לא לבלבל>\n\n"
        f"📚 *מקורות:* <שמות הרצאות/תרגולים>\n"
        f"```\n\n"
        f"### תבנית לסעיף 'טיפי זהב' בסוף המסמך:\n"
        f"```\n"
        f"## 💎 טיפי זהב למבחנים ולהבנת החומר\n\n"
        f"### למבחן (מה הדגישו המרצים)\n"
        f"- 🚨 *הרצאה X:* '<ציטוט>' — <משמעות>\n"
        f"...\n\n"
        f"### טעויות נפוצות\n"
        f"- ⚠️ <טעות>: <איך להימנע>\n"
        f"...\n\n"
        f"### טריקים והבנות עומק\n"
        f"- 🎯 <טריק>: <מתי להשתמש>\n"
        f"...\n"
        f"```\n\n"
        f"# רשימת המקורות הזמינים (השתמש בשמות אלה בדיוק לייחוס):\n{source_list}\n\n"
        f"# חומר הקורס (קטעים מתוך {len(files)} סיכומים):\n\n{joined}"
    )

    from google import genai
    from google.genai import types as genai_types
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        return None
    client = genai.Client(api_key=key)
    _model = 'gemini-2.5-flash'   # 5x cheaper than pro; sufficient for derivative content
    resp = client.models.generate_content(
        model=_model,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            max_output_tokens=15000, temperature=0.2,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    try:
        from utils.usage_tracker import log_gemini
        log_gemini(resp, _model, op='course_summary.build')
    except Exception: pass

    body = (resp.text or '').strip()
    if not body or len(body) < 500:
        return None

    COURSE_SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    full = (
        f'<!-- sig: {sig} -->\n'
        f'# 📚 סיכום-על — {course_he}\n\n'
        f'*מסמך לימוד מקיף שנבנה אוטומטית מ-{len(files)} סיכומים. '
        f'עדכון אחרון: {datetime.now().strftime("%Y-%m-%d %H:%M")}*\n\n'
        f'---\n\n' + body
    )
    out_md.write_text(full, encoding='utf-8')

    # Render PDF
    try:
        from generators.pdf_generator import markdown_to_pdf
        out_pdf = out_md.with_suffix('.pdf')
        markdown_to_pdf(body, str(out_pdf), title=f'סיכום-על — {course_he}')
    except Exception as e:
        print(f'  PDF render failed: {e}')

    return out_md


def build_all(force: bool = False) -> dict:
    out: dict[str, str | None] = {}
    for ck in COURSE_HE:
        try:
            p = build_course_summary(ck, force=force)
            out[ck] = str(p) if p else None
            if p: print(f'  ✅ {ck}: {p.name}')
        except Exception as e:
            print(f'  ❌ {ck}: {e}')
            out[ck] = None
    return out


if __name__ == '__main__':
    import sys
    force = '--force' in sys.argv
    if len(sys.argv) > 1 and sys.argv[1] in COURSE_HE:
        p = build_course_summary(sys.argv[1], force=force)
        print(p if p else 'failed')
    else:
        build_all(force=force)
