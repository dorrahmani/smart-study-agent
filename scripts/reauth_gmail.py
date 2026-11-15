"""
reauth_gmail.py — חידוש הרשאת Gmail כש-refresh token נדחה ע"י Google.

מה זה עושה:
  1. מגבה את ה-pickle הישן (אם קיים)
  2. מוחק אותו כדי לאלץ OAuth flow מלא
  3. מריץ את GmailSender — זה יפתח דפדפן עם דף הסכמה של Google
  4. אחרי שתאשר בדפדפן — token חדש יישמר

דרישה: GMAIL_CREDENTIALS_JSON מוגדר ב-.env (כבר מוגדר אצלך).
"""
from __future__ import annotations
import os, sys, shutil, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv(ROOT / '.env', override=True)

token_path = ROOT / 'config' / 'gmail_token.pickle'
backup_path = ROOT / 'config' / f'gmail_token.pickle.{int(time.time())}.bak'

if token_path.exists():
    shutil.copy2(token_path, backup_path)
    print(f"📦 גיבוי: {backup_path.name}")
    token_path.unlink()
    print(f"🗑️  הקובץ הישן נמחק — עכשיו נפתח OAuth flow חדש")

print("\n🌐 פותח דפדפן לאישור גישה ל-Gmail...")
print("   (אם הדפדפן לא נפתח אוטומטית — תועתק כתובת לטרמינל)\n")

from generators.gmail_sender import GmailSender
mailer = GmailSender()  # מפעיל את flow.run_local_server אם אין pickle תקף
print("\n✅ token חדש נשמר. המערכת תשתמש בו בריצה הבאה")
