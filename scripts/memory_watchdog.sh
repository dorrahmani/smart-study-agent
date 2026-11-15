#!/bin/bash
# scripts/memory_watchdog.sh — Memory pressure detector + selective restart.
#
# Runs every 5 minutes via cron. Checks:
#   - free RAM < 400 MB        → drop kernel page caches
#   - free RAM < 200 MB        → also restart the restartable services
#   - disk usage > 88%         → alert via WhatsApp (utils.notify; Telegram deleted 10/11)
#   - an ENABLED watched service is down → start it
#
# Whitelist of "restartable" services: WhatsApp worker + dashboard (safe to restart).
# Pipeline NEVER restarted mid-run (would corrupt summaries).
# 10/11/2026: the Telegram bot is gone (unit disabled). This watchdog started it again every 5 minutes because it
# checked "active", not "enabled" — a disabled unit is now never started or restarted here.
#
# Cron: */5 * * * * /home/ubuntu/smart_study_agent/scripts/memory_watchdog.sh
set -u
LOG=/tmp/memory_watchdog.log
REPO=/home/ubuntu/smart_study_agent
RESTARTABLE="smart-study-wa-worker.service dashboard.service"
WATCHED="smart-study-wa-worker.service smart-study-wa-bridge.service dashboard.service"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

# WhatsApp via utils.notify — once a day per text (dedup key); undelivered → data/undelivered_alerts.jsonl
alert() {
    log "ALERT: $1"
    ( cd "$REPO" && WD_MSG="$1" timeout 60 venv/bin/python3 -c "
import os, sys, datetime
sys.path.insert(0, '.')
from utils.notify import notify
m = os.environ['WD_MSG']
notify(m, key=f'watchdog:{datetime.date.today()}:{m[:60]}', markdown=False)
" >/dev/null 2>&1 ) || true
}

enabled() { systemctl is-enabled --quiet "$1" 2>/dev/null; }

# ── 1. Memory check ────────────────────────────────────────────────
FREE_MB=$(free -m | awk '/^Mem:/ {print $7}')
TOTAL_MB=$(free -m | awk '/^Mem:/ {print $2}')

# Pipeline lock check — never restart if pipeline is running
PIPELINE_RUNNING=$(pgrep -f 'daily_full_update' || true)

if [ "$FREE_MB" -lt 200 ]; then
    log "CRITICAL memory: ${FREE_MB} MB available (total ${TOTAL_MB} MB)"
    # Drop page cache (no harm — kernel re-reads from disk)
    sudo sync && echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null 2>&1 || true
    if [ -z "$PIPELINE_RUNNING" ]; then
        log "Restarting $RESTARTABLE to free memory"
        for svc in $RESTARTABLE; do
            if enabled "$svc"; then sudo systemctl restart "$svc" 2>/dev/null || true; fi
        done
        alert "⚠️ VM memory was critical (${FREE_MB}MB free). Auto-restarted the WhatsApp worker + dashboard."
    else
        log "Pipeline is running — NOT restarting services; just dropped caches"
    fi
elif [ "$FREE_MB" -lt 400 ]; then
    log "Memory low: ${FREE_MB} MB available"
    # Just drop caches, no service restart
    sudo sync && echo 1 | sudo tee /proc/sys/vm/drop_caches >/dev/null 2>&1 || true
fi

# ── 2. Disk check ──────────────────────────────────────────────────
DISK_PCT=$(df / | awk 'NR==2 {gsub("%",""); print $5}')
if [ "$DISK_PCT" -gt 88 ]; then
    log "Disk usage: ${DISK_PCT}%"
    alert "💾 דיסק מתמלא: ${DISK_PCT}%. בדוק logs / vault size."
fi

# ── 3. (Telegram bot heartbeat — removed 10/11/2026, the bot is gone) ──

# ── 4. Per-service status check (enabled units only) ───────────────
for svc in $WATCHED; do
    if enabled "$svc"; then                 # disabled = deliberately off → leave it
        if ! systemctl is-active --quiet "$svc"; then
            log "$svc is not active — starting"
            sudo systemctl start "$svc" || true
            alert "🔄 ${svc} היה down. אתחלתי."
        fi
    fi
done

# Rotate log if too big
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    mv "$LOG" "$LOG.old"
fi

exit 0
