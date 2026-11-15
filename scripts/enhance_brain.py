"""
scripts/enhance_brain.py
========================
שדרוג עומק לוולט:
  Phase 1 (חינם): שרשרת הרצאות (← prev | next →)
  Phase 2 (חינם): קישורים בין-מושגים (co-occurrence)
  Phase 3 ($0.03): תוכן עשיר לכל מושג עם Gemini Flash

הכל idempotent. בלוקים מסומנים ב-AUTO comments — אפשר להריץ שוב בלי לכפול.
"""
import sys, os, re, json, yaml, time, traceback
from datetime import datetime
from pathlib import Path
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

sys.path.insert(0, '.')
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except: pass

from utils.env import _load_env_dotfile
_load_env_dotfile()

VAULT = Path(os.environ.get('OBSIDIAN_VAULT_PATH', str(Path.home() / 'Documents' / 'Obsidian Vault')))
STUDY = VAULT / 'Smart Study Agent'
CONCEPTS = STUDY / 'מושגים'

config = yaml.safe_load(open('config/courses.yaml', encoding='utf-8').read())
courses_cfg = config.get('courses', config)
COURSE_FOLDER = {
    'intro_to_cs': 'מבוא מורחב למדמח',
    'calculus_2': 'חשבון דיפרנציאלי ואינטגרלי 2ב',
    'discrete_math_2': 'מתמטיקה בדידה 2',
    'linear_algebra_2': 'אלגברה לינארית 2ב',
    'microeconomics': "מיקרו כלכלה א'",
}

FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
def split_fm(text: str):
    m = FM_RE.match(text)
    if not m: return {}, '', text
    try: fm = yaml.safe_load(m.group(1)) or {}
    except: fm = {}
    return fm, text[:m.end()], text[m.end():]

print_lock = Lock()
def log(*a, **kw):
    with print_lock: print(*a, **kw, flush=True)

# ───────────────────────────────────────────────────────────
# Phase 1: Lecture chain navigation
# ───────────────────────────────────────────────────────────
NAV_HEAD = '<!-- AUTO-NAV:BEGIN -->'
NAV_TAIL = '<!-- AUTO-NAV:END -->'

DATE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\s*-\s*(.*)$')

def lecture_chain():
    log('\n[1/3] 🔗 בונה שרשרת ניווט הרצאות...')
    n_total = 0
    for ck, folder in COURSE_FOLDER.items():
        cd = STUDY / folder
        if not cd.exists(): continue
        # אסוף את כל ההערות עם תאריך בשם, חצה לפי type
        notes = []
        for f in cd.glob('*.md'):
            if f.stem.startswith('_'): continue
            m = DATE_RE.match(f.stem)
            if not m: continue
            fm, _, _ = split_fm(f.read_text(encoding='utf-8'))
            ntype = fm.get('type', 'הרצאה')
            notes.append({'path': f, 'date': m.group(1), 'type': ntype, 'stem': f.stem})
        # בנה שרשרות נפרדות לפי type (הרצאה / תרגול)
        for ntype in {'הרצאה', 'תרגול'}:
            chain = sorted([n for n in notes if n['type'] == ntype], key=lambda n: n['date'])
            for i, n in enumerate(chain):
                prev_link = f"← [[{chain[i-1]['stem']}|{chain[i-1]['date']}]]" if i > 0 else "—"
                next_link = f"[[{chain[i+1]['stem']}|{chain[i+1]['date']}]] →" if i < len(chain)-1 else "—"
                block = f"""{NAV_HEAD}
> **{ntype}ות בקורס:** {prev_link} | **{n['date']}** | {next_link}
{NAV_TAIL}"""
                full = n['path'].read_text(encoding='utf-8')
                if NAV_HEAD in full:
                    full = re.sub(re.escape(NAV_HEAD) + r'.*?' + re.escape(NAV_TAIL),
                                   block, full, flags=re.DOTALL)
                else:
                    # הכנס אחרי frontmatter
                    fm_match = FM_RE.match(full)
                    if fm_match:
                        full = full[:fm_match.end()] + '\n' + block + '\n\n' + full[fm_match.end():]
                    else:
                        full = block + '\n\n' + full
                n['path'].write_text(full, encoding='utf-8')
                n_total += 1
    log(f'  ✅ עודכנו {n_total} הערות עם נווטי שרשרת')

# ───────────────────────────────────────────────────────────
# Phase 2: Concept-to-concept co-occurrence links
# ───────────────────────────────────────────────────────────
COOCCUR_HEAD = '<!-- AUTO-COOCCUR:BEGIN -->'
COOCCUR_TAIL = '<!-- AUTO-COOCCUR:END -->'

