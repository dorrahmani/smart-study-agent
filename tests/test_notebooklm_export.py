"""
tests/test_notebooklm_export.py — End-to-end contract test for
exporters/notebooklm_obsidian.export_artifact.

Feeds 3 fake artifact dicts (quiz, podcast, video) and asserts:
  - target .md file exists in VAULT_NOTEBOOKLM_DIR/{course_key}/
  - frontmatter parses as YAML
  - frontmatter has the expected keys (type, artifact_type, course,
    course_key, date, title, generated_from, file_path, tags)

Cleans up created files at the end so the vault stays pristine.

Usage:  python -m tests.test_notebooklm_export
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import yaml

# Make project root importable when run via `python -m tests.test_notebooklm_export`
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from exporters.notebooklm_obsidian import export_artifact  # noqa: E402
from config.paths import VAULT_NOTEBOOKLM_DIR  # noqa: E402


# ─── fake artifacts ───────────────────────────────────────────────────────
FAKE_QUIZ = {
    'course_key':     'linear_algebra_2',
    'course_name':    'אלגברה לינארית 2ב',
    'artifact_type':  'quiz',
    'title':          'TEST_FAKE Quiz on Eigenvalues',
    'date':           '2026-04-29',
    'generated_from': ['[[הרצאה 1 - linear_algebra_2]]', '[[הרצאה 2 - linear_algebra_2]]'],
    'file_path':      'data/notebooklm/linear_algebra_2/2026-04-29-quiz.json',
    'data': [
        {
            'q': 'What is an eigenvalue?',
            'options': ['A scalar', 'A vector', 'A matrix', 'A function'],
            'correct': 0,
            'explanation': 'An eigenvalue is a scalar λ such that Av = λv.',
        },
        {
            'q': 'How many eigenvalues does an n×n matrix have (with multiplicity)?',
            'options': ['n', 'n-1', '2n', 'Infinitely many'],
            'correct': 0,
        },
    ],
    'markdown': None,
}

FAKE_PODCAST = {
    'course_key':     'calculus_2',
    'course_name':    'חשבון דיפרנציאלי ואינטגרלי 2ב',
    'artifact_type':  'podcast',
    'title':          'TEST_FAKE Podcast - Multiple Integrals',
    'date':           '2026-04-29',
    'generated_from': ['[[הרצאה 5 - calculus_2]]'],
    'file_path':      'data/notebooklm/calculus_2/2026-04-29-podcast.mp3',
    'data':           None,
    'markdown':       None,
}

FAKE_VIDEO = {
    'course_key':     'intro_to_cs',
    'course_name':    'מבוא מורחב למדמ"ח',
    'artifact_type':  'video',
    'title':          'TEST_FAKE Video - Recursion Walkthrough',
    'date':           '2026-04-29',
    'generated_from': ['[[הרצאה 3 - intro_to_cs]]'],
    'file_path':      'data/notebooklm/intro_to_cs/2026-04-29-video.mp4',
    'data':           None,
    'markdown':       None,
}

REQUIRED_KEYS = {
    'type', 'artifact_type', 'title', 'course', 'course_key',
    'date', 'generated_from', 'file_path', 'tags',
}


def _parse_frontmatter(md_text: str) -> dict:
    assert md_text.startswith('---\n'), 'note must start with YAML frontmatter'
    end = md_text.index('\n---\n', 4)
    fm_block = md_text[4:end]
    return yaml.safe_load(fm_block)


def _check(artifact: dict) -> tuple[bool, str, Path | None]:
    try:
        out = export_artifact(artifact)
    except Exception as e:
        return False, f'export_artifact raised: {e!r}\n{traceback.format_exc()}', None

    if not out.exists():
        return False, f'output file does not exist: {out}', out

    text = out.read_text(encoding='utf-8')
    try:
        fm = _parse_frontmatter(text)
    except Exception as e:
        return False, f'frontmatter is not valid YAML: {e!r}', out

    missing = REQUIRED_KEYS - set(fm.keys())
    if missing:
        return False, f'frontmatter missing keys: {missing}\nfm={fm}', out

    if fm.get('artifact_type') != artifact['artifact_type']:
        return False, f'artifact_type mismatch: fm={fm.get("artifact_type")!r} expected={artifact["artifact_type"]!r}', out

    if fm.get('course_key') != artifact['course_key']:
        return False, f'course_key mismatch: fm={fm.get("course_key")!r} expected={artifact["course_key"]!r}', out

    tags = fm.get('tags') or []
    if artifact['artifact_type'] not in tags or artifact['course_key'] not in tags:
        return False, f'tags missing artifact_type/course_key: {tags}', out

    return True, 'ok', out


def main() -> int:
    print(f'VAULT_NOTEBOOKLM_DIR = {VAULT_NOTEBOOKLM_DIR}')
    print()

    cases = [('quiz', FAKE_QUIZ), ('podcast', FAKE_PODCAST), ('video', FAKE_VIDEO)]
    created: list[Path] = []
    failures: list[tuple[str, str]] = []

    for name, art in cases:
        ok, msg, path = _check(art)
        status = 'PASS' if ok else 'FAIL'
        print(f'[{status}] {name:8s} -> {path}')
        if not ok:
            print(f'        {msg}')
            failures.append((name, msg))
        if path is not None:
            created.append(path)

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
    print('RESULT: all 3 artifacts produced valid Obsidian notes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
