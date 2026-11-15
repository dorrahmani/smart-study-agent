"""
scripts/moodle_light_check.py — a new quiz / assignment is noticed within hours, not at the 19:00 UTC scrape (15/11).

Software 1's quick-check quizzes are open for two weeks each and one of their questions is certain to be in the exam:
Lecture 5 Quiz must reach the owner the same day it opens. 3× a day on weekdays (crontab: deploy/crontab.txt):

    1  the existing logged-in session (collectors/moodle_collector's TAU SSO login), once
    2  per course page: ONE read of the course's activity list (core_courseformat_get_state — the read-only call the
       course page makes itself; a plain GET of course/view.php misses lazily-rendered sections: probability's and
       Software 1's quizzes/assignments aren't in its HTML at all, checked 15/11)
    3  only for an activity not seen before: one GET of its view page (due / opens / state), at most MAX_NEW a run
    4  new entries merged into data/assignments/_cache.json (the nightly scrape's shape); known entries only get
       Moodle's completion mark; then agent/obligations.refresh() → the store; agent/proactive's next tick says it
    polite   2 s between requests; any error/timeout stops the run and backs off (1 h, 2 h, 4 h … ≤ 24 h)
    quiet    never alerts (a timeout is the university's, not ours): it logs and the nightly scrape still runs
Never starts, submits or marks anything (no "Mark as done"). No LLM.

    venv/bin/python3 scripts/moodle_light_check.py            # the cron run
    venv/bin/python3 scripts/moodle_light_check.py --dry-run  # read, print what's new, write nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATE = ROOT / 'data' / 'moodle_light_check.json'
CACHE = ROOT / 'data' / 'assignments' / '_cache.json'
PAUSE = 2.0
MAX_NEW = 6
BACKOFF_MAX_H = 24
TIMEOUT = 20


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def backing_off(state: dict, now: datetime) -> bool:
    try:
        return bool(state.get('next_ok')) and now < datetime.fromisoformat(state['next_ok'])
    except ValueError:
        return False


def fail(state: dict, now: datetime, why: str) -> dict:
    n = int(state.get('fails') or 0) + 1
    return {**state, 'fails': n, 'last_error': why[:200], 'last_error_at': now.isoformat(timespec='minutes'),
            'next_ok': (now + timedelta(hours=min(2 ** (n - 1), BACKOFF_MAX_H))).isoformat(timespec='minutes')}


def ok(state: dict, now: datetime, new: int) -> dict:
    return {**state, 'fails': 0, 'next_ok': '', 'last_ok': now.isoformat(timespec='minutes'), 'last_new': new}


def merge(cache: dict, found: list[tuple[str, str, dict]], details, now: datetime, max_new: int = MAX_NEW) -> tuple[dict, list[dict]]:
    """found = [(course key, course name, cm)] (cm from moodle_api.course_state). → (cache, new entries). New = an
    assign / quiz whose course:id the cache doesn't have; details(url) → collectors' fetch_assignment_details (one GET).
    A known entry only takes Moodle's completion mark. Nothing is removed (the nightly scrape owns that)."""
    from collectors.moodle_assignment_scraper import make_entry
    from utils.deadlines import key_of
    items = [a for a in cache.get('assignments') or [] if isinstance(a, dict)]
    have = {key_of(a): a for a in items}
    new = []
    for ck, name, cm in found:
        if cm.get('module') not in ('assign', 'quiz') or not cm.get('url'):
            continue
        k = f"{ck}:{cm['id']}"
        if k in have:
            if 'completion' in cm:
                have[k]['completion'] = cm['completion']
            continue
        if len(new) >= max_new:
            continue                                   # the rest next run (or tonight)
        link = {'url': cm['url'], 'assign_id': cm['id'], 'title': cm['name'], 'kind': cm['module'],
                'section': cm.get('section', ''), **({'completion': cm['completion']} if 'completion' in cm else {})}
        e = make_entry(ck, name, link, details(cm['url']) or {}, now)
        e['source'] = 'moodle_light'
        have[k] = e
        items.append(e)
        new.append(e)
    return {**cache, 'assignments': items}, new


