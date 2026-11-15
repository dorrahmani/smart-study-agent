"""
scripts/merge_concept_duplicates.py
====================================
מאחד 8 קבוצות מושגים כפולים שזוהו ב-🔍 דוח כפילויות.md.
לכל זוג: שומר את הגרסה עם __suffix (יותר ספציפית), מאחד תוכן, מעדכן wikilinks.
"""
import sys, os, re
from pathlib import Path

sys.path.insert(0, '.')
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except: pass

VAULT = Path(os.environ.get('OBSIDIAN_VAULT_PATH', str(Path.home() / 'Documents' / 'Obsidian Vault')))
STUDY = VAULT / 'Smart Study Agent'
CONCEPTS = STUDY / 'מושגים'

# (loser, winner) — מוחקים את loser, מפנים אליו → winner
MERGES = [
    ('budget_constraint',                   'budget_constraint__econ'),
    ('consumer_optimization_problem',       'consumer_optimization_problem__econ'),
    ('eigenspace',                          'eigenspace__math'),
    ('eigenvalue',                          'eigenvalue__math'),
    ('eigenvector',                         'eigenvector__math'),
    ('function_definition',                 'function_definition__cs'),
    ('indifference_curve',                  'indifference_curve__econ'),
    ('utility_function',                    'utility_function__econ'),
    # plural → singular
    ('indifference_curves__econ',           'indifference_curve__econ'),
]

FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)

def split_fm(text: str) -> tuple[str, str]:
    m = FM_RE.match(text)
    if not m:
        return '', text
    return text[:m.end()], text[m.end():]

def merge_pair(loser_stem: str, winner_stem: str, stats: dict):
    loser_path = CONCEPTS / f'{loser_stem}.md'
    winner_path = CONCEPTS / f'{winner_stem}.md'
    if not loser_path.exists():
        print(f'  ⏭️  {loser_stem}: אין קובץ לוזר — מדלג')
        return
    if not winner_path.exists():
        print(f'  ⚠️  {winner_stem}: אין קובץ winner — מדלג')
        return

    # קרא תוכן
    loser_text = loser_path.read_text(encoding='utf-8')
    winner_text = winner_path.read_text(encoding='utf-8')
    _, loser_body = split_fm(loser_text)
    winner_fm, winner_body = split_fm(winner_text)

    # אחד תוכן (הוסף את הloser בסוף ה-winner תחת כותרת)
    loser_body_clean = loser_body.strip()
    if loser_body_clean and loser_body_clean != winner_body.strip():
        merge_section = f'\n\n## תוכן ממוזג מ-`{loser_stem}`\n\n{loser_body_clean}\n'
        new_winner = winner_fm + winner_body.rstrip() + merge_section
        winner_path.write_text(new_winner, encoding='utf-8')
        print(f'  📝 [{winner_stem}] הוספתי תוכן מ-{loser_stem}')

    # עדכן wikilinks בכל הוולט: [[loser]] / [[loser|alias]] → [[winner]] / [[winner|alias]]
    pat_alias = re.compile(r'\[\[' + re.escape(loser_stem) + r'\|([^\]]+)\]\]')
    pat_plain = re.compile(r'\[\[' + re.escape(loser_stem) + r'\]\]')

    files_updated = 0
    links_updated = 0
    for f in STUDY.rglob('*.md'):
        if f == loser_path or f == winner_path:
            continue
        try:
            text = f.read_text(encoding='utf-8')
        except Exception:
            continue
        # ספור הופעות
        n_alias = len(pat_alias.findall(text))
        n_plain = len(pat_plain.findall(text))
        if n_alias + n_plain == 0:
            continue
        # החלף
        text = pat_alias.sub(rf'[[{winner_stem}|\1]]', text)
        text = pat_plain.sub(f'[[{winner_stem}]]', text)
        f.write_text(text, encoding='utf-8')
        files_updated += 1
        links_updated += n_alias + n_plain
    if links_updated:
        print(f'  🔗 [{loser_stem}]: עודכנו {links_updated} קישורים ב-{files_updated} קבצים')

    # מחק את הloser
    loser_path.unlink()
    print(f'  🗑️  נמחק: {loser_stem}.md')

    stats['merged'] += 1
    stats['links_updated'] += links_updated

def main():
    print(f'🌿 Vault: {STUDY}')
    if not CONCEPTS.exists():
        print('❌ אין תיקיית מושגים')
        return

    print(f'\n🔄 מתחיל מיזוג {len(MERGES)} זוגות...\n')
    stats = {'merged': 0, 'links_updated': 0}
    for loser, winner in MERGES:
        print(f'━━ {loser}  →  {winner}')
        merge_pair(loser, winner, stats)
        print()

    print('═══════════════════════════════════')
    print(f'✅ סיום:')
    print(f'  • קבצים שמוזגו: {stats["merged"]}/{len(MERGES)}')
    print(f'  • קישורים שעודכנו: {stats["links_updated"]}')
    # ספירה סופית
    remaining = len([f for f in CONCEPTS.glob('*.md') if not f.stem.startswith('_')])
    print(f'  • מושגים נשארים: {remaining} (היו 70, אמור להיות ~{70-stats["merged"]})')

if __name__ == '__main__':
    main()
