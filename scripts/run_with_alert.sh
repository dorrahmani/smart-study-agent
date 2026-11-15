#!/bin/bash
# scripts/run_with_alert.sh — Wrapper that runs a command and alerts via WhatsApp (utils.notify) on failure.
# 10/11/2026: the Telegram curl is gone (bot deleted). If WhatsApp can't take it, utils.notify keeps it in
# data/undelivered_alerts.jsonl (shown on the dashboard + in the weekly bot report).
# Usage: run_with_alert.sh <name> <command> [args...]
# Example: run_with_alert.sh "Daily Pipeline" /home/ubuntu/smart_study_agent/scripts/run_daily.sh

NAME="$1"
shift
LOG=$(mktemp /tmp/alert_log_XXXXXX.txt)
START=$(date '+%Y-%m-%d %H:%M:%S')

# Run command, capture output + exit code
"$@" > "$LOG" 2>&1
RC=$?
END=$(date '+%Y-%m-%d %H:%M:%S')

# Always: append to /tmp/cron_health.log
{
    echo "[$START → $END] $NAME exit=$RC"
} >> /tmp/cron_health.log

# 13/10: a failure goes to WhatsApp, once a day per job (utils.notify dedups by
# key and holds it during quiet hours).
if [ "$RC" -ne 0 ]; then
    ( cd /home/ubuntu/smart_study_agent && CRON_NAME="$NAME" CRON_RC="$RC" CRON_TAIL="$(tail -c 600 "$LOG")" \
      venv/bin/python3 -c "
import os, sys, datetime
sys.path.insert(0, '.')
from utils.notify import notify
n, rc, tail = os.environ['CRON_NAME'], os.environ['CRON_RC'], os.environ.get('CRON_TAIL', '')
notify(f'⚠️ משימה מתוזמנת נכשלה: {n} (קוד {rc})\n\n{tail[-400:]}', key=f'cron-fail:{n}:{datetime.date.today()}')
" >/dev/null 2>&1 ) || true
fi

# 13/10: the output used to be thrown away (the per-job logs/*.log stayed empty) — pass it on to the caller.
cat "$LOG"
rm -f "$LOG"
exit "$RC"
