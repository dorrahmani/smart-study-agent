"""
panopto_inventory.py — Logs into Panopto, queries each course folder,
prints the full session list, and diffs against our local metadata.

Use: shows every Panopto session vs every entry we've collected, so we
can manually confirm nothing is missing.
"""
from __future__ import annotations
import sys
import os
import json
from pathlib import Path
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

from collectors.panopto_collector import PanoptoCollector
import yaml


def _date_str(raw: str) -> str:
    """Parse Panopto date string (handles both /Date(ms)/ and ISO)."""
    dt = PanoptoCollector.parse_panopto_date(raw)
    return dt.strftime('%Y-%m-%d') if dt else '????-??-??'


def main():
    # Load config
    with open(ROOT / 'config' / 'courses.yaml', 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)

    pc = PanoptoCollector(config_path=str(ROOT / 'config' / 'courses.yaml'))
    pc.login()

    META_DIR = ROOT / 'data' / 'panopto'

    print('\n' + '=' * 70)
    print('PANOPTO MANUAL INVENTORY  vs  LOCAL METADATA')
    print('=' * 70)

    for course_key, course_cfg in cfg['courses'].items():
        print(f'\n\n========== {course_key} ({course_cfg["name"]}) ==========')

        panopto_cfg = course_cfg['sources']['panopto']
        if not panopto_cfg.get('enabled'):
            print('  Panopto disabled in config')
            continue

        folder_id = panopto_cfg.get('folder_id')
        tutorial_folder_id = panopto_cfg.get('tutorial_folder_id')

        # Load existing metadata
        meta_path = META_DIR / f'{course_key}_metadata.json'
        existing = []
        if meta_path.exists():
            try:
                existing = json.loads(meta_path.read_text(encoding='utf-8'))
            except Exception:
                existing = []
        existing_ids = {e['video_id'] for e in existing}
        existing_dates = sorted({(e['date'], e['title']) for e in existing})

        print(f'  📂 Local metadata: {len(existing)} entries')
        for d, t in existing_dates:
            print(f'     {d}  {t}')

        # Query Panopto folder for ALL sessions (last 30 days)
        for label, fid in [('lectures', folder_id),
                          ('tutorials', tutorial_folder_id)]:
            if not fid or fid == 'TO_BE_FILLED':
                continue
            print(f'\n  🌐 Panopto folder ({label}) {fid}:')
            try:
                sessions = pc.search_sessions(
                    query='', days_back=30, folder_id=fid
                )
            except Exception as e:
                print(f'     ❌ search failed: {e}')
                continue

            print(f'     Found {len(sessions)} sessions in last 30 days')

            for s in sessions:
                vid = s.get('DeliveryID', '')
                raw_title = pc._clean_title(s.get('SessionName', '?'))
                date = _date_str(s.get('StartTime', ''))
                in_meta = '✅' if vid in existing_ids else '🆕'
                folder = s.get('FolderName', '')
                # Trim folder name to keep output narrow
                folder_short = folder[:40] + '…' if len(folder) > 40 else folder
                print(f'     {in_meta} {date}  {raw_title}  [{folder_short}]  vid={vid[:8]}')

    print('\n' + '=' * 70)
    print('LEGEND:')
    print('  ✅ — already in our metadata')
    print('  🆕 — Panopto session NOT in our metadata (collected nothing for it)')
    print('=' * 70)


if __name__ == '__main__':
    main()
