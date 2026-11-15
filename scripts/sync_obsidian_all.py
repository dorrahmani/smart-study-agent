"""scripts/sync_obsidian_all.py — מעדכן את כל הסיכומים+חידונים ב-Obsidian + מריץ relinker."""
import sys, traceback
sys.path.insert(0, '.')
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except: pass
sys.stdout = open(sys.stdout.fileno(), 'w', encoding='utf-8', errors='replace', buffering=1)

from utils.env import _load_env_dotfile
_load_env_dotfile()

from exporters.obsidian_exporter import export_all_summaries, export_all_quizzes, export_assignments
from vault_sync.relinker import relink

print('🌿 ייצוא כל הסיכומים ל-Vault (force=True)...', flush=True)
n_sum = export_all_summaries(force=True)
print(f'  ✅ {n_sum} סיכומים יוצאו/עודכנו', flush=True)

print('\n🧠 ייצוא כל החידונים ל-Vault...', flush=True)
try:
    n_q = export_all_quizzes(force=True)
    print(f'  ✅ {n_q} חידונים יוצאו', flush=True)
except Exception as e:
    print(f'  ⚠️ חידונים: {e}', flush=True)

print('\n📝 ייצוא מטלות...', flush=True)
try:
    p = export_assignments()
    print(f'  ✅ נשמר: {p}', flush=True)
except Exception as e:
    print(f'  ⚠️ מטלות: {e}', flush=True)

print('\n🔗 מריץ relinker (קישורים, מושגים)...', flush=True)
try:
    stats = relink(use_api=True, write_concept_notes=True)
    print(f'  ✅ relinker: {stats}', flush=True)
except Exception as e:
    print(f'  ⚠️ relinker: {e}', flush=True)
    traceback.print_exc()

print('\n🎉 סנכרון Obsidian הושלם.')
