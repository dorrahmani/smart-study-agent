"""scripts/gcal_auth.py — one-time Google Calendar consent (run on the owner's PC, not the VM).

    python scripts/gcal_auth.py path/to/oauth_client.json

Opens the browser → the owner approves "See and edit events on your calendars" (nothing else) →
writes config/gcal_token.json. Then copy it to the VM:
    scp -i ~/.ssh/smart_study.key config/gcal_token.json ubuntu@<your-server>:smart_study_agent/config/
    ssh ... chmod 600 smart_study_agent/config/gcal_token.json
The OAuth consent screen must be "In production" (Testing → token revoked after 7 days).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    import json
    import os
    from google_auth_oauthlib.flow import InstalledAppFlow
    from integrations.gcal import SCOPES, TOKEN
    if len(sys.argv) >= 2:
        flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    else:                                   # reuse the existing Desktop client (GMAIL_CREDENTIALS_JSON in .env)
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
        raw = os.getenv('GMAIL_CREDENTIALS_JSON')
        if not raw:
            print(__doc__)
            return 2
        flow = InstalledAppFlow.from_client_config(json.loads(raw), SCOPES)
    creds = flow.run_local_server(port=0, prompt='consent', access_type='offline')
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json(), encoding='utf-8')
    print(f'OK - saved {TOKEN} (scope: calendar.events only). Copy it to the VM — see the docstring.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
