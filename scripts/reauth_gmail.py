"""
reauth_gmail.py — renews Gmail authorization when the refresh token is rejected by Google.

What it does:
  1. backs up the old pickle (if it exists)
  2. deletes it to force a full OAuth flow
  3. runs GmailSender — this opens a browser with Google's consent page
  4. after you approve in the browser — a new token is saved

Requirement: GMAIL_CREDENTIALS_JSON is set in .env (already set for you).
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
mailer = GmailSender()  # runs flow.run_local_server if there is no valid pickle
print("\n✅ token חדש נשמר. המערכת תשתמש בו בריצה הבאה")
