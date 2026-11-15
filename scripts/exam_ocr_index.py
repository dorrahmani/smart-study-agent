# -*- coding: utf-8 -*-
"""scripts/exam_ocr_index.py — OCR past exams (scanned PDFs) into searchable text.

For every PDF under data/exams_raw/{course_key}/, run Gemini Vision OCR once,
save structured markdown to data/exams_text/{course_key}/{stem}.md, and mirror
into the Obsidian vault (🧾 מבחנים/{course}) with frontmatter so the RAG index
picks it up (course_key filter works). Cached: a PDF whose .md already exists
is never re-OCR'd — the cost is one-time.

Run:  python scripts/exam_ocr_index.py            # process all missing
      python scripts/exam_ocr_index.py --limit 5  # first 5 (smoke test)
"""
from __future__ import annotations
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

from utils.env import _load_env_dotfile  # noqa: E402
_load_env_dotfile()
if os.environ.get('GEMINI_API_KEY'):
    os.environ.pop('GOOGLE_API_KEY', None)

RAW = ROOT / 'data' / 'exams_raw'
OUT = ROOT / 'data' / 'exams_text'
BUDGET_LOCK = ROOT / 'data' / 'budget_lock.json'

COURSE_HE = {
    'intro_to_cs': 'מבוא מורחב למדמ"ח', 'calculus_2': 'חדו"א 2ב',
    'discrete_math_2': 'בדידה 2', 'linear_algebra_2': 'לינארית 2ב',
    'microeconomics': 'מיקרו א',
    'probability': 'הסתברות', 'software_1': 'תוכנה 1',
    'microeconomics_2': 'מיקרו ב', 'macroeconomics': 'מאקרו',
}
MOED = {'A': 'א', 'B': 'ב', 'C': 'ג'}


def parse_name(stem: str) -> dict:
    """Infer year / moed / is_solution from the filename."""
    s = stem
    is_sol = bool(re.search(r'SOL|פתרון', s, re.IGNORECASE))
    m = re.search(r'(20\d\d)', s)
    year = m.group(1) if m else '?'
    moed = '?'
    m2 = re.search(r'20\d\d[AB](A|B|C|Sample|mid)', s, re.IGNORECASE)
    if m2:
        g = m2.group(1)
        moed = 'לדוגמה' if g.lower() == 'sample' else ('אמצע' if g.lower() == 'mid' else MOED.get(g.upper(), g))
    elif 'מועד א' in s:
        moed = 'א'
    elif 'מועד ב' in s:
        moed = 'ב'
    elif 'לדוגמה' in s or 'Sample' in s:
        moed = 'לדוגמה'
    return {'year': year, 'moed': moed, 'is_sol': is_sol}


def ocr_pdf(pdf: Path, ck: str, meta: dict, client, types) -> str | None:
    kind = 'פתרון מבחן' if meta['is_sol'] else 'מבחן'
    prompt = (
        f'זהו {kind} בקורס {COURSE_HE.get(ck, ck)} (שנה {meta["year"]}, מועד {meta["moed"]}). '
        'תמלל אותו למסמך markdown בעברית, מובנה כך:\n'
        '- לכל שאלה: כותרת "## שאלה N (נושא: <הנושא המתמטי/כלכלי של השאלה>)" '
        'ואז נוסח השאלה המלא (כולל כל הסעיפים). מתמטיקה ב-LaTeX פשוט ($...$).\n'
        + ('- מכיוון שזה פתרון: אחרי כל שאלה כתוב את עיקרי דרך הפתרון והתשובה הסופית.\n' if meta['is_sol'] else '')
        + '- אל תמציא טקסט שאינו קריא — כתוב [לא קריא] במקומו.\n'
        '- בלי הקדמות — התחל ישר מהשאלה הראשונה.'
    )
    data = pdf.read_bytes()
    if len(data) > 18 * 1024 * 1024:
        return None
    resp = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=[types.Part.from_bytes(data=data, mime_type='application/pdf'), prompt],
        config=types.GenerateContentConfig(
            max_output_tokens=6000, temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=0)),
    )
    try:
        from utils.usage_tracker import log_gemini
        log_gemini(resp, 'gemini-2.5-flash', op='exam_ocr')
    except Exception:
        pass
    txt = (resp.text or '').strip()
    return txt if len(txt) > 200 else None


def vault_mirror(ck: str, stem: str, meta: dict, body: str) -> None:
    try:
        from config.paths import VAULT_DIR  # already points at '.../Smart Study Agent'
        d = Path(VAULT_DIR) / '🧾 מבחנים' / COURSE_HE.get(ck, ck)
        d.mkdir(parents=True, exist_ok=True)
        kind = 'פתרון' if meta['is_sol'] else 'מבחן'
        fm = (f'---\ntitle: "{kind} {meta["year"]} מועד {meta["moed"]} — {COURSE_HE.get(ck, ck)}"\n'
              f'course_key: {ck}\ntype: exam\nyear: "{meta["year"]}"\nmoed: "{meta["moed"]}"\n'
              f'is_solution: {str(meta["is_sol"]).lower()}\ndate: "{meta["year"]}-01-01"\n---\n\n')
        (d / f'{stem}.md').write_text(fm + body, encoding='utf-8')
    except Exception as e:
        print(f'  vault mirror failed: {e}', flush=True)


def main() -> None:
    limit = None
    if '--limit' in sys.argv:
        limit = int(sys.argv[sys.argv.index('--limit') + 1])
    from google import genai
    from google.genai import types
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    client = genai.Client(api_key=key)

    done = failed = skipped = 0
    for ck_dir in sorted(RAW.iterdir()):
        if not ck_dir.is_dir():
            continue
        ck = ck_dir.name
        for pdf in sorted(ck_dir.glob('*.pdf')):
            out_md = OUT / ck / f'{pdf.stem}.md'
            if out_md.exists() and out_md.stat().st_size > 300:
                skipped += 1
                continue
            if BUDGET_LOCK.exists():
                print('⛔ budget locked — stopping', flush=True)
                print(f'PROGRESS done={done} failed={failed} skipped={skipped}', flush=True)
                print('DONE', flush=True)
                return
            if limit is not None and done >= limit:
                print(f'PROGRESS done={done} failed={failed} skipped={skipped} (limit)', flush=True)
                print('DONE', flush=True)
                return
            meta = parse_name(pdf.stem)
            try:
                txt = ocr_pdf(pdf, ck, meta, client, types)
            except Exception as e:
                print(f'  ❌ {ck}/{pdf.name}: {str(e)[:90]}', flush=True)
                failed += 1
                continue
            if not txt:
                print(f'  ⚠️ empty OCR: {ck}/{pdf.name}', flush=True)
                failed += 1
                continue
            out_md.parent.mkdir(parents=True, exist_ok=True)
            out_md.write_text(txt, encoding='utf-8')
            vault_mirror(ck, pdf.stem, meta, txt)
            done += 1
            print(f'  ✅ [{done}] {ck}/{pdf.name} ({len(txt)} chars)', flush=True)

    print(f'PROGRESS done={done} failed={failed} skipped={skipped}', flush=True)
    # rebuild RAG so the bot can search exams immediately
    try:
        from rag.vault_indexer import build_index
        r = build_index(force=False) or {}
        print(f'RAG rebuilt: {r.get("chunks", r.get("n_chunks", "?"))} chunks', flush=True)
    except Exception as e:
        print(f'RAG rebuild failed: {e}', flush=True)
    print('DONE', flush=True)


if __name__ == '__main__':
    main()
