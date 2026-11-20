#!/usr/bin/env python3
"""
scripts/consistency_check.py — the nightly consistency check (19/11, improvement 19). No LLM. See agent/consistency.py.

    venv/bin/python3 scripts/consistency_check.py --dry     # read only: what it finds and what it WOULD fix; no fix,
                                                            # no state, no message
    venv/bin/python3 scripts/consistency_check.py           # fix the safe ones (logged), write the report, tell the owner
                                                            # (only new non-trivial findings, at most once a day)
Writes data/consistency_report.json, data/consistency_fixes.jsonl (each fix with its undo), data/consistency_state.json.
The calendar is read only (list); nothing on it is changed. Cron (Israel time): 02:50 nightly, see deploy/crontab.txt;
the message waits out quiet hours in utils.notify's morning digest. Exit 0 unless the script itself crashes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / '.env', override=True)

from agent import consistency as cc  # noqa: E402


def _read(name: str, default):
    try:
        return json.loads((cc.DATA / name).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _sources() -> str:
    """All the code's text, to tell which data files something still names."""
    out = []
    for pat in ('**/*.py', '**/*.sh', '**/*.ps1', 'deploy/**/*', 'config/*.yaml'):
        for p in ROOT.glob(pat):
            if p.is_file() and not any(x in p.parts for x in ('venv', 'venv312', '.git', 'data', 'node_modules', '.claude')):
                try:
                    out.append(p.read_text(encoding='utf-8', errors='ignore'))
                except OSError:
                    pass
    return '\n'.join(out)


def gather(now: datetime) -> list[dict]:
    from agent import closure, life, tasks as tk
    findings: list[dict] = []
    owner_closed = closure.items(None)
    steps = [
        ('reminders', lambda: cc.reminders_for_closed(life._load()['reminders'], owner_closed)),
        ('reopened', lambda: cc.reopened(tk._load()['tasks'], _read('obligations.json', {}))),
        ('missed', lambda: cc.missed(_read('obligations.json', {}), now)),
        ('schedule', lambda: cc.schedule_issues(_read('schedule_overrides.json', []), _lesson_events(now),
                                                (_read('tau_calendar.json', {}) or {}).get('events') or [],
                                                _yaml_sessions(), now.date())),
        ('duplicates', lambda: cc.duplicates(_tagged(now))),
        ('orphans', lambda: cc.orphans(cc.DATA, now, _sources())),
    ]
    for name, fn in steps:
        try:
            got = fn()
            print(f'  {name}: {len(got)}')
            findings += got
        except Exception as e:                       # one broken source never hides the others
            print(f'  ⚠️ {name}: {e!r}'[:200])
    return findings


def _lesson_events(now: datetime) -> list[dict]:
    from integrations import gcal
    from agent import proactive
    if not gcal.configured():
        return []
    out = []
    for i in range(0, 15):
        out += [ev for ev in gcal.events_on((now + timedelta(days=i)).date())
                if proactive._is_lesson(ev) or proactive._KIND_WORDS & proactive._words(ev.get('summary') or '')]
    return out


def _tagged(now: datetime) -> dict:
    from integrations import gcal
    if not gcal.configured():
        return {}
    svc = gcal._service()
    lo, hi = (now - timedelta(days=7)).date(), (now + timedelta(days=60)).date()
    return {f'{t}={v}': gcal.list_tagged(t, v, lo, hi, service=svc)
            for t, v in (('tau_src', 'lessons'), ('ssa_src', 'task'), ('ssa_src', 'office_hours'), ('ssa_src', 'escalation'))}


def _yaml_sessions() -> dict:
    import yaml
    try:
        from config.paths import ACTIVE_COURSES
    except Exception:
        ACTIVE_COURSES = []
    cfg = yaml.safe_load((ROOT / 'config' / 'courses.yaml').read_text(encoding='utf-8')) or {}
    courses = cfg.get('courses', cfg) or {}
    return {ck: {'code': (courses.get(ck) or {}).get('course_code'), 'slots': (courses.get(ck) or {}).get('my_sessions') or []}
            for ck in ACTIVE_COURSES if (courses.get(ck) or {}).get('my_sessions')}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='read only: no fixes, no state, no message')
    ap.add_argument('--no-send', action='store_true', help='fix and report to the file, but no message')
    a = ap.parse_args(argv)
    now = datetime.now()
    print(f'{now:%F %T} consistency check{" (dry run)" if a.dry else ""}')
    findings = gather(now)
    fixable = [f for f in findings if f.get('fix')]
    if a.dry:
        print(f'\nfindings: {len(findings)}  ·  would fix: {len(fixable)}')
        for f in findings:
            print(f"  {'FIX ' + f['fix'] if f.get('fix') else 'report'} · {f['kind']} · {f['text']}")
        text, _keys = cc.message(findings, {}, now.date())
        print('\nmessage it would send:\n' + (text or '(none — nothing non-trivial)'))
        return 0
    fixed = cc.apply(fixable, now)
    state = _read('consistency_state.json', {})
    text, keys = cc.message(findings, state, now.date())
    report = {'ts': now.isoformat(timespec='seconds'), 'findings': findings, 'fixed': len(fixed)}
    try:
        (cc.REPORT).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    except OSError as e:
        print(f'  ⚠️ report: {e!r}')
    if text and not a.no_send:
        from utils import notify
        res = notify.notify(text, key=f'consistency:{now:%Y-%m-%d}')
        print(f'  message: {res}')
        if res in ('sent', 'queued', 'dup'):
            state['sent_day'] = now.date().isoformat()
            rep = state.setdefault('reported', {})
            rep.update({k: now.isoformat(timespec='seconds') for k in keys})
            cut = (now - timedelta(days=60)).isoformat()
            state['reported'] = {k: v for k, v in rep.items() if v >= cut}
            (cc.STATE).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'{datetime.now():%F %T} done: {len(findings)} findings, {len(fixed)} fixed, message: {"yes" if text else "no"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
