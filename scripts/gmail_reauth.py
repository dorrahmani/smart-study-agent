"""One-time Gmail re-authorization. Opens a browser for Google consent and
saves a fresh token to config/gmail_token.pickle. Run this in a NORMAL terminal
(not headless) — it needs you to log in and approve in the browser.

    cd C:\\Users\\you\\Documents\\smart_study_agent
    python scripts\\gmail_reauth.py
"""
import sys, os
sys.path.insert(0, '.')
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"), override=True)

from pathlib import Path
tok = Path('config/gmail_token.pickle')
if tok.exists():
    tok.unlink()
    print('🗑️ טוקן ישן נמחק')

print('🌐 נפתח דפדפן להתחברות Google — התחבר ואשר את ההרשאה...')
from generators.gmail_sender import GmailSender
GmailSender()   # triggers the OAuth browser flow and saves a fresh token
print('✅ התחברות ל-Gmail חודשה! אפשר לחזור ולהגיד לי "שלח".')
