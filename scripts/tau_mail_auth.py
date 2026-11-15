"""scripts/tau_mail_auth.py — one-time consent for the UNIVERSITY mailbox, read-only (run on the owner's PC, not the VM).

    python scripts/tau_mail_auth.py

Opens the browser → sign in as student3@mail.tau.ac.il → approve "View your email messages and settings"
(read-only — the agent can't send, delete or change anything) → writes config/tau_mail_token.json. Then:
    scp -i ~/.ssh/smart_study.key config/tau_mail_token.json ubuntu@<your-server>:smart_study_agent/config/
    ssh -i ~/.ssh/smart_study.key ubuntu@<your-server> chmod 600 smart_study_agent/config/tau_mail_token.json
Reuses the existing Desktop OAuth client (GMAIL_CREDENTIALS_JSON in .env). If Google says the university blocks
the app, the fallback is a forwarding rule in the university Gmail → the personal Gmail (The owner sets it himself).
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from dotenv import load_dotenv
    from google_auth_oauthlib.flow import InstalledAppFlow
    from integrations.tau_mail import SCOPES, TOKEN
    load_dotenv(ROOT / '.env')
    raw = os.getenv('GMAIL_CREDENTIALS_JSON')
    if not raw:
        print('GMAIL_CREDENTIALS_JSON is missing from .env')
        return 2
    flow = InstalledAppFlow.from_client_config(json.loads(raw), SCOPES)
    creds = flow.run_local_server(port=0, prompt='consent', access_type='offline',
                                  login_hint='student3@mail.tau.ac.il')
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json(), encoding='utf-8')
    print(f'OK - saved {TOKEN} (scope: gmail.readonly only). Copy it to the VM — see the docstring.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
