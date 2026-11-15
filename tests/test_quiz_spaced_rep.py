"""
tests/test_quiz_spaced_rep.py — verify quiz/flashcard artifacts emit
markdown that obsidian-spaced-repetition recognizes.

Two fake quizzes:
  1. short Q&A → single-line `שאלה::תשובה`
  2. long Q&A with options/explanation → multi-line `שאלה\\n?\\nתשובה`

Asserts:
  - `#flashcards` tag is in the YAML frontmatter `tags:` list
  - `#flashcards/<course_key>` deck tag appears in body
  - `::` separator appears for the short card
  - `\\n?\\n` separator appears for the long card
  - install hint callout `[!info] Spaced Repetition` is present

Cleans up the produced files at the end.

Usage:  PYTHONIOENCODING=utf-8 python -m tests.test_quiz_spaced_rep
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from exporters.notebooklm_obsidian import export_artifact  # noqa: E402
from config.paths import VAULT_NOTEBOOKLM_DIR  # noqa: E402


# ─── fake artifacts ───────────────────────────────────────────────────────
SHORT_QUIZ = {
    'course_key':    'linear_algebra_2',
    'course_name':   'אלגברה לינארית 2ב',
    'artifact_type': 'quiz',
    'title':         'TEST_FAKE Short Spaced-Rep Quiz',
    'date':          '2026-04-29',
    'generated_from': ['[[הרצאה 1 - linear_algebra_2]]'],
    'file_path':     'data/notebooklm/linear_algebra_2/short_quiz.json',
    'data': [
        # short → single-line `::`
        {'q': 'מה זה ערך עצמי?', 'options': None, 'correct': None,
         'explanation': 'סקלר λ כך ש-Av=λv'},
    ],
}

LONG_QUIZ = {
    'course_key':    'linear_algebra_2',
    'course_name':   'אלגברה לינארית 2ב',
    'artifact_type': 'quiz',
    'title':         'TEST_FAKE Long Spaced-Rep Quiz',
    'date':          '2026-04-29',
    'generated_from': ['[[הרצאה 2 - linear_algebra_2]]'],
    'file_path':     'data/notebooklm/linear_algebra_2/long_quiz.json',
    'data': [
        {
            'q': 'מהו מספר הערכים העצמיים של מטריצה n×n מעל המרוכבים (כולל ריבוי)?',
            'options': ['n', 'n-1', '2n', 'אינסוף'],
            'correct': 0,
            'explanation': 'מהפולינום האופייני שהוא ממעלה n מעל C, ולכן n שורשים עם ריבוי.',
        },
    ],
}


def _parse_frontmatter(md_text: str) -> dict:
    assert md_text.startswith('---\n'), 'note must start with YAML frontmatter'
    end = md_text.index('\n---\n', 4)
    return yaml.safe_load(md_text[4:end])


def _check_short(path: Path) -> tuple[bool, str]:
    text = path.read_text(encoding='utf-8')
    fm = _parse_frontmatter(text)
    tags = fm.get('tags') or []
    if 'flashcards' not in tags:
        return False, f'expected "flashcards" in frontmatter tags, got {tags!r}'
    if '#flashcards/linear_algebra_2' not in text:
        return False, 'missing deck tag #flashcards/linear_algebra_2 in body'
    if '[!info] Spaced Repetition' not in text:
        return False, 'missing install-hint callout in body'
    if '::' not in text:
        return False, 'expected single-line `::` separator for short card'
    # find the `::` line and validate it is `שאלה::תשובה`
    line = next((ln for ln in text.splitlines() if '::' in ln and not ln.startswith('#')), '')
    if not line:
        return False, 'no `::` content line found'
    left, _, right = line.partition('::')
    if not left.strip() or not right.strip():
        return False, f'malformed flashcard line: {line!r}'
    return True, 'ok'


def _check_long(path: Path) -> tuple[bool, str]:
    text = path.read_text(encoding='utf-8')
    fm = _parse_frontmatter(text)
    tags = fm.get('tags') or []
    if 'flashcards' not in tags:
        return False, f'expected "flashcards" in frontmatter tags, got {tags!r}'
    if '#flashcards/linear_algebra_2' not in text:
        return False, 'missing deck tag #flashcards/linear_algebra_2 in body'
    if '[!info] Spaced Repetition' not in text:
        return False, 'missing install-hint callout in body'
    if '\n?\n' not in text:
        return False, 'expected multi-line `\\n?\\n` separator for long card'
    return True, 'ok'


def main() -> int:
    print(f'VAULT_NOTEBOOKLM_DIR = {VAULT_NOTEBOOKLM_DIR}')
    print()

    cases = [
        ('short-quiz', SHORT_QUIZ, _check_short),
        ('long-quiz',  LONG_QUIZ,  _check_long),
    ]
    created: list[Path] = []
    failures: list[tuple[str, str]] = []

    for name, art, checker in cases:
        try:
            out = export_artifact(art)
        except Exception as e:
            failures.append((name, f'export_artifact raised: {e!r}\n{traceback.format_exc()}'))
            print(f'[FAIL] {name}: export raised')
            continue
        created.append(out)
        ok, msg = checker(out)
        status = 'PASS' if ok else 'FAIL'
        print(f'[{status}] {name:11s} -> {out}')
        if not ok:
            print(f'        {msg}')
            failures.append((name, msg))

    # cleanup
    print()
    print('Cleanup:')
    for p in created:
        try:
            if p.exists():
                p.unlink()
                print(f'  removed {p}')
        except Exception as e:
            print(f'  WARN could not remove {p}: {e}')

    print()
    if failures:
        print(f'RESULT: {len(failures)} failure(s)')
        return 1
    print('RESULT: spaced-repetition formatting OK for both short + long quizzes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
