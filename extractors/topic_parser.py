"""
extractors/topic_parser.py — Extract the 'topics' list from a summary's first page.

Summaries already split topics on the first page (under "## תוכן השיעור — מפורט לפי נושאים"
or as H2/H3 headers). We parse the list as-is, no re-extraction.
"""

from __future__ import annotations
import re

_HDR = re.compile(r'^(#{2,3})\s+(.+?)\s*$', re.MULTILINE)
_BULLET = re.compile(r'^[-*+]\s+(.+?)\s*$', re.MULTILINE)
_SECTION_HEADERS = ('תוכן השיעור', 'נושאים', 'מפת השיעור', 'סדר היום')


def topics_from_first_page(md_body: str, max_topics: int = 12) -> list[str]:
    """Heuristic: prefer bullets under a 'topics' section; fallback to H2/H3 headers."""
    # Try section bullets first
    for header in _SECTION_HEADERS:
        m = re.search(rf'##\s*[^\n]*{re.escape(header)}[^\n]*\n(.*?)(?=\n##\s|\Z)',
                      md_body, re.DOTALL)
        if m:
            bullets = _BULLET.findall(m.group(1))
            cleaned = [_clean(t) for t in bullets if t.strip()]
            cleaned = [t for t in cleaned if t]
            if cleaned:
                return cleaned[:max_topics]

    # Fallback: H2/H3 headers from the first chunk
    first_chunk = md_body[:6000]
    headers = [_clean(h[1]) for h in _HDR.findall(first_chunk)]
    headers = [h for h in headers if h and not _is_meta(h)]
    return headers[:max_topics]


def _clean(s: str) -> str:
    s = re.sub(r'\[[^\]]*\]', '', s)         # drop timestamps like [00:01:23]
    s = re.sub(r'^\d+\.\s*', '', s)          # drop leading "1. "
    s = re.sub(r'\s+', ' ', s).strip(' -•:')
    return s.strip()


def _is_meta(s: str) -> bool:
    low = s.lower()
    return any(w in s for w in ('חומרים שנסרקו', 'עלויות', 'API')) or low.startswith(('cost', 'token'))
