"""
scripts/nlm_keepalive.py — watch the server's NotebookLM login, renew it only when it needs it, and say so once when it
really died (14/11, reworked 15/11).

14/11 it rotated the cookies every 20 min (`auth refresh`, as notebooklm-py advises for idle profiles). It did not
hold: ok 20:20–21:40 UTC, dead from 22:00 UTC, while the PC's Chrome profile — same Google session (same SID) — was
still signed in the next morning and re-minted at once. Nothing else touched the session in that window (no job, no
PC run), and the library's cookie saves are locked delta merges, so it wasn't a write race. With rotation only on real
use (before 14/11) the copy lived 1–3 days; with rotation every 20 min it lived 2 h. So: Google drops the server's COPY
of a session that the PC's Chrome owns, and rotating it from the server more often doesn't help. The server now
rotates only when it has to:

  1. read-only probe: `notebooklm auth check --test --passive` (no rotation, no write)      → ok: done
  2. probe failed → ONE `auth refresh --verify` (rotation + the library's own recovery)       → ok: done
  3. still failing → classify:
       AUTH_ERROR                        → dead: ONE WhatsApp alert per Israel day, from the first dead run
       NETWORK / RATE_LIMITED / timeout  → transient: never an alert
       anything else (12/11's CSRF one)  → dead after 3 in a row (~1 h)
  ok after an alert → one "back" line.
Each run logs when __Secure-1PSIDTS was last set (from its 1-year expiry; never a value), so the next death shows how
old the server's cookies were when Google dropped them.

The fix itself comes from the PC (scripts/pc/nlm_session_refresh.py, task SmartStudy_NLM_Session) — or, durably, the
library's master-token login (docs/RUNBOOK.md).

Cron (every 20 min):  */20 * * * * cd ~/smart_study_agent && venv/bin/python3 scripts/nlm_keepalive.py >> logs/nlm_keepalive.log 2>&1
No LLM. Exit code is always 0 (this is the alert; the cron wrapper must not double it).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STATE = ROOT / 'data' / 'nlm_session.json'
STORAGE = Path.home() / '.notebooklm' / 'profiles' / 'default' / 'storage_state.json'
IL = ZoneInfo('Asia/Jerusalem')
TIMEOUT = 150
TRANSIENT = {'NETWORK_ERROR', 'RATE_LIMITED', 'TIMEOUT'}
OTHER_FAILS_TO_DEAD = 3
CLI = [sys.executable, '-m', 'notebooklm', '--quiet']


def classify(returncode: int | None, stdout: str) -> str:
    """`auth refresh --verify --json` → 'ok' | the library's error code ('AUTH_ERROR', …) | 'TIMEOUT' | 'UNKNOWN'."""
    if returncode is None:
        return 'TIMEOUT'
    try:
        out = json.loads((stdout or '').strip() or '{}')
    except ValueError:
        out = {}
    if returncode == 0 and out.get('status') == 'ok' and out.get('verified', True):
        return 'ok'
    code = str(out.get('code') or '').upper()
    if not code and 'authentication expired' in (stdout or '').lower():
        code = 'AUTH_ERROR'
    return code or 'UNKNOWN'


def probe_ok(returncode: int | None, stdout: str) -> bool:
    """`auth check --test --passive --json` → the session answers (read-only)."""
    try:
        d = json.loads((stdout or '').strip() or '{}')
    except ValueError:
        return False
    return returncode == 0 and d.get('status') == 'ok' and (d.get('checks') or {}).get('token_fetch') is True


def decide(result: str, st: dict) -> tuple[dict, str | None]:
    """The new state and what to tell the owner: None | 'dead' | 'back'. Pure."""
    st = dict(st)
    if result == 'ok':
        return {'last_ok': st.get('now')}, ('back' if st.get('alerted') else None)
    st['last_error'] = result
    if result in TRANSIENT:
        return st, None
    st['fails'] = int(st.get('fails') or 0) + 1
    st.setdefault('dead_since', st.get('now'))
    if result == 'AUTH_ERROR' or st['fails'] >= OTHER_FAILS_TO_DEAD:
        return st, 'dead'
    return st, None


