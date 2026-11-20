"""
fix_all_summaries.py — deletes every summary with an error and re-summarizes from the cache.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

from dotenv import load_dotenv
load_dotenv(override=True)

SUMMARIES_DIR = Path('data/summaries')
ERROR_MARKERS = ['שגיאה ביצירת', 'לא ניתן להתחבר', 'אין מקורות זמינים']

# delete files with an error
deleted = []
for md in SUMMARIES_DIR.rglob('*.md'):
    try:
        content = md.read_text(encoding='utf-8', errors='replace')
        if any(m in content for m in ERROR_MARKERS):
            md.unlink()
            pdf = md.with_suffix('.pdf')
            if pdf.exists():
                pdf.unlink()
            deleted.append(md.name)
            print(f'🗑️  מחק: {md}')
    except Exception as e:
        print(f'⚠️  {md}: {e}')

print(f'\n✅ נמחקו {len(deleted)} קבצים עם שגיאות')
print('▶️  מריץ resummarize_from_cache...\n')

# run resummarize
import importlib, runpy
runpy.run_path('scripts/resummarize_from_cache.py', run_name='__main__')
