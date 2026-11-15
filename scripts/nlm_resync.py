"""
scripts/nlm_resync.py — replace a summary that NotebookLM already holds, after its file was fixed (a fact fix).

sync_sources only adds, and skips a name it synced before, so a corrected PDF never reaches the notebooks on its own:
they go on answering from the old text. This takes the old source out of every notebook (scope) that lists the name
and uploads the file again. Without --apply it changes nothing and prints what it would do. No LLM (the NotebookLM
web session).

    venv/bin/python3 scripts/nlm_resync.py "data/summaries/software_1/tutorials/2026-10-21 - Michael - Recitation 1.pdf"
    venv/bin/python3 scripts/nlm_resync.py --apply "<pdf>" ["<pdf>" ...]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def course_of(pdf: Path) -> str:
    """data/summaries/<course>/<lectures|tutorials>/<name>.pdf → <course>."""
    parts = pdf.resolve().parts
    i = max(i for i, p in enumerate(parts) if p == 'summaries')
    return parts[i + 1]


def plan(pdfs: list[Path], idx: dict) -> list[tuple[str, str, Path]]:
    """(course, scope, pdf) for every notebook that already lists the file's name."""
    out = []
    for pdf in pdfs:
        ck = course_of(pdf)
        for scope, entry in (idx.get(ck) or {}).items():
            if pdf.name in (entry.get('sources_synced') or []):
                out.append((ck, scope, pdf))
    return out


def main(argv=None, agent=None, idx=None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description='replace fixed summaries in NotebookLM; changes nothing without --apply')
    ap.add_argument('pdf', nargs='+', help='summary PDFs (data/summaries/<course>/<kind>/<name>.pdf)')
    ap.add_argument('--apply', action='store_true', help='really remove the old source and upload the file again')
    a = ap.parse_args(argv)
    from generators import notebooklm_agent as nlm
    pdfs = [Path(p) for p in a.pdf]
    for p in pdfs:
        if not p.exists():
            print(f'no such file: {p}')
            return 2
    idx = idx if idx is not None else nlm._load_notebooks_index()
    todo = plan(pdfs, idx)
    for p in pdfs:
        if not any(t[2] == p for t in todo):
            print(f'not in any notebook yet (the nightly sync adds it): {p.name}')
    if not a.apply:
        for ck, scope, p in todo:
            print(f'would replace in {ck}/{scope}: {p.name}')
        print('nothing changed (add --apply)')
        return 0
    if agent is None:
        if not nlm.NotebookLMAgent.is_authenticated():
            print('NotebookLM is not logged in — nothing changed')
            return 1
        agent = nlm.NotebookLMAgent()
    bad = 0
    for ck, scope, p in todo:
        r = agent.remove_sources(ck, scope, [p.name], apply=True)
        s = agent.sync_sources(course_key=ck, scope=scope, summary_pdfs=[str(p)])
        ok = p.name in s.get('added', [])
        bad += not ok
        print(f"{'✅' if ok else '❌'} {ck}/{scope}: {p.name} — removed {len(r['removed'])}, "
              f"{'uploaded again' if ok else 'upload failed: ' + str(s.get('skipped'))[:200]}")
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