def run(now: datetime | None = None, session=None, dry: bool = False, sleep=time.sleep, state_path: Path | None = None,
        cache_path: Path | None = None, courses: list[str] | None = None, refresh=None) -> dict:
    now = now or datetime.now()
    sp, cp = state_path or STATE, cache_path or CACHE
    state = _load(sp, {})
    if backing_off(state, now):
        print(f"  ⏸ backing off until {state['next_ok']} ({state.get('last_error', '')})")
        return {'skipped': 'backoff', 'new': []}
    from collectors.announcement_scraper import moodle_ids
    from collectors.moodle_api import course_state, sesskey
    from config.paths import ACTIVE_COURSES, COURSE_NAMES
    try:
        if session is None:
            from collectors.moodle_assignment_scraper import login_and_get_session
            session = login_and_get_session()
        if not session:
            raise RuntimeError('Moodle login failed')
        key = sesskey(session)
        if not key:
            raise RuntimeError('no sesskey (login did not stick)')
    except Exception as e:
        print(f'  ⚠️ Moodle not reachable now: {e!r}'[:200])
        if not dry:
            _save(sp, fail(state, now, repr(e)))
        return {'skipped': 'login', 'new': []}
    found, err = [], ''
    for ck in courses or ACTIVE_COURSES:
        for cid in moodle_ids(ck, None):
            sleep(PAUSE)
            try:
                found += [(ck, COURSE_NAMES.get(ck, ck), cm) for cm in course_state(session, cid, key, strict=True,
                                                                                     timeout=TIMEOUT)]
            except Exception as e:
                err = f'{ck} {cid}: {e!r}'
                break
        if err:
            break

    memo: dict[str, dict] = {}

    def details(url):
        if url not in memo:
            sleep(PAUSE)
            from collectors.moodle_assignment_scraper import fetch_assignment_details
            try:
                memo[url] = fetch_assignment_details(session, url) or {}
            except Exception as e:
                print(f'  ⚠️ {url}: {e!r}'[:160])
                memo[url] = {}
        return memo[url]
    cache = _load(cp, {'assignments': []})
    if dry:
        known = {f"{a.get('course_key')}:{a.get('assign_id')}" for a in cache.get('assignments') or [] if isinstance(a, dict)}
        new = [cm['name'] for ck, _n, cm in found if cm.get('module') in ('assign', 'quiz') and f"{ck}:{cm['id']}" not in known]
        print(f'  read {len(found)} activities; new: {new or "none"}' + (f'; stopped: {err}' if err else ''))
        return {'new': new, 'error': err, 'found': len(found)}
    merged, new = merge(cache, found, details, now)          # the detail GETs happen here, once
    if found:
        fresh = _load(cp, {'assignments': []})               # the nightly scrape may have written meanwhile
        if fresh.get('_ts') != cache.get('_ts'):
            merged, new = merge(fresh, found, details, now)  # same pages, from memo: no second request
        merged['_light_ts'] = now.timestamp()
        _save(cp, merged)
    _save(sp, fail(state, now, err) if err else ok(state, now, len(new)))
    for e in new:
        print(f"  🆕 {e['course_key']}: {e['title']} ({e['kind']}) due {e.get('due_date') or '—'}")
    print(f'  read {len(found)} activities, {len(new)} new' + (f' — stopped early, backing off: {err}'[:200] if err else ''))
    try:
        (refresh or __import__('agent.obligations', fromlist=['refresh']).refresh)(now)
    except Exception as e:
        print(f'  ⚠️ obligations refresh: {e!r}'[:200])
    return {'new': new, 'error': err, 'found': len(found)}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description='light Moodle check: new quizzes/assignments → the obligations store')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    print(f'[{datetime.now():%Y-%m-%d %H:%M}] moodle light check')
    run(dry=a.dry_run)
    return 0                                               # never a failing exit: no alert from a wrapper either


if __name__ == '__main__':
    sys.exit(main())
