"""
pipelines/weekly_digest.py — `📅 שבוע {YYYY-WW}.md` digest auto-generator.

For each ISO week, write/refresh a single note at:
    {VAULT_DIR}/📅 שבועות/{YYYY}-W{WW}.md

Contents (Hebrew):
    - Lectures that arrived this week (sorted by date)
    - Assignments published / due / graded this week
    - Announcements this week
    - Top concepts gaining new references this week
    - Per-course mini status table (lectures / assignments / announcements)

Idempotent — re-running rewrites the file with current data. Safe to call
multiple times in the same week.

Usage:
    from pipelines.weekly_digest import write_weekly_digest
    path = write_weekly_digest()      # current ISO week
    path = write_weekly_digest(year=2026, week=18)  # explicit
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import yaml

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from config.paths import (
    VAULT_DIR, COURSE_NAMES, COURSE_ORDER,
)


WEEKS_DIR = VAULT_DIR / '📅 שבועות'


# ─── ISO week math ─────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now()


def _iso_year_week(d: datetime) -> tuple[int, int]:
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _iso_week_bounds(year: int, week: int) -> tuple[datetime, datetime]:
    """Monday 00:00 .. Sunday 23:59:59 for the given ISO week."""
    monday = datetime.strptime(f'{year}-W{week:02d}-1', '%G-W%V-%u')
    sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return monday, sunday


def _in_window(date_str: str | None, start: datetime, end: datetime) -> bool:
    if not date_str:
        return False
    s = str(date_str)[:10]
    try:
        d = datetime.strptime(s, '%Y-%m-%d')
    except ValueError:
        return False
    return start.date() <= d.date() <= end.date()


# ─── Data gathering ────────────────────────────────────────────────────────

def gather_for_week(year: int, week: int) -> dict:
    """All vault items that belong to this ISO week, grouped for rendering."""
    from dashboard import vault_reader
    monday, sunday = _iso_week_bounds(year, week)

    # Lectures + tutorials
    lectures   = []
    tutorials  = []
    for s in vault_reader.list_summaries():
        if _in_window(s.get('date'), monday, sunday):
            t = (s.get('fm') or {}).get('type_en') or s.get('type')
            (tutorials if t == 'tutorial' else lectures).append(s)

    # Assignments — divided into "published" (created this week) and
    # "due" (due_date this week). Status appears on each row.
    assignments_published: list[dict] = []
    assignments_due:       list[dict] = []
    for a in vault_reader.list_assignments(course=None):
        fm = a.get('fm') or {}
        created = fm.get('created') or a.get('date')
        if _in_window(created, monday, sunday):
            assignments_published.append(a)
        due = fm.get('due_date')
        if _in_window(due, monday, sunday):
            assignments_due.append(a)

    # Announcements
    announcements = []
    if hasattr(vault_reader, 'list_announcements'):
        for n in vault_reader.list_announcements():
            if _in_window(n.get('date'), monday, sunday):
                announcements.append(n)
    else:
        # fallback: scan VAULT_ANNOUNCEMENTS_DIR for stems prefixed YYYY-MM-DD
        from config.paths import VAULT_ANNOUNCEMENTS_DIR
        if VAULT_ANNOUNCEMENTS_DIR.exists():
            for md in VAULT_ANNOUNCEMENTS_DIR.rglob('*.md'):
                stem = md.stem
                if len(stem) >= 10 and _in_window(stem[:10], monday, sunday):
                    announcements.append({
                        'stem': stem, 'date': stem[:10],
                        'title': stem[10:].lstrip(' -').strip(),
                        'path': md, 'course_key': None,
                    })

    # Concept activity — count new wikilinks added this week.
    # Cheap heuristic: which concept notes were modified this week
    # (Obsidian rewrites them as new links arrive via relinker).
    concepts_active: list[tuple[str, int, str]] = []  # (stem, n_refs, course_key)
    concepts_dir = VAULT_DIR / 'מושגים'
    if concepts_dir.exists():
        for md in concepts_dir.rglob('*.md'):
            try:
                mtime = datetime.fromtimestamp(md.stat().st_mtime)
            except OSError:
                continue
            if not (monday <= mtime <= sunday):
                continue
            try:
                head = md.read_text(encoding='utf-8', errors='replace')[:1500]
                if head.startswith('---'):
                    fm = yaml.safe_load(head.split('---', 2)[1]) or {}
                else:
                    fm = {}
            except Exception:
                fm = {}
            n = int(fm.get('n_references') or 0)
            ck = fm.get('course_key') or (fm.get('tags') or [None])[0]
            concepts_active.append((md.stem, n, ck))
    concepts_active.sort(key=lambda r: -r[1])

    return {
        'monday':                monday,
        'sunday':                sunday,
        'lectures':              lectures,
        'tutorials':             tutorials,
        'assignments_published': assignments_published,
        'assignments_due':       assignments_due,
        'announcements':         announcements,
        'concepts_active':       concepts_active,
    }


# ─── Per-course status ─────────────────────────────────────────────────────

def per_course_status(payload: dict) -> dict[str, dict[str, int]]:
    """
    {course_key: {lectures, tutorials, assignments_published, due, announcements}}.
    """
    keys = COURSE_ORDER + ['unknown']
    base: dict[str, dict[str, int]] = {
        k: {'lectures': 0, 'tutorials': 0, 'assignments_published': 0,
            'assignments_due': 0, 'announcements': 0}
        for k in keys
    }

    def _add(rows, field):
        for r in rows:
            ck = r.get('course_key') or 'unknown'
            base.setdefault(ck, dict(base['unknown']))[field] += 1

    _add(payload['lectures'],              'lectures')
    _add(payload['tutorials'],             'tutorials')
    _add(payload['assignments_published'], 'assignments_published')
    _add(payload['assignments_due'],       'assignments_due')
    _add(payload['announcements'],         'announcements')
    return base


# ─── Rendering ─────────────────────────────────────────────────────────────

def _wikilink(stem_or_dict) -> str:
    if isinstance(stem_or_dict, str):
        return f'[[{stem_or_dict}]]'
    s = stem_or_dict.get('stem') or stem_or_dict.get('filename', '').rstrip('.md')
    title = stem_or_dict.get('title') or s
    if title == s:
        return f'[[{s}]]'
    return f'[[{s}|{title}]]'


def render_digest(year: int, week: int, payload: dict) -> str:
    monday = payload['monday']
    sunday = payload['sunday']

    fm = {
        'type':       'weekly_digest',
        'type_en':    'weekly_digest',
        'iso_year':   year,
        'iso_week':   week,
        'monday':     monday.strftime('%Y-%m-%d'),
        'sunday':     sunday.strftime('%Y-%m-%d'),
        'modified':   datetime.now().isoformat(timespec='seconds'),
        'tags':       ['weekly_digest', 'auto'],
    }
    out = ['---', yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).rstrip(), '---', '']
    out.append(f'# 📅 שבוע {year}-W{week:02d}')
    out.append(f'_({monday.strftime("%d/%m/%Y")} – {sunday.strftime("%d/%m/%Y")})_')
    out.append('')

    # Per-course mini table
    status = per_course_status(payload)
    out.append('## 📊 סטטוס לפי קורס')
    out.append('')
    out.append('| קורס | הרצאות | תרגולים | מטלות חדשות | מועד הגשה | הודעות |')
    out.append('|---|:-:|:-:|:-:|:-:|:-:|')
    for ck in COURSE_ORDER:
        s = status.get(ck, {})
        if not any(s.values()):
            continue
        name = COURSE_NAMES.get(ck, ck)
        out.append(
            f'| {name} '
            f'| {s["lectures"]} '
            f'| {s["tutorials"]} '
            f'| {s["assignments_published"]} '
            f'| {s["assignments_due"]} '
            f'| {s["announcements"]} |'
        )
    out.append('')

    # Lectures
    if payload['lectures'] or payload['tutorials']:
        out.append('## 🎓 הרצאות ותרגולים')
        out.append('')
        for s in sorted(payload['lectures'] + payload['tutorials'],
                        key=lambda r: r.get('date') or ''):
            ck = s.get('course_key', '')
            cname = COURSE_NAMES.get(ck, ck)
            out.append(f'- **{s.get("date","")}** · _{cname}_ — {_wikilink(s)}')
        out.append('')

    # Assignments published
    if payload['assignments_published']:
        out.append('## 📝 מטלות חדשות')
        out.append('')
        for a in sorted(payload['assignments_published'],
                        key=lambda r: (r.get('fm') or {}).get('created') or ''):
            ck = a.get('course_key', '')
            cname = COURSE_NAMES.get(ck, ck)
            status_emoji = {
                'pending':   '⏳', 'submitted': '✅',
                'graded':    '💯', 'late':      '⚠️',
            }.get(a.get('status', ''), '◽')
            due = (a.get('fm') or {}).get('due_date')
            due_part = f' · יעד: {due}' if due else ''
            out.append(
                f'- {status_emoji} _{cname}_ — {_wikilink(a)}{due_part}'
            )
        out.append('')

    # Assignments due
    if payload['assignments_due']:
        out.append('## ⏰ מטלות שמועד הגשתן השבוע')
        out.append('')
        for a in sorted(payload['assignments_due'],
                        key=lambda r: (r.get('fm') or {}).get('due_date') or ''):
            ck = a.get('course_key', '')
            cname = COURSE_NAMES.get(ck, ck)
            due = (a.get('fm') or {}).get('due_date', '?')
            out.append(f'- **{due}** · _{cname}_ — {_wikilink(a)}')
        out.append('')

    # Announcements
    if payload['announcements']:
        out.append('## 📢 הודעות')
        out.append('')
        for n in sorted(payload['announcements'],
                        key=lambda r: r.get('date') or '', reverse=True):
            ck = n.get('course_key') or ''
            cname = COURSE_NAMES.get(ck, ck) if ck else ''
            stem = n.get('stem')
            title = n.get('title') or stem
            prefix = f'_{cname}_ — ' if cname else ''
            out.append(f'- {n.get("date","")} · {prefix}{_wikilink(n)}')
        out.append('')

    # Concepts
    if payload['concepts_active']:
        out.append('## 💡 מושגים פעילים השבוע')
        out.append('')
        for stem, n, ck in payload['concepts_active'][:8]:
            cname = COURSE_NAMES.get(ck, ck) if ck else ''
            tag = f' _({cname})_' if cname else ''
            out.append(f'- {_wikilink(stem)} · {n} קישורים{tag}')
        out.append('')

    out.append('---')
    out.append('_מסמך זה נבנה אוטומטית ע"י Smart Study Agent._')
    out.append('')

    return '\n'.join(out)


# ─── Public ────────────────────────────────────────────────────────────────

def write_weekly_digest(
    year: int | None = None,
    week: int | None = None,
) -> Path:
    """Write the digest and return its path."""
    if year is None or week is None:
        y, w = _iso_year_week(_now())
        year = year or y
        week = week or w

    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    payload = gather_for_week(year, week)
    body = render_digest(year, week, payload)

    out = WEEKS_DIR / f'{year}-W{week:02d}.md'
    out.write_text(body, encoding='utf-8')
    return out


# ─── CLI ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='Generate `📅 שבוע WW.md` digest.')
    p.add_argument('--year', type=int, default=None)
    p.add_argument('--week', type=int, default=None)
    p.add_argument('--all-past', action='store_true',
                   help='Generate digests for every ISO week in the past 12 weeks.')
    args = p.parse_args()

    if args.all_past:
        today = _now()
        for delta_weeks in range(12):
            d = today - timedelta(weeks=delta_weeks)
            y, w = _iso_year_week(d)
            path = write_weekly_digest(y, w)
            print(f'wrote {path.name}')
    else:
        path = write_weekly_digest(args.year, args.week)
        print(f'wrote {path}')
