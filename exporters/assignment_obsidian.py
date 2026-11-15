"""
exporters/assignment_obsidian.py — Assignments → Obsidian Vault md notes.

Reads data/assignments/_cache.json (untouched by us — produced by the existing
fetch_assignments_now.py / assignment_tracker.py pipeline) and writes one md
file per assignment under VAULT_ASSIGNMENTS_DIR / {course_key} / {stem}.md.

Idempotent: only rewrites if the source data is newer than the existing file.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

from exporters._frontmatter_lint import validate_frontmatter
from config.paths import (
    ASSIGNMENTS_CACHE,
    VAULT_ASSIGNMENTS_DIR,
    COURSE_NAMES,
)


_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(s: str) -> str:
    s = _SAFE_RE.sub('_', s).strip().strip('.')
    return s[:120] or 'untitled'


def _frontmatter(data: dict) -> str:
    # Drop None values
    clean = {k: v for k, v in data.items() if v is not None and v != []}
    body = yaml.safe_dump(clean, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)
    return f'---\n{body}---\n\n'


def _classify_status(a: dict) -> str:
    if a.get('grade') is not None or a.get('graded'):
        return 'graded'
    if a.get('submitted'):
        return 'submitted'
    return 'pending'


def _build_md(a: dict) -> tuple[str, dict]:
    """Return (full_md_text, frontmatter_dict)."""
    course_key = a.get('course_key', '')
    course_name = a.get('course_name') or COURSE_NAMES.get(course_key, course_key)
    title = a.get('title', 'Untitled')
    status = _classify_status(a)

    fm: dict[str, Any] = {
        'type':        'assignment',
        'title':       title,
        'course':      course_name,
        'course_key':  course_key,
        'status':      status,
        'due_date':    a.get('due_date'),
        'days_left':   a.get('days_left'),
        'source':      a.get('source'),
        'url':         a.get('url'),
        'local_path':  a.get('local_path'),
        'submission_files': a.get('submission_files'),
        'grade':       a.get('grade'),
        'feedback':    a.get('feedback'),
        'submitted_at':a.get('submitted_at'),
        'tags':        ['assignment', course_key, f'status-{status}'],
        'rag_tags':    a.get('rag_tags'),
        'related':     a.get('related'),
        'created':     datetime.now().strftime('%Y-%m-%d'),
    }
    fm = {k: v for k, v in fm.items() if v not in (None, [], '')}

    # Body
    icon = {'pending': '⏳', 'submitted': '✅', 'graded': '🎓'}.get(status, '📋')
    lines = [
        f'# {icon} {title}',
        '',
        f'**קורס:** {course_name}',
        f'**סטטוס:** {status}',
    ]
    if a.get('due_date'):
        lines.append(f'**מועד הגשה:** {a["due_date"]}')
    if a.get('days_left') is not None:
        lines.append(f'**ימים שנותרו:** {a["days_left"]}')
    if a.get('grade') is not None:
        lines.append(f'**ציון:** {a["grade"]}')
    if a.get('feedback'):
        lines += ['', '## הערות מרצה', '', str(a['feedback'])]
    if a.get('url'):
        lines += ['', f'[קישור למקור]({a["url"]})']
    if a.get('local_path'):
        lines += ['', f'**קובץ תרגיל:** `{a["local_path"]}`']
    if a.get('submission_files'):
        lines += ['', '## קבצי הגשה']
        for f in a['submission_files']:
            lines.append(f'- `{f}`')

    body = '\n'.join(lines) + '\n'
    return _frontmatter(fm) + body, fm


def export_assignments(force: bool = False) -> int:
    """Export all assignments from cache to vault. Returns count exported."""
    if not ASSIGNMENTS_CACHE.exists():
        return 0
    data = json.loads(ASSIGNMENTS_CACHE.read_text(encoding='utf-8'))
    assignments = data.get('assignments', [])
    cache_mtime = ASSIGNMENTS_CACHE.stat().st_mtime

    count = 0
    for a in assignments:
        course_key = a.get('course_key') or 'unknown'
        course_dir = VAULT_ASSIGNMENTS_DIR / course_key
        course_dir.mkdir(parents=True, exist_ok=True)

        title = a.get('title') or 'untitled'
        stem = _safe_filename(title)
        out = course_dir / f'{stem}.md'

        if not force and out.exists() and out.stat().st_mtime >= cache_mtime:
            continue

        md, fm_used = _build_md(a)
        missing = validate_frontmatter(fm_used, 'assignment')
        if missing:
            print(f'⚠️ frontmatter lint: assignment {out.name} missing {missing}')
        out.write_text(md, encoding='utf-8')
        count += 1
    return count


if __name__ == '__main__':
    n = export_assignments(force='--force' in sys.argv)
    print(f'Exported {n} assignments to {VAULT_ASSIGNMENTS_DIR}')