def concept_cooccurrence():
    log('\n[2/3] 🕸️ מחשב קישורים בין-מושגים מ-co-occurrence...')
    # אסוף concepts: מכל הרצאה
    concept_to_lectures = defaultdict(list)  # concept_stem → [lecture stems]
    for ck, folder in COURSE_FOLDER.items():
        cd = STUDY / folder
        if not cd.exists(): continue
        for f in cd.glob('*.md'):
            if f.stem.startswith('_'): continue
            fm, _, _ = split_fm(f.read_text(encoding='utf-8'))
            for c in (fm.get('concepts') or []):
                if isinstance(c, str):
                    concept_to_lectures[c.strip()].append(f.stem)

    # מטריצת co-occurrence: לכל זוג מושגים — בכמה הרצאות הם מופיעים יחד
    cooccur = defaultdict(Counter)  # a → Counter({b: count})
    for lec_stem in {l for lecs in concept_to_lectures.values() for l in lecs}:
        # מצא את כל המושגים בהרצאה הזו
        cs_in_lec = [c for c, lecs in concept_to_lectures.items() if lec_stem in lecs]
        for i, a in enumerate(cs_in_lec):
            for b in cs_in_lec[i+1:]:
                cooccur[a][b] += 1
                cooccur[b][a] += 1

    # לכל מושג — הוסף top-5 קשורים בגוף הקובץ
    n_concepts = 0
    n_links = 0
    for cf in CONCEPTS.glob('*.md'):
        if cf.stem.startswith('_'): continue
        related = cooccur.get(cf.stem, {})
        if not related: continue
        # סנן: רק כאלה שמופיעים יחד 2+ פעמים, מוגבל ל-7
        top = [(c, n) for c, n in related.most_common(15) if n >= 2][:7]
        if not top: continue
        block_lines = [
            COOCCUR_HEAD,
            '## 🔗 מושגים קשורים',
            '',
            '> נמצאו דרך co-occurrence — מושגים שמופיעים יחד בהרצאות.',
            '',
        ]
        for c, n in top:
            block_lines.append(f'- [[{c}]] ({n} הרצאות משותפות)')
        block_lines.append(COOCCUR_TAIL)
        block_text = '\n'.join(block_lines)
        full = cf.read_text(encoding='utf-8')
        if COOCCUR_HEAD in full:
            full = re.sub(re.escape(COOCCUR_HEAD) + r'.*?' + re.escape(COOCCUR_TAIL),
                           block_text, full, flags=re.DOTALL)
        else:
            full = full.rstrip() + '\n\n' + block_text + '\n'
        cf.write_text(full, encoding='utf-8')
        n_concepts += 1
        n_links += len(top)
    log(f'  ✅ {n_concepts} מושגים קיבלו {n_links} קישורים בין-מושגיים')

# ───────────────────────────────────────────────────────────
# Phase 3: Generate rich content with Gemini Flash
# ───────────────────────────────────────────────────────────
RICH_HEAD = '<!-- AUTO-RICH:BEGIN -->'
RICH_TAIL = '<!-- AUTO-RICH:END -->'

PROMPT_TEMPLATE = """You are an expert tutor. Write a concise reference card in **Hebrew (right-to-left)** for the concept below, based on the lecture excerpts provided.

**Concept name:** `{concept}`
**Course context:** {courses}

**Excerpts from lectures where this concept appears:**
{excerpts}

---

Write a markdown reference card with EXACTLY this structure (in Hebrew):

## הגדרה
(2-3 sentences in Hebrew. Use precise mathematical/technical language. Include LaTeX math like $\\lambda$ if relevant.)

## נוסחה / סימון
(If applicable: the formula, notation, or canonical representation. Use LaTeX `$...$` or `$$...$$`. If not applicable, write "—".)

## דוגמה קצרה
(One concrete example, ideally something a student would calculate by hand. Use LaTeX where helpful.)

## קשור ל
(3-5 directly related concepts as a comma-separated list, in Hebrew or English transliteration matching the original concept names.)

Constraints:
- Total length: 120-180 words.
- Stay grounded in the excerpts; don't invent.
- Hebrew throughout. Math in LaTeX.
- No introductory phrases like "this is a concept that...". Get straight to the definition.
"""

def get_excerpt(lecture_path: Path, concept: str, max_chars: int = 800) -> str:
    """מחפש קטע בהרצאה שמזכיר את המושג."""
    try:
        text = lecture_path.read_text(encoding='utf-8')
    except: return ''
    _, _, body = split_fm(text)
    # חפש את שם המושג (גם בעברית עם hint מ-stem)
    # פשוט: קח 800 תווים סביב הופעה ראשונה, או את 600 הראשונים
    return body[:max_chars]

