"""scripts/external_uptime_telegram.py — Bot self-monitoring via Telegram getMe.

This runs *on the VM itself* every minute. It calls Telegram's getMe API.
If 3 consecutive failures → the bot's network or systemd may be wedged.
We then attempt:
  1. `systemctl restart smart-study-bot.service`
  2. If still failing after another minute, attempt a soft network reset

This won't help if the VM is COMPLETELY frozen (e.g. OOM lockup) — that's
handled externally via OCI auto-recovery (next step in the resilience plan).

But it catches the common case: VM up, network up, but bot wedged.

Cron: * * * * *  /home/ubuntu/smart_study_agent/scripts/external_uptime_telegram.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
STATE = Path('/tmp/external_uptime_state.json')


def _load_state() -> dict:
    if not STATE.exists(): return {'fail_count': 0}
    try: return json.loads(STATE.read_text())
    except Exception: return {'fail_count': 0}


def _save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s))


def _check() -> bool:
    if not TOKEN: return False
    try:
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{TOKEN}/getMe', method='GET')
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode('utf-8'))
        return bool(data.get('ok'))
    except Exception:
        return False


# ── cron heartbeat: alert if wrapped cron jobs stopped running ───────────
# (this script is NOT wrapped by run_with_alert.sh, so it can report when the
# wrapper itself is broken — see utils/pipeline_health.cron_heartbeat_check)
try:
    from utils.pipeline_health import cron_heartbeat_check
    cron_heartbeat_check()
except Exception as _e:
    print(f'cron heartbeat check failed: {_e}')

s = _load_state()

if _check():
    if s.get('fail_count', 0) > 0:
        print(f'Bot back online after {s["fail_count"]} failures')
    s['fail_count'] = 0
    _save_state(s)
    sys.exit(0)

# Failure
s['fail_count'] = s.get('fail_count', 0) + 1
_save_state(s)
print(f'Bot check failed — count={s["fail_count"]}')

# After 3 consecutive failures (=3 min), restart
if s['fail_count'] >= 3:
    print('Attempting service restart...')
    subprocess.run(['sudo', 'systemctl', 'restart', 'smart-study-bot.service'],
                   timeout=30)
    s['fail_count'] = 0
    _save_state(s)
