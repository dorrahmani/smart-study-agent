"""
collectors/moodle_api.py — a course's full content from Moodle's own AJAX API (the course page's HTML isn't enough).

19/10: Moodle 4 renders some sections lazily — probability's "הגשת מטלות" (with the owner's already-submitted exercise 1)
and Software 1's lecture folders / quizzes never appeared in the page HTML the scrapers read. core_courseformat_get_state
returns every activity (assign, quiz, resource, folder, url…) with its section, as the page itself loads it.
"""
from __future__ import annotations

import json
import re

MOODLE = 'https://moodle.tau.ac.il'


def sesskey(session) -> str:
    m = re.search(r'"sesskey":"([^"]+)"', session.get(f'{MOODLE}/my/', timeout=30).text)
    return m.group(1) if m else ''


def course_state(session, course_id: str, key: str | None = None, strict: bool = False, timeout: int = 30) -> list[dict]:
    """[{id, module, name, url, section, completion}] for every activity of the course. [] on any failure — or, with
    strict=True, the failure is raised (scripts/moodle_light_check backs off on it). A read-only web service call (the
    one the course page itself makes; Moodle's AJAX API takes it as a POST). completion: Moodle's completionstate
    (0 = not marked, 1 = done …), absent when the course doesn't track it."""
    key = key or sesskey(session)
    if not key:
        if strict:
            raise RuntimeError('no sesskey (logged out?)')
        return []
    body = [{'index': 0, 'methodname': 'core_courseformat_get_state', 'args': {'courseid': int(course_id)}}]
    try:
        resp = session.post(f'{MOODLE}/lib/ajax/service.php?sesskey={key}&info=core_courseformat_get_state',
                            json=body, timeout=timeout)
        if strict and getattr(resp, 'status_code', 200) >= 400:
            raise RuntimeError(f'HTTP {resp.status_code}')
        r = resp.json()[0]
        if r.get('error'):
            if strict:
                raise RuntimeError(str(r.get('exception') or r.get('error'))[:120])
            return []
        st = json.loads(r['data'])
    except Exception:
        if strict:
            raise
        return []
    secs = {s['id']: s.get('title', '') for s in st.get('section') or []}
    import html
    return [{'id': str(c.get('id')), 'module': c.get('module', ''), 'name': html.unescape(c.get('name', '')),
             'url': c.get('url') or '', 'section': html.unescape(secs.get(c.get('sectionid'), '') or ''),
             **({'completion': c['completionstate']} if c.get('istrackeduser') and 'completionstate' in c else {})}
            for c in st.get('cm') or []]
