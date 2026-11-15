# scripts/vault_pull.ps1 — bring the server's "second brain" into Obsidian on this PC.
#
# The server builds the vault nightly and mirrors it to gdrive:smart-study-agent/vault
# (scripts/sync_after.sh). This pulls it into the local Obsidian folder.
#   * copy, never sync  — nothing local is ever deleted (notes you add stay)
#   * --update          — a note you edited here after the server's version is NOT overwritten
#   * .obsidian/ skipped — your Obsidian settings/layout stay yours
# Scheduled by the "SmartStudy_VaultPull" Windows task (daily 08:30 + at logon).
$ErrorActionPreference = "Continue"
$rclone = "$env:LOCALAPPDATA\rclone\rclone.exe"
$dst    = "$env:USERPROFILE\Documents\Obsidian Vault\Smart Study Agent"
$log    = Join-Path $env:LOCALAPPDATA "smart-study-vault-pull.log"

"[$(Get-Date -Format s)] vault pull start" | Out-File -Append -Encoding utf8 $log
& $rclone copy "gdrive:smart-study-agent/vault" $dst --update --transfers 8 `
    --exclude ".obsidian/**" --stats-one-line --stats 0 2>&1 |
    Out-File -Append -Encoding utf8 $log
$code = $LASTEXITCODE

# 02/11: notes the server retired (a duplicate lesson, a misfiled one) are listed in vault_removed.txt — copy never
# deletes, so they're moved aside here too, into the hidden .replaced\ folder (never deleted).
$removed = Join-Path $dst "vault_removed.txt"
if (Test-Path -LiteralPath $removed) {
    foreach ($rel in Get-Content -LiteralPath $removed -Encoding utf8) {
        if (-not $rel.Trim()) { continue }
        $src = Join-Path $dst ($rel -replace '/', '\')
        if (Test-Path -LiteralPath $src) {
            $to = Join-Path (Join-Path $dst ".replaced") ($rel -replace '/', '\')
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
            Move-Item -LiteralPath $src -Destination $to -Force
            "  moved aside: $rel" | Out-File -Append -Encoding utf8 $log
        }
    }
}
"[$(Get-Date -Format s)] vault pull done (exit $code)" | Out-File -Append -Encoding utf8 $log
