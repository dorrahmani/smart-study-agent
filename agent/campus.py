"""
agent/campus.py — walking time between the TAU buildings on the owner's timetable, and the hops he can't make on time (19/11,
improvement 14).

Example: three classes back to back in buildings at opposite ends of the campus. agent/proactive already says a
lesson ↔ off-campus hop; this is the on-campus one. Study domain only (SSA_STUDY=1).

WALK — a static table, rough minutes, door to door at a normal pace. SOURCE: estimated by hand from the official TAU
campus map (https://www.tau.ac.il/campus-map, Ramat Aviv main campus, ~800 m end to end, ~80 m/min) — not measured.
Buildings are grouped into areas; same building 0, same area 3, otherwise the area table below. The names are the
ones the university site and config/courses.yaml use for the rooms (tau_calendar.json "BuildingNameHeb"). The table is
TAU's (the worked example); another campus: config/campus.json = {"buildings": {name: [area, regex]}, "areas":
[[area_a, area_b, minutes], …]}, read instead of the table below. Examples of TAU names:
"ברגלס - כלכלה", "שנקר - פיזיקה", "קפלון - פיזיקה", "לב אודיטוריום", "בניין צ'ק פוינט", "אודיטוריום דאך",
"אורנשטיין", "ביה״ס לכלכלה". A building not in the table → no claim (never a guess).

A hop is tight when the walk is longer than the gap + SLACK (a class rarely runs to the last minute; 5 minutes).
No model; nothing here sends anything.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

SLACK = 5
SAME_AREA = 3
# building → (area, the regex that finds it in a location line)
BUILDINGS = {
    'ברגלס':      ('social', r'ברגלס|ביה["״׳\']?ס\s+לכלכלה|בית\s+הספר\s+לכלכלה|כלכלה\s+\d'),
    'נפתלי':      ('social', r'נפתלי|מדעי\s+החברה'),
    'שנקר':       ('exact', r'שנקר'),
    'קפלון':      ('exact', r'קפלון'),
    'אורנשטיין':  ('exact', r'אורנשטיין|אורנשטין'),
    'שרייבר':     ('exact', r'שרייבר|שריבר'),
    "צ'ק פוינט":  ('exact', r'צ[\'׳]?\s*ק\s*פוינט|check\s*point'),
    'לב':         ('exact', r'(?<![א-ת])לב\s+אודיטוריום|אודיטוריום\s+לב|אולם\s+לב'),
    'דאך':        ('exact', r'דאך'),
    'הנדסה':      ('engineering', r'הנדסה|וולפסון|כיתות\s+הנדסה'),
    'גילמן':      ('humanities', r'גילמן'),
    'רוזנברג':    ('humanities', r'רוזנברג'),
    'סוראסקי':    ('humanities', r'סוראסקי|הספרייה\s+המרכזית'),
    'טרובוביץ':   ('law', r'טרובוביץ|משפטים'),
}
AREA_MIN = {
    frozenset({'exact', 'social'}): 8,
    frozenset({'exact', 'humanities'}): 6,
    frozenset({'exact', 'engineering'}): 5,
    frozenset({'exact', 'law'}): 9,
    frozenset({'social', 'humanities'}): 4,
    frozenset({'social', 'engineering'}): 9,
    frozenset({'social', 'law'}): 5,
    frozenset({'humanities', 'engineering'}): 8,
    frozenset({'humanities', 'law'}): 6,
    frozenset({'engineering', 'law'}): 11,
}

try:                                                     # your own campus instead of the TAU example
    import json as _json
    from pathlib import Path as _Path
    _cfg = _json.loads((_Path(__file__).resolve().parent.parent / 'config' / 'campus.json').read_text(encoding='utf-8'))
    BUILDINGS = {str(k): (str(v[0]), str(v[1])) for k, v in (_cfg.get('buildings') or {}).items()} or BUILDINGS
    AREA_MIN = {frozenset({str(a), str(b)}): int(m) for a, b, m in _cfg.get('areas') or []} or AREA_MIN
except (OSError, ValueError, TypeError, AttributeError, IndexError):
    pass
_RX = [(name, area, re.compile(rx, re.I)) for name, (area, rx) in BUILDINGS.items()]


def building_of(location: str) -> tuple[str, str] | None:
    """'ברגלס - כלכלה, חדר 012' → ('ברגלס', 'social'); None when the line names no building in the table."""
    loc = location or ''
    for name, area, rx in _RX:
        if rx.search(loc):
            return name, area
    return None


def walk(a: str, b: str) -> int | None:
    """Rough walking minutes between two location lines, or None (either building unknown)."""
    ba, bb = building_of(a), building_of(b)
    if not ba or not bb:
        return None
    if ba[0] == bb[0]:
        return 0
    if ba[1] == bb[1]:
        return SAME_AREA
    return AREA_MIN.get(frozenset({ba[1], bb[1]}))


def tight(end1: datetime, loc1: str, start2: datetime, loc2: str) -> dict | None:
    """The hop from one session to the next, when the walk doesn't fit the gap: {walk, gap, frm, to}."""
    w = walk(loc1, loc2)
    gap = int((start2 - end1).total_seconds() // 60)
    if w is None or gap < 0 or w <= gap + SLACK:
        return None
    return {'walk': w, 'gap': gap, 'frm': building_of(loc1)[0], 'to': building_of(loc2)[0]}


def leave_at(start2: datetime, hop: dict) -> datetime:
    """When to leave for the next session so he walks in on time."""
    return start2 - timedelta(minutes=hop['walk'])
