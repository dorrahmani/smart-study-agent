"""
vault_sync/relinker.py — Build the cross-note graph.

Given the current vault state:
1. Extract concepts for any note that doesn't have them (via concept_extractor).
2. Write/update one concept note per unique concept under VAULT_CONCEPTS_DIR.
3. Update each note's frontmatter `concepts:` and `related:` based on:
   - shared concepts (≥1 common concept with another note)
   - same-week lecture↔tutorial pairs
   - assignments ↔ lectures with overlapping topics

Idempotent. Updates frontmatter in place without touching the body.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import yaml

from config.paths import (
    VAULT_DIR, VAULT_CONCEPTS_DIR, VAULT_ASSIGNMENTS_DIR,
    COURSE_NAMES,
)
from dashboard import vault_reader as vr
from extractors.concept_extractor import extract_concepts
from extractors.topic_parser import topics_from_first_page

_FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', re.DOTALL)


def _read_fm(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding='utf-8', errors='replace')
    m = _FM_RE.match(raw)
    if not m:
        return {}, raw
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm if isinstance(fm, dict) else {}, m.group(2)


def _write_fm(path: Path, fm: dict, body: str) -> None:
    fm = {k: v for k, v in fm.items() if v not in (None, [], '')}
    out = '---\n' + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + '---\n' + body
    path.write_text(out, encoding='utf-8')


def _stem_to_wikilink(stem: str) -> str:
    return f'[[{stem}]]'


def _ensure_concepts_for_note(note_path: Path, fm: dict, body: str,
                              use_api: bool = True, force: bool = False) -> list[str]:
    """Ensure the note's frontmatter has `concepts:`. Returns the list."""
    if not force and fm.get('concepts'):
        return list(fm['concepts'])
    topics = fm.get('topics') or fm.get('topics_from_first_page') or topics_from_first_page(body)
    course_key = fm.get('course_key', '')
    opening = body[:800]
    concepts = extract_concepts(note_path, topics, opening, course_key, use_api=use_api)
    if concepts:
        fm['concepts'] = concepts
    if topics and not fm.get('topics_from_first_page'):
        fm['topics_from_first_page'] = topics
    return concepts


_AUTO_BLOCK_RE = re.compile(r'<!-- (AUTO-[A-Z-]+):BEGIN -->.*?<!-- \1:END -->', re.DOTALL)


def _write_concept_note(concept: str, linked_courses: set[str]) -> Path:
    """Write the concept note's header, KEEPING the AUTO-* blocks other steps added.

    The daily pipeline relinks (here) before brain enrichment (scripts/enhance_brain.py
    adds AUTO-COOCCUR and the paid AUTO-RICH card). A plain overwrite wiped those
    blocks every day, so the same 234 cards were regenerated daily (~$0.28/day).
    The file is only rewritten when its text actually changes.
    """
    VAULT_CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
    out = VAULT_CONCEPTS_DIR / f'{concept}.md'
    fm = {
        'type':   'concept',
        'title':  concept,
        'tags':   ['concept'] + sorted(linked_courses),
    }
    body = f'# 🧠 {concept}\n\n'
    body += '*מושג מקשר בין הרצאות / קורסים. ראה backlinks.*\n'
    old = ''
    if out.exists():
        try:
            old = out.read_text(encoding='utf-8', errors='replace')
        except OSError:
            old = ''
    blocks = [m.group(0) for m in _AUTO_BLOCK_RE.finditer(old)]
    if blocks:
        body = body + '\n' + '\n\n'.join(blocks) + '\n'
    text = '---\n' + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + '---\n\n' + body
    if text != old:
        out.write_text(text, encoding='utf-8')
    return out


def relink(use_api: bool = True, write_concept_notes: bool = True,
           force_concepts: bool = False) -> dict:
    """Run the full relink. Returns stats."""
    vr.invalidate_cache()
    notes = vr.list_summaries()

    # Pass 1 — extract concepts where missing
    paths_to_fm: dict[str, tuple[Path, dict, str]] = {}
    for n in notes:
        path = Path(n['path'])
        fm, body = _read_fm(path)
        concepts = _ensure_concepts_for_note(path, fm, body, use_api=use_api, force=force_concepts)
        paths_to_fm[str(path)] = (path, fm, body)

    # Pass 2 — index concept → notes
    concept_to_stems: dict[str, list[str]] = defaultdict(list)
    concept_to_courses: dict[str, set[str]] = defaultdict(set)
    stem_to_path: dict[str, Path] = {}
    for n in notes:
        path = Path(n['path'])
        stem = path.stem
        stem_to_path[stem] = path
        fm = paths_to_fm[str(path)][1]  # type: ignore
        for c in (fm.get('concepts') or []):
            concept_to_stems[c].append(stem)
            if fm.get('course_key'):
                concept_to_courses[c].add(fm['course_key'])

    # Pass 3 — write concept notes (only those used by ≥2 notes — avoid clutter)
    if write_concept_notes:
        for concept, stems in concept_to_stems.items():
            if len(set(stems)) >= 2:
                _write_concept_note(concept, concept_to_courses[concept])

    # Pass 4 — set `related:` in each note (shared concepts + same-week)
    relinked = 0
    for n in notes:
        path = Path(n['path'])
        _, fm, body = paths_to_fm[str(path)]
        related = set(fm.get('related') or [])

        # By concept
        for c in (fm.get('concepts') or []):
            for other_stem in concept_to_stems.get(c, []):
                if other_stem != path.stem:
                    related.add(_stem_to_wikilink(other_stem))

        # Same-week opposite-type
        if fm.get('date'):
            try:
                d = datetime.strptime(str(fm['date'])[:10], '%Y-%m-%d').date()
                for m in notes:
                    if m['path'] == n['path']:
                        continue
                    if m.get('course_key') != fm.get('course_key'):
                        continue
                    if not m.get('date'):
                        continue
                    try:
                        md = datetime.strptime(str(m['date'])[:10], '%Y-%m-%d').date()
                    except ValueError:
                        continue
                    if abs((md - d).days) <= 7:
                        related.add(_stem_to_wikilink(Path(m['path']).stem))
            except ValueError:
                pass

        # Cap to 20 most recent / unique
        related_list = sorted(related)[:30]
        if related_list:
            fm['related'] = related_list
        _write_fm(path, fm, body)
        relinked += 1

    vr.invalidate_cache()
    return {
        'notes_processed': len(notes),
        'concepts_total':  len(concept_to_stems),
        'concept_notes_written': sum(1 for s in concept_to_stems.values() if len(set(s)) >= 2),
        'relinked':        relinked,
    }


if __name__ == '__main__':
    import sys
    use_api = '--no-api' not in sys.argv
    force = '--force' in sys.argv
    print('Running relinker...', '(API)' if use_api else '(fallback only)',
          '[FORCE re-extract]' if force else '')
    stats = relink(use_api=use_api, force_concepts=force)
    print(stats)
