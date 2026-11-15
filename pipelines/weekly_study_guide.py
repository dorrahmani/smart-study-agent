"""
pipelines/weekly_study_guide.py — Per-course weekly homework study guide.

Trigger: every Monday-or-later run, scan for assignments that became visible in
the last 7 days. Group by course. For each course with ≥1 new assignment,
generate a NotebookLM study_guide focused on the topics those assignments
cover (with the last 2 lecture summaries as additional context).

Output: 🎓 NotebookLM/{course}/{monday-of-this-week} - study_guide -
        מדריך שיעורי בית — שבוע WW.md
        with `homework_targets:` linking back to each assignment note.

Idempotent — won't re-generate if a guide for this ISO week already exists.

Cost: $0 (NotebookLM/Gemini quota).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from config.paths import (
    COURSE_NAMES, COURSE_ORDER,
    VAULT_NOTEBOOKLM_DIR, VAULT_ASSIGNMENTS_DIR,
)


# ─── Helpers ───────────────────────────────────────────────────────────────

def _iso_week(d: datetime) -> tuple[int, int]:
    """Return (year, week) per ISO 8601."""
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _monday_of(d: datetime) -> str:
    """YYYY-MM-DD of the Monday in the same ISO week as `d`."""
    return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')


def _safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s).strip().strip('.')[:100]


def _assignment_wikilink(asg: dict) -> str:
    """Build [[Note Stem|displayed title]] for a vault assignment."""
    stem = asg.get('stem') or asg.get('filename', '').replace('.md', '')
    title = asg.get('title') or stem
    if stem == title:
        return f'[[{stem}]]'
    return f'[[{stem}|{title}]]'


def _within_last_n_days(date_iso: str | None, n: int = 7) -> bool:
    if not date_iso:
        return False
    try:
        d = datetime.strptime(str(date_iso)[:10], '%Y-%m-%d')
    except ValueError:
        return False
    return d >= datetime.now() - timedelta(days=n)


# ─── Discovery ─────────────────────────────────────────────────────────────

def find_new_assignments_per_course(window_days: int = 7) -> dict[str, list[dict]]:
    """{course_key: [assignment_dicts...]}, only items created within window."""
    from dashboard import vault_reader

    out: dict[str, list[dict]] = {}
    for asg in vault_reader.list_assignments(course=None):
        created = (asg.get('fm') or {}).get('created') or asg.get('date')
        if not _within_last_n_days(created, n=window_days):
            continue
        ck = asg.get('course_key')
        if not ck:
            continue
        out.setdefault(ck, []).append(asg)
    return out


def _last_lecture_summaries(course_key: str, n: int = 2) -> list[dict]:
    """Last n lecture summaries for the course, sorted newest-first."""
    from dashboard import vault_reader
    sums = vault_reader.list_summaries(course=course_key, type='lecture')
    sums = sorted(
        sums,
        key=lambda s: s.get('date') or '',
        reverse=True,
    )
    return sums[:n]


# ─── Existing-guide check (idempotency) ────────────────────────────────────

def _guide_already_exists(course_key: str, year: int, week: int) -> Path | None:
    """Return existing path if a guide for this ISO week exists, else None."""
    course_dir = VAULT_NOTEBOOKLM_DIR / course_key
    if not course_dir.exists():
        return None
    week_token = f'שבוע {year}-W{week:02d}'
    for md in course_dir.glob('* - study_guide - *.md'):
        if week_token in md.name:
            return md
    return None


# ─── Instructions builder ──────────────────────────────────────────────────

def build_instructions(
    assignments:    list[dict],
    recent_lectures: list[dict],
) -> str:
    """Hebrew prompt focusing the NotebookLM agent on this week's homework."""
    titles = [a.get('title') or a.get('stem', '?') for a in assignments]
    lecture_titles = [l.get('title') or l.get('stem', '?') for l in recent_lectures]

    lines = [
        'בנה מדריך לימוד ממוקד ובהיר בעברית, שיעזור לי להגיש את המטלות הבאות השבוע:',
        '',
    ]
    for t in titles:
        lines.append(f'  • {t}')
    lines += [
        '',
        'המדריך צריך לכלול:',
        '  1) סיכום קצר של הנושאים הרלוונטיים מההרצאות האחרונות.',
        '  2) הסבר על המושגים המרכזיים, עם דוגמאות.',
        '  3) טיפים ושגיאות נפוצות שצריך להימנע מהן בכל מטלה.',
        '  4) תרגילים מתודיים שעוזרים לפתח אינטואיציה לפני הגשת המטלה.',
        '  5) קישור ברור בין כל מטלה לחומר ההרצאה ממנו היא נובעת.',
    ]
    if lecture_titles:
        lines += ['', 'הקשר ההרצאות האחרונות:']
        for t in lecture_titles:
            lines.append(f'  • {t}')
    return '\n'.join(lines)


