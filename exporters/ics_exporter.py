"""
exporters/ics_exporter.py — Export assignment due dates to .ics calendar.

Output: data/calendar.ics (subscribe in Google Calendar via 'Add by URL'
or import once).

No external deps — RFC 5545 minimal generator.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from config.paths import DATA_DIR
from dashboard import vault_reader as vr

ICS_PATH = DATA_DIR / 'calendar.ics'


def _escape(s: str) -> str:
    return (s or '').replace('\\', '\\\\').replace(',', '\\,').replace(';', '\\;').replace('\n', '\\n')


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime('%Y%m%dT%H%M%S')


def _parse_due(due) -> datetime | None:
    if not due:
        return None
    s = str(due)
    # Try full ISO
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        pass
    # Date only
    m = re.match(r'(\d{4}-\d{2}-\d{2})', s)
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y-%m-%d').replace(hour=23, minute=59)
        except ValueError:
            return None
    return None


def export_ics() -> Path:
    """Build calendar.ics from all pending assignments in the vault."""
    vr.invalidate_cache()
    assigns = vr.list_assignments()

    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//Smart Study Agent//assignments//HE',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:Smart Study Agent — מטלות',
    ]
    now_str = _fmt_dt(datetime.now())
    for a in assigns:
        if a.get('status') in ('graded',):
            continue
        due = _parse_due(a.get('due_date'))
        if not due:
            continue
        title = a.get('title', 'מטלה')
        course = a.get('course') or a.get('course_key', '')
        url = a.get('url', '')
        desc = f"קורס: {course}\nסטטוס: {a.get('status','pending')}\n{url}"
        uid = f'assignment-{a.get("course_key","x")}-{re.sub(r"[^a-z0-9]+","-",title.lower())[:40]}@smart-study'
        lines += [
            'BEGIN:VEVENT',
            f'UID:{uid}',
            f'DTSTAMP:{now_str}',
            f'DTSTART:{_fmt_dt(due - timedelta(hours=1))}',
            f'DTEND:{_fmt_dt(due)}',
            f'SUMMARY:{_escape(f"📋 {title} — {course}")}',
            f'DESCRIPTION:{_escape(desc)}',
        ]
        if url:
            lines.append(f'URL:{url}')
        lines += ['BEGIN:VALARM', 'TRIGGER:-P1D', 'ACTION:DISPLAY',
                  f'DESCRIPTION:{_escape(title)} בעוד יום', 'END:VALARM']
        lines += ['BEGIN:VALARM', 'TRIGGER:-PT2H', 'ACTION:DISPLAY',
                  f'DESCRIPTION:{_escape(title)} בעוד שעתיים', 'END:VALARM']
        lines.append('END:VEVENT')
    lines.append('END:VCALENDAR')

    ICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ICS_PATH.write_text('\r\n'.join(lines) + '\r\n', encoding='utf-8')
    return ICS_PATH


if __name__ == '__main__':
    p = export_ics()
    print(f'Wrote {p}')
