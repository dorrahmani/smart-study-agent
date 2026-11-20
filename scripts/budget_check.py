"""scripts/budget_check.py — Daily check on this month's API spend.

Reads data/usage.jsonl, sums spend since the 1st of this month.
Thresholds:
  - $20 → ⚠️ soft warning (one-time per month)
  - $50 → ⛔ critical: pause summarization by writing data/budget_lock.json

The summarizer / pipeline reads budget_lock.json and refuses to call Gemini
if locked. User must delete the lock file manually (or send "בטל מגבלת תקציב" via bot).

Cron suggestion (daily 13:00 UTC = 16:00 Israel):
  0 13 * * *  /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Budget Check" \
    bash -c "cd /home/ubuntu/smart_study_agent && venv/bin/python3 scripts/budget_check.py"
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
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

from utils.usage_tracker import summary as cost_summary

SOFT_LIMIT_USD = 20.0
HARD_LIMIT_USD = 50.0
NOTIFIED_PATH = ROOT / 'data' / 'budget_notified.json'  # tracks notifications sent this month
LOCK_PATH = ROOT / 'data' / 'budget_lock.json'


def _send(text: str) -> None:
    from utils.notify import deliver            # WhatsApp only (10/11: Telegram deleted)
    deliver(text, markdown=True)


def _load_notified() -> dict:
    if not NOTIFIED_PATH.exists(): return {}
    try: return json.loads(NOTIFIED_PATH.read_text(encoding='utf-8'))
    except Exception: return {}


def _save_notified(d: dict) -> None:
    NOTIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTIFIED_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                             encoding='utf-8')


from utils.budget import month_cap
month_key = datetime.utcnow().strftime('%Y-%m')
# 19/11 a month can have its own cap (config/budget_caps.json); HARD_LIMIT_USD is the default
HARD_LIMIT_USD = month_cap(month_key, HARD_LIMIT_USD)
SOFT_LIMIT_USD = min(SOFT_LIMIT_USD, round(HARD_LIMIT_USD * 0.4, 2))
s = cost_summary('month')
total = float(s.get('total_usd', 0))
n_calls = int(s.get('n_calls', 0))
audio_min = float(s.get('audio_minutes_total', 0))
month_key = datetime.utcnow().strftime('%Y-%m')

print(f'Month {month_key}: ${total:.2f} ({n_calls} calls, {audio_min:.0f} audio min)')

notified = _load_notified()
notify_state = notified.get(month_key, {})

# Hard limit → lock
if total >= HARD_LIMIT_USD and not notify_state.get('hard'):
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(json.dumps({
        'locked_at':  datetime.utcnow().isoformat() + 'Z',
        'reason':     f'Monthly spend ${total:.2f} exceeded hard limit ${HARD_LIMIT_USD}',
        'month':      month_key,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    _send(
        f'⛔ *תקציב חודשי חרג ל-${total:.2f}!*\n\n'
        f'מגבלה קשיחה: ${HARD_LIMIT_USD}\n\n'
        f'*Summarization הוקפא* — כדי לחדש:\n'
        f'  • שלח לי: "בטל מגבלת תקציב"\n'
        f'  • או מחק `data/budget_lock.json`\n\n'
        f'*פירוט החודש:*\n' +
        '\n'.join(f'  • `{m}`: ${d["cost_usd"]:.2f}'
                  for m, d in list(s.get('by_model', {}).items())[:5])
    )
    notify_state['hard'] = True
    notified[month_key] = notify_state
    _save_notified(notified)
    print('⛔ HARD LIMIT — wrote lock file and sent alert.')
elif total >= SOFT_LIMIT_USD and not notify_state.get('soft'):
    _send(
        f'⚠️ *התקציב החודשי הגיע ל-${total:.2f}*\n\n'
        f'(התראה רכה ב-${SOFT_LIMIT_USD}, מגבלה קשיחה ב-${HARD_LIMIT_USD})\n'
        f'{n_calls} קריאות API החודש{f", {audio_min:.0f} דקות תמלול" if audio_min > 0 else ""}.\n\n'
        f'*הגדולים ביותר:*\n' +
        '\n'.join(f'  • `{m}`: ${d["cost_usd"]:.2f}'
                  for m, d in list(s.get('by_model', {}).items())[:3])
    )
    notify_state['soft'] = True
    notified[month_key] = notify_state
    _save_notified(notified)
    print(f'⚠️ Soft warning sent.')
else:
    print(f'✅ Below thresholds (or already notified).')
