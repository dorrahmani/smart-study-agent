"""scripts/audit_summaries.py — full integrity check of the summaries system."""
from __future__ import annotations
import os, sys, re, shutil
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except: pass

from utils.env import _load_env_dotfile
_load_env_dotfile()

VAULT_ROOT = Path(os.environ.get('OBSIDIAN_VAULT_PATH', str(Path.home() / 'Documents' / 'Obsidian Vault'))) / 'Smart Study Agent'
SUMMARIES_ROOT = ROOT / 'data' / 'summaries'

COURSE_FOLDERS = {
    'מבוא מורחב למדמח': 'intro_to_cs',
    'חשבון דיפרנציאלי ואינטגרלי 2ב': 'calculus_2',
    'מתמטיקה בדידה 2': 'discrete_math_2',
    'אלגברה לינארית 2ב': 'linear_algebra_2',
    "מיקרו כלכלה א'": 'microeconomics',
}

DATE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\s*-\s*')

def classify(stem: str) -> str:
    s = stem.lower()
    if 'תרגול' in s or 'תרגיל' in s or 'recitation' in s:
        return 'tutorials'
    if 'הרצא' in s or 'lecture' in s or 'lec' in s:
        return 'lectures'
    return 'lectures'

stats = defaultdict(int)
print('🔍 Auditing...\n')

for vault_folder, ck in COURSE_FOLDERS.items():
    vault_dir = VAULT_ROOT / vault_folder
    if not vault_dir.exists():
        print(f'⚠️  {vault_folder} — חסר')
        continue
    print(f'📚 {ck}')
    for md in sorted(vault_dir.glob('*.md')):
        if md.stem.startswith('_'): continue
        if not DATE_RE.match(md.stem): continue
        kind = classify(md.stem)
        target_dir = SUMMARIES_ROOT / ck / kind
        target_dir.mkdir(parents=True, exist_ok=True)
        target_md = target_dir / md.name

        if not target_md.exists():
            shutil.copy2(md, target_md)
            print(f'  📝 העתק: {md.name[:50]} → {kind}/')
            stats['md_copied'] += 1
        else:
            stats['md_already_ok'] += 1

        if not target_md.with_suffix('.pdf').exists():
            stats['pdf_missing'] += 1

print(f'\n📊 סיכום:')
for k, v in sorted(stats.items()):
    print(f'  {k}: {v}')

# Generate missing PDFs
if stats.get('pdf_missing', 0) > 0:
    print(f'\n📄 יוצר {stats["pdf_missing"]} PDFs חסרים...')
    try:
        from generators.pdf_generator import markdown_to_pdf
        def md_to_pdf(md_path, pdf_path):
            content = Path(md_path).read_text(encoding='utf-8')
            stem = Path(md_path).stem
            markdown_to_pdf(content, pdf_path, title=stem, course_name='')
    except Exception as e:
        print(f'cannot import markdown_to_pdf: {e}')
        md_to_pdf = None
    if md_to_pdf:
        n_done = 0
        for ck in COURSE_FOLDERS.values():
            for kind in ('lectures', 'tutorials'):
                d = SUMMARIES_ROOT / ck / kind
                if not d.exists(): continue
                for md in d.glob('*.md'):
                    pdf = md.with_suffix('.pdf')
                    if pdf.exists(): continue
                    try:
                        md_to_pdf(str(md), str(pdf))
                        if pdf.exists():
                            n_done += 1
                            print(f'  ✅ {md.name[:50]}')
                    except Exception as e:
                        print(f'  ❌ {md.name[:30]}: {e}')
        print(f'\n📄 PDFs נוצרו: {n_done}')
    else:
        print('⚠️  אין מנוע יצירת PDF זמין')

print('\n🎉 Done.')
