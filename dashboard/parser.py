"""
parser.py — extracts data from summary .md files for the dashboard
"""

import re
import json
from pathlib import Path

# Single source of truth — do NOT redefine paths or the course registry here.
# This file used to keep its own copies: cwd-relative paths, the legacy
# sent_log (frozen 2026-04-24) and a course list missing new semesters.
from config.paths import (
    SUMMARIES_DIR, SENT_LOG_PATH, RATINGS_PATH, LAST_RUN_PATH,
    COURSE_NAMES, COURSE_ORDER,
)


# ─── helpers ────────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            pass
    return default

def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

def load_sent_log() -> set:
    """Filenames already sent.

    The canonical log is a dict keyed by FULL path (POSIX on the VM; some
    one-off scripts wrote Windows back-slash paths). Callers compare
    against bare filenames (md_file.name), so reduce each key to its
    basename. A legacy list-of-filenames still works.
    """
    data = load_json(SENT_LOG_PATH, {})
    keys = data.keys() if isinstance(data, dict) else data
    return {re.split(r'[\\/]', str(k))[-1] for k in keys}

def load_ratings() -> dict:
    return load_json(RATINGS_PATH, {})

def save_ratings(ratings: dict):
    save_json(RATINGS_PATH, ratings)

def load_last_run() -> str:
    """Returns a string with the last run's time, readable"""
    if not LAST_RUN_PATH.exists():
        return "לא רץ עדיין"
    try:
        from datetime import datetime, timezone
        ts = float(LAST_RUN_PATH.read_text().strip())
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        return dt.strftime('%d/%m/%Y %H:%M')
    except Exception:
        return "לא ידוע"


# ─── source parsing ──────────────────────────────────────────────────────────

def _parse_sources(content: str) -> dict:
    sources = {'transcript': None, 'pdf': None, 'board_frames': None}

    m = re.search(r'## חומרים שנסרקו.*?\n(.*?)(?=\n##|\Z)', content, re.DOTALL)
    if not m:
        return sources
    section = m.group(1)

    # Transcript
    if 'הרצאה מוקלטת' in section or 'כתוביות' in section:
        if 'מקוריות' in section:
            sources['transcript'] = 'Panopto'
        elif 'Whisper' in section or 'OpenAI' in section:
            sources['transcript'] = 'Whisper'
        else:
            sources['transcript'] = 'תמלול'

    # PDF
    if 'חומר כתוב' in section:
        if 'Moodle' in section or 'מודל' in section:
            sources['pdf'] = 'Moodle'
        elif 'wikidot' in section.lower():
            sources['pdf'] = 'Wikidot'
        elif 'רשימות' in section or 'ספר' in section:
            sources['pdf'] = 'ספר'
        else:
            sources['pdf'] = 'PDF'

    # Board — two formats: "X צילומי מסך" (old) or "board_frames": X (from add_board_reading)
    bm = re.search(r'(\d+)\s*צילומי מסך', section)
    if bm:
        sources['board_frames'] = int(bm.group(1))
    else:
        bm2 = re.search(r'"board_frames":\s*(\d+)', content)
        if bm2 and int(bm2.group(1)) > 0:
            sources['board_frames'] = int(bm2.group(1))
        elif 'לוח הכיתה' in content:
            # board section exists — mark as 1 even if count unknown
            sources['board_frames'] = 1

    return sources


def _parse_costs(content: str) -> dict:
    costs = {'claude': 0.0, 'openai': 0.0, 'professor': 0.0, 'total': 0.0}
    m = re.search(r'סה"כ:\s*\$([0-9.]+)', content)
    if m:
        costs['total'] = float(m.group(1))
        costs['claude'] = costs['total']
    # professor cost (if embedded in the tips section header)
    prof_m = re.search(r'\*\*פרופסור\*\*.*?\$([0-9.]+)', content)
    if prof_m:
        costs['professor'] = float(prof_m.group(1))
    return costs


# ─── main loader ─────────────────────────────────────────────────────────────

def get_all_summaries() -> list:
    """Read summaries.

    Primary source = Obsidian Vault (canonical DB) via dashboard.vault_reader.
    Fallback: legacy data/summaries/*.md regex-based parsing.

    Returned dicts keep the same shape as before so the rest of the dashboard
    is unchanged.
    """
    try:
        from dashboard import vault_reader as _vr
        from config.paths import VAULT_DIR
        if VAULT_DIR.exists():
            return _from_vault(_vr)
    except Exception:
        pass
    return _get_all_summaries_legacy()


