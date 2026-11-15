"""generators/anki_export.py — Convert local flashcards deck → .apkg for Anki.

One deck per course; cards include question, answer, source, last grade.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent


def _safe_html(text: str) -> str:
    """Convert plain text to safe HTML — line breaks + escape."""
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    text = text.replace('\n', '<br>')
    return text


def export_apkg(out_path: Path | None = None,
                deck_name: str = 'Smart Study Agent',
                course_filter: str | None = None) -> Path:
    """Export flashcards_deck.json → .apkg.

    Args:
        out_path: where to write the .apkg. Default: data/flashcards.apkg.
        deck_name: top-level deck name shown in Anki.
        course_filter: if set, only export cards with this course_key.

    Returns the output path.
    """
    import genanki  # type: ignore

    deck_json = ROOT / 'data' / 'flashcards_deck.json'
    out_path = out_path or (ROOT / 'data' / 'flashcards.apkg')

    if not deck_json.exists():
        raise FileNotFoundError(f'{deck_json} not found — no flashcards generated yet.')

    cards: list[dict] = json.loads(deck_json.read_text(encoding='utf-8'))
    if course_filter:
        cards = [c for c in cards if c.get('course_key') == course_filter]
    if not cards:
        raise ValueError('No cards to export.')

    # Build a course → list mapping for sub-decks
    by_course: dict[str, list[dict]] = {}
    for c in cards:
        ck = c.get('course_key', 'unknown')
        by_course.setdefault(ck, []).append(c)

    COURSE_HE = {
        'intro_to_cs':       'מבוא מורחב',
        'calculus_2':        'חדו״א 2',
        'discrete_math_2':   'בדידה 2',
        'linear_algebra_2':  'לינארית 2',
        'microeconomics':    'מיקרו א\'',
    }

    # Anki model — single field for Q, single field for A
    model = genanki.Model(
        model_id=1607392319,  # any fixed int
        name='SmartStudyHebrewQ&A',
        fields=[
            {'name': 'Question'},
            {'name': 'Answer'},
            {'name': 'Source'},
            {'name': 'CourseKey'},
        ],
        templates=[{
            'name': 'Card 1',
            'qfmt': '<div dir="rtl" style="font-family:sans-serif;font-size:1.1em">{{Question}}</div>',
            'afmt': ('<div dir="rtl" style="font-family:sans-serif">{{FrontSide}}'
                     '<hr id="answer">{{Answer}}'
                     '<div style="font-size:0.75em;color:#888;margin-top:1em">'
                     'מקור: {{Source}} ({{CourseKey}})</div></div>'),
        }],
        css=('.card {direction:rtl;text-align:right;font-family:Arial,sans-serif;'
             'background:#fafafa;color:#222;padding:1em}'),
    )

    decks: list[genanki.Deck] = []
    for ck, ck_cards in sorted(by_course.items()):
        ck_he = COURSE_HE.get(ck, ck)
        sub_deck_id = 2059400110 + abs(hash(ck)) % 10_000_000
        sub_deck = genanki.Deck(sub_deck_id, f'{deck_name}::{ck_he}')
        for c in ck_cards:
            note = genanki.Note(
                model=model,
                fields=[
                    _safe_html(c.get('q', '')),
                    _safe_html(c.get('a', '')),
                    _safe_html(c.get('source_stem', '')),
                    ck_he,
                ],
                tags=[ck, f'diff{c.get("difficulty", 3)}'],
            )
            sub_deck.add_note(note)
        decks.append(sub_deck)

    pkg = genanki.Package(decks)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pkg.write_to_file(str(out_path))
    return out_path


if __name__ == '__main__':
    import sys
    course = sys.argv[1] if len(sys.argv) > 1 else None
    p = export_apkg(course_filter=course)
    print(f'✅ Wrote {p}')
    print(f'   Open in Anki: File → Import → {p}')
