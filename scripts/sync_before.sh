#!/bin/bash
# sync_before.sh — Pull latest data from Google Drive before pipeline run
# NOTE: uses rclone COPY (additive) not SYNC — never deletes local-only files.
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="gdrive:smart-study-agent/data"
LOCAL="$REPO/data"
SYNC_DIRS=(moodle panopto summaries reference assignments homework quizzes notebooklm)
echo "[$(date '+%H:%M:%S')] Syncing Drive -> local (pre-run)..."
for dir in "${SYNC_DIRS[@]}"; do
    mkdir -p "$LOCAL/$dir"
    echo "  $dir ..."
    rclone copy "$REMOTE/$dir" "$LOCAL/$dir" --transfers 4 --quiet
done
echo "[$(date '+%H:%M:%S')] Download done"
