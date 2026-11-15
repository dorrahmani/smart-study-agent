"""
scripts/nlm_remove_moved_aside.py — sources a notebook still holds whose file was moved aside to replaced/.

sync_sources only adds, so a summary moved aside after it was uploaded stays in the notebook. Nothing runs this on its
own: the nightly sync only logs how many there are. Without --apply it changes nothing and prints what it would remove.
The files themselves are not touched. No LLM.

    venv312/bin/python scripts/nlm_remove_moved_aside.py --course software_1 --only 'מיקרו כלכלה ב'
    venv312/bin/python scripts/nlm_remove_moved_aside.py --course software_1 --only 'מיקרו כלכלה ב' --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description='moved-aside sources still in the notebooks; changes nothing without --apply')
    ap.add_argument('--course', action='append', help='only this course (repeatable)')
    ap.add_argument('--only', default='', help='only names containing this text')
    ap.add_argument('--apply', action='store_true', help='really remove them from the notebook')
    a = ap.parse_args()
    from generators import notebooklm_agent as nlm
    idx = nlm._load_notebooks_index()
    agent = nlm.NotebookLMAgent()
    for ck in a.course or list(idx):
        for scope, entry in (idx.get(ck) or {}).items():
            stale = [n for n in nlm.moved_aside_sources(ck, entry.get('sources_synced', [])) if a.only in n]
            if not stale:
                continue
            r = agent.remove_sources(ck, scope, stale, apply=a.apply)
            print(f'{ck}/{scope}:')
            for n in r['removed' if a.apply else 'matched']:
                print(f"  {'removed' if a.apply else 'would remove'}: {n}")
            for n in r['missing']:
                print(f'  no source with this title in the notebook: {n}')
    if not a.apply:
        print('nothing changed (add --apply to remove)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
