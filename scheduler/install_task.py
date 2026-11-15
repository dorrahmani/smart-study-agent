"""
scheduler/install_task.py — Manage Windows Scheduled Tasks that run
sync_runner. Two modes:

  * HOURLY (legacy)   — one task that fires every N hours.
  * DAILY-AT (Phase 3) — multiple tasks, each firing daily at a fixed clock time.
                         e.g. 08:00 + 18:00 → tasks SmartStudyAgent_Sync_0800
                         and SmartStudyAgent_Sync_1800.

Idempotent: re-installing replaces the existing task definition (/F).

CLI:
    python -m scheduler.install_task --install-daily 08:00 18:00
    python -m scheduler.install_task --install --hours 3      # legacy hourly
    python -m scheduler.install_task --status
    python -m scheduler.install_task --uninstall
"""

from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Legacy single-task name (HOURLY mode)
TASK_NAME = 'SmartStudyAgent_Sync'

# Phase 3 — twice-daily default (08:00, 18:00). Tasks are named
# SmartStudyAgent_Sync_HHMM so each clock time has its own task definition.
DAILY_DEFAULT_TIMES = ('08:00', '18:00')

# All Phase-3 daily task names start with this prefix — used by uninstall/status.
DAILY_PREFIX = 'SmartStudyAgent_Sync_'

_TIME_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')


def _runner_cmd() -> str:
    """Build the /TR string. Prefer pythonw.exe so no console window flashes."""
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    runner = ROOT / 'scheduler' / 'sync_runner.py'
    return f'"{pythonw}" "{runner}"'


def _validate_time(t: str) -> str:
    """Validate HH:MM and normalize to zero-padded form."""
    m = _TIME_RE.match(t.strip())
    if not m:
        raise ValueError(f'Invalid time {t!r} — expected HH:MM (24h).')
    return f'{int(m.group(1)):02d}:{m.group(2)}'


def _task_name_for_time(t: str) -> str:
    """SmartStudyAgent_Sync_0800 for '08:00'."""
    return f'{DAILY_PREFIX}{t.replace(":", "")}'


# ─── Status / discovery ────────────────────────────────────────────────────

def is_installed() -> bool:
    """True if EITHER the legacy hourly task OR any daily-at task exists."""
    if _query_task_exists(TASK_NAME):
        return True
    return bool(list_daily_tasks())


