#!/bin/bash
# scripts/backup_to_drive.sh — Daily backup of critical data to Google Drive.
# Runs from cron 04:00 UTC = 07:00 Israel daily.
set -e

REPO="/home/ubuntu/smart_study_agent"
REMOTE="${BACKUP_REMOTE:-gdrive:smart-study-agent-backup}"  # override for test runs
DATESTAMP=$(date -u +'%Y-%m-%d')

# ── Folders to backup ─────────────────────────────────────────────────────
BACKUP_PATHS=(
  "data/summaries"
  "data/homework"
  "data/assignments"
  "data/chat_history.json"
  "data/vault_index.tfidf.json"
  "data/sent_log.json"
  "config/sent_log.json"
  "config/courses.yaml"
  "config/professors.yaml"
  "config/notebooklm_notebooks.json"
)

# ── Secrets: NEVER uploaded in plaintext ──────────────────────────────────
# Until 09/2026 .env (TAU password + every API key) and the Gmail token went to
# Drive as-is. Now they are tarred and GPG-encrypted to a public key; the
# PRIVATE key exists only on the owner's PC (see docs/RUNBOOK.md, "restore secrets").
# Missing key → the backup FAILS (alert) instead of falling back to plaintext.
BACKUP_GPG_RECIPIENT="${BACKUP_GPG_RECIPIENT:-}"   # set in .env: your GPG key fingerprint (gpg --list-keys)
SECRET_REPO_PATHS=(".env" "config/secrets" "config/gmail_token.pickle" "config/gcal_token.json" "config/wa_bridge.env" "data/wa_bridge")
SECRET_HOME_PATHS=(".config/rclone/rclone.conf" ".notebooklm/profiles/default/storage_state.json" ".notebooklm/storage_state.json")

echo "[$(date '+%H:%M:%S')] === Daily backup to $REMOTE/$DATESTAMP ==="

cd "$REPO"

# Each path keeps its own place in the backup (restore = copy it straight back).
# `rclone copy DIR DEST` copies DIR's *contents*, so a directory goes to
# DEST/<its own path>; a file goes to DEST/<its parent dir>. Until 09/2026 every
# directory was dumped into its parent (summaries + homework + assignments all
# merged under data/, secrets merged into config/, .env under a mangled "．" dir).
FAILED=0
for p in "${BACKUP_PATHS[@]}"; do
  [ -e "$p" ] || continue
  if [ -d "$p" ]; then
    dest="$REMOTE/$DATESTAMP/$p"
  else
    rel_dir=$(dirname "$p")
    if [ "$rel_dir" = "." ]; then dest="$REMOTE/$DATESTAMP"; else dest="$REMOTE/$DATESTAMP/$rel_dir"; fi
  fi
  echo "  ⬆️  $p"
  if ! rclone copy "$p" "$dest" --transfers 4 --quiet 2>&1; then
    echo "    ⚠️  rclone failed for $p"
    FAILED=$((FAILED + 1))
  fi
done

# Obsidian vault ("second brain") — lives outside the repo; was never backed up
# until 09/2026 (the Gemini concept-enrichment layer can't be rebuilt for free).
VAULT_DIR="$HOME/Documents/Obsidian Vault/Smart Study Agent"
if [ -d "$VAULT_DIR" ]; then
  echo "  ⬆️  obsidian-vault"
  if ! rclone copy "$VAULT_DIR" "$REMOTE/$DATESTAMP/obsidian-vault" --transfers 8 --quiet          --exclude ".obsidian/workspace*" 2>&1; then
    echo "    ⚠️  rclone failed for obsidian-vault"; FAILED=$((FAILED + 1))
  fi
fi

# encrypted secrets bundle
SECRETS_OUT=$(mktemp /tmp/secrets_XXXXXX.tar.gz.gpg)
rm -f "$SECRETS_OUT"   # gpg refuses to overwrite; mktemp only reserved a unique name
repo_items=(); for s in "${SECRET_REPO_PATHS[@]}"; do [ -e "$REPO/$s" ] && repo_items+=("$s"); done
home_items=(); for s in "${SECRET_HOME_PATHS[@]}"; do [ -e "$HOME/$s" ] && home_items+=("$s"); done
echo "  🔐 secrets (${#repo_items[@]} repo + ${#home_items[@]} home items) → secrets.tar.gz.gpg"
set -o pipefail   # a tar failure must fail the pipeline, not just gpg's exit code
if tar -czf - -C "$REPO" "${repo_items[@]}" -C "$HOME" "${home_items[@]}" |
     gpg --batch --yes --trust-model always --encrypt \
         --recipient "$BACKUP_GPG_RECIPIENT" -o "$SECRETS_OUT" &&
   [ -s "$SECRETS_OUT" ] &&
   rclone copyto "$SECRETS_OUT" "$REMOTE/$DATESTAMP/secrets.tar.gz.gpg" --quiet; then
  :
else
  echo "    ⚠️  encrypted secrets backup FAILED (key $BACKUP_GPG_RECIPIENT imported? gpg --list-keys)"
  FAILED=$((FAILED + 1))
fi
rm -f "$SECRETS_OUT"

# ── Cleanup: keep last 14 days of daily backups ───────────────────────────
echo "[$(date '+%H:%M:%S')] Cleaning old backups (>14 days)..."
CUTOFF=$(date -u -d '14 days ago' +'%Y-%m-%d')
rclone lsd "$REMOTE/" 2>/dev/null | awk '{print $NF}' | while read -r d; do
  if [[ "$d" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && [[ "$d" < "$CUTOFF" ]]; then
    echo "  🗑️  removing $d"
    rclone purge "$REMOTE/$d" --quiet 2>&1 || true
  fi
done

if [ "$FAILED" -gt 0 ]; then
  # non-zero exit → run_with_alert.sh sends a WhatsApp alert (was silent before)
  echo "[$(date '+%H:%M:%S')] ❌ Backup finished with $FAILED failed path(s)"; exit 1
fi
echo "[$(date '+%H:%M:%S')] ✅ Backup complete"
