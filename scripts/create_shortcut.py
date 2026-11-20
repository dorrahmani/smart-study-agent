"""
create_shortcut.py — creates a desktop shortcut for Smart Study Agent
One-time run: python scripts/create_shortcut.py
"""

import sys
import subprocess
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass


def _make_shortcut(name: str, bat_path: Path, project_root: Path, icon_index: int = 23) -> bool:
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        desktop = Path.home() / "OneDrive" / "Desktop"
    shortcut_path = desktop / f"{name}.lnk"

    ps_script = f"""
$WshShell = New-Object -comObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath = "{bat_path}"
$Shortcut.WorkingDirectory = "{project_root}"
$Shortcut.Description = "{name}"
$Shortcut.IconLocation = "C:\\Windows\\System32\\shell32.dll, {icon_index}"
$Shortcut.Save()
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True
    )
    if result.returncode == 0 and shortcut_path.exists():
        print(f"✅ {name}: {shortcut_path}")
        return True
    print(f"❌ שגיאת PowerShell: {result.stderr.strip()}")
    return False


def create_desktop_shortcut():
    project_root = Path(__file__).parent.parent.resolve()
    ok1 = _make_shortcut("Smart Study Agent",     project_root / "run_now.bat",       project_root, icon_index=23)
    ok2 = _make_shortcut("Smart Study Dashboard", project_root / "run_dashboard.bat", project_root, icon_index=14)
    return ok1 or ok2


if __name__ == '__main__':
    print("\n📌 יוצר קיצורי דרך לשולחן העבודה...")
    success = create_desktop_shortcut()
    if success:
        print("\n✅ הכל מוכן!")
        print("   📚 Smart Study Agent    — לחץ פעמיים להרצת הסוכן")
        print("   📊 Smart Study Dashboard — לחץ פעמיים לפתיחת הדשבורד")
    else:
        print("\n⚠️  ליצור ידנית: לחץ ימני על run_dashboard.bat → 'שלח אל' → שולחן עבודה")
    input("\nלחץ Enter לסיום...")
