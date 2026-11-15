' Silent wrapper for local_vm_alert.ps1 — no window flash
Set objShell = CreateObject("WScript.Shell")
objShell.Run "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File ""C:\path\to\smart_study_agent\scripts\local_vm_alert.ps1""", 0, False
