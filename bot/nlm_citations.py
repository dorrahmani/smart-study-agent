"""bot/nlm_citations.py — Format NotebookLM references with timestamps + Panopto links.

NotebookLM returns references like {title, snippet}. Snippets sometimes contain
timestamps like "at 13:45" or "00:13:45". We:
  1. Extract timestamp from snippet (best effort)
  2. Look up the Panopto URL from data/panopto_metadata.json (if exists)
  3. Build deep-link: <panopto_url>?start=<seconds>
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PANOPTO_META = ROOT / 'data' / 'panopto_metadata.json'

_TS_RE = re.compile(
    r'(?:'
    r'(\d{1,2}):(\d{2}):(\d{2})'    # H:MM:SS
    r'|'
    r'(\d{1,3}):(\d{2})'             # MM:SS
    r')'
)


def _ts_to_seconds(text: str) -> int | None:
    m = _TS_RE.search(text)
    if not m: return None
    if m.group(1) is not None:
        h = int(m.group(1)); mm = int(m.group(2)); s = int(m.group(3))
        return h*3600 + mm*60 + s
    if m.group(4) is not None:
        mm = int(m.group(4)); s = int(m.group(5))
        return mm*60 + s
    return None


def _ts_to_label(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h: return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'


def _load_panopto_meta() -> dict:
    if not PANOPTO_META.exists(): return {}
    try:
        return json.loads(PANOPTO_META.read_text(encoding='utf-8'))
    except Exception:
        return {}


def format_references(refs: list[dict], max_items: int = 5) -> str:
    """Returns markdown lines describing each reference, with timestamp+link if available.
    Returns empty string if no refs."""
    if not refs: return ''
    meta = _load_panopto_meta()
    lines: list[str] = []
    seen_titles: set[str] = set()
    for r in refs[:max_items]:
        title = (r.get('title') or '').strip()
        if not title or title in seen_titles: continue
        seen_titles.add(title)
        snippet = (r.get('snippet') or '').strip()
        secs = _ts_to_seconds(snippet)

        # Try to find panopto URL by matching title or stem
        url = None
        if isinstance(meta, dict):
            # meta keyed by stem
            for stem, info in meta.items():
                if not isinstance(info, dict): continue
                if stem in title or title in stem or info.get('title') == title:
                    url = info.get('panopto_url') or info.get('url')
                    if url and secs is not None:
                        sep = '&' if '?' in url else '?'
                        url = f'{url}{sep}start={secs}'
                    break

        line = f'  📖 *{title}*'
        if secs is not None and url:
            line += f' — [🎬 {_ts_to_label(secs)}]({url})'
        elif secs is not None:
            line += f' — 🎬 {_ts_to_label(secs)}'
        lines.append(line)
    return '\n'.join(lines)
