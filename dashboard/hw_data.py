"""
dashboard/hw_data.py — one homework list for the dashboard, from the same sources the bot uses.

The owner 19/10 (screenshot of the old page): solutions listed as homework of their own, no due dates, last semester's items,
no way to download or to send an answer for checking. Here: Moodle (assignments/quizzes/homework files, via
data/assignments/_cache.json) + exercises on a lecturer's site (data/assignments/lecturer.json), this semester's
courses only; "פתרון תרגיל N" is attached to "תרגיל N" (never listed alone); the same exercise from two group pages /
two sources is one row (Moodle's copy — which knows "submitted" — wins); each row knows its file for download and
for the professor check.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOL_RX = re.compile(r'פתרון|פתרונות|solution', re.I)
NUM_RX = re.compile(r'(?:תרגיל(?: בית)?|מטלה|ex(?:ercise)?|hw|homework|quiz|lecture)\D{0,6}(\d{1,2})', re.I)


def _num(title: str) -> str:
    m = NUM_RX.search(title or '')
    return m.group(1) if m else ''


def _load(path: Path, key: str | None = None) -> list:
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return (d.get(key) or []) if key and isinstance(d, dict) else (d if isinstance(d, list) else [])


def _local_files(course_key: str) -> dict[str, str]:
    """{title: local path} of the course's downloaded Moodle files (collectors/course_files)."""
    try:
        idx = json.loads((ROOT / 'data' / 'moodle' / course_key / 'files' / 'index.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {e['title']: str(ROOT / e['file']) for e in idx.values() if e.get('file')}


def unified(active: list[str] | None = None, now: datetime | None = None) -> list[dict]:
    """[{course_key, course_name, title, number, kind, due, submitted, file, file_url, solution, moodle_url, source}]"""
    if active is None:
        from config.paths import ACTIVE_COURSES
        active = list(ACTIVE_COURSES)
    raw = [a for a in _load(ROOT / 'data' / 'assignments' / '_cache.json', 'assignments') if isinstance(a, dict)]
    raw += [a for a in _load(ROOT / 'data' / 'assignments' / 'lecturer.json') if isinstance(a, dict)]
    raw = [a for a in raw if a.get('course_key') in active]
    rows: dict[tuple, dict] = {}
    solutions: dict[tuple, str] = {}
    files_by_course = {ck: _local_files(ck) for ck in active}
    for a in raw:
        ck, title = a['course_key'], (a.get('title') or '').strip()
        n = _num(title)
        local = files_by_course.get(ck, {}).get(title) or a.get('pdf') or a.get('local_path') or ''
        if SOL_RX.search(title):                                   # a solution: attach, never list on its own
            if n:
                solutions[(ck, n)] = local or a.get('url', '')
            continue
        due = None
        try:
            due = datetime.fromisoformat(str(a.get('due_date'))) if a.get('due_date') not in (None, 'None', '') else None
        except ValueError:
            pass
        key = (ck, n) if n and a.get('kind') != 'quiz' else (ck, a.get('kind', ''), title)
        row = {'course_key': ck, 'course_name': a.get('course_name', ck), 'title': title, 'number': n,
               'kind': a.get('kind', 'assign'), 'due': due, 'submitted': str(a.get('submitted')) == 'True',
               'file': local if local and Path(local).exists() else '',
               'file_url': a.get('assignment_file_url') if a.get('assignment_file_url') not in (None, 'None') else
               (a.get('url') if a.get('source') == 'lecturer_site' else ''),
               'moodle_url': a.get('url') if 'moodle.tau.ac.il' in str(a.get('url', '')) else '',
               'source': 'האתר של המרצה' if a.get('source') == 'lecturer_site' else 'מודל'}
        old = rows.get(key)
        if old is None:
            rows[key] = row
        else:                                                     # the same exercise twice: keep the richest facts
            for f in ('due', 'file', 'file_url', 'moodle_url'):
                old[f] = old[f] or row[f]
            old['submitted'] = old['submitted'] or row['submitted']
            if row['source'] == 'מודל':
                old['source'] = 'מודל'
    for r in rows.values():
        r['solution'] = solutions.get((r['course_key'], r['number']), '') if r['number'] else ''
    return sorted(rows.values(), key=lambda r: (r['due'] is None, r['due'] or datetime.max, r['course_key'],
                                                int(r['number'] or 99)))
