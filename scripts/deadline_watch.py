"""scripts/deadline_watch.py — submission-aware homework reminders (utils/deadlines.py).

Every 2 hours, 07:00–23:00 Israel. Cheap when nothing is due (no Moodle login);
when an assignment is due within ~3 days it re-checks the submission status live
first, so a homework you handed in this afternoon is never "reminded" tonight.
During quiet hours it does nothing — the next run re-plans with accurate wording.
Also flushes the overnight digest of utils/notify.

    venv/bin/python3 scripts/deadline_watch.py [--dry-run] [--no-live]
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass


def _sync_calendar(dl, items, state, now, dry_run) -> None:
    """Opt-in mirror of upcoming/just-passed deadlines into Google Calendar (never fails the run)."""
    if os.getenv('SSA_DEADLINES_TO_CALENDAR') != '1' or dry_run:
        return
    try:
        from datetime import timedelta
        from integrations import gcal
        if not gcal.configured():
            return
        window = [{**a, 'key': dl.key_of(a)} for a in items
                  if now - timedelta(days=2) <= dl._due(a) <= now + timedelta(days=30)]
        done = {a['key'] for a in window if dl.is_done(a, state)}
        print(f'calendar: {gcal.sync_deadlines(window, done)} deadline events synced')
    except Exception as e:
        print(f'  ⚠️ calendar sync failed: {e}')


def main(dry_run: bool = False, live: bool = True, now: datetime | None = None) -> int:
    from utils import deadlines as dl
    from utils.notify import notify, in_quiet_hours, flush_digest

    now = now or datetime.now()
    if in_quiet_hours(now) and not dry_run:
        print('quiet hours — nothing to do')
        return 0
    if not dry_run:
        n = flush_digest(now)
        if n:
            print(f'digest: {n} queued messages sent')

    items = dl.load_items()
    state = dl.load_state()
    if live and items:
        items = dl.refresh_live(items, now)
    _sync_calendar(dl, items, state, now, dry_run)
    actions = dl.plan_actions(items, state, now)
    if not actions:
        print(f'{len(items)} assignments, nothing to send')
        return 0
    failures = 0
    for act in actions:
        text, buttons, priority = dl.message(act, now)
        due = dl._due(act['item'])
        nkey = f"hw:{act['key']}:{act['kind']}:{act.get('stage', '')}:{due:%Y%m%d%H%M}"
        if dry_run:
            print(f"[{act['kind']}/{act.get('stage', '')}] {text}  buttons={buttons}")
            continue
        res = notify(text, key=nkey, priority=priority, buttons=buttons, now=now)
        print(f"{act['key']} {act['kind']} {act.get('stage', '')}: {res}")
        if res in ('sent', 'queued', 'dup'):
            dl.apply_action(state, act)
        else:
            failures += 1                                  # state untouched → retried next run
    if not dry_run:
        dl.save_state(state)
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    sys.exit(main(dry_run='--dry-run' in sys.argv, live='--no-live' not in sys.argv))
