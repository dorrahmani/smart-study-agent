"""generators/ical_export.py — Generate iCal (.ics) file from assignments cache.

Output: data/deadlines.ics — subscribe in iOS Calendar / Google Calendar / Outlook.

Each deadline becomes one VEVENT with:
  - Title: [Course] Assignment title
  - Date: due_date as all-day event (or specific time if present)
  - Description: link to Moodle submission page (if available)
  - Alarm: 24h before + 1h before
  - UID: stable hash of (course_key, title) so updates replace existing events
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Course key → Hebrew display name
COURSE_HE = {
    'intro_to_cs':       'מבוא מורחב',
    'calculus_2':        'חדו״א 2',
    'discrete_math_2':   'בדידה 2',
    'linear_algebra_2':  'לינארית 2',
    'microeconomics':    'מיקרו א\'',
}


def _escape_ical(text: str) -> str:
    """Escape iCal text values per RFC 5545."""
    if not text:
        return ''
    text = text.replace('\\', '\\\\')
    text = text.replace('\n', '\\n').replace('\r', '')
    text = text.replace(',', '\\,').replace(';', '\\;')
    return text


def _fold(line: str) -> str:
    """Fold long iCal lines to 75 octets per RFC 5545."""
    if len(line.encode('utf-8')) <= 75:
        return line
    out = []
    enc = line.encode('utf-8')
    while len(enc) > 75:
        # Find safe break point (don't split UTF-8 multibyte char)
        n = 75
        while (enc[n] & 0xC0) == 0x80:
            n -= 1
        out.append(enc[:n].decode('utf-8'))
        enc = enc[n:]
    if enc:
        out.append(enc.decode('utf-8'))
    return '\r\n '.join(out)


def _uid_for(course_key: str, title: str) -> str:
    h = hashlib.sha1(f'{course_key}::{title}'.encode('utf-8')).hexdigest()[:16]
    return f'{h}@smart-study-agent.local'


def _parse_due(due: str) -> datetime | None:
    """Parse various date formats into a UTC datetime, or None."""
    if not due or due == 'None' or due is None:
        return None
    if isinstance(due, datetime):
        return due if due.tzinfo else due.replace(tzinfo=timezone.utc)
    s = str(due).strip()
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M',
                '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M',
                '%Y-%m-%d', '%d/%m/%Y %H:%M', '%d/%m/%Y'):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Try ISO with offset
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


def build_ics(out_path: Path | None = None) -> Path:
    """Read assignments cache, write .ics to data/deadlines.ics. Returns path."""
    cache_path = ROOT / 'data' / 'assignments' / '_cache.json'
    out_path = out_path or (ROOT / 'data' / 'deadlines.ics')

    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding='utf-8'))
        except Exception:
            cache = {}

    items = cache.get('assignments', []) or []

    now = datetime.now(timezone.utc)
    dtstamp = now.strftime('%Y%m%dT%H%M%SZ')

    lines: list[str] = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//Smart Study Agent//Deadlines//HE',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:📚 דדליינים — Smart Study',
        'X-WR-CALDESC:דדליינים אקדמיים מתעדכנים אוטומטית',
        'X-WR-TIMEZONE:Asia/Jerusalem',
        # Define the Asia/Jerusalem timezone so iOS/Android render correctly
        'BEGIN:VTIMEZONE',
        'TZID:Asia/Jerusalem',
        'BEGIN:STANDARD',
        'DTSTART:19700101T020000',
        'TZOFFSETFROM:+0300',
        'TZOFFSETTO:+0200',
        'TZNAME:IST',
        'RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU',
        'END:STANDARD',
        'BEGIN:DAYLIGHT',
        'DTSTART:19700101T020000',
        'TZOFFSETFROM:+0200',
        'TZOFFSETTO:+0300',
        'TZNAME:IDT',
        'RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1FR',
        'END:DAYLIGHT',
        'END:VTIMEZONE',
    ]

    n_events = 0
    for a in items:
        if not isinstance(a, dict):
            continue
        if a.get('submitted'):
            continue  # don't show submitted assignments
        due = _parse_due(a.get('due_date'))
        if not due:
            continue

        ck = a.get('course_key', '')
        course_he = COURSE_HE.get(ck, ck or '?')
        title = (a.get('title') or '?').strip()
        url = a.get('url') or ''

        summary = f'[{course_he}] {title}'
        desc_parts = [f'קורס: {course_he}', f'מטלה: {title}']
        if url:
            desc_parts.append(f'קישור: {url}')
        description = '\\n'.join(_escape_ical(p) for p in desc_parts)

        uid = _uid_for(ck, title)
        # Event in Asia/Jerusalem local time. iOS handles TZID properly.
        local = due.astimezone(timezone(timedelta(hours=3)))  # display ts only
        dt_local = local.strftime('%Y%m%dT%H%M%S')
        # End = +1 hour for events with specific time
        end_local = (local + timedelta(hours=1)).strftime('%Y%m%dT%H%M%S')

        lines.extend([
            'BEGIN:VEVENT',
            f'UID:{uid}',
            f'DTSTAMP:{dtstamp}',
            f'DTSTART;TZID=Asia/Jerusalem:{dt_local}',
            f'DTEND;TZID=Asia/Jerusalem:{end_local}',
            _fold(f'SUMMARY:{_escape_ical(summary)}'),
            _fold(f'DESCRIPTION:{description}'),
            'STATUS:CONFIRMED',
            'TRANSP:OPAQUE',
            # 24h alarm
            'BEGIN:VALARM',
            'ACTION:DISPLAY',
            f'DESCRIPTION:{_escape_ical("דדליין מחר: " + title)}',
            'TRIGGER:-P1D',
            'END:VALARM',
            # 1h alarm
            'BEGIN:VALARM',
            'ACTION:DISPLAY',
            f'DESCRIPTION:{_escape_ical("דדליין בעוד שעה: " + title)}',
            'TRIGGER:-PT1H',
            'END:VALARM',
            'END:VEVENT',
        ])
        n_events += 1

    lines.append('END:VCALENDAR')

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text('\r\n'.join(lines) + '\r\n', encoding='utf-8')
    return out_path


if __name__ == '__main__':
    p = build_ics()
    print(f'✅ Wrote {p}')
    print(f'   Subscribe in iOS Calendar: copy file to iPhone, tap to open.')
