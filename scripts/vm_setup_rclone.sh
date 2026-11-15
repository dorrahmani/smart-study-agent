#!/bin/bash
# vm_setup_rclone.sh — Run once on the Oracle VM to install rclone + configure Drive access
# Usage: bash vm_setup_rclone.sh
# Requires: internet access on the VM

set -e

echo "=== Installing rclone ==="
curl -fsSL https://rclone.org/install.sh | sudo bash

echo "=== Writing rclone config ==="
mkdir -p ~/.config/rclone

# The Drive credentials are NOT stored in this repo (they used to be — a
# full-Drive-scope refresh token; rotated 09/2026). Two ways to provide them:
#   a) copy your working config from the PC:
#        scp ~/.config/rclone/rclone.conf ubuntu@<vm>:.config/rclone/rclone.conf
#   b) headless OAuth: on the PC run  `rclone authorize "drive"`, then on the VM
#        run `rclone config`, choose remote "gdrive", type drive, and paste the token.
if [ ! -s ~/.config/rclone/rclone.conf ]; then
  echo "No ~/.config/rclone/rclone.conf yet - provide it (see comments above), then re-run."
  exit 1
fi

chmod 600 ~/.config/rclone/rclone.conf
echo "=== Testing Drive connection ==="
rclone lsd gdrive:smart-study-agent --max-depth 1

echo "=== Creating local data directories ==="
REPO="$HOME/smart_study_agent"
DATA="$REPO/data"
for dir in moodle panopto summaries reference assignments homework quizzes notebooklm; do
    mkdir -p "$DATA/$dir"
done

echo ""
echo "=== Setup complete! ==="
echo "Next steps:"
echo "  1. Run: bash $REPO/scripts/sync_before.sh   (to pull data from Drive)"
echo "  2. Run your pipeline"
echo "  3. Run: bash $REPO/scripts/sync_after.sh    (to push results to Drive)"
