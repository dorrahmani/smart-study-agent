"""
utils/system_health.py — one table for "is everything alive?" (18/11/2026). The dashboard's 🩺 מערכת tab renders it.

Read-only, no LLM, no network, no secrets: it only reads files the system already writes (the cron wrapper's log, the
per-job logs, data/*.json) and `crontab -l`. Every row is {group, name, status, detail, checked}:
    status  'ok' (green) · 'warn' (yellow) · 'fail' (red) · 'na' (grey: nothing to judge yet, e.g. /tmp wiped by a reboot)
    checked ISO time of the evidence (a log line / file mtime), so a stale green is visible as stale.
rows() never raises: a broken collector becomes one yellow row, not a dead page.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
LOGS = ROOT / 'logs'
CRON_HEALTH = Path('/tmp/cron_health.log')
IL = ZoneInfo('Asia/Jerusalem')
OK, WARN, FAIL, NA = 'ok', 'warn', 'fail', 'na'


def row(group: str, name: str, status: str, detail: str, checked=None) -> dict:
    if isinstance(checked, (int, float)):
        checked = datetime.fromtimestamp(checked, timezone.utc).isoformat(timespec='seconds')
    elif isinstance(checked, datetime):
        checked = checked.astimezone(timezone.utc).isoformat(timespec='seconds')
    return {'group': group, 'name': name, 'status': status, 'detail': detail, 'checked': checked or ''}


def _json(p: Path):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _age_h(ts: float, now: float) -> float:
    return (now - ts) / 3600


def _tail(p: Path, n: int = 40, max_bytes: int = 60000) -> list[str]:
    try:
        with p.open('rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode('utf-8', 'replace').splitlines()[-n:]
    except OSError:
        return []


# ── cron schedule: when should a job have last fired? ─────────────────────────────────────────────────────────────

def _field(spec: str, lo: int, hi: int) -> set[int] | None:
    """One cron field → the set of values; None = unparseable."""
    out: set[int] = set()
    for part in spec.split(','):
        m = re.fullmatch(r'(\*|\d+(?:-\d+)?)(?:/(\d+))?', part)
        if not m:
            return None
        base, step = m.group(1), int(m.group(2) or 1)
        if base == '*':
            a, b = lo, hi
        elif '-' in base:
            a, b = map(int, base.split('-'))
        else:
            a = b = int(base)
            if m.group(2):
                b = hi
        out |= set(range(a, b + 1, step))
    return out


def parse_schedule(five: list[str]):
    mi, ho, dom, mon, dow = (_field(five[0], 0, 59), _field(five[1], 0, 23), _field(five[2], 1, 31),
                             _field(five[3], 1, 12), _field(five[4], 0, 7))
    if None in (mi, ho, dom, mon, dow):
        return None
    return {'min': mi, 'hour': ho, 'dom': dom, 'mon': mon, 'dow': {d % 7 for d in dow},
            'dom_star': five[2] == '*', 'dow_star': five[4] == '*'}


def prev_fire(sched: dict, now: datetime, max_days: int = 8) -> datetime | None:
    """The latest minute <= now at which the schedule fires (in now's timezone), looking back max_days."""
    t = now.replace(second=0, microsecond=0)
    for _ in range(max_days * 1440):
        cron_dow = (t.weekday() + 1) % 7                      # python Mon=0 → cron Sun=0
        if sched['dom_star'] or sched['dow_star']:
            day_ok = (t.day in sched['dom']) and (cron_dow in sched['dow'])
        else:
            day_ok = (t.day in sched['dom']) or (cron_dow in sched['dow'])
        if t.minute in sched['min'] and t.hour in sched['hour'] and t.month in sched['mon'] and day_ok:
            return t
        t -= timedelta(minutes=1)
    return None


def parse_crontab(text: str) -> list[dict]:
    """Active crontab lines → {name, sched, tz, log, wrapped}. Comment lines, env lines and unparseable ones are skipped."""
    jobs = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        parts = s.split(None, 5)
        if len(parts) < 6:
            continue
        sched = parse_schedule(parts[:5])
        if not sched:
            continue
        cmd = parts[5]
        tz = re.match(r'TZ=(\S+)', cmd)
        wrapped = re.search(r'run_with_alert\.sh"?\s+"([^"]+)"', cmd)
        if wrapped:
            name = wrapped.group(1)
        else:
            sc = re.search(r'scripts/([\w.-]+?)(?:\.py|\.sh)\b', cmd)
            name = (sc.group(1) if sc else cmd[:30]).replace('_', ' ')
        log = re.search(r'>>\s*(\S+)', cmd)
        jobs.append({'name': name, 'sched': sched, 'tz': tz.group(1) if tz else None,
                     'log': Path(log.group(1)) if log else None, 'wrapped': bool(wrapped), 'once_a_year': not (
                         sched['mon'] == set(range(1, 13)))})
    return jobs


def read_wrapper_log(path: Path = CRON_HEALTH) -> dict[str, dict]:
    """/tmp/cron_health.log lines `[start → end] Name exit=N` → {name: {'start': naive-str, 'rc': int}} (the last each)."""
    last: dict[str, dict] = {}
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return last
    for m in re.finditer(r'^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) → [^\]]+\] (.+) exit=(\d+)\s*$', text, re.M):
        last[m.group(2)] = {'start': m.group(1), 'rc': int(m.group(3))}
    return last


def judge_job(job: dict, rec: dict | None, log_mtime: float | None, now: datetime, boot: float | None,
              local_tz) -> tuple[str, str, float | None]:
    """→ (status, detail, last_run_epoch). The wrapper log stamps each line in the TZ the job ran with; cron itself schedules in the server's zone."""
    tz = ZoneInfo(job['tz']) if job['tz'] else local_tz                    # the log's clock; the SCHEDULE is the server's (UTC)
    expected = prev_fire(job['sched'], (now - timedelta(minutes=30)).astimezone(local_tz))   # 30 min of grace for a long run
    last = None
    if rec:
        try:
            last = datetime.strptime(rec['start'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=tz).timestamp()
        except ValueError:
            last = None
    if last is None and log_mtime:
        last = log_mtime
    if expected is None:
        return (OK if last else NA), 'לא מתוזמן ב-8 הימים האחרונים' + ('' if last else ' · אין רישום'), last
    if last is None:
        if boot and expected.timestamp() < boot:
            return NA, 'אין רישום עדיין (השרת הופעל מחדש מאז הריצה המתוכננת)', None
        return WARN, f'אין רישום ריצה; הייתה אמורה לרוץ {expected.astimezone(IL):%d/%m %H:%M}', None
    when = datetime.fromtimestamp(last, IL)
    if rec and rec['rc'] != 0:
        return FAIL, f'הריצה האחרונה נכשלה (קוד {rec["rc"]}) · {when:%d/%m %H:%M}', last
    if last < expected.timestamp() - 120 and not job['wrapped'] and not rec:
        return NA, f'ריצה שקטה: היומן מתעדכן רק כשיש מה לכתוב (אחרון {when:%d/%m %H:%M}); הדופק למעלה בודק שה-cron חי', last
    if last < expected.timestamp() - 120:
        return WARN, f'לא רצה בזמן: אמורה הייתה {expected.astimezone(IL):%d/%m %H:%M}, אחרונה {when:%d/%m %H:%M}', last
    return OK, f'רצה {when:%d/%m %H:%M}' + ('' if rec else ' (לפי הלוג, ללא קוד יציאה)'), last


def _crontab_text() -> str:
    try:
        r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        return (ROOT / 'deploy' / 'crontab.txt').read_text(encoding='utf-8')
    except OSError:
        return ''


def _boot_epoch() -> float | None:
    try:
        up = float(Path('/proc/uptime').read_text().split()[0])
        return datetime.now(timezone.utc).timestamp() - up
    except (OSError, ValueError):
        return None


def cron_rows(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    recs, boot = read_wrapper_log(), _boot_epoch()
    local_tz = datetime.now().astimezone().tzinfo
    out = []
    for job in parse_crontab(_crontab_text()):
        if job['once_a_year']:
            continue
        mt = _mtime(job['log']) if job['log'] else None
        st, detail, last = judge_job(job, recs.get(job['name']), mt, now, boot, local_tz)
        out.append(row('cron', job['name'], st, detail, last))
    order = {FAIL: 0, WARN: 1, NA: 2, OK: 3}
    return sorted(out, key=lambda r: (order[r['status']], r['name']))


# ── the single-source rows ─────────────────────────────────────────────────────────────────────────────────────────

def heartbeat_row(now_ts: float) -> dict:
    try:
        from utils.pipeline_health import cron_heartbeat_warning
        w = cron_heartbeat_warning()
    except Exception as e:
        return row('cron', 'דופק cron (cron_heartbeat)', WARN, f'לא נבדק: {e}')
    m = _mtime(CRON_HEALTH)
    return row('cron', 'דופק cron (cron_heartbeat)', FAIL if w else OK,
               (w or 'יומן העטיפה מתעדכן — המשימות רצות').lstrip('🚨 '), m)


def nlm_row(now_ts: float) -> dict:
    lines = [l for l in _tail(LOGS / 'nlm_keepalive.log', 30) if 'nlm keepalive' in l]
    m = _mtime(LOGS / 'nlm_keepalive.log')
    if not lines:
        return row('NotebookLM', 'NotebookLM keepalive', NA, 'אין רישום', m)
    last = lines[-1]
    age = _age_h(m, now_ts) if m else 99
    low = last.lower()
    if 'dead' in low or 'fail' in low or 'error' in low:
        return row('NotebookLM', 'NotebookLM keepalive', FAIL, last[:140], m)
    if age > 1:
        return row('NotebookLM', 'NotebookLM keepalive', WARN, f'אין ריצה כבר {age:.1f} שעות · {last[:100]}', m)
    return row('NotebookLM', 'NotebookLM keepalive', OK, last[17:140] if len(last) > 17 else last, m)


def drive_row(now_ts: float) -> dict:
    p = Path('/tmp/backup.log')
    m = _mtime(p)
    tail = _tail(p, 12)
    if m is None:
        return row('Drive', 'Drive / rclone — גיבוי יומי', NA, 'אין יומן גיבוי (/tmp נמחק באתחול)')
    done = any('Backup complete' in l for l in tail)
    age = _age_h(m, now_ts)
    if not done:
        return row('Drive', 'Drive / rclone — גיבוי יומי', FAIL, 'הגיבוי האחרון לא הסתיים בהצלחה', m)
    return row('Drive', 'Drive / rclone — גיבוי יומי', OK if age < 36 else WARN,
               f'הצליח לפני {age:.0f} שעות' + ('' if age < 36 else ' — ישן מהצפוי'), m)


def vault_push_row(now_ts: float) -> dict:
    p = LOGS / 'vault_assistant.log'
    m = _mtime(p)
    pushes = [l for l in _tail(p, 80) if l.strip().startswith('push ')]
    if not pushes or m is None:
        return row('Drive', 'Drive / rclone — דחיפת הכספת', NA, 'אין רישום', m)
    bad = [l for l in pushes[-4:] if 'rclone' in l]
    age = _age_h(m, now_ts)
    st = FAIL if bad else (OK if age < 20 else WARN)
    return row('Drive', 'Drive / rclone — דחיפת הכספת', st, ('נכשלה: ' + bad[-1].strip()) if bad else f'נדחפה לפני {age:.0f} שעות', m)


def bridge_row(now_ts: float) -> dict:
    p = DATA / 'wa_bridge' / 'status.json'
    d = _json(p) or {}
    active = None
    try:
        r = subprocess.run(['systemctl', 'is-active', 'smart-study-wa-bridge'], capture_output=True, text=True, timeout=5)
        active = r.stdout.strip() == 'active'
    except (OSError, subprocess.TimeoutExpired):
        pass
    m = _mtime(p)
    if 'logged_out' in d:
        return row('WhatsApp', 'גשר WhatsApp', FAIL, 'המכשיר המקושר התנתק — צריך לסרוק QR מחדש', d['logged_out'])
    if active is False:
        return row('WhatsApp', 'גשר WhatsApp', FAIL, 'שירות הגשר לא פעיל', m)
    if 'connected' in d:
        since = datetime.fromtimestamp(d['connected'], IL)
        return row('WhatsApp', 'גשר WhatsApp', OK, f'מחובר מאז {since:%d/%m %H:%M}' + ('' if active else ' (מצב השירות לא נבדק)'), d['connected'])
    return row('WhatsApp', 'גשר WhatsApp', NA if active is None else WARN, 'אין סטטוס חיבור', m)


def budget_row(now_ts: float) -> dict:
    lock = _json(DATA / 'budget_lock.json')
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    try:
        from utils.usage_tracker import summary
        total = float((summary('month') or {}).get('total_usd', 0) or 0)
    except Exception:
        total = None
    spend = f'הוצאה החודש: ${total:.2f}' if total is not None else 'הוצאה לא זמינה'
    if lock and lock.get('month') in (None, month):
        return row('תקציב', 'נעילת תקציב', FAIL, f'נעול — {spend}', _mtime(DATA / 'budget_lock.json'))
    return row('תקציב', 'נעילת תקציב', OK if total is None or total < 20 else WARN, f'לא נעול · {spend}', _mtime(DATA / 'usage.jsonl'))


def deploy_row(now_ts: float) -> dict:
    p = LOGS / 'deploy.log'
    ev = [l for l in _tail(p, 30) if re.search(r'deploy [0-9a-f]{7,} -> [0-9a-f]{7,}', l)]
    if not ev:
        return row('פריסה', 'פריסה אחרונה', NA, 'אין רישום פריסה', _mtime(p))
    m = re.match(r'(\S+) deploy (\S+) -> (\S+)', ev[-1].strip())
    if not m:
        return row('פריסה', 'פריסה אחרונה', NA, ev[-1][:100], _mtime(p))
    ts = datetime.fromisoformat(m.group(1))
    fail = [l for l in _tail(p, 8) if re.search(r'reject|FAIL|✗|❌', l)]
    return row('פריסה', 'פריסה אחרונה', WARN if fail else OK, f'commit {m.group(3)[:7]} (מ-{m.group(2)[:7]}) · {ts.astimezone(IL):%d/%m %H:%M}', ts)


def security_row(now_ts: float) -> dict:
    d = _json(DATA / 'security_status.json')
    if not d:
        return row('אבטחה', 'בדיקת אבטחה לילית', NA, 'אין תוצאה')
    s = d.get('summary') or {}
    p, w, f = s.get('pass', 0), s.get('warn', 0), s.get('fail', 0)
    total = p + w + f
    ts = datetime.fromisoformat(d['ts']) if d.get('ts') else None
    stale = ts is None or (now_ts - ts.timestamp()) > 36 * 3600
    st = FAIL if f else (WARN if (w or stale) else OK)
    bad = ', '.join(c['id'] for c in d.get('checks', []) if c.get('status') != 'PASS')[:90]
    return row('אבטחה', 'בדיקת אבטחה לילית', st,
               f'ציון {p}/{total} · {p} PASS · {w} WARN · {f} FAIL' + (f' · {bad}' if bad else '') + (' · ישן' if stale else ''), ts)


def backup_test_rows(now_ts: float) -> list[dict]:
    d = _json(DATA / 'backup_test.json')
    if not d:
        return [row('גיבויים', 'בדיקת שחזור גיבויים', NA, 'עוד לא רצה (שבועית, ראשון 05:30 UTC)')]
    ts = datetime.fromisoformat(d['ts']) if d.get('ts') else None
    old = ts is None or (now_ts - ts.timestamp()) > 9 * 86400
    smap = {'OK': OK, 'WARN': WARN, 'FAIL': FAIL}
    over = smap.get(d.get('overall'), WARN)
    out = [row('גיבויים', 'בדיקת שחזור גיבויים — סיכום', WARN if (old and over == OK) else over,
               f'{d.get("overall")} · {len(d.get("checks", []))} בדיקות' + (' · ישן מ-9 ימים' if old else ''), ts)]
    for c in d.get('checks', []):
        out.append(row('גיבויים', c.get('label') or c.get('id', '?'), smap.get(c.get('status'), WARN), c.get('reason', ''), ts))
    return out


def latency_row(now_ts: float) -> dict:
    p = DATA / 'latency_summary.json'
    d = _json(p)
    if not d:
        return row('בוט', 'זמן תגובה (p50/p95)', NA, 'ממתין לנתונים (data/latency_summary.json עוד לא נכתב)')
    days = d.get('days') or {}
    key = max(days) if days else None
    cur = (days.get(key) if key else None) or d.get('all') or {}
    tot = cur.get('total') or {}
    if not tot:
        return row('בוט', 'זמן תגובה (p50/p95)', NA, 'אין עדיין תורות', _mtime(p))
    p50, p95 = tot.get('p50', 0), tot.get('p95', 0)
    st = FAIL if p95 > 90 else (WARN if p95 > 45 else OK)
    return row('בוט', 'זמן תגובה (p50/p95)', st, f'{key or "כל התקופה"}: p50 {p50:.1f}s · p95 {p95:.1f}s · {cur.get("n", "?")} תורות', _mtime(p))


def _jsonl_row(group: str, name: str, fname: str, now_ts: float, what: str) -> dict:
    p = DATA / fname
    m = _mtime(p)
    if m is None:
        return row(group, name, NA, f'{fname} עוד לא נכתב (אין {what} עדיין)')
    lines = _tail(p, 2000, 400000)
    week = 0
    for l in lines:
        try:
            ts = str((json.loads(l).get('ts') or json.loads(l).get('time') or ''))[:19]
            if ts and (now_ts - datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp()) < 7 * 86400:
                week += 1
        except (ValueError, AttributeError):
            continue
    return row(group, name, OK, f'{week} {what} ב-7 הימים האחרונים · עדכון אחרון {datetime.fromtimestamp(m, IL):%d/%m %H:%M}', m)


def websearch_row(now_ts: float) -> dict:
    return _jsonl_row('בוט', 'חיפושי רשת', 'websearch_log.jsonl', now_ts, 'חיפושים')


def factfix_row(now_ts: float) -> dict:
    return _jsonl_row('בוט', 'תיקוני עובדות', 'fact_fixes.jsonl', now_ts, 'תיקונים')


def collect(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    rows: list[dict] = []
    for fn in (heartbeat_row, nlm_row, drive_row, vault_push_row, bridge_row, budget_row, deploy_row, security_row,
               latency_row, websearch_row, factfix_row):
        try:
            rows.append(fn(ts))
        except Exception as e:
            rows.append(row('מערכת', fn.__name__, WARN, f'הבדיקה נכשלה: {type(e).__name__}'))
    try:
        rows += backup_test_rows(ts)
    except Exception as e:
        rows.append(row('גיבויים', 'בדיקת שחזור גיבויים', WARN, f'הבדיקה נכשלה: {type(e).__name__}'))
    try:
        rows += cron_rows(now)
    except Exception as e:
        rows.append(row('cron', 'משימות cron', WARN, f'הבדיקה נכשלה: {type(e).__name__}'))
    return rows


def overall(rows: list[dict]) -> str:
    return FAIL if any(r['status'] == FAIL for r in rows) else (WARN if any(r['status'] == WARN for r in rows) else OK)