def il(ts: str | None) -> str:
    """ISO server time (UTC on the VM) → 'HH:MM' Israel."""
    try:
        d = datetime.fromisoformat(ts)
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return d.astimezone(IL).strftime('%d/%m %H:%M')
    except (TypeError, ValueError):
        return ''


def cookie_facts(path: Path | None = None) -> dict:
    """When __Secure-1PSIDTS was last set (its expiry is set+1 year). Names and times only — never a value."""
    try:
        d = json.loads((path or STORAGE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    exp = [c.get('expires') for c in d.get('cookies', []) if c.get('name') == '__Secure-1PSIDTS'
           and str(c.get('domain', '')).endswith('google.com') and (c.get('expires') or 0) > 0]
    if not exp:
        return {'psidts_set': None}
    return {'psidts_set': (datetime.fromtimestamp(max(exp), timezone.utc) - timedelta(days=365))
            .replace(tzinfo=None).isoformat(timespec='minutes')}


def alert_text(since: str | None) -> str:
    from agent.known_alerts import how_to
    when = il(since)
    head = (f"⚠️ NotebookLM: ההתחברות בשרת פגה{f' (מאז {when})' if when else ''}. "
            'המשימה SmartStudy_NLM_Session במחשב מחדשת אותה לבד כשהמחשב דולק ומחובר; אם עד הבוקר לא חזר — לחדש ידנית:')
    return head + '\n\n' + (how_to('⚠️ NotebookLM לא עונה — איך לבצע?') or '')


def master_token_path() -> Path:
    home = os.environ.get('NOTEBOOKLM_HOME')
    base = Path(home) if home else Path.home() / '.notebooklm'
    return base / 'profiles' / 'default' / 'master_token.json'


def _cli(*args: str) -> tuple[int | None, str]:
    try:
        p = subprocess.run([*CLI, *args, '--json'], capture_output=True, text=True, timeout=TIMEOUT, cwd=str(ROOT))
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        return None, ''


def check() -> tuple[str, str]:
    """(result, how): 'ok'/'passive' when the read-only probe answers; else one refresh and its classification."""
    if probe_ok(*_cli('auth', 'check', '--test', '--passive')):
        return 'ok', 'passive'
    result = classify(*_cli('auth', 'refresh', '--verify'))
    if result != 'ok' and master_token_path().exists():
        # 17/11: with a master token the server mints its own Google session (no PC involved) — tried before any alert
        try:
            subprocess.run([*CLI, 'login', '--master-token-refresh'], capture_output=True, text=True,
                           timeout=TIMEOUT, cwd=str(ROOT))
        except subprocess.TimeoutExpired:
            pass
        again = classify(*_cli('auth', 'refresh', '--verify'))
        if again == 'ok':
            return 'ok', 'master-token'
        return again, 'master-token-failed'
    return result, 'refresh'


def main(state_path: Path | None = None, run=check, send=None, now: datetime | None = None,
         facts=cookie_facts) -> str:
    path = state_path or STATE
    now = now or datetime.now()
    try:
        st = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        st = {}
    st['now'] = now.isoformat(timespec='seconds')
    result, how = run()
    new, tell = decide(result, st)
    f = facts() or {}
    if 'psidts_set' in f:
        new['psidts_set'] = f.get('psidts_set')
    if send is None:
        from utils.notify import notify as send
    day = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(IL).date().isoformat()
    if tell == 'dead':
        if send(alert_text(new.get('dead_since')), key=f'nlm-auth:{day}') in ('sent', 'queued', 'dup'):
            new['alerted'] = True
    elif tell == 'back':        # 16/11 off by default (agent/noise 'system_ok'): utils/notify logs it, "תחזיר … חזר לעבוד" sends
        send('✅ NotebookLM חזר לעבוד בשרת.', key=f'nlm-auth-back:{now.isoformat(timespec="minutes")}')
    new.pop('now', None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(new, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)
    print(f'{now:%Y-%m-%d %H:%M} nlm keepalive: {result} ({how}; psidts set {new.get("psidts_set") or "?"})'
          + (f' → {tell}' if tell else ''))
    return result


if __name__ == '__main__':
    main()
    sys.exit(0)