def generate_concept_content(concept_stem: str, lecture_paths: list[Path],
                              courses_str: str, client) -> str | None:
    """קורא ל-Gemini Flash להפיק תוכן עשיר."""
    excerpts_text = ''
    for i, lp in enumerate(lecture_paths[:3]):  # עד 3 הרצאות
        ex = get_excerpt(lp, concept_stem, 600)
        if ex:
            excerpts_text += f'\n--- מהרצאה {i+1} ({lp.stem}) ---\n{ex}\n'
    if not excerpts_text.strip():
        return None
    prompt = PROMPT_TEMPLATE.format(
        concept=concept_stem,
        courses=courses_str,
        excerpts=excerpts_text[:6000],
    )
    from google.genai import types as genai_types
    backoffs = [10, 30, 60, 120, 240]
    for attempt, sleep_s in enumerate(backoffs, 1):
        try:
            resp = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=2048,
                    temperature=0.3,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            from utils.usage_tracker import log_gemini as _lg; _lg(resp, 'gemini-2.5-flash', op='enhance_brain')
            txt = resp.text or ''
            if len(txt) > 200:
                return txt
            log(f'    ⚠️ {concept_stem}: תגובה קצרה ({len(txt)} chars), ניסיון {attempt}/5')
        except Exception as e:
            err_str = str(e)
            if '503' in err_str or 'UNAVAILABLE' in err_str:
                log(f'    ⏳ {concept_stem}: 503, sleep {sleep_s}s ({attempt}/5)')
            else:
                log(f'    ⚠️ {concept_stem}: {e}')
                return None
        if attempt < 5:
            time.sleep(sleep_s)
    return None

def enrich_concept_content(skip_existing: bool = True):
    log('\n[3/3] 📖 מייצר תוכן עשיר למושגים (Gemini Flash)...')
    # אסוף mapping: concept → [lecture paths]
    concept_to_lec = defaultdict(list)
    concept_to_courses = defaultdict(set)
    for ck, folder in COURSE_FOLDER.items():
        cd = STUDY / folder
        if not cd.exists(): continue
        for f in cd.glob('*.md'):
            if f.stem.startswith('_'): continue
            fm, _, _ = split_fm(f.read_text(encoding='utf-8'))
            for c in (fm.get('concepts') or []):
                if isinstance(c, str):
                    concept_to_lec[c.strip()].append(f)
                    concept_to_courses[c.strip()].add(courses_cfg[ck]['name'])

    # רשימת מושגים לעיבוד
    concept_files = {f.stem: f for f in CONCEPTS.glob('*.md') if not f.stem.startswith('_')}

    # יוזם Gemini client
    from generators.summarizer import AISummarizer
    s = AISummarizer()
    client = s.client

    targets = []
    for stem, cf in concept_files.items():
        text = cf.read_text(encoding='utf-8')
        if skip_existing and RICH_HEAD in text:
            continue
        if stem not in concept_to_lec:
            continue  # אין הרצאות שמזכירות
        targets.append(stem)

    log(f'  🎯 יעדים: {len(targets)} מושגים (מתוך {len(concept_files)})')
    if not targets:
        log('  (כולם כבר עשירים — הסקריפט idempotent)')
        return

    # רץ במקביל × 4
    success = 0
    fail = 0
    def worker(stem):
        nonlocal success, fail
        cf = concept_files[stem]
        lec_paths = concept_to_lec[stem]
        courses = ' + '.join(sorted(concept_to_courses[stem]))
        log(f'  🤖 [{stem}] {len(lec_paths)} הרצאות, {courses}')
        content = generate_concept_content(stem, lec_paths, courses, client)
        if not content:
            fail += 1
            return
        block = f'{RICH_HEAD}\n## 📖 רפרנס מורחב\n\n{content.strip()}\n{RICH_TAIL}'
        full = cf.read_text(encoding='utf-8')
        if RICH_HEAD in full:
            full = re.sub(re.escape(RICH_HEAD) + r'.*?' + re.escape(RICH_TAIL),
                           block, full, flags=re.DOTALL)
        else:
            full = full.rstrip() + '\n\n' + block + '\n'
        cf.write_text(full, encoding='utf-8')
        success += 1
        log(f'    ✅ {stem} ({len(content)} תווים)')

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(worker, t) for t in targets]
        for fut in as_completed(futures):
            try: fut.result()
            except Exception as e:
                log(f'    💥 {e}')
                fail += 1

    log(f'\n  📊 {success} הצליחו · {fail} נכשלו')

# ───────────────────────────────────────────────────────────
def main():
    log(f'🌿 Vault: {STUDY}')
    if not STUDY.exists(): return

    lecture_chain()
    concept_cooccurrence()
    enrich_concept_content(skip_existing=True)

    log('\n🎉 שדרוג המוח הושלם.')

if __name__ == '__main__':
    try: main()
    except Exception as e:
        log(f'💥 קריסה: {e}')
        traceback.print_exc()
