# scripts/pc/install_nlm_session_task.ps1 — register the "SmartStudy_NLM_Session" Windows task (14/11, 15/11).
#
# Copies nlm_session_refresh.py to %USERPROFILE%\.smart_study\ (so the task never depends on which branch the repo is
# on; not AppData — Claude Desktop virtualizes AppData writes, a task would not see them) and runs it with pythonw
# (no window): every 4 hours (00/04/08/12/16/20; a start missed while the PC slept runs as soon as it can), 3 minutes
# after logon, and 2 minutes after the PC wakes from sleep (15/11: it slept through 08:00 while the server was dead).
# Cheap when the server is fine: one read-only ssh check, no browser.
# Re-running this updates the copy and the task. Log: %USERPROFILE%\.smart_study\nlm_session.log
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pc\install_nlm_session_task.ps1
$ErrorActionPreference = "Stop"
$name   = "SmartStudy_NLM_Session"
$dir    = Join-Path $env:USERPROFILE ".smart_study"
$script = Join-Path $dir "nlm_session_refresh.py"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item -Force (Join-Path $PSScriptRoot "nlm_session_refresh.py") $script

$py  = (Get-Command python).Source
$pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }

$action   = New-ScheduledTaskAction -Execute $pyw -Argument "`"$script`""
$every4h  = New-ScheduledTaskTrigger -Daily -At 00:00
$every4h.Repetition = (New-ScheduledTaskTrigger -Once -At 00:00 -RepetitionInterval (New-TimeSpan -Hours 4) `
                       -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
$logon    = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT3M"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger @($every4h, $logon) -Settings $settings -Principal $principal `
        -Description "Keeps the server's NotebookLM login alive (smart_study_agent scripts/pc/nlm_session_refresh.py)"

# + "the PC woke up" (System log, Power-Troubleshooter event 1): the cmdlets have no event trigger, so via the XML
$xml = [xml](Export-ScheduledTask -InputObject $task)
$ns  = $xml.DocumentElement.NamespaceURI
$ev  = $xml.CreateElement("EventTrigger", $ns)
$query = '<QueryList><Query Id="0" Path="System"><Select Path="System">' +
         "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]" +
         '</Select></Query></QueryList>'
foreach ($kv in @(@("Enabled", "true"), @("Delay", "PT2M"), @("Subscription", $query))) {
    $el = $xml.CreateElement($kv[0], $ns); $el.InnerText = $kv[1]; [void]$ev.AppendChild($el)
}
[void]$xml.Task.Triggers.AppendChild($ev)
Register-ScheduledTask -TaskName $name -Xml $xml.OuterXml -Force | Out-Null
"registered $name -> $pyw $script"
