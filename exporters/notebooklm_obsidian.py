"""
exporters/notebooklm_obsidian.py — NotebookLM artifacts → Vault md.

Artifact types: quiz, podcast, video, flashcard, study_guide, mind_map.
Output: VAULT_NOTEBOOKLM_DIR / {course_key} / {YYYY-MM-DD} - {artifact_type} - {title}.md

Each note links back (`generated_from`) to the source lectures used.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from config.paths import VAULT_NOTEBOOKLM_DIR, COURSE_NAMES
from exporters._frontmatter_lint import validate_frontmatter

_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(s: str) -> str:
    return (_SAFE_RE.sub('_', s).strip().strip('.'))[:100] or 'artifact'


def _fm(d: dict) -> str:
    d = {k: v for k, v in d.items() if v not in (None, [], '')}
    return '---\n' + yaml.safe_dump(d, allow_unicode=True, sort_keys=False) + '---\n\n'


def export_artifact(artifact: dict) -> Path:
    """
    artifact = {
        'course_key', 'course_name',
        'artifact_type': 'quiz' | 'podcast' | 'video' | 'flashcard' | 'study_guide' | 'mind_map',
        'title': str,
        'date': YYYY-MM-DD,
        'generated_from': ['[[lecture wikilink]]', ...],
        'file_path': path to MP3/MP4/JSON,
        'data': dict | list | None,    # quiz JSON / flashcards / etc.
        'markdown': str | None,        # ready-made markdown body (study guide)
    }
    """
    course_key = artifact.get('course_key', 'unknown')
    course_name = artifact.get('course_name') or COURSE_NAMES.get(course_key, course_key)
    art_type = artifact.get('artifact_type', 'artifact')
    course_dir = VAULT_NOTEBOOKLM_DIR / course_key
    course_dir.mkdir(parents=True, exist_ok=True)

    date_str = (artifact.get('date') or datetime.now().strftime('%Y-%m-%d'))[:10]
    title = artifact.get('title', art_type)
    out = course_dir / f'{date_str} - {art_type} - {_safe(title)}.md'

    tags = ['notebooklm', art_type, course_key]
    if art_type in ('quiz', 'flashcard'):
        tags.append('flashcards')

    fm: dict[str, Any] = {
        'type':            'notebooklm',
        'artifact_type':   art_type,
        'title':           title,
        'course':          course_name,
        'course_key':      course_key,
        'date':            date_str,
        'generated_from':  artifact.get('generated_from'),
        'file_path':       artifact.get('file_path'),
        'tags':            tags,
    }

    icon = {
        'quiz': '🧠', 'podcast': '🎙️', 'video': '🎬',
        'flashcard': '🃏', 'study_guide': '📖', 'mind_map': '🗺️',
    }.get(art_type, '🎓')

    body = f'# {icon} {title}\n\n'
    body += f'**קורס:** {course_name}\n**סוג:** {art_type}\n\n'

    if art_type in ('quiz', 'flashcard'):
        body += (
            '> [!info] Spaced Repetition\n'
            '> התקן את הפלאגין `obsidian-spaced-repetition`. הקלד `Ctrl+P` → '
            '`Spaced Repetition: Open Flashcard Review` כדי להתחיל.\n\n'
        )

    if artifact.get('generated_from'):
        body += '## מקורות\n\n'
        for link in artifact['generated_from']:
            body += f'- {link}\n'
        body += '\n'

    if artifact.get('markdown'):
        body += artifact['markdown'].strip() + '\n'
    elif art_type == 'quiz' and isinstance(artifact.get('data'), (list, dict)):
        body += '## כרטיסיות\n\n'
        body += f'#flashcards/{course_key} #{course_key}\n\n'
        questions = artifact['data'] if isinstance(artifact['data'], list) else artifact['data'].get('questions', [])
        for i, q in enumerate(questions, 1):
            question = (q.get('q') or '').strip()
            options = q.get('options') or []
            correct_idx = q.get('correct')
            explanation = (q.get('explanation') or '').strip()

            # Build the answer: if multiple-choice, show correct option; append
            # all options + explanation as multi-line context.
            correct_opt = ''
            if isinstance(correct_idx, int) and 0 <= correct_idx < len(options):
                correct_opt = str(options[correct_idx]).strip()

            # Decide single-line vs multi-line. Multi-line when there are
            # multiple-choice options to render, when the question itself
            # spans multiple lines, or when the combined Q+A is too long
            # for one line. A standalone short Q + explanation still fits
            # on one line (`שאלה::תשובה`).
            short_answer = correct_opt or explanation
            multi_line = bool(options) or '\n' in question \
                or '\n' in short_answer \
                or len(question) + len(short_answer) > 120

            if multi_line:
                body += f'{question}\n?\n'
                if correct_opt:
                    body += f'**תשובה:** {correct_opt}\n'
                if options:
                    for j, opt in enumerate(options):
                        marker = '✅' if j == correct_idx else '○'
                        body += f'- {marker} {opt}\n'
                if explanation:
                    body += f'\n> {explanation}\n'
                body += '\n'
            else:
                # short Q&A → single-line `::` flashcard
                answer = correct_opt or explanation or ''
                body += f'{question}::{answer}\n\n'
    elif art_type == 'flashcard' and isinstance(artifact.get('data'), list):
        body += '## כרטיסיות\n\n'
        body += f'#flashcards/{course_key} #{course_key}\n\n'
        for c in artifact['data']:
            front = (c.get('front') or '').strip()
            back = (c.get('back') or '').strip()
            if not front:
                continue
            # Use multi-line variant when either side spans multiple lines
            # or together they exceed a reasonable single-line length.
            if '\n' in front or '\n' in back or len(front) + len(back) > 120:
                body += f'{front}\n?\n{back}\n\n'
            else:
                body += f'{front}::{back}\n\n'
    elif artifact.get('file_path'):
        if art_type == 'podcast':
            body += f'\n![[{Path(artifact["file_path"]).name}]]\n'
        else:
            body += f'\n[קובץ]({artifact["file_path"]})\n'

    # Lint before writing (non-blocking; exporters log but never crash).
    missing = validate_frontmatter(fm, 'nlm_artifact')
    if missing:
        print(f'⚠️ frontmatter lint: nlm_artifact {out.name} '
              f'missing keys {missing}')

    out.write_text(_fm(fm) + body, encoding='utf-8')
    return out
