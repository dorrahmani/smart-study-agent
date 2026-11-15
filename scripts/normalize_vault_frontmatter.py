"""
scripts/normalize_vault_frontmatter.py — One-off vault frontmatter audit + fixer.

Phase-3 §B1+B2 (additive only).

For every note in the vault, ensure these fields exist (without removing
or overwriting existing values):
    type, type_en, course, course_key, date, created, modified, tags

For concept notes, also ensure:
    n_references, seen_in (built from notes that wikilink in)

Default mode is `--dry-run`. To actually write, pass `--apply`.

Usage:
    python -m scripts.normalize_vault_frontmatter            # dry-run
    python -m scripts.normalize_vault_frontmatter --apply    # write
    python -m scripts.normalize_vault_frontmatter --type concept --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import yaml

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from config.paths import (
    VAULT_DIR, COURSE_NAMES, VAULT_COURSE_FOLDER_TO_KEY,
)


# ─── Constants ─────────────────────────────────────────────────────────────

FM_RE = re.compile(r'\A---\n(.*?)\n---\n?', re.DOTALL)
DATE_FROM_NAME_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')
WIKILINK_RE = re.compile(r'\[\[([^\]\n]+?)(?:\|[^\]]+)?\]\]')

# Map Hebrew `type` values → English aliases for cross-language tooling.
TYPE_HE_TO_EN = {
    'הרצאה':  'lecture',
    'תרגול':  'tutorial',
    'חידון':  'quiz',
}


# ─── Helpers ───────────────────────────────────────────────────────────────

def split_frontmatter(text: str) -> tuple[dict, str, bool]:
    """Return (fm_dict, body, had_fm). Empty fm_dict if no/invalid frontmatter."""
    m = FM_RE.match(text)
    if not m:
        return {}, text, False
    raw = m.group(1)
    body = text[m.end():]
    try:
        fm = yaml.safe_load(raw) or {}
        if not isinstance(fm, dict):
            fm = {}
    except Exception:
        fm = {}
    return fm, body, True


def join_frontmatter(fm: dict, body: str) -> str:
    return (
        '---\n'
        + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
        + '---\n'
        + (body if body.startswith('\n') else '\n' + body if body else '')
    ).rstrip() + '\n'


def infer_course_key_from_path(path: Path) -> str | None:
    """First parent folder matching VAULT_COURSE_FOLDER_TO_KEY (or course_key)."""
    for part in path.relative_to(VAULT_DIR).parts:
        if part in VAULT_COURSE_FOLDER_TO_KEY:
            return VAULT_COURSE_FOLDER_TO_KEY[part]
        if part in COURSE_NAMES:
            return part
    return None


def infer_date_from_name(stem: str) -> str | None:
    m = DATE_FROM_NAME_RE.search(stem)
    return m.group(1) if m else None


def file_ctime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_ctime).strftime('%Y-%m-%d')


def file_mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds')


# ─── Concept reference graph ───────────────────────────────────────────────

def build_concept_references(all_notes: list[Path]) -> dict[str, list[str]]:
    """
    Return {concept_stem: [list of stems that wikilink to it]}.
    Only counts incoming wikilinks — used for `seen_in` and `n_references`.
    """
    refs: dict[str, set[str]] = defaultdict(set)
    for note in all_notes:
        try:
            text = note.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        # strip the frontmatter so we don't count `related:` items twice;
        # `related:` already lists wikilink targets, but those ALSO appear
        # in the body usually. We DO want to count both representations.
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            # de-duplicate by target stem (Obsidian resolves to .md)
            refs[target].add(note.stem)
    return {k: sorted(v) for k, v in refs.items()}


# ─── Per-note normalizer ───────────────────────────────────────────────────

def normalize_one(
    path:        Path,
    refs_map:    dict[str, list[str]],
) -> tuple[dict, dict, list[str]]:
    """
    Returns (old_fm, new_fm, changes_list). Pure — does NOT write the file.
    """
    text = path.read_text(encoding='utf-8', errors='replace')
    fm, _body, _had_fm = split_frontmatter(text)
    new = dict(fm)  # additive copy
    changes: list[str] = []

    # ── type / type_en ──────────────────────────────────────────────
    t = new.get('type')
    if t in TYPE_HE_TO_EN and 'type_en' not in new:
        new['type_en'] = TYPE_HE_TO_EN[t]
        changes.append(f'+type_en={TYPE_HE_TO_EN[t]}')
    elif isinstance(t, str) and t and 'type_en' not in new:
        # Already-English types just mirror over.
        new['type_en'] = t
        changes.append(f'+type_en={t}')

    # ── course_key / course ────────────────────────────────────────
    if 'course_key' not in new:
        ck = infer_course_key_from_path(path)
        if ck:
            new['course_key'] = ck
            changes.append(f'+course_key={ck}')
    if 'course' not in new and new.get('course_key') in COURSE_NAMES:
        new['course'] = COURSE_NAMES[new['course_key']]
        changes.append(f'+course={new["course"]}')
    elif (new.get('course_key') == 'cross-course'
          or (not new.get('course_key') and new.get('type') == 'concept')):
        # leave course unset — concept might span courses
        pass

    # ── date ───────────────────────────────────────────────────────
    if not new.get('date'):
        d = infer_date_from_name(path.stem)
        if d:
            new['date'] = d
            changes.append(f'+date={d}')
        elif new.get('created'):
            new['date'] = new['created'][:10]
            changes.append(f'+date={new["date"]} (from created)')
        else:
            new['date'] = file_ctime_iso(path)
            changes.append(f'+date={new["date"]} (from ctime)')

    # ── created / modified ─────────────────────────────────────────
    if 'created' not in new:
        new['created'] = file_ctime_iso(path)
        changes.append(f'+created={new["created"]}')
    new['modified'] = file_mtime_iso(path)  # always refresh
    if fm.get('modified') != new['modified']:
        changes.append(f'~modified={new["modified"]}')

    # ── tags ───────────────────────────────────────────────────────
    tags = list(new.get('tags') or [])
    ck = new.get('course_key')
    if ck and ck not in tags:
        tags.append(ck)
        changes.append(f'+tag={ck}')
    if new.get('type_en') and new['type_en'] not in tags:
        tags.append(new['type_en'])
        changes.append(f'+tag={new["type_en"]}')
    new['tags'] = tags

    # ── concept enrichment ─────────────────────────────────────────
    if new.get('type') == 'concept':
        seen_in = refs_map.get(path.stem, [])
        if 'n_references' not in new or new.get('n_references') != len(seen_in):
            new['n_references'] = len(seen_in)
            changes.append(f'~n_references={len(seen_in)}')
        if 'seen_in' not in new and seen_in:
            new['seen_in'] = [f'[[{s}]]' for s in seen_in[:25]]
            changes.append(f'+seen_in={len(seen_in)} backlinks')

    return fm, new, changes


def write_note(path: Path, new_fm: dict) -> None:
    """Write back the file with new_fm prepended; preserve body verbatim."""
    text = path.read_text(encoding='utf-8', errors='replace')
    _, body, had_fm = split_frontmatter(text)
    out = join_frontmatter(new_fm, body if had_fm else '\n' + text)
    path.write_text(out, encoding='utf-8')


# ─── Discovery ─────────────────────────────────────────────────────────────

def discover_notes(type_filter: str | None = None) -> list[Path]:
    """Every .md file under the vault, optionally filtered by `type:` value."""
    all_md = [p for p in VAULT_DIR.rglob('*.md') if not _hidden(p)]
    if not type_filter:
        return all_md
    keep: list[Path] = []
    for p in all_md:
        try:
            text = p.read_text(encoding='utf-8', errors='replace')
            fm, _, _ = split_frontmatter(text)
        except Exception:
            continue
        if fm.get('type') == type_filter or fm.get('type_en') == type_filter:
            keep.append(p)
    return keep


def _hidden(p: Path) -> bool:
    return any(part.startswith('.') for part in p.parts)


# ─── CLI ───────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description='Vault frontmatter normalizer (additive only).'
    )
    p.add_argument('--apply', action='store_true',
                   help='Write changes. Without this, just preview (dry-run).')
    p.add_argument('--type', type=str, default=None,
                   help='Restrict to a single type (e.g. concept, assignment, lecture).')
    p.add_argument('--limit', type=int, default=0,
                   help='Process only N files (0 = no limit).')
    args = p.parse_args()

    notes = discover_notes(type_filter=args.type)
    print(f'Vault: {VAULT_DIR}')
    print(f'Notes to process: {len(notes)}')
    if args.type:
        print(f'  filter: type={args.type}')
    if args.limit:
        notes = notes[:args.limit]
        print(f'  limit: {args.limit}')
    print()

    print('Building wikilink reference graph (for concept enrichment)...')
    refs_map = build_concept_references(
        [p for p in VAULT_DIR.rglob('*.md') if not _hidden(p)]
    )
    print(f'  found {len(refs_map)} distinct wikilink targets')
    print()

    n_changed = 0
    n_skipped = 0
    by_change_type: Counter = Counter()
    samples: list[tuple[str, list[str]]] = []

    for path in notes:
        try:
            old, new, changes = normalize_one(path, refs_map)
        except Exception as e:
            print(f'  ! error on {path.name}: {e}')
            n_skipped += 1
            continue

        # `modified` always changes — we filter that out for "no-op" detection
        meaningful = [c for c in changes if not c.startswith('~modified=')]
        if not meaningful:
            n_skipped += 1
            continue

        n_changed += 1
        for c in meaningful:
            kind = c.split('=', 1)[0]
            by_change_type[kind] += 1
        if len(samples) < 12:
            samples.append((path.name, meaningful[:6]))

        if args.apply:
            write_note(path, new)

    print(f'Would change: {n_changed}    No-op: {n_skipped}')
    print()
    print('Top change types:')
    for k, n in by_change_type.most_common(15):
        print(f'  {k}: {n}')
    print()
    print('Samples:')
    for name, ch in samples:
        print(f'  {name}')
        for c in ch:
            print(f'      {c}')

    if args.apply:
        print()
        print(f'✅ Applied changes to {n_changed} files.')
    else:
        print()
        print('(dry-run — no files written. Pass --apply to commit.)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
