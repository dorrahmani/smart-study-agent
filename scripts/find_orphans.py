"""
scripts/find_orphans.py — Vault health: orphans, dead links, duplicate titles.

A pure analyzer — never writes or deletes anything. Outputs a JSON report
that the dashboard consumes (dashboard/vault_health.py).

Definitions:
  • Orphan: a note with 0 incoming AND 0 outgoing wikilinks.
  • Dead link: a `[[wikilink]]` whose target is not the stem of any vault note.
  • Duplicate title: same title (or stem) appearing in different folders.

Usage:
    python -m scripts.find_orphans               # print to stdout
    python -m scripts.find_orphans --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from config.paths import VAULT_DIR


WIKILINK_RE = re.compile(r'\[\[([^\]\n]+?)(?:\|[^\]]+)?\]\]')
FM_RE       = re.compile(r'\A---\n(.*?)\n---\n?', re.DOTALL)


def _hidden(p: Path) -> bool:
    return any(part.startswith('.') for part in p.parts)


def _split_fm(text: str) -> dict:
    m = FM_RE.match(text)
    if not m:
        return {}
    try:
        d = yaml.safe_load(m.group(1)) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def scan_vault() -> dict:
    """Return a structured report of orphans, dead links, duplicates."""
    notes = [p for p in VAULT_DIR.rglob('*.md') if not _hidden(p)]
    by_stem: dict[str, list[Path]] = defaultdict(list)
    by_title: dict[str, list[Path]] = defaultdict(list)
    incoming: dict[str, set[str]] = defaultdict(set)
    outgoing: dict[str, set[str]] = defaultdict(set)
    fm_cache: dict[Path, dict] = {}

    # Pass 1 — index by stem & title
    for p in notes:
        by_stem[p.stem].append(p)
        try:
            text = p.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        fm = _split_fm(text)
        fm_cache[p] = fm
        title = (fm.get('title') or p.stem).strip()
        by_title[title].append(p)

    # Build a multi-key index so that Obsidian-style `folder/stem` links
    # resolve to the same note as bare `stem` links. Stems and titles are
    # both valid resolution keys.
    valid_stems = set(by_stem.keys())
    valid_titles = set(by_title.keys())

    def _resolve(target: str) -> str | None:
        """Return the canonical stem of a wikilink target, or None if dead."""
        t = target.strip()
        if not t:
            return None
        # strip a `#heading` or `^anchor` suffix
        t = t.split('#', 1)[0].split('^', 1)[0].strip()
        # try as-is, then last path component, then title
        if t in valid_stems:
            return t
        last = t.split('/')[-1].split('\\')[-1]
        if last in valid_stems:
            return last
        if t in valid_titles or last in valid_titles:
            return last if last in valid_titles else t
        return None

    # Pass 2 — wikilink graph (path-aware)
    for p in notes:
        try:
            text = p.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        for m in WIKILINK_RE.finditer(text):
            target_raw = m.group(1).strip()
            resolved = _resolve(target_raw)
            if resolved:
                outgoing[p.stem].add(resolved)
                incoming[resolved].add(p.stem)
            else:
                # store unresolved with sentinel marker so it counts as
                # outgoing (the note isn't fully orphan) but resolves to dead.
                outgoing[p.stem].add(f'__DEAD__:{target_raw}')

    # Orphans
    orphans = []
    for p in notes:
        in_count  = len(incoming.get(p.stem, set()))
        out_count = len([
            x for x in outgoing.get(p.stem, set())
            if not x.startswith('__DEAD__:')
        ])
        # Notes with only dead-link outgoings still don't qualify as truly
        # connected. We keep this nuance: in_count==0 AND out_count==0.
        all_outgoing = len(outgoing.get(p.stem, set()))
        if in_count == 0 and all_outgoing == 0:
            fm = fm_cache.get(p, {})
            orphans.append({
                'stem':       p.stem,
                'rel_path':   str(p.relative_to(VAULT_DIR)),
                'type':       fm.get('type'),
                'course_key': fm.get('course_key'),
            })

    # Dead links
    dead: list[dict] = []
    for source, targets in outgoing.items():
        for t in targets:
            if t.startswith('__DEAD__:'):
                dead.append({'source': source, 'target': t.split(':', 1)[1]})

    # Duplicates: same stem in 2+ files OR same title in 2+ files
    duplicates_by_stem = {
        stem: [str(p.relative_to(VAULT_DIR)) for p in ps]
        for stem, ps in by_stem.items() if len(ps) > 1
    }
    duplicates_by_title = {
        t: [str(p.relative_to(VAULT_DIR)) for p in ps]
        for t, ps in by_title.items()
        if len(ps) > 1 and not any(p.stem == t for p in ps)  # skip if also a dup-stem
    }

    # Counts
    orphans.sort(key=lambda r: (r.get('type') or '', r['stem']))

    return {
        'total_notes':         len(notes),
        'orphans':             orphans,
        'dead_links':          dead,
        'duplicates_by_stem':  duplicates_by_stem,
        'duplicates_by_title': duplicates_by_title,
        'orphan_count':        len(orphans),
        'dead_link_count':     len(dead),
    }


def main() -> int:
    p = argparse.ArgumentParser(description='Vault health: orphans, dead links, duplicates.')
    p.add_argument('--json', type=str, default=None,
                   help='Write the full report to a JSON file.')
    p.add_argument('--quiet', action='store_true')
    args = p.parse_args()

    rep = scan_vault()

    if args.json:
        Path(args.json).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        print(f'Wrote {args.json}')

    if not args.quiet:
        print(f'Vault: {VAULT_DIR}')
        print(f'  total notes:         {rep["total_notes"]}')
        print(f'  orphans:             {rep["orphan_count"]}')
        print(f'  dead links:          {rep["dead_link_count"]}')
        print(f'  duplicate stems:     {len(rep["duplicates_by_stem"])}')
        print(f'  duplicate titles:    {len(rep["duplicates_by_title"])}')
        if rep['orphans'][:5]:
            print()
            print('Top orphans:')
            for o in rep['orphans'][:8]:
                print(f"  • {o['stem']}  [{o.get('type','?')}]  {o['rel_path']}")
        if rep['dead_links'][:5]:
            print()
            print('Sample dead links:')
            seen: set = set()
            for d in rep['dead_links']:
                key = (d['source'], d['target'])
                if key in seen:
                    continue
                seen.add(key)
                print(f"  • {d['source']} → [[{d['target']}]]")
                if len(seen) >= 8:
                    break
    return 0


if __name__ == '__main__':
    sys.exit(main())
