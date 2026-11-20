"""
vault_sync/notewriter.py — idempotent note writes for the assistant area (16/11).

A note is written only when its text changes (an earlier relinker rewrote 405 concept notes every night: new mtimes, the
Drive copy, the PC pull and the RAG staleness check all redid their work, and the brain enrichment paid again).
No timestamp of "when it was generated" ever goes into a note, so an unchanged source gives a byte-identical note.

    w = Writer(vault_dir)                       # real run
    w = Writer(vault_dir, out_root=Path('/tmp/x'))   # dry: writes the would-be notes under /tmp/x, compares with the vault
    w.note('👩‍💼 העוזר/👥 אנשים/פלוני.md', {'type': 'person', …}, body)   → 'new' | 'changed' | 'same'
Front-matter through yaml.safe_dump only (Hebrew quotes break hand-built YAML).

17/11 ownership: the writer never writes into the owner's own folder ("✍️ שלי": ValueError), and never replaces an existing note that
the bot doesn't own: a note is the bot's only when its front-matter says `source: assistant`. Anything else (a note he made by
hand inside a bot folder, a pipeline note with the same name) is kept as it is and counted as 'foreign_kept'.
"""
from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import re

import yaml

_FM_RX = re.compile(r'^---\s*\n(.*?)\n---', re.S)


def bot_owned(text: str) -> bool:
    """True when a note's front-matter marks it as written by the assistant (so the writer may replace it)."""
    m = _FM_RX.match(text or '')
    if not m:
        return False
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return False
    return isinstance(fm, dict) and fm.get('source') == 'assistant'


def _his(rel: str) -> bool:
    from vault_sync.privacy import MINE
    parts = Path(rel).parts
    return bool(parts) and (parts[0] == MINE or MINE in parts)


def render(front: dict, body: str) -> str:
    fm = {k: v for k, v in front.items() if v not in (None, '', [], {})}
    return '---\n' + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1000) + '---\n\n' + body.rstrip() + '\n'


class Writer:
    def __init__(self, vault_dir: Path, out_root: Path | None = None):
        self.vault = Path(vault_dir)
        self.out = Path(out_root) if out_root else None
        self.stats: Counter = Counter()
        self.by_folder: Counter = Counter()
        self.written: list[str] = []

    @property
    def dry(self) -> bool:
        return self.out is not None

    def _target(self, rel: str) -> Path:
        return (self.out or self.vault) / rel

    def note(self, rel: str, front: dict, body: str) -> str:
        return self.text(rel, render(front, body))

    def text(self, rel: str, text: str) -> str:
        if _his(rel):
            raise ValueError(f'the bot never writes into his own notes: {rel}')
        real = self.vault / rel
        try:
            old = real.read_text(encoding='utf-8')
        except OSError:
            old = None
        if old is not None and old != text and not bot_owned(old):
            self.stats['foreign_kept'] += 1                # not ours: never replaced
            return 'kept'
        state = 'new' if old is None else ('same' if old == text else 'changed')
        self.stats[state] += 1
        self.by_folder[str(Path(rel).parent)] += 1
        if self.dry or state != 'same':
            t = self._target(rel)
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(text, encoding='utf-8')
            if state != 'same':
                self.written.append(rel)
        return state

    def copy(self, rel: str, src: Path) -> str:
        """A stored file (a PDF he sent) → beside its note. Same size + same bytes → untouched."""
        if _his(rel):
            raise ValueError(f'the bot never writes into his own notes: {rel}')
        real = self.vault / rel
        if real.exists() and real.stat().st_size == src.stat().st_size and real.read_bytes() == src.read_bytes():
            self.stats['same_file'] += 1
            return 'same'
        self.stats['new_file'] += 1
        self.by_folder[str(Path(rel).parent)] += 1
        if self.dry:
            return 'new'                                   # a dry run doesn't copy his files to /tmp
        real.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, real)
        self.written.append(rel)
        return 'new'
