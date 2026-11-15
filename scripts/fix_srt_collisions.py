"""
fix_srt_collisions.py — One-shot cleanup for legacy un-dated SRT paths.

For every metadata entry whose srt_path is un-dated:
  1. Re-download the SRT from Panopto with the dated filename
  2. Update the metadata's srt_path field to point at the dated file

Skips entries that already have dated paths.
The summaries in vault are not touched — they're already correct.
"""
from __future__ import annotations
import sys
import json
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

from collectors.panopto_collector import PanoptoCollector

PANOPTO_DIR = ROOT / 'data' / 'panopto'

COURSES = ['linear_algebra_2', 'microeconomics', 'discrete_math_2',
           'calculus_2', 'intro_to_cs']


def main():
    pc = PanoptoCollector(config_path=str(ROOT / 'config' / 'courses.yaml'))
    pc.login()

    total_fixed = 0
    total_failed = 0
    for course in COURSES:
        meta_path = PANOPTO_DIR / f'{course}_metadata.json'
        if not meta_path.exists():
            continue

        entries = json.loads(meta_path.read_text(encoding='utf-8'))
        print(f'\n=== {course} ({len(entries)} entries) ===')

        changed = False
        for e in entries:
            current_srt = e.get('srt_path', '')
            date = e.get('date', '')
            title = e.get('title', '')
            vid = e.get('video_id', '')

            target_stem = pc._safe_stem(title, course, date)
            target_rel = f'data\\panopto\\{target_stem}.srt'
            target_full = PANOPTO_DIR / f'{target_stem}.srt'

            if current_srt == target_rel:
                # Already dated
                continue

            print(f'  📥 {date} {title}')
            if target_full.exists():
                print(f'     ✅ dated SRT exists ({target_full.stat().st_size} bytes); just updating metadata')
            else:
                # Try Panopto SRT download with dated filename
                try:
                    new_path = pc.download_srt(vid, title, course, date)
                except Exception as ex:
                    print(f'     ❌ download failed: {ex}')
                    total_failed += 1
                    continue

                if not new_path:
                    # No SRT on Panopto — keep the entry's old path; mark as un-dated
                    print(f'     ⚠️  Panopto returned no SRT (likely needs Whisper); leaving as-is')
                    total_failed += 1
                    continue

            # Update metadata
            e['srt_path'] = target_rel
            changed = True
            total_fixed += 1

        if changed:
            meta_path.write_text(
                json.dumps(entries, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
            print(f'  💾 metadata updated')

    print(f'\n=== DONE: {total_fixed} fixed, {total_failed} failed/skipped ===')


if __name__ == '__main__':
    main()
