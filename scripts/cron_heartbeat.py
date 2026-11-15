"""scripts/cron_heartbeat.py — NOT wrapped by run_with_alert.sh: WhatsApp alerts that must not depend on the wrapper.

10/11/2026: two dead-man switches lived in Telegram jobs that are gone with the bot —
  * the cron heartbeat (wrapped jobs stopped running) ran inside external_uptime_telegram.py, every minute;
  * "last evening's pipeline didn't run / didn't finish" headed the Telegram morning briefing.
Both run here now, each alert once (utils.notify dedups by key; the pipeline one once a day, after 07:00 Israel):
    */15 * * * * /home/ubuntu/smart_study_agent/venv/bin/python3 /home/ubuntu/smart_study_agent/scripts/cron_heartbeat.py >> /tmp/cron_heartbeat.log 2>&1
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass


def missed_run_check(now: datetime | None = None, send=None) -> str | None:
    """The morning dead-man switch: the pipeline warning → WhatsApp once a day (07:00–22:00 Israel). → what was sent."""
    from zoneinfo import ZoneInfo
    now = now or datetime.now(ZoneInfo('Asia/Jerusalem'))
    if not 7 <= now.hour < 22:
        return None
    from utils.pipeline_health import missed_run_warning
    from utils.semester import load_config
    w = missed_run_warning(ROOT / 'data', load_config(ROOT))
    if not w:
        return None
    if send is None:
        from utils.notify import notify
        send = lambda text, key: notify(text, key=key, markdown=False)
    send(w, f'missed-run:{now.date()}')
    return w


if __name__ == '__main__':
    try:
        from utils.pipeline_health import cron_heartbeat_check
        cron_heartbeat_check()
    except Exception as e:
        print(f'cron heartbeat check failed: {e}')
    try:
        missed_run_check()
    except Exception as e:
        print(f'missed-run check failed: {e}')
