"""
exporters/announcement_obsidian.py — Moodle announcements → Vault md.

Input shape (passed by the future moodle_watcher):
    {
        "course_key": str,
        "course_name": str,
        "title": str,
        "sender": str,
        "date": "YYYY-MM-DD" or ISO,
        "body": str (markdown or plain),
        "moodle_url": str,
    }

Output: VAULT_ANNOUNCEMENTS_DIR / {course_key} / {YYYY-MM-DD} - {safe_title}.md
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

import yaml

from config.paths import VAULT_ANNOUNCEMENTS_DIR, COURSE_NAMES
from exporters._frontmatter_lint import validate_frontmatter

_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(s: str) -> str:
    return (_SAFE_RE.sub('_', s).strip().strip('.'))[:120] or 'announcement'


def _frontmatter(d: dict) -> str:
    d = {k: v for k, v in d.items() if v not in (None, [], '')}
    return '---\n' + yaml.safe_dump(d, allow_unicode=True, sort_keys=False) + '---\n\n'


def export_announcement(ann: dict) -> Path:
    course_key = ann.get('course_key', 'unknown')
    course_name = ann.get('course_name') or COURSE_NAMES.get(course_key, course_key)
    course_dir = VAULT_ANNOUNCEMENTS_DIR / course_key
    course_dir.mkdir(parents=True, exist_ok=True)

    date_str = (ann.get('date') or datetime.now().strftime('%Y-%m-%d'))[:10]
    title = ann.get('title', 'הודעה')
    out = course_dir / f'{date_str} - {_safe_filename(title)}.md'

    fm = {
        'type':       'announcement',
        'title':      title,
        'course':     course_name,
        'course_key': course_key,
        'date':       date_str,
        'sender':     ann.get('sender'),
        'moodle_url': ann.get('moodle_url'),
        'tags':       ['announcement', course_key],
    }
    body = f"# 📢 {title}\n\n"
    if ann.get('sender'):
        body += f"**מאת:** {ann['sender']}\n"
    body += f"**תאריך:** {date_str}\n\n---\n\n"
    body += (ann.get('body') or '').strip() + '\n'
    if ann.get('moodle_url'):
        body += f"\n---\n\n[פתח ב-Moodle]({ann['moodle_url']})\n"

    missing = validate_frontmatter(fm, 'announcement')
    if missing:
        print(f'⚠️ frontmatter lint: announcement {out.name} missing {missing}')

    out.write_text(_frontmatter(fm) + body, encoding='utf-8')
    return out


def export_announcements(announcements: Iterable[dict]) -> int:
    n = 0
    for a in announcements:
        export_announcement(a)
        n += 1
    return n
