"""
utils/pipeline_health.py — catch the failures that DON'T crash.

run_with_alert.sh only pings on a non-zero exit code. The dangerous failures
of a new semester are silent: the schedule filter drops every recording, a
course is skipped because its folder id is still a placeholder, a course stops
producing summaries. The pipeline exits 0 and nobody hears about it.

Two mechanisms, both consumed by step 11 of pipelines/daily_full_update.py:

  pipeline_warn(msg)       any component prints a marker line into the run log;
                           extract_pipeline_warnings() pulls them back out.
  stale_courses(...)       per active course: has a new summary appeared in the
                           last `threshold_days`? Catches every silent cause at
                           once, including ones nobody thought of yet.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

MARKER = '🚨 PIPELINE-WARN:'
_DATE_PREFIX = re.compile(r'^(\d{4}-\d{2}-\d{2})')


def pipeline_warn(msg: str) -> None:
    print(f'{MARKER} {msg}', flush=True)


def extract_pipeline_warnings(log_text: str) -> list[str]:
    """Unique warnings in first-seen order."""
    seen, out = set(), []
    for line in log_text.splitlines():
        i = line.find(MARKER)
        if i < 0:
            continue
        msg = line[i + len(MARKER):].strip()
        if msg and msg not in seen:
            seen.add(msg)
            out.append(msg)
    return out


def _issue_key(issue: str) -> str:
    """'… מתוך 11 …' and '… מתוך 12 …' are the same issue: numbers don't make it new."""
    return re.sub(r'\d+', '#', ' '.join(issue.split()))


def fresh_issues(issues: list[str], state: Path, days: int = 7, now=None, mark: bool = False) -> list[str]:
    """10/11: the nightly health report sent the same 9 structural warnings every night (The owner's groups aren't recorded,
    so 'folder_id not filled' / 'no recording matches my schedule' for 3 courses) — alert fatigue that hides a real
    one. → the issues not reported in the last `days`. mark=True records them as reported now — called only after the
    send went out, so a failed send doesn't silence anything. A broken state file means everything is fresh."""
    import json, os
    from datetime import datetime
    now = now or datetime.now()
    try:
        seen = json.loads(Path(state).read_text(encoding='utf-8'))
        if not isinstance(seen, dict):
            seen = {}
    except (OSError, ValueError):
        seen = {}
    out = []
    for i in issues:
        k = _issue_key(i)
        try:
            last = datetime.fromisoformat(seen.get(k, ''))
        except ValueError:
            last = None
        if last is None or now - last >= timedelta(days=days):
            out.append(i)
            seen[k] = now.isoformat(timespec='seconds')
    if not mark:
        return out
    cutoff = now - timedelta(days=days * 4)
    seen = {k: v for k, v in seen.items() if v >= cutoff.isoformat(timespec='seconds')}
    try:
        tmp = Path(state).with_suffix('.tmp')
        tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, state)
    except OSError:
        pass
    return out


# ── dead-man switch: did the evening run happen at all? ──────────────────
# Step 11 can't report a run that never started (cron broken, crash before
# step 11, VM rebooted mid-run). The pipeline stamps start/finish here and the
# morning briefing — a separate cron job — checks the stamp.
LAST_RUN_FILE = 'last_pipeline_run.json'


def record_run(data_dir: Path, **fields) -> None:
    import json, os
    p = Path(data_dir) / LAST_RUN_FILE
    try:
        cur = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    except Exception:
        cur = {}
    cur.update(fields)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


RUN_GRACE_HOURS = 3     # 13/11: a run takes 20-50 min (the 11/11 one 47); one that started this recently is running


def missed_run_warning(data_dir: Path, config: dict, now=None, max_age_hours: float = 24):
    """Hebrew warning if the last evening run should have happened but didn't
    finish within `max_age_hours`; None when all is well (or no run was due).
    11/11 21:45 Israel: "התחיל ב-19:00 ולא הסתיים" went out while that very run was 45 minutes in and finished a
    minute later — the 15-minute heartbeat reads the record mid-run. A run started under RUN_GRACE_HOURS ago is
    running, not stuck."""
    import json
    from datetime import datetime
    from utils.semester import daily_pipeline_should_run
    now = now or datetime.now()
    due, _ = daily_pipeline_should_run(config, (now - timedelta(days=1)).date())
    if not due:
        return None
    p = Path(data_dir) / LAST_RUN_FILE
    try:
        rec = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return '🚨 הצינור היומי לא רץ (אין רישום ריצה בכלל) — לבדוק cron ב-VM'
    fin = rec.get('finished')
    started = rec.get('started')
    try:
        fin_dt = datetime.fromisoformat(fin) if fin else None
    except ValueError:
        fin_dt = None
    if fin_dt and (now - fin_dt) <= timedelta(hours=max_age_hours):
        failed = rec.get('failed_steps') or []
        return (f'⚠️ הריצה האחרונה הסתיימה, אבל {len(failed)} שלבים נכשלו: {", ".join(failed)[:150]}'
                if failed else None)
    if started and (not fin_dt or started > fin):
        try:
            if now - datetime.fromisoformat(started) < timedelta(hours=RUN_GRACE_HOURS):
                return None
        except ValueError:
            pass
        return f'🚨 הצינור התחיל ב-{started[:16]} ולא הסתיים — ייתכן שנתקע או קרס'
    return f'🚨 הצינור היומי לא רץ מאז {(fin or "?")[:16]} — לבדוק cron ב-VM'