def _query_task_exists(name: str) -> bool:
    try:
        out = subprocess.run(
            ['schtasks', '/Query', '/TN', name],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
        return out.returncode == 0
    except Exception:
        return False


def list_daily_tasks() -> list[str]:
    """All currently-installed SmartStudyAgent_Sync_HHMM task names."""
    try:
        out = subprocess.run(
            ['schtasks', '/Query', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
        if out.returncode != 0:
            return []
    except Exception:
        return []

    found: list[str] = []
    for line in (out.stdout or '').splitlines():
        # CSV: "TaskName","Next Run Time","Status"
        m = re.match(r'^"([^"]+)"', line)
        if not m:
            continue
        name = m.group(1).lstrip('\\')
        if name.startswith(DAILY_PREFIX):
            found.append(name)
    return sorted(set(found))


def status() -> dict:
    """Status of the legacy hourly task AND any daily-at tasks."""
    out = {'legacy_hourly': None, 'daily_at': {}}

    res = subprocess.run(
        ['schtasks', '/Query', '/TN', TASK_NAME, '/FO', 'LIST', '/V'],
        capture_output=True, text=True,
        encoding='utf-8', errors='replace',
    )
    if res.returncode == 0:
        out['legacy_hourly'] = (res.stdout or '').strip()[:2000]

    for name in list_daily_tasks():
        sub = subprocess.run(
            ['schtasks', '/Query', '/TN', name, '/FO', 'LIST', '/V'],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
        if sub.returncode == 0:
            out['daily_at'][name] = (sub.stdout or '').strip()[:1500]
    return out


# ─── Install (legacy hourly) ───────────────────────────────────────────────

def install(interval_hours: int = 3) -> dict:
    """Legacy: single SmartStudyAgent_Sync task that fires every N hours."""
    cmd = [
        'schtasks', '/Create',
        '/TN', TASK_NAME,
        '/TR', _runner_cmd(),
        '/SC', 'HOURLY', '/MO', str(interval_hours),
        '/F',
        '/RL', 'LIMITED',
    ]
    res = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding='utf-8', errors='replace',
    )
    return {
        'installed':      res.returncode == 0,
        'returncode':     res.returncode,
        'stdout':         (res.stdout or '').strip(),
        'stderr':         (res.stderr or '').strip(),
        'interval_hours': interval_hours,
        'command':        ' '.join(cmd),
    }


# ─── Install (Phase 3 daily-at — multiple times per day) ──────────────────

def install_daily_times(times: list[str] | tuple[str, ...] = DAILY_DEFAULT_TIMES) -> dict:
    """
    Create one SmartStudyAgent_Sync_HHMM task per time. Re-running replaces.

    Returns {
      'installed': bool,        # True if every task succeeded
      'tasks': [{name,time,returncode,stdout,stderr}],
      'times': [...],
    }
    """
    norm = [_validate_time(t) for t in times]
    results = []
    all_ok = True
    for t in norm:
        name = _task_name_for_time(t)
        cmd = [
            'schtasks', '/Create',
            '/TN', name,
            '/TR', _runner_cmd(),
            '/SC', 'DAILY',
            '/ST', t,
            '/F',
            '/RL', 'LIMITED',
        ]
        res = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
        ok = (res.returncode == 0)
        all_ok = all_ok and ok
        results.append({
            'name':       name,
            'time':       t,
            'returncode': res.returncode,
            'stdout':     (res.stdout or '').strip(),
            'stderr':     (res.stderr or '').strip(),
            'ok':         ok,
        })
    return {'installed': all_ok, 'tasks': results, 'times': norm}


# ─── Uninstall (both flavours) ─────────────────────────────────────────────

def uninstall() -> dict:
    """Remove the legacy hourly task AND all SmartStudyAgent_Sync_HHMM tasks."""
    deleted = []
    failed  = []

    # 1. legacy
    if _query_task_exists(TASK_NAME):
        r = subprocess.run(
            ['schtasks', '/Delete', '/TN', TASK_NAME, '/F'],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
        (deleted if r.returncode == 0 else failed).append({
            'name': TASK_NAME, 'returncode': r.returncode,
            'stderr': (r.stderr or '').strip(),
        })

    # 2. all daily tasks
    for name in list_daily_tasks():
        r = subprocess.run(
            ['schtasks', '/Delete', '/TN', name, '/F'],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
        (deleted if r.returncode == 0 else failed).append({
            'name': name, 'returncode': r.returncode,
            'stderr': (r.stderr or '').strip(),
        })

    return {
        'uninstalled': not failed,
        'deleted':     deleted,
        'failed':      failed,
    }


# ─── CLI ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(
        description='Manage SmartStudyAgent_Sync scheduled tasks (legacy hourly + Phase-3 daily-at).'
    )
    p.add_argument('--install',       action='store_true',
                   help='Legacy hourly install — single task, every --hours.')
    p.add_argument('--install-daily', nargs='+', metavar='HH:MM',
                   help='Phase 3: install one task per HH:MM time. '
                        'Example: --install-daily 08:00 18:00')
    p.add_argument('--uninstall',     action='store_true', help='Delete every Sync task.')
    p.add_argument('--status',        action='store_true', help='Show all installed Sync tasks.')
    p.add_argument('--hours',         type=int, default=3,
                   help='Interval in hours for legacy --install (default 3).')
    args = p.parse_args()

    if args.uninstall:
        print(uninstall())
    elif args.status:
        print(status())
    elif args.install_daily:
        print(install_daily_times(args.install_daily))
    elif args.install:
        print(install(interval_hours=args.hours))
    else:
        # backward compat: default action = legacy hourly install
        print(install(interval_hours=args.hours))
