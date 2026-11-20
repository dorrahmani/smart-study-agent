"""
scripts/vault_assistant.py — the assistant ↔ the Obsidian vault, twice a day (16/11). No model, no paid API: $0.

    venv/bin/python3 scripts/vault_assistant.py --dry --out /tmp/vault_dry    # write the would-be notes there, touch nothing
    venv/bin/python3 scripts/vault_assistant.py                               # the real run (cron)

The real run, in order (each step on its own; one failing doesn't stop the rest):
  1. The owner's "✍️ שלי" from the PC: Drive (vault_inbox) → data/vault_inbox_staging → the vault (vault_sync/mine.merge: never
     deletes, conflicts keep both)
  2. the assistant area "👩‍💼 העוזר" (vault_sync/assistant_export): only notes whose text changed are written
  3. between 08:00 and 22:00: new "תזכיר לי…" / "לעוזר:" lines in his notes → a ✅ question in WhatsApp (asked once each)
  3b. 18/11 past-exam statistics per course (agent/exam_plan) → data/exam_plan + "👩‍💼 העוזר/🎓 לימודים/📊 מבחנים"
  4. 17/11 the graph (vault_sync/maps): a map per area, a hub per course, dead links in old week digests → text
  5. 17/11 the BM25 index of his / the assistant's / the brain's notes → data/personal_index.json (the mind's memory tool)
  6. the assistant folder + the notes the maps wrote → Drive (rclone COPY, never sync), so the PC's pull has them without
     waiting for the daily pipeline
Cron (VM, Israel time): 40 8,23 * * *  — see docs/RUNBOOK or the commit that added this.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REMOTE_VAULT = 'gdrive:smart-study-agent/vault'


def _weekly():
    from scripts import weekly_bot_report as w
    return w.build_text(w.collect())


def _push(vault: Path, folders: list[str]) -> None:
    for f in folders:
        d = vault / f
        if not d.is_dir():
            continue
        try:                                                 # copy, never sync (global rule)
            r = subprocess.run(['rclone', 'copy', str(d), f'{REMOTE_VAULT}/{f}', '--transfers', '4', '--quiet',
                                '--exclude', '.replaced/**'], capture_output=True, text=True, timeout=600)
            print(f'  push {f}: {"ok" if r.returncode == 0 else "rclone " + str(r.returncode)}')
        except (OSError, subprocess.SubprocessError) as e:
            print(f'  push {f} failed: {e!r}'[:200])


def _push_files(vault: Path, rels: list[str]) -> None:
    """Only these notes → Drive (copy, never sync)."""
    rels = sorted({r for r in rels if r})
    if not rels:
        return
    lst = ROOT / 'data' / 'vault_push_list.txt'
    try:
        lst.write_text('\n'.join(rels) + '\n', encoding='utf-8')
        r = subprocess.run(['rclone', 'copy', str(vault), REMOTE_VAULT, '--files-from-raw', str(lst), '--transfers', '4',
                            '--quiet'], capture_output=True, text=True, timeout=600)
        print(f'  push {len(rels)} map notes: {"ok" if r.returncode == 0 else "rclone " + str(r.returncode)}')
    except (OSError, subprocess.SubprocessError) as e:
        print(f'  push maps failed: {e!r}'[:200])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=True)            # scrub() masks his TAU ID / user name by value
    except ImportError:
        pass
    from config.paths import VAULT_DIR
    from vault_sync import assistant_export, mine
    from vault_sync.privacy import AREA, MINE
    dry = '--dry' in argv
    out = Path(argv[argv.index('--out') + 1]) if '--out' in argv else None
    if dry and out is None:
        out = Path('/tmp/vault_assistant_dry')
    vault = Path(VAULT_DIR)
    now = datetime.now()
    if not dry:
        try:
            if mine.pull():
                print('  mine merge:', mine.merge(mine.STAGING, vault / MINE))
            else:
                print('  mine pull: nothing (rclone missing or remote empty)')
        except Exception as e:
            print(f'  ⚠️ mine: {e!r}'[:300])
    stats = assistant_export.export(vault_dir=vault, out_root=out, now=now, weekly_text=_weekly)
    written = stats.pop('written', [])
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    if not dry:
        try:                                                 # 18/11 exam statistics per course (agent/exam_plan): $0, cached text
            from agent import exam_plan
            print('  exam plan:', json.dumps(exam_plan.build_all(vault_dir=vault), ensure_ascii=False))
        except Exception as e:
            print(f'  ⚠️ exam plan: {e!r}'[:300])
        try:                                                 # 19/11 the nightly scrape's feedback + quiz reviews → the log, notes
            from agent import grades
            print('  grades: new', [r['id'] for r in grades.refresh_local()])
        except Exception as e:
            print(f'  ⚠️ grades: {e!r}'[:300])
    map_written: list[str] = []
    try:
        from vault_sync import maps
        ms = maps.build(vault, out_root=out)
        map_written = ms.pop('written', [])
        print('  maps:', json.dumps(ms, ensure_ascii=False))
    except Exception as e:
        print(f'  ⚠️ maps: {e!r}'[:300])
    if dry:
        print(f'dry run: notes under {out}; nothing written to the vault, nothing sent')
        return 0
    try:
        from vault_sync import personal_index
        idx = personal_index.build_index(vault, save=personal_index.INDEX)
        print(f'  personal index: {len(idx["sig"])} notes, {len(idx["chunks"])} paragraphs')
    except Exception as e:
        print(f'  ⚠️ personal index: {e!r}'[:300])
    if written:
        print(f'  {len(written)} notes written')
    if 8 <= now.hour < 22:
        try:
            from whatsapp import outbox
            print('  inbox asked:', mine.ask(outbox.owner_chat(), vault / MINE))
        except Exception as e:
            print(f'  ⚠️ inbox: {e!r}'[:300])
    if os.environ.get('SSA_VAULT_PUSH', '1') != '0':
        _push(vault, [AREA])
        _push_files(vault, map_written)
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    os.chdir(str(ROOT))
    sys.exit(main())
