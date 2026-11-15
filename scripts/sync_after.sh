#!/bin/bash
# sync_after.sh — Push results to Google Drive after pipeline run
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="gdrive:smart-study-agent/data"
LOCAL="$REPO/data"
SYNC_DIRS=(moodle panopto summaries reference assignments homework quizzes notebooklm)
echo "[$(date '+%H:%M:%S')] Syncing local -> Drive (post-run)..."
for dir in "${SYNC_DIRS[@]}"; do
    if [ -d "$LOCAL/$dir" ]; then
        echo "  $dir ..."
        # copy, never sync (global rule): sync would DELETE Drive files that are
        # missing locally. Trade-off: deletions no longer propagate — to remove
        # something for good, delete it on both local and Drive.
        rclone copy "$LOCAL/$dir" "$REMOTE/$dir" --transfers 4 --quiet
    fi
done
echo "[$(date '+%H:%M:%S')] Upload done"

# Obsidian vault → Drive (the PC pulls it into Obsidian: scripts/vault_pull.ps1).
# copy, never sync: nothing is ever deleted on Drive.
VAULT_DIR="$HOME/Documents/Obsidian Vault/Smart Study Agent"
if [ -d "$VAULT_DIR" ]; then
    echo "  obsidian vault ..."
    rclone copy "$VAULT_DIR" "gdrive:smart-study-agent/vault" --transfers 8 --quiet --exclude ".obsidian/workspace*"
fi
