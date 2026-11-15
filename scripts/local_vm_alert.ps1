# scripts/local_vm_alert.ps1 — VM watchdog on PC. Alerts via Telegram + popup on lockup.
param(
    [string]$VM_IP = "your-server-public-ip",   # public IP (optional)
    [string]$VM_TAILNET_IP = "your-server-tailnet-ip",  # SSH address (Tailscale)
    [int]$Threshold = 3,
    [int]$CooldownMinutes = 30
)
$ErrorActionPreference = 'SilentlyContinue'
$StateFile = "$env:USERPROFILE\.smart_study_vm_state.json"
$LogFile   = "$env:USERPROFILE\.smart_study_vm.log"

$envPath = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
$token = $null; $chat = $null
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^TELEGRAM_BOT_TOKEN=(.+)$') { $token = $matches[1].Trim('"').Trim("'") }
        if ($line -match '^TELEGRAM_USER_ID=(.+)$')   { $chat  = $matches[1].Trim('"').Trim("'") }
    }
}

function Write-Log { param([string]$m) "$([DateTime]::Now.ToString('s')) $m" | Out-File -Append -Encoding utf8 $LogFile }

function Send-Telegram {
    param([string]$text)
    if (-not $token -or -not $chat) { return }
    try {
        $body = @{ chat_id = $chat; text = $text } | ConvertTo-Json
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 15 | Out-Null
    } catch { Write-Log "Telegram fail: $_" }
}

function Show-Toast {
    param([string]$title, [string]$msg)
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $b = New-Object System.Windows.Forms.NotifyIcon
        $b.Icon = [System.Drawing.SystemIcons]::Warning
        $b.BalloonTipTitle = $title
        $b.BalloonTipText  = $msg
        $b.Visible = $true
        $b.ShowBalloonTip(15000)
        Start-Sleep -Seconds 1
        $b.Dispose()
    } catch {}
}

$state = @{ fail_count = 0; last_alert = 0 }
if (Test-Path $StateFile) {
    try { $state = Get-Content $StateFile -Raw | ConvertFrom-Json -AsHashtable } catch {}
}

# Alive = SSH answers over Tailscale (public SSH closed since 03/10/2026).
# Guard: if this PC itself is off the tailnet, the check is skipped, so a PC whose
# Tailscale is off can never trigger a false auto-reboot of a healthy VM.
$reachable = $false
try {
    $tcpClient = New-Object System.Net.Sockets.TcpClient
    $task = $tcpClient.ConnectAsync($VM_TAILNET_IP, 22)
    $task.Wait(5000) | Out-Null
    $reachable = $tcpClient.Connected
    $tcpClient.Close()
} catch { $reachable = $false }
if (-not $reachable) {
    # The public IP no longer answers anything (no SSH, no ping). If THIS PC isn't on the tailnet,
    # we simply can't tell — skip without counting a failure (never reboot a VM we can't see).
    $ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    & $ts status *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "PC is not connected to Tailscale - skipping VM check (no failure counted)"
        exit 0
    }
}

if ($reachable) {
    if ($state.fail_count -gt 0) {
        Write-Log "VM back online after $($state.fail_count) failures"
        Send-Telegram "VM is back online (was down $($state.fail_count) checks). Smart Study Agent recovered."
    }
    $state.fail_count = 0
} else {
    $state.fail_count = [int]$state.fail_count + 1
    Write-Log "VM ping failed (count=$($state.fail_count))"
    if ($state.fail_count -ge $Threshold) {
        $now = [int][double]::Parse((Get-Date -UFormat %s))
        $lastAlert = if ($state.last_alert) { [int]$state.last_alert } else { 0 }
        if (($now - $lastAlert) -ge ($CooldownMinutes * 60)) {
            Write-Log "VM down for $($state.fail_count) checks - attempting auto-reboot via OCI CLI"
            Send-Telegram "VM is unreachable. Attempting auto-reboot via OCI API..."

            $instanceId = 'ocid1.instance.oc1.il-jerusalem-1.anwxiljrduggmzaclrtaike7khfkxknlow5wthbr53zzu3tsbl2uj3lrxhla'
            $ociPath = "$env:APPDATA\Python\Python314\Scripts\oci.exe"
            $env:SUPPRESS_LABEL_WARNING = 'True'

            # Try soft reset first, then force reset if needed
            $rebootOk = $false
            try {
                $out = & $ociPath compute instance action --instance-id $instanceId --action SOFTRESET --query 'data.\"lifecycle-state\"' 2>&1 | Out-String
                Write-Log "SOFTRESET output: $out"
                if ($out -match 'RUNNING|STARTING|REBOOTING|RUNNING') { $rebootOk = $true }
            } catch { Write-Log "SOFTRESET failed: $_" }

            if (-not $rebootOk) {
                try {
                    Write-Log "Trying RESET (force)..."
                    $out = & $ociPath compute instance action --instance-id $instanceId --action RESET --query 'data.\"lifecycle-state\"' 2>&1 | Out-String
                    Write-Log "RESET output: $out"
                    if ($out -match 'RUNNING|STARTING|REBOOTING') { $rebootOk = $true }
                } catch { Write-Log "RESET failed: $_" }
            }

            if ($rebootOk) {
                Send-Telegram "VM auto-rebooted via OCI API. Should be back within 2 minutes."
                Show-Toast "Smart Study VM" "Auto-rebooted. Recovery in 2 min."
            } else {
                Send-Telegram "VM DOWN - auto-reboot FAILED. Manual action needed: Force reboot in OCI Console."
                Show-Toast "Smart Study VM Down" "Auto-reboot failed - check OCI Console manually."
            }
            $state.last_alert = $now
            $state.fail_count = 0
        } else { Write-Log "Cooldown - no alert/reboot" }
    }
}

$state | ConvertTo-Json | Out-File -Encoding utf8 $StateFile
