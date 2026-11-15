#!/bin/bash

# Semester window: single source of truth = config/courses.yaml (settings.schedule),
# read via utils/semester.py. Exit 3 = skip. A gate ERROR runs anyway (fail-open,
# the pipeline is harmless when there is nothing new) and says so in the log.
GATE_MSG=$(cd /home/ubuntu/smart_study_agent && venv/bin/python3 -m utils.semester --daily 2>&1)
GATE_RC=$?
if [ "$GATE_RC" -eq 3 ]; then
  echo "$(date) run_daily: $GATE_MSG - skipping"; exit 0
elif [ "$GATE_RC" -ne 0 ]; then
  echo "$(date) run_daily: semester gate error (rc=$GATE_RC): $GATE_MSG - running anyway"
fi
# Daily Smart Study Agent pipeline runner
set -e

cd /home/ubuntu/smart_study_agent

# ── No-class day check ────────────────────────────────────────────────────
# If today is in data/no_class_days.json, skip the pipeline entirely.
TODAY=$(date +'%Y-%m-%d')
NC_FILE="/home/ubuntu/smart_study_agent/data/no_class_days.json"
if [ -f "$NC_FILE" ] && grep -q "\"$TODAY\"" "$NC_FILE" 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] ⛔ $TODAY מסומן כ'ללא לימודים' — דילוג." \
        >> /home/ubuntu/smart_study_agent/data/daily_full_log.txt
    exit 0
fi

# 1. Pull latest data from Google Drive
echo "[$(date '+%H:%M:%S')] === Sync from Drive ===" >> data/daily_full_log.txt
bash /home/ubuntu/smart_study_agent/scripts/sync_before.sh >> data/daily_full_log.txt 2>&1

# 2. Run the pipeline (includes Drive upload in step 10)
source venv/bin/activate
SSA_STDOUT_IS_LOG=1 PYTHONIOENCODING=utf-8 python3 -m pipelines.daily_full_update >> data/daily_full_log.txt 2>&1
