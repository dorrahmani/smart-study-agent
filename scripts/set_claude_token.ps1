# scripts/set_claude_token.ps1 — put the Claude subscription token on the VM without it passing
# through any chat. Run right AFTER `claude setup-token` printed a token and you copied it (Ctrl+C).
#   powershell -ExecutionPolicy Bypass -File scripts\set_claude_token.ps1
# Reads the token from the CLIPBOARD (pasting into a hidden prompt lost/added a character twice),
# shows only its first/last characters to compare with the screen, sends it over SSH (Tailscale)
# on stdin, and stores it on the VM (config/claude_agent.env + the isolated agent user's copy, 600).
# No backslashes in the remote command: Windows ssh strips them ("\n" arrived as a literal "n"
# appended to the token — that was the 401 "invalid token").

$token = (Get-Clipboard -Raw) -replace '\s', ''      # a wrapped copy adds line breaks — remove them
$m = [regex]::Match($token, 'sk-ant-oat01-[A-Za-z0-9_\-]{80,}')
if (-not $m.Success) {
    Write-Host "No token in the clipboard. Paste it below (right-click pastes in this window), then Enter." -ForegroundColor Yellow
    $typed = (Read-Host "Token") -replace '\s', ''
    $m = [regex]::Match($typed, 'sk-ant-oat01-[A-Za-z0-9_\-]{80,}')
    Clear-Host                                         # don't leave the token visible on screen
    if (-not $m.Success) {
        Write-Host "That isn't a complete Claude token (sk-ant-oat01-..., ~108 characters). Nothing was sent." -ForegroundColor Red
        exit 1
    }
}
$token = $m.Value
Write-Host ("Found a token: {0}...{1}  ({2} characters)" -f $token.Substring(0, 16), $token.Substring($token.Length - 4), $token.Length)
$ok = Read-Host "Does the start and end match what's on the screen? (y/n)"
if ($ok -notin @('y', 'Y', 'yes')) { Write-Host "Cancelled - nothing was sent."; exit 1 }

$key = if ($env:SSH_KEY) { $env:SSH_KEY } else { Join-Path $env:USERPROFILE ".ssh\smart_study.key" }
$server = if ($env:SSA_SERVER) { $env:SSA_SERVER } else { "ubuntu@your-server" }
$remote = 'umask 077; read -r T; T=$(printf %s "$T" | tr -cd "A-Za-z0-9_-"); echo "CLAUDE_CODE_OAUTH_TOKEN=$T" > ~/smart_study_agent/config/claude_agent.env && chmod 600 ~/smart_study_agent/config/claude_agent.env && sudo install -o claudeagent -g claudeagent -m 600 ~/smart_study_agent/config/claude_agent.env /home/claudeagent/.claude_token.env && echo SAVED'
$result = $token | ssh -o BatchMode=yes -i $key $server $remote
$token = $null
Set-Clipboard -Value ' '                              # don't leave the token sitting in the clipboard

if ($result -match "SAVED") {
    Write-Host "Done - the token is on the server (private file). You can close this window." -ForegroundColor Green
} else {
    Write-Host "Something failed. Is Tailscale connected on this PC?" -ForegroundColor Red
}
