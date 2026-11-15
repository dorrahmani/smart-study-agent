"""
scripts/setup_check.py — tells you what is still missing before the system can run.

    python scripts/setup_check.py          # check only (never prints secret values)
    python scripts/setup_check.py --init   # also create .env / config/courses.yaml / config/professors.yaml
                                           # from the shipped examples (never overwrites)

Exit code 0 when every REQUIRED item is in place.
"""
from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OK, WARN, BAD = '✅', '⚠️ ', '❌'

REQUIRED_ENV = {
    'TAU_USERNAME': 'TAU SSO username (Panopto/Moodle login)',
    'TAU_ID': 'ID number — the TAU login form asks for it',
    'TAU_PASSWORD': 'TAU SSO password',
    'GEMINI_API_KEY': 'https://aistudio.google.com/apikey (billing on + budget alert)',
    'DASHBOARD_PASSWORD': 'any long random string; the dashboard refuses access without it',
}
OPTIONAL_ENV = {
    'OPENAI_API_KEY': 'transcription when Panopto has no captions (else those lectures are skipped)',
    'WA_OWNER': 'WhatsApp bot: YOUR number, digits only (9725XXXXXXXX)',
    'WA_OWNER_NAME': 'how the bot calls you',
    'WA_PARTNER_NAME': 'how the bot calls your partner (calendar proposals)',
    'TELEGRAM_BOT_TOKEN': 'Telegram alerts (optional)',
    'TELEGRAM_USER_ID': 'REQUIRED if TELEGRAM_BOT_TOKEN is set — else the bot answers everyone',
    'GMAIL_CREDENTIALS_JSON': 'summaries by e-mail (Gmail OAuth desktop client)',
    'RECIPIENT_EMAIL': 'where summaries are e-mailed',
    'SSA_ACTIVE_COURSES': "this semester's course keys from config/courses.yaml",
}
PACKAGES = ['dotenv', 'yaml', 'requests', 'google.genai', 'playwright', 'streamlit', 'bs4']


def load_env() -> dict:
    env = {}
    p = ROOT / '.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(REQUIRED_ENV) + list(OPTIONAL_ENV):
        if os.environ.get(k):
            env.setdefault(k, os.environ[k])
    return env


def init() -> None:
    for src, dst in (('.env.example', '.env'), ('config/courses.example.yaml', 'config/courses.yaml'),
                     ('config/professors.example.yaml', 'config/professors.yaml')):
        s, d = ROOT / src, ROOT / dst
        if d.exists():
            print(f'{OK} {dst} already exists — left as is')
        else:
            shutil.copyfile(s, d)
            print(f'{OK} created {dst} from {src} — now edit it')


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8')   # Windows consoles default to a legacy code page
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--init', action='store_true')
    a = ap.parse_args()
    if a.init:
        init()
        print()
    bad = 0
    print(f'Python {sys.version.split()[0]}', OK if sys.version_info >= (3, 11) else f'{BAD} need 3.11+ (3.12 recommended)')
    bad += sys.version_info < (3, 11)

    print('\n── packages')
    for m in PACKAGES:
        try:
            importlib.import_module(m)
            print(f'{OK} {m}')
        except Exception:
            print(f'{BAD} {m} — pip install -r requirements.txt'); bad += 1
    ff = os.environ.get('FFMPEG_PATH') or shutil.which('ffmpeg')
    print(f'{OK} ffmpeg' if ff else f'{WARN} ffmpeg not found — needed for audio/board reading (set FFMPEG_PATH)')

    print('\n── .env')
    if not (ROOT / '.env').exists():
        print(f'{BAD} no .env — run: python scripts/setup_check.py --init'); bad += 1
    env = load_env()
    for k, why in REQUIRED_ENV.items():
        if env.get(k):
            print(f'{OK} {k}')
        else:
            print(f'{BAD} {k} — {why}'); bad += 1
    for k, why in OPTIONAL_ENV.items():
        print(f'{OK} {k}' if env.get(k) else f'{WARN} {k} (optional) — {why}')
    if env.get('TELEGRAM_BOT_TOKEN') and not env.get('TELEGRAM_USER_ID'):
        print(f'{BAD} TELEGRAM_USER_ID is empty while a bot token is set — the bot would answer anyone'); bad += 1
    key = env.get('GEMINI_API_KEY', '')
    if key and not key.startswith('AIza'):
        print(f'{WARN} GEMINI_API_KEY does not look like a Google key (should start with AIza)')

    print('\n── config')
    cy = ROOT / 'config' / 'courses.yaml'
    if not cy.exists():
        print(f'{BAD} config/courses.yaml missing — --init copies the example, then put YOUR courses in it'); bad += 1
    else:
        import yaml
        cfg = yaml.safe_load(cy.read_text(encoding='utf-8')) or {}
        courses = cfg.get('courses') or {}
        print(f'{OK} config/courses.yaml: {len(courses)} course(s)')
        def _fake(v) -> bool:
            v = str(v or '')
            return not v or v.startswith(('00000000-', '999', 'TO_BE', 'TODO'))
        placeholder = [k for k, c in courses.items()
                       if _fake(((c.get('sources') or {}).get('panopto') or {}).get('folder_id'))
                       or _fake(((c.get('sources') or {}).get('moodle') or {}).get('course_id'))]
        if placeholder:
            print(f'{WARN} still example ids (Panopto folder / Moodle id) in: {", ".join(placeholder)}')
        act = [x.strip() for x in env.get('SSA_ACTIVE_COURSES', '').split(',') if x.strip()]
        missing = [x for x in act if x not in courses]
        if missing:
            print(f'{BAD} SSA_ACTIVE_COURSES lists keys not in courses.yaml: {", ".join(missing)}'); bad += 1
        sched = (cfg.get('settings') or {}).get('schedule') or {}
        print(f"{OK} semester window {sched.get('semester_start')} → {sched.get('semester_end')} (pipeline idles outside it)")

    print('\n── sessions / tokens (created on first login, all gitignored)')
    for p, what in ((ROOT / 'config' / 'gmail_token.pickle', 'Gmail (python scripts/gmail_reauth.py)'),
                    (Path.home() / '.notebooklm' / 'profiles' / 'default' / 'storage_state.json', 'NotebookLM (python -m notebooklm login)'),
                    (ROOT / 'config' / 'gcal_token.json', 'Google Calendar (python scripts/gcal_auth.py <client.json>)')):
        print(f'{OK} {what}' if p.exists() else f'{WARN} not yet: {what}')

    print('\n── safety')
    try:
        tracked = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True).stdout.split()
        leaked = [t for t in tracked if t == '.env' or t.endswith(('.pickle', 'storage_state.json')) or t in ('config/courses.yaml',)]
        print(f'{BAD} tracked by git (remove + rotate!): {", ".join(leaked)}' if leaked else f'{OK} no secret files tracked by git')
        bad += bool(leaked)
    except Exception:
        print(f'{WARN} git not available — skipped the tracked-files check')
    print('   Before any push: python scripts/release_scan.py')

    print('\n' + (f'{OK} ready' if not bad else f'{BAD} {bad} required item(s) missing — see above'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
