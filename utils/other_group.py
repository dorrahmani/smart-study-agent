"""
utils/other_group.py — "this summary is from another group" (14/11/26).

When the owner's own group's class isn't recorded, the weekly pick (collectors/weekly_pick) takes another group's recording of
that week and marks it in the metadata (other_group, host, start). That must be visible wherever the summary reaches
him: one short Hebrew line at the top of the summary itself (so the PDF, the site's lesson page and the vault note carry
it), and a tag in the email subject and the WhatsApp caption. The line in the summary is the single source of truth
after the summary is written — senders read it from the .md. No LLM.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

MARK = 'מקבוצה אחרת'
TAG = '(מקבוצה אחרת)'
_DAYS = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']        # datetime.weekday()


def label(kind: str, host: str = '', start: str = '', date: str = '', group: str = '') -> str:
    """→ '> ↪️ **מקבוצה אחרת:** הרצאה של lecturer_a, יום רביעי 04/11 14:11 — ההקלטה של הקבוצה שלך לא עלתה השבוע.'"""
    when = ''
    try:
        dt = datetime.fromisoformat(start or date)
        when = f'יום {_DAYS[dt.weekday()]} {dt:%d/%m}' + (f' {dt:%H:%M}' if start and 'T' in start else '')
    except ValueError:
        pass
    who = ' של ' + host if host else ''
    grp = f' (קבוצה {group})' if group else ''
    return (f'> ↪️ **{MARK}:** {kind}{who}{grp}' + (f', {when}' if when else '')
            + ' — ההקלטה של הקבוצה שלך לא עלתה השבוע.')


def has(text: str) -> bool:
    """Does this summary say it is from another group (its first lines)?"""
    return any(MARK in ln and ln.lstrip().startswith('>') for ln in (text or '').splitlines()[:40])


def line_of(text: str) -> str:
    """The label line itself, without the quote mark ('' when there is none)."""
    for ln in (text or '').splitlines()[:40]:
        if MARK in ln and ln.lstrip().startswith('>'):
            return re.sub(r'^\s*>\s*', '', ln).replace('**', '').strip()
    return ''


def add(text: str, line: str) -> str:
    """Put the line at the top of the summary's body, once. The body starts after the header block (the input hash,
    the date, the scanned-materials list) at its first '---' — the vault note and gen_missing_pdfs strip what is before
    it — so the line goes right after that rule, followed by a blank line. Without such a block: at the very top."""
    if not line or has(text):
        return text
    lines = (text or '').split('\n')
    for i, ln in enumerate(lines[:40]):
        if ln.strip() == '---' and i > 0:
            rest = lines[i + 1:]
            while rest and not rest[0].strip():
                rest = rest[1:]
            return '\n'.join(lines[:i + 1] + ['', line, ''] + rest)
        if ln.startswith('## ') and 'חומרים שנסרקו' not in ln:
            break
    top = 0
    while top < len(lines) and (lines[top].startswith('<!--') or re.match(r'^\*נוצר', lines[top])):
        top += 1
    return '\n'.join(lines[:top] + ([''] if top else []) + [line, ''] + lines[top:])


def keep(original: str, stripped: str) -> str:
    """A consumer that strips the summary's header keeps the line (it is the first thing the reader must see)."""
    ln = next((x for x in (original or '').splitlines()[:40] if MARK in x and x.lstrip().startswith('>')), '')
    return stripped if not ln or ln in stripped else f'{ln}\n\n{stripped}'


def _meta_entry(ck: str, date: str, title: str, panopto: Path) -> dict | None:
    try:
        meta = json.loads((panopto / f'{ck}_metadata.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    stem = f'{ck}_{date}_{title}'
    for m in meta if isinstance(meta, list) else []:
        srt = Path(str(m.get('srt_path') or '').replace('\\', '/')).stem
        if srt == stem or (str(m.get('date', ''))[:10] == date and m.get('title') == title):
            return m
    return None


def label_for(ck: str, date: str, title: str, kind: str = '', panopto: Path | None = None) -> str:
    """The line for an SRT's summary — '' unless its metadata says it is another group's."""
    if panopto is None:
        from config.paths import PANOPTO_DIR as panopto
    m = _meta_entry(ck, date, title, Path(panopto))
    if not m or not m.get('other_group'):
        return ''
    kind = kind or ('תרגול' if m.get('type') == 'tutorial' else 'הרצאה')
    return label(kind, m.get('host') or '', m.get('start') or '', date, m.get('group') or '')


def with_label(summary: str, ck: str, date: str, title: str, kind: str = '', panopto: Path | None = None) -> str:
    """A new summary of an SRT → the same summary, with the line when the recording is another group's."""
    try:
        return add(summary, label_for(ck, date, title, kind, panopto))
    except Exception as e:                            # the label never costs a summary
        print(f'  ⚠️ other-group label: {e}')
        return summary


def md_has(md: Path) -> bool:
    try:
        with open(md, encoding='utf-8') as f:
            return has(''.join(f.readline() for _ in range(40)))
    except OSError:
        return False