# ─── Export ────────────────────────────────────────────────────────────────

def _read_guide_md(path: str) -> str:
    """The agent saves a .md file with the body — return it (or fallback note)."""
    try:
        text = Path(path).read_text(encoding='utf-8')
        return text.strip()
    except Exception:
        return f'_(הקובץ נשמר ב-{path} — פתח ידנית כדי לראות את התוכן)_'


def export_weekly_guide(
    course_key:        str,
    course_name:       str,
    iso_year:          int,
    iso_week:          int,
    monday_iso:        str,
    md_path_from_agent: str,
    assignments:       list[dict],
) -> Path:
    """Write the study guide note to 🎓 NotebookLM/{course}/."""
    from exporters.notebooklm_obsidian import export_artifact

    title  = f'מדריך שיעורי בית — שבוע {iso_year}-W{iso_week:02d}'
    body   = _read_guide_md(md_path_from_agent)
    links  = [_assignment_wikilink(a) for a in assignments]

    return export_artifact({
        'course_key':     course_key,
        'course_name':    course_name,
        'artifact_type':  'study_guide',
        'title':          title,
        'date':           monday_iso,
        'generated_from': links,
        'file_path':      md_path_from_agent,
        'markdown':       body,
    })


# ─── Public entry point ────────────────────────────────────────────────────

def generate_weekly_study_guides(
    window_days: int = 7,
    dry_run:     bool = False,
) -> dict:
    """
    Returns {
      'generated': [{course_key, path, n_assignments}],
      'skipped':   [{course_key, reason}],
      'errors':    [{course_key, error}],
      'cost_usd':  0.0,
    }
    """
    from generators.notebooklm_agent import NotebookLMAgent

    out: dict = {
        'generated': [], 'skipped': [], 'errors': [], 'cost_usd': 0.0,
    }

    new_per_course = find_new_assignments_per_course(window_days=window_days)
    if not new_per_course:
        return out  # no assignments this window

    if not NotebookLMAgent.is_authenticated():
        for ck in new_per_course:
            out['skipped'].append({
                'course_key': ck,
                'reason': 'NotebookLM not authenticated.',
            })
        return out

    agent = NotebookLMAgent()
    today = datetime.now()
    iso_y, iso_w = _iso_week(today)
    monday_iso = _monday_of(today)

    for course_key, asgs in new_per_course.items():
        course_name = COURSE_NAMES.get(course_key, course_key)

        # Idempotency — skip if this week's guide already exists.
        existing = _guide_already_exists(course_key, iso_y, iso_w)
        if existing:
            out['skipped'].append({
                'course_key': course_key,
                'reason':     f'guide already exists at {existing.name}',
            })
            continue

        recent = _last_lecture_summaries(course_key, n=2)
        instructions = build_instructions(asgs, recent)

        if dry_run:
            out['generated'].append({
                'course_key':    course_key,
                'path':          '(dry-run)',
                'n_assignments': len(asgs),
                'preview':       instructions[:200],
            })
            continue

        try:
            md_path = agent.generate_study_guide(
                course_key, scope='all',
                language='he',
                extra_instructions=instructions,
            )
            out_path = export_weekly_guide(
                course_key=course_key,
                course_name=course_name,
                iso_year=iso_y, iso_week=iso_w,
                monday_iso=monday_iso,
                md_path_from_agent=md_path,
                assignments=asgs,
            )
            out['generated'].append({
                'course_key':    course_key,
                'path':          str(out_path),
                'n_assignments': len(asgs),
            })
        except Exception as e:
            out['errors'].append({
                'course_key': course_key,
                'error':      str(e),
            })

    return out


# ─── CLI ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    import json
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    p = argparse.ArgumentParser(
        description='Generate weekly homework-focused NotebookLM study guides.'
    )
    p.add_argument('--window-days', type=int, default=7,
                   help='How many days back to scan for new assignments (default 7).')
    p.add_argument('--dry-run', action='store_true',
                   help='Show what would be generated; do not call NotebookLM.')
    args = p.parse_args()

    rep = generate_weekly_study_guides(
        window_days=args.window_days, dry_run=args.dry_run,
    )
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
