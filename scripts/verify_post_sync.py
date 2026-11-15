"""
verify_post_sync.py — Run after sync_all to:
  1. Re-audit SRT collisions (uses dated-filename code path)
  2. Verify discrete_math_2 19.04 was re-collected with new dated filename
  3. Cross-reference vault state vs metadata per course

Output: a structured Hebrew report.
"""
from __future__ import annotations
import os
import sys
import json
from pathlib import Path
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
VAULT = Path(os.environ.get('OBSIDIAN_VAULT_PATH', str(Path.home() / 'Documents' / 'Obsidian Vault'))) / 'Smart Study Agent'

COURSES = {
    'calculus_2':       'חשבון דיפרנציאלי ואינטגרלי 2ב',
    'linear_algebra_2': 'אלגברה לינארית 2ב',
    'discrete_math_2':  'מתמטיקה בדידה 2',
    'microeconomics':   "מיקרו כלכלה א'",
    'intro_to_cs':      'מבוא מורחב למדמח',
}


def audit_collisions():
    print('\n' + '=' * 70)
    print('§1  SRT COLLISION AUDIT')
    print('=' * 70)

    META_DIR = ROOT / 'data' / 'panopto'
    overall_clean = True

    for ck in COURSES:
        meta_path = META_DIR / f'{ck}_metadata.json'
        if not meta_path.exists():
            continue

        entries = json.loads(meta_path.read_text(encoding='utf-8'))
        by_srt = defaultdict(list)
        for e in entries:
            by_srt[e.get('srt_path', '')].append(e)

        collisions = {k: v for k, v in by_srt.items() if len(v) > 1}
        if not collisions:
            print(f'  ✅ {ck}: {len(entries)} entries, no SRT collisions')
        else:
            overall_clean = False
            print(f'  ⚠️  {ck}: {len(collisions)} collisions')
            for srt, items in collisions.items():
                print(f'     {srt}')
                for e in items:
                    print(f'       - {e.get("date","?")} | {e.get("title","?")}')

    return overall_clean


def verify_discrete_19_04():
    print('\n' + '=' * 70)
    print('§2  DISCRETE 2026-04-19 RE-COLLECTION')
    print('=' * 70)

    meta_path = ROOT / 'data' / 'panopto' / 'discrete_math_2_metadata.json'
    entries = json.loads(meta_path.read_text(encoding='utf-8'))

    target = [e for e in entries if e.get('date') == '2026-04-19']
    if not target:
        print('  ❌ No 2026-04-19 entry found in metadata — sync did NOT re-collect')
        return False

    e = target[0]
    print(f'  ✅ Entry exists: {e["title"]}')
    print(f'     SRT: {e["srt_path"]}')

    srt_full = ROOT / e['srt_path']
    if not srt_full.exists():
        # Maybe new filename has date prefix
        possible = list((ROOT / 'data' / 'panopto').glob('*2026-04-19*.srt'))
        if possible:
            print(f'  ℹ️  SRT in metadata path missing, but found dated file:')
            for p in possible:
                print(f'     {p.name}  ({p.stat().st_size} bytes)')
            return True
        print(f'  ❌ SRT file does not exist: {srt_full}')
        return False

    size = srt_full.stat().st_size
    print(f'  ✅ SRT file size: {size:,} bytes')

    # Check vault note exists
    vault_dir = VAULT / 'מתמטיקה בדידה 2'
    matching = list(vault_dir.glob('2026-04-19*.md')) if vault_dir.exists() else []
    if matching:
        print(f'  ✅ Vault note: {matching[0].name}')
        return True
    print('  ⚠️  No vault note for 2026-04-19 yet (summary not regenerated)')
    return False


def cross_reference():
    print('\n' + '=' * 70)
    print('§3  METADATA vs VAULT (per course)')
    print('=' * 70)

    for ck, vault_dir_name in COURSES.items():
        meta_path = ROOT / 'data' / 'panopto' / f'{ck}_metadata.json'
        vault_dir = VAULT / vault_dir_name

        meta = []
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
            except Exception:
                pass

        meta_dates = sorted({(e.get('date', '?'), e.get('title', '?'))
                             for e in meta})

        vault_files = []
        if vault_dir.exists():
            vault_files = sorted([
                f for f in vault_dir.rglob('*.md')
                if any(d in f.name for d in ['2026-04', '2026-05'])
            ])

        print(f'\n--- {ck} ---')
        print(f'  Metadata: {len(meta)}, Vault notes: {len(vault_files)}')

        # Build vault date set
        vault_date_set = set()
        for f in vault_files:
            # extract YYYY-MM-DD from filename
            name = f.name
            if len(name) >= 10 and name[4] == '-' and name[7] == '-':
                vault_date_set.add(name[:10])

        meta_date_set = {e.get('date', '') for e in meta}

        only_in_meta = sorted(meta_date_set - vault_date_set - {''})
        only_in_vault = sorted(vault_date_set - meta_date_set)
        in_both = sorted(meta_date_set & vault_date_set)

        if only_in_meta:
            print(f'  📋 In metadata, NOT yet in vault: {only_in_meta}')
        if only_in_vault:
            print(f'  📁 In vault, not in metadata: {only_in_vault}')
        if in_both:
            print(f'  ✅ In both: {in_both}')


if __name__ == '__main__':
    print('=== POST-SYNC VERIFICATION ===')
    clean = audit_collisions()
    discrete_ok = verify_discrete_19_04()
    cross_reference()

    print('\n' + '=' * 70)
    print('SUMMARY')
    print('=' * 70)
    print(f'  Collision-free: {"✅" if clean else "⚠️  collisions remain"}')
    print(f'  Discrete 19.04: {"✅" if discrete_ok else "❌ missing/incomplete"}')
