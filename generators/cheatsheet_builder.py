"""generators/cheatsheet_builder.py — Auto-build a single-page formula cheatsheet per course.

For each course, reads all summary .md files in the vault, extracts formulas/theorems/definitions,
sends them through Gemini to consolidate + organize into a tight, exam-ready cheatsheet.

Output:
    vault/cheatsheets/{course_key}_cheatsheet.md
    vault/cheatsheets/{course_key}_cheatsheet.pdf

Idempotent: skips if no new summaries since last build (hash check on summary list).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAULT_DIR = ROOT / 'vault'
CHEATSHEETS_DIR = VAULT_DIR / 'cheatsheets'
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
        out.extend(sorted(d.glob('*.md'), key=lambda p: p.stem))
    return out


def _course_hash(files: list[Path]) -> str:
    """Hash based on (filename, mtime, size) — invalidates when summaries change."""
    h = hashlib.sha256()
    for f in sorted(files, key=lambda p: p.name):
        try:
            st = f.stat()
            h.update(f'{f.name}:{int(st.st_mtime)}:{st.st_size};'.encode())
        except Exception: pass
    return h.hexdigest()[:16]


def _extract_formula_blocks(text: str) -> list[str]:
    """Grep formulas, definitions, theorems from a summary."""
    blocks: list[str] = []
    # Section headings + LaTeX blocks + definition markers
    patterns = [
        r'(?:^|\n)#+\s*[^\n]{2,80}',                # headings
        r'\$\$[^$]+\$\$',                            # block LaTeX
        r'(?:📌|🔢|🔑)\s*\*\*[^\n*]+\*\*[^\n]+',    # icon-headed lines
        r'(?:^|\n)>\s*🔑\s*\*\*לזכור:?\*\*[^\n]+',  # "remember" blocks
    ]
    for p in patterns:
        for m in re.finditer(p, text, flags=re.MULTILINE):
            s = m.group(0).strip()
            if 15 < len(s) < 600:
                blocks.append(s)
    return blocks


def build_cheatsheet(course_key: str, force: bool = False) -> Path | None:
    """Build a single-page cheatsheet for one course. Returns output .md path."""
    files = _list_course_summaries(course_key)
    if not files:
        print(f'  no summaries for {course_key}')
        return None
    sig = _course_hash(files)
    out_md = CHEATSHEETS_DIR / f'{course_key}_cheatsheet.md'

    # Skip if up-to-date
    if not force and out_md.exists():
        head = out_md.read_text(encoding='utf-8', errors='replace')[:500]
        m = re.search(r'<!--\s*sig:\s*([0-9a-f]+)\s*-->', head)
        if m and m.group(1) == sig:
            return out_md  # cached

    # Extract candidate formulas from all summaries
    all_blocks: list[str] = []
    for f in files:
        try:
            body = f.read_text(encoding='utf-8', errors='replace')
            all_blocks.extend(_extract_formula_blocks(body))
        except Exception: pass

    if not all_blocks:
        print(f'  no formula blocks found in {course_key}')
        return None

    # Cap input size — keep prompt manageable
    joined = '\n\n'.join(all_blocks)
    if len(joined) > 40000:
        joined = joined[:40000]

    course_he = COURSE_HE.get(course_key, course_key)
    prompt = (
        f"אתה בונה דף עזר מרוכז לקורס *{course_he}*. קיבלת קטעי הגדרות, משפטים ונוסחאות "
        f"מכל הסיכומים של הקורס. בנה דף-אחד-עמוד מרוכז ומאורגן:\n\n"
        f"## חוקים:\n"
        f"1. ארגן לפי נושאים גדולים (לא לפי הרצאות).\n"
        f"2. הסר כפילויות — אם אותו משפט מופיע 3 פעמים, פעם אחת בלבד.\n"
        f"3. שמור על LaTeX קיים — אל תהפוך ל-Unicode (זה מסמך Markdown שיתרנדר).\n"
        f"4. השתמש במבנה ברור: כותרות (## נושא), הגדרות מודגשות, נוסחאות ב-$$.\n"
        f"5. מקסימום 2000 מילים — דחוס, ללא מילים מיותרות.\n"
        f"6. **אין הקדמות, אין הסברים, אין דוגמאות מפורטות** — רק עובדות.\n"
        f"7. השתמש באייקונים: 📌 הגדרה | 🔢 משפט | ⚡ נוסחה | 🔑 לזכור.\n\n"
        f"# קטעים גולמיים מסיכומי הקורס:\n\n{joined}"
    )

    from google import genai
    from google.genai import types as genai_types
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    client = genai.Client(api_key=key)
    _model = 'gemini-2.5-flash'   # 5x cheaper than pro, sufficient for derivative content
    resp = client.models.generate_content(
        model=_model,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            max_output_tokens=8000, temperature=0.2,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    try:
        from utils.usage_tracker import log_gemini
        log_gemini(resp, _model, op='cheatsheet.build')
    except Exception: pass
    body = (resp.text or '').strip()
    if not body:
        print(f'  cheatsheet build failed for {course_key}: empty response')
        return None

    CHEATSHEETS_DIR.mkdir(parents=True, exist_ok=True)
    full = (
        f'<!-- sig: {sig} -->\n'
        f'# 📋 דף עזר — {course_he}\n\n'
        f'*מתעדכן אוטומטית מ-{len(files)} סיכומים. עדכון אחרון: '
        f'{__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")}*\n\n'
        f'---\n\n'
        + body
    )
    out_md.write_text(full, encoding='utf-8')

    # Render PDF
    try:
        from generators.pdf_generator import markdown_to_pdf
        out_pdf = out_md.with_suffix('.pdf')
        markdown_to_pdf(body, str(out_pdf), title=f'דף עזר — {course_he}')
    except Exception as e:
        print(f'  PDF render failed: {e}')

    return out_md


def build_all_cheatsheets(force: bool = False) -> dict:
    """Build cheatsheets for all known courses. Returns {course: path|None}."""
    results: dict[str, str | None] = {}
    for ck in COURSE_HE:
        try:
            p = build_cheatsheet(ck, force=force)
            results[ck] = str(p) if p else None
            if p: print(f'  ✅ {ck}: {p.name}')
        except Exception as e:
            print(f'  ❌ {ck}: {e}')
            results[ck] = None
    return results


if __name__ == '__main__':
    import sys
    force = '--force' in sys.argv
    if len(sys.argv) > 1 and sys.argv[1] in COURSE_HE:
        build_cheatsheet(sys.argv[1], force=force)
    else:
        build_all_cheatsheets(force=force)
