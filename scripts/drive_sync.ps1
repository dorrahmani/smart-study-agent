#!/usr/bin/env pwsh
# drive_sync.ps1 - Sync smart_study_agent data to/from Google Drive
# Drive remote: gdrive:smart-study-agent/data/
# Usage: .\drive_sync.ps1 [up|down|status]
# Uses rclone COPY, never SYNC: "down" with sync once deleted local summaries
# that were not yet on Drive. Deletions no longer propagate — delete on both sides.

$rclone   = "$env:LOCALAPPDATA\rclone\rclone.exe"
$localDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "data")
$remote   = "gdrive:smart-study-agent/data"

# Directories to sync (excludes logs, temp, large indexes)
$syncDirs = @(
    "moodle",
    "panopto",
    "summaries",
    "reference",
    "assignments",
    "homework",
    "quizzes",
    "notebooklm"
)

$mode = if ($args.Count -gt 0) { $args[0] } else { "up" }

switch ($mode) {
    "up" {
        Write-Host "Syncing local to Drive..."
        foreach ($dir in $syncDirs) {
            $src = Join-Path $localDir $dir
            $dst = "$remote/$dir"
            if (Test-Path $src) {
                Write-Host "  $dir ..."
                & $rclone copy $src $dst --progress --transfers 4 2>&1 | Where-Object { $_ -notmatch "^$" }
            }
        }
        Write-Host "Upload done"
    }
    "down" {
        Write-Host "Syncing Drive to local..."
        foreach ($dir in $syncDirs) {
            $src = "$remote/$dir"
            $dst = Join-Path $localDir $dir
            New-Item -ItemType Directory -Force -Path $dst | Out-Null
            Write-Host "  $dir ..."
            & $rclone copy $src $dst --progress --transfers 4 2>&1 | Where-Object { $_ -notmatch "^$" }
        }
        Write-Host "Download done"
    }
    "status" {
        Write-Host "Drive vs local differences:"
        $totalDiffs = 0
        foreach ($dir in $syncDirs) {
            $src = Join-Path $localDir $dir
            $dst = "$remote/$dir"
            if (Test-Path $src) {
                # --combined outputs lines prefixed: = (same) + (local only) - (remote only) * (different)
                $lines = & $rclone check $src $dst --combined - 2>&1
                $diffs = $lines | Where-Object { $_ -match '^[+\-\*] ' }
                if ($diffs.Count -gt 0) {
                    Write-Host "  $dir : $($diffs.Count) diff(s)"
                    $diffs | ForEach-Object { Write-Host "    $_" }
                    $totalDiffs += $diffs.Count
                } else {
                    Write-Host "  $dir : in sync"
                }
            }
        }
        if ($totalDiffs -eq 0) {
            Write-Host "All directories are in sync with Drive"
        } else {
            Write-Host "$totalDiffs total diff(s) - run 'up' to push local changes"
        }
    }
    default {
        Write-Host "Usage: drive_sync.ps1 [up|down|status]"
        Write-Host "  up     - upload local to Drive (default)"
        Write-Host "  down   - download Drive to local"
        Write-Host "  status - show differences"
    }
}