def _from_vault(vr) -> list:
    sent_log = load_sent_log()
    ratings  = load_ratings()
    out = []
    for n in vr.list_summaries():
        # Reconstruct legacy shape
        type_folder = "lectures" if vr._normalize_type(n) == "lecture" else "tutorials"
        course = n.get("course_key") or ""
        body   = n.get("body", "") or n.get("raw", "")
        sources = _parse_sources(body)
        costs   = _parse_costs(body)
        # Cost from frontmatter overrides body parse if present
        if n.get("cost_usd"):
            try:
                costs["total"] = float(n["cost_usd"])
                costs["claude"] = costs["total"]
            except (TypeError, ValueError):
                pass
        rating_key = f"{course}/{type_folder}/{n['filename']}"
        rating_data = ratings.get(rating_key, {})
        out.append({
            'path':            n['path'],
            'filename':        n['filename'],
            'course':          course,
            'course_name':     COURSE_NAMES.get(course, n.get('course') or course),
            'type_folder':     type_folder,
            'type_label':      'הרצאה' if type_folder == 'lectures' else 'תרגול',
            'date':            n.get('date', ''),
            'title':           n.get('title', n['filename']),
            'sources':         sources,
            'costs':           costs,
            'sent':            n['filename'] in sent_log,
            'rating':          rating_data.get('rating'),
            'notes':           rating_data.get('notes', ''),
            'feedback_pending':rating_data.get('feedback_pending', False),
            'rating_key':      rating_key,
            # New fields surfaced from vault frontmatter
            'topics':          n.get('topics') or [],
            'concepts':        n.get('concepts') or [],
            'tags':            n.get('tags') or [],
            'weakness_score':  n.get('weakness_score'),
        })
    course_idx = {k: i for i, k in enumerate(COURSE_ORDER)}
    out.sort(key=lambda s: (
        course_idx.get(s['course'], 99),
        s['date'],
        0 if s['type_folder'] == 'lectures' else 1
    ))
    return out


def _get_all_summaries_legacy() -> list:
    summaries = []
    sent_log = load_sent_log()
    ratings  = load_ratings()

    if not SUMMARIES_DIR.exists():
        return summaries

    for md_file in sorted(SUMMARIES_DIR.rglob("*.md")):
        if md_file.name in ('sent_log.json',):
            continue
        # must be inside course/lectures|tutorials/
        parts = md_file.relative_to(SUMMARIES_DIR).parts
        if len(parts) != 3:
            continue

        course, type_folder, filename = parts
        try:
            content = md_file.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue

        # Date + title from the name
        stem = md_file.stem
        dm = re.match(r'^(\d{4}-\d{2}-\d{2})\s*-\s*(.+)$', stem)
        date  = dm.group(1) if dm else ''
        title = dm.group(2) if dm else stem

        # relative key for ratings
        rating_key = f"{course}/{type_folder}/{md_file.name}"
        rating_data = ratings.get(rating_key, {})

        summaries.append({
            'path':            str(md_file),
            'filename':        md_file.name,
            'course':          course,
            'course_name':     COURSE_NAMES.get(course, course),
            'type_folder':     type_folder,
            'type_label':      'הרצאה' if type_folder == 'lectures' else 'תרגול',
            'date':            date,
            'title':           title,
            'sources':         _parse_sources(content),
            'costs':           _parse_costs(content),
            'sent':            md_file.name in sent_log,
            'rating':          rating_data.get('rating'),        # 'up' / 'down' / None
            'notes':           rating_data.get('notes', ''),
            'feedback_pending':rating_data.get('feedback_pending', False),
            'rating_key':      rating_key,
        })

    # Sort: by course → date → type
    course_order = {k: i for i, k in enumerate(COURSE_ORDER)}
    summaries.sort(key=lambda s: (
        course_order.get(s['course'], 99),
        s['date'],
        0 if s['type_folder'] == 'lectures' else 1
    ))
    return summaries


def get_totals(summaries: list) -> dict:
    return {
        'count':        len(summaries),
        'sent':         sum(1 for s in summaries if s['sent']),
        'lectures':     sum(1 for s in summaries if s['type_folder'] == 'lectures'),
        'tutorials':    sum(1 for s in summaries if s['type_folder'] == 'tutorials'),
        'with_board':   sum(1 for s in summaries if s['sources']['board_frames']),
        'with_pdf':     sum(1 for s in summaries if s['sources']['pdf']),
        'claude_cost':  sum(s['costs']['claude'] for s in summaries),
        'openai_cost':  sum(s['costs']['openai'] for s in summaries),
        'total_cost':   sum(s['costs']['total']  for s in summaries),
    }
