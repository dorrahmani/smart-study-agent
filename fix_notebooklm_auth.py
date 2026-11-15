"""
fix_notebooklm_auth.py — כניסה ידנית ל-NotebookLM ושמירת storage_state.json.
הרץ ב-PowerShell: python fix_notebooklm_auth.py
"""
import sys, pathlib, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Cross-platform profile path: ~/.notebooklm/... (works on Windows, Linux, macOS).
# The hardcoded Windows path created literal `C:\Users\...` directories on Linux.
_BASE = pathlib.Path.home() / ".notebooklm"
BROWSER_PROFILE = _BASE / "browser_profile"
STORAGE_PATH    = _BASE / "storage_state.json"
NOTEBOOKLM_URL  = "https://notebooklm.google.com"
ACCOUNTS_URL    = "https://accounts.google.com/"

# Refuse to run if no display (Playwright will crash with X server error).
if os.name != 'nt' and not os.environ.get('DISPLAY'):
    print(
        "❌ אין DISPLAY (אין מסך ב-VM). NotebookLM auth דורש דפדפן חזותי.\n"
        "   הרץ את הסקריפט הזה על מחשב מקומי, ואז העלה את ~/.notebooklm ל-VM."
    )
    sys.exit(1)

from playwright.sync_api import sync_playwright

print("פותח דפדפן...")

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_PROFILE),
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--password-store=basic",
        ],
        ignore_default_args=["--enable-automation"],
    )

    page = context.pages[0] if context.pages else context.new_page()
    page.goto(NOTEBOOKLM_URL)

    print()
    print("=" * 55)
    print("  בדפדפן שנפתח:")
    print("  1. התחבר לחשבון גוגל אם נדרש")
    print("  2. המתן עד שרואים את דף NotebookLM הראשי")
    print("  3. חזור לכאן ולחץ ENTER")
    print("=" * 55)
    input("\n  [לחץ ENTER לאחר שנכנסת ל-NotebookLM] ")

    # שלב קריטי: כפיית קוקיות .google.com (בדיוק כמו notebooklm login)
    print("\nמרענן קוקיות...")
    try:
        page.goto(ACCOUNTS_URL, wait_until="load", timeout=15000)
    except Exception:
        pass
    try:
        page.goto(NOTEBOOKLM_URL, wait_until="load", timeout=15000)
    except Exception:
        pass

    final_url = page.url
    print(f"URL סופי: {final_url}")

    # NotebookLM now redirects to notebook.google.com (without "lm"); accept both.
    if ("notebook" in final_url and "google.com" in final_url
            and "signin" not in final_url and "/login" not in final_url
            and "accounts.google" not in final_url):
        context.storage_state(path=str(STORAGE_PATH))
        STORAGE_PATH.chmod(0o600)
        print(f"\nstorage_state.json נשמר ב: {STORAGE_PATH}")
        print("סיום — ניתן לסגור את הדפדפן.")
    else:
        print(f"\nERROR: עדיין לא ב-NotebookLM (URL: {final_url})")
        print("נסה שוב ובדפדפן — כנס לחשבון והמתן לדף NotebookLM לפני לחיצת ENTER.")
        context.close()
        sys.exit(1)

    context.close()