def last_summary_date(summaries_dir: Path, course_key: str) -> date | None:
    """Newest YYYY-MM-DD filename prefix under <course>/lectures|tutorials."""
    newest = None
    for sub in ('lectures', 'tutorials'):
        d = Path(summaries_dir) / course_key / sub
        if not d.is_dir():
            continue
        for md in d.glob('*.md'):
            m = _DATE_PREFIX.match(md.name)
            if not m:
                continue
            try:
                dt = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if newest is None or dt > newest:
                newest = dt
    return newest


def stale_courses(summaries_dir: Path, active_courses: list[str], today: date,
                  semester_start: date, semester_end: date | None = None,
                  threshold_days: int = 10) -> list[tuple[str, date | None]]:
    """Active courses with no summary dated within `threshold_days`.

    Every course meets weekly (lecture + tutorial) and recordings land the
    next evening, so 10 days without anything means something is broken — a
    single cancelled class (holiday) still leaves the other session that week.
    Silent until the semester is `threshold_days` old, so week one's normal
    upload lag never fires it.
    """
    if today < semester_start + timedelta(days=threshold_days):
        return []
    if semester_end and today > semester_end:
        return []
    cutoff = today - timedelta(days=threshold_days)
    out = []
    for ck in active_courses:
        last = last_summary_date(summaries_dir, ck)
        if last is None or last < cutoff:
            out.append((ck, last))
    return out


# ── secret files must stay private (600) ─────────────────────────────────
SECRET_GLOBS = ['.env', 'config/*.pickle', 'config/secrets/*', 'config/recipients*.json*',
                'config/*token*.json', 'config/*credentials*.json']


def insecure_secret_files(root: Path) -> list[str]:
    """Secret files readable by group/other (POSIX only; [] on Windows)."""
    import os, stat
    if os.name != 'posix':
        return []
    bad = []
    for pat in SECRET_GLOBS:
        for f in Path(root).glob(pat):
            if f.is_file() and f.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                bad.append(f'{f.relative_to(root).as_posix()} ({oct(f.stat().st_mode & 0o777)[2:]})')
    return bad


# ── cron heartbeat — independent of run_with_alert.sh ────────────────────
# 26-27/09/2026: a deploy dropped the executable bit from run_with_alert.sh and
# EVERY wrapped cron job failed for ~34h with no alert, because the wrapper is
# what sends alerts. This check runs from a job that is NOT wrapped
# (scripts/cron_heartbeat.py; until 10/11 external_uptime_telegram.py) and looks at the wrapper's own
# log: no entry for `max_hours` means wrapped jobs have stopped running.
CRON_HEALTH_LOG = Path('/tmp/cron_health.log')


def cron_heartbeat_warning(log_path: Path = CRON_HEALTH_LOG, max_hours: float = 10,
                           now_ts: float | None = None, uptime_s: float | None = None):
    import time
    now_ts = now_ts or time.time()
    if uptime_s is None:
        try:
            uptime_s = float(Path('/proc/uptime').read_text().split()[0])
        except Exception:
            uptime_s = 1e9
    if not Path(log_path).exists():
        # /tmp is wiped on reboot — give the first jobs time to run
        return None if uptime_s < max_hours * 3600 else \
            f'🚨 cron: אין רישום ריצות ({log_path}) כבר {uptime_s / 3600:.0f} שעות — משימות ה-cron לא רצות'
    age_h = (now_ts - Path(log_path).stat().st_mtime) / 3600
    if age_h > max_hours and uptime_s > max_hours * 3600:
        return (f'🚨 cron: אף משימה עטופה לא רצה {age_h:.0f} שעות. '
                f'לבדוק: ls -l scripts/run_with_alert.sh (הרשאת הרצה), crontab -l, /tmp/*.log')
    return None


def cron_heartbeat_check(state_path: Path = Path('/tmp/cron_heartbeat_alert.json'),
                         realert_hours: float = 12) -> None:
    import json, time
    w = cron_heartbeat_warning()
    if not w:
        return
    try:
        last = json.loads(state_path.read_text()).get('ts', 0)
    except Exception:
        last = 0
    if time.time() - last < realert_hours * 3600:
        return
    from utils.notify import deliver              # WhatsApp only (10/11: Telegram deleted)
    if deliver(w, markdown=False):
        state_path.write_text(json.dumps({'ts': time.time()}))
