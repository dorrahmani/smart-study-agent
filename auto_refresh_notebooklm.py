"""
auto_refresh_notebooklm.py — רץ ברקע (Task Scheduler) כל 4 שעות.
מרענן את storage_state.json בשקט ללא פתיחת חלון דפדפן.
כותב ל-logs/notebooklm_auth_refresh.log.
מחזיר exit code 0 בהצלחה, 1 בכשל.
"""
import sys
import os
import pathlib
import datetime
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ──────────────────────────────────────────────────────────────────
SCRIPT_DIR      = pathlib.Path(__file__).resolve().parent
BROWSER_PROFILE = pathlib.Path.home() / ".notebooklm" / "browser_profile"
STORAGE_PATH    = pathlib.Path.home() / ".notebooklm" / "storage_state.json"
LOG_DIR         = SCRIPT_DIR / "logs"
LOG_FILE        = LOG_DIR / "notebooklm_auth_refresh.log"
NOTEBOOKLM_URL  = "https://notebooklm.google.com"
ACCOUNTS_URL    = "https://accounts.google.com/"   # כפיית קוקיות .google.com

LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    ts  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    log("=== auto_refresh_notebooklm — התחלה ===")

    # בדוק שה-profile קיים לפני ניסיון פתיחה
    if not BROWSER_PROFILE.exists():
        log(f"ERROR: browser_profile לא נמצא: {BROWSER_PROFILE}")
        log("יש להריץ fix_notebooklm_auth.py פעם אחת כדי ליצור את הפרופיל")
        return 1

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("ERROR: playwright לא מותקן — pip install playwright")
        return 1

    try:
        with sync_playwright() as p:
            log("פותח דפדפן headless...")
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE),
                headless=True,          # ←  שקט לחלוטין
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--password-store=basic",
                    "--no-sandbox",
                    "--disable-gpu",
                ],
                ignore_default_args=["--enable-automation"],
            )

            page = context.pages[0] if context.pages else context.new_page()

            log(f"מנווט ל-{NOTEBOOKLM_URL} ...")
            try:
                page.goto(NOTEBOOKLM_URL, wait_until="load", timeout=60_000)
            except Exception as e:
                log(f"goto timeout/error (ממשיכים): {e!r}")

            current_url = page.url
            log(f"URL נוכחי: {current_url}")

            # אם הגענו ל-login — הסשן פג ואי אפשר לרענן בשקט
            if "notebooklm.google.com" not in current_url:
                log("ERROR: הגענו לדף התחברות — הסשן פג!")
                log("הרץ fix_notebooklm_auth.py ידנית כדי להתחבר מחדש")
                context.close()
                return 1

            # שלב קריטי: ניווט ל-accounts.google.com לכפיית קוקיות .google.com
            # (בדיוק כמו notebooklm login עושה — חיוני לישראל/אזורים עם .google.co.il)
            log("מרענן קוקיות .google.com ...")
            try:
                page.goto(ACCOUNTS_URL, wait_until="load", timeout=15_000)
            except Exception:
                pass
            try:
                page.goto(NOTEBOOKLM_URL, wait_until="load", timeout=15_000)
            except Exception:
                pass

            # שמור את ה-storage_state המעודכן
            context.storage_state(path=str(STORAGE_PATH))
            STORAGE_PATH.chmod(0o600)
            log(f"storage_state.json עודכן בהצלחה: {STORAGE_PATH}")
            context.close()

    except Exception:
        log(f"ERROR בלתי צפוי:\n{traceback.format_exc()}")
        return 1

    log("=== auto_refresh_notebooklm — סיום מוצלח ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
