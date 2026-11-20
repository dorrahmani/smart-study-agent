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

# 16/11 the other direction, for ONE folder only: the notes the owner writes himself ("✍️ שלי", i.e. "Mine") go up to
# gdrive:smart-study-agent/vault_inbox, where the server picks them up (vault_sync/mine.py: never deletes, a conflict keeps
# both). The server never writes into that folder, and this pull skips it, so nothing he writes is ever overwritten.
# The name is built from code points: Windows PowerShell 5 reads a BOM-less script as ANSI.
$mineName = [string][char]0x270D + [char]0xFE0F + " " + [char]0x05E9 + [char]0x05DC + [char]0x05D9
$mine = Join-Path $dst $mineName
if (-not (Test-Path -LiteralPath $mine)) {
    New-Item -ItemType Directory -Force -Path $mine | Out-Null
    $inbox = Join-Path $mine "Inbox.md"
    @("# Inbox", "",
      ("- [ ] " + [char]0x05EA + [char]0x05D6 + [char]0x05DB + [char]0x05D9 + [char]0x05E8 + " " + [char]0x05DC + [char]0x05D9 + " ..."),   # parenthesised: the comma binds tighter than + (it split the line into one letter per line)
      "") | Out-File -Encoding utf8 $inbox
}
# the notes that exist here now: the server stops answering from one he deleted (moves it aside, never deletes)
# 17/11: the filter was "-notmatch" with a regex that in practice matched every path, so the manifest was always empty and
# a note he deleted here never left the server. Now no regex: the relative path, and no part of it may start with a dot
# (.obsidian, .replaced, the manifest itself).
$manifest = Join-Path $mine ".ssa_manifest.txt"
$rels = @(Get-ChildItem -LiteralPath $mine -Recurse -File |
    ForEach-Object { $_.FullName.Substring($mine.Length + 1) } |
    Where-Object { -not ($_.Split([char]92) | Where-Object { $_.StartsWith('.') }) })
# empty (no notes at all): the server then retires nothing; it never reads an empty manifest as "all deleted"
if ($rels.Count -gt 0) { $rels | Out-File -Encoding utf8 $manifest } else { "" | Out-File -Encoding utf8 $manifest }
& $rclone copy $mine "gdrive:smart-study-agent/vault_inbox" --update --transfers 4 `
    --exclude ".obsidian/**" --exclude ".replaced/**" --stats-one-line --stats 0 2>&1 |
    Out-File -Append -Encoding utf8 $log

# 17/11 keep both: a note here that the server's newer copy would replace is first moved into .replaced\pull-<date>\
# (hidden in Obsidian, never deleted), so an edit he made outside his own folder is never lost. .replaced\ isn't pulled.
$backup = Join-Path (Join-Path $dst ".replaced") ("pull-" + (Get-Date -Format "yyyyMMdd"))
& $rclone copy "gdrive:smart-study-agent/vault" $dst --update --transfers 8 --backup-dir $backup `
    --exclude ".obsidian/**" --exclude "/.replaced/**" --exclude "/$mineName/**" --stats-one-line --stats 0 2>&1 |
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
