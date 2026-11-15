"""
collectors/panopto_discovery.py — the new semester's Panopto folders, found automatically (10/10/26).

config/courses.yaml had folder_id: TO_BE_FILLED for the 4 new courses, so the collector fell back to searching by
course name — which can catch other groups' lectures. TAU Panopto folders are named "(<course number><group>-
<year>-<semester>) …", e.g. "(0368200201-2026-1) הסתברות וסטטיסטיקה (לדו-חוגי)". The owner's own groups come from the
my.tau schedule cache (lecture 'שיעור' / tutorial 'תרגיל'). While the collector runs anyway (already logged in), one
Panopto search per still-missing course finds the folder whose name has exactly the owner's course+group+term.
No LLM. Saved in data/panopto_folders.json; the yaml (if ever filled by hand) always wins.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from config.paths import DATA_DIR

STORE = DATA_DIR / 'panopto_folders.json'


def term(today: date | None = None) -> str:
    """TAU numbering: semester A (Oct–Feb) = '<year it starts>-1', semester B = '<year>-2'."""
    t = today or date.today()
    if t.month >= 9:
        return f'{t.year}-1'
    if t.month <= 2:
        return f'{t.year - 1}-1'
    return f'{t.year}-2'


def my_groups() -> dict[str, dict]:
    """{course key: {'number': '03682002', 'lecture': '01', 'tutorial': '02'}} from the my.tau schedule cache."""
    from collectors.announcement_scraper import course_numbers
    nums = course_numbers()                                       # {'3682002': 'probability', …}
    try:
        events = json.loads((DATA_DIR / 'tau_calendar.json').read_text(encoding='utf-8'))['events']
    except (OSError, ValueError, KeyError):
        return {}
    out: dict[str, dict] = {}
    for e in events:
        num8, grp = (e.get('course_number') or ''), (e.get('group') or '')
        key = nums.get(num8.lstrip('0'))
        if not key or not grp or e.get('is_exam'):
            continue
        d = out.setdefault(key, {'number': num8})
        if 'שיעור' in (e.get('kind') or ''):
            d.setdefault('lecture', grp)
        elif 'תרגיל' in (e.get('kind') or ''):
            d.setdefault('tutorial', grp)
    return out


def pick(folders: dict[str, str], number8: str, group: str, t: str) -> str:
    """The folder whose name carries exactly '<number><group>-<term>' ('' if none / ambiguous)."""
    tag = f'{number8}{group}-{t}'
    hits = [fid for fid, name in folders.items() if tag in (name or '')]
    return hits[0] if len(hits) == 1 else ''


_CODE = re.compile(r'(?<!\d)(\d{8})(\d{2})-(\d{4}-[12])(?!\d)')


def pick_combined(folders: dict[str, str], number8: str, t: str) -> str:
    """11/11: Software 1, micro B and macro have ONE folder for all groups — '(0368215799-2026-1) …', '(1011210998-…'.
    No folder of the owner's group → the single folder of this course number + term whose group is a combined code (9x),
    or the only folder of that course + term at all. Another course's number never counts; two candidates → ''.
    Recordings in it are then filtered by the owner's weekly slots (match_by_schedule)."""
    cands = {fid: m.group(2) for fid, name in folders.items()
             for m in [_CODE.search(name or '')] if m and m.group(1) == number8 and m.group(3) == t}
    combined = [fid for fid, g in cands.items() if g.startswith('9')]
    if len(combined) == 1:
        return combined[0]
    return next(iter(cands)) if len(cands) == 1 else ''


def load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or STORE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def discover(search, missing: dict[str, str], groups: dict | None = None, path: Path | None = None,
             today: date | None = None) -> dict[str, dict]:
    """search(query) → Panopto sessions (with FolderID/FolderName); missing = {course key: search query}.
    → the newly found {course key: {'folder_id', 'folder_name', 'tutorial_folder_id'?, …}}."""
    groups = groups if groups is not None else my_groups()
    store = load(path)
    t = term(today)
    found = {}
    for key, query in missing.items():
        g = groups.get(key)
        if not g or not g.get('lecture') or store.get(key, {}).get('folder_id'):
            continue
        sessions = []
        for q in ([query] if isinstance(query, str) else list(query)):     # the name, then other names (a rename)
            try:
                sessions += search(q) or []
            except Exception as e:
                print(f'  ⚠️ panopto discovery {key}: {e}')
        folders = {s.get('FolderID'): re.sub(r'<[^>]+>', '', s.get('FolderName', '')) for s in sessions if s.get('FolderID')}
        lec, grp = pick(folders, g['number'], g['lecture'], t), g['lecture']
        if not lec:
            lec, grp = pick_combined(folders, g['number'], t), 'combined'
        if not lec:
            continue
        rec = {'folder_id': lec, 'folder_name': folders[lec].strip(), 'group': grp}
        if g.get('tutorial') and (tut := pick(folders, g['number'], g['tutorial'], t)) and tut != lec:
            rec.update(tutorial_folder_id=tut, tutorial_folder_name=folders[tut], tutorial_group=g['tutorial'])
        store[key] = rec
        found[key] = rec
    if found:
        (path or STORE).parent.mkdir(parents=True, exist_ok=True)
        (path or STORE).write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding='utf-8')
    return found


def folders_for(key: str, path: Path | None = None) -> tuple[str | None, str | None]:
    rec = load(path).get(key) or {}
    return rec.get('folder_id'), rec.get('tutorial_folder_id')


_WD = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def portal_sessions(key: str) -> list[dict]:
    """The owner's weekly lecture/tutorial slots for a course from the my.tau cache — the same shape as courses.yaml
    my_sessions ({type, day, start, end, group}). 13/10: the config's slots are a snapshot from the semester's
    planning (its room was already stale); the site follows changes. [] if the cache has nothing → use the config."""
    import yaml
    try:
        cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / 'config' / 'courses.yaml').read_text(encoding='utf-8'))
        code = str(((cfg.get('courses') or {}).get(key) or {}).get('course_code') or '')
        events = json.loads((DATA_DIR / 'tau_calendar.json').read_text(encoding='utf-8'))['events']
    except (OSError, ValueError, KeyError, AttributeError):
        return []
    if not code.isdigit():
        return []
    out, seen, dates = [], {}, {}
    for e in events:
        if e.get('course_number') != code or e.get('is_exam') or not e.get('time'):
            continue
        kind = 'lecture' if 'שיעור' in (e.get('kind') or '') else 'tutorial' if 'תרגיל' in (e.get('kind') or '') else ''
        if not kind:
            continue
        slot = (kind, _WD[date.fromisoformat(e['date']).weekday()], e['time'], e.get('end_time') or '', e.get('group') or '')
        dates.setdefault(slot, []).append(e['date'])
        if slot not in seen:
            d = dict(zip(('type', 'day', 'start', 'end', 'group'), slot))
            if e.get('lecturer'):           # 11/11: two tutorials recorded in the same slot → the one naming his tutor
                d['lecturer'] = e['lecturer']
            seen[slot] = d
            out.append(d)
    # 11/11: a one-off class of his (a makeup on another day) next to a weekly slot of the same type is not a second
    # weekly slot — it's that date only (The owner: never 2 lectures or 2 tutorials a week).
    for slot, d in seen.items():
        if len(dates[slot]) == 1 and any(k[0] == slot[0] and len(v) >= 2 for k, v in dates.items()):
            d['date'] = dates[slot][0]
            d['moved'] = True
    return out
