"""
utils/srt_quality.py
====================
Shared SRT quality helpers — imported by all batch scripts.

Root cause of the "Cyrillic SRT" incident (2026-05-11 לינארית):
  Panopto auto-captions for Hebrew math lectures sometimes produce
  phonetic Russian/Cyrillic transliteration instead of Hebrew text.
  The SRT file exists and is large (>5000 bytes) so size-only checks
  pass silently, causing summaries to be generated from PDF alone.

Fix: always call srt_is_good_hebrew() before using any SRT in a batch.
"""

from pathlib import Path


def srt_is_good_hebrew(path, min_chars: int = 5000, min_ratio: float = 0.20) -> bool:
    """
    Returns True only if the SRT is usable Hebrew content.

    Checks:
    1. File exists and is >= min_chars bytes
    2. Hebrew character ratio >= min_ratio (catches Cyrillic transliterations)

    min_ratio=0.20 means ≥20% of alphabetic chars must be Hebrew (א-ת).
    Cyrillic SRTs score ~0%, English SRTs for Hebrew lectures score ~0-5%.
    Real Hebrew lecture SRTs typically score 50-90%.
"""
    p = Path(path)
    if not p.exists() or p.stat().st_size < min_chars:
        return False
    text  = p.read_text(encoding='utf-8', errors='replace')
    heb   = sum(1 for c in text if 'א' <= c <= 'ת')
    alpha = sum(1 for c in text if c.isalpha())
    if alpha == 0:
        return False
    ratio = heb / alpha
    if ratio < min_ratio:
        # Print diagnostic so it's visible in logs
        print(f"  ⚠️ SRT לא עברי ({ratio:.0%} Hebrew) — {p.name}")
        return False
    return True


def quarantine_bad_srt(path) -> None:
    """Rename a known-bad SRT to .srt.bad so it won't be reused."""
    p = Path(path)
    if p.exists():
        bad = p.with_suffix('.srt.bad')
        try:
            p.rename(bad)
            print(f"  🗑️ SRT גרוע הועבר ל-{bad.name}")
        except Exception as e:
            print(f"  ⚠️ לא ניתן להעביר SRT גרוע: {e}")
