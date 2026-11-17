"""
scripts/setup_check.py — tells you what is still missing before the assistant can run.

    python scripts/setup_check.py                   # check only (never prints secret values)
    python scripts/setup_check.py --init            # also create .env from .env.example (never overwrites)
    python scripts/setup_check.py --init --student  # …and config/courses.yaml / professors.yaml for the student domain

Two setup paths (SETUP.md):
  1. assistant only  (SSA_STUDY=0, the default) — Gemini + your own WhatsApp number + Google Calendar
  2. + the student domain (SSA_STUDY=1) — university login, courses.yaml, Moodle/Panopto, mail, NotebookLM

Exit code 0 when every REQUIRED item of the path you are on is in place.
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

# path 1 — the personal assistant
REQUIRED_ENV = {
    'GEMINI_API_KEY': 'https://aistudio.google.com/apikey (billing on + budget alert)',
    'WA_OWNER': 'YOUR WhatsApp number, digits only (9725XXXXXXXX) — the only one the bot obeys',
    'WA_BOT_NUMBER': 'the number the bridge is linked to: a second SIM / eSIM, not WA_OWNER',
}
OPTIONAL_ENV = {
    'WA_OWNER_NAME': 'how the bot calls you',
    'WA_PARTNER_NAME': 'partner mode: how the bot calls your partner',
    'WA_ALLOWED': 'partner mode: other numbers allowed to talk to the bot (comma-separated)',
    'DASHBOARD_PASSWORD': 'only if you run the dashboard — it refuses access without one',
    'TELEGRAM_BOT_TOKEN': 'Telegram alerts (optional)',
    'TELEGRAM_USER_ID': 'REQUIRED if TELEGRAM_BOT_TOKEN is set — else the bot answers everyone',
}
# path 2 — the student domain
STUDENT_REQUIRED_ENV = {
    'TAU_USERNAME': 'university SSO username (Panopto/Moodle login)',
    'TAU_ID': 'ID number — the TAU login form asks for it',
    'TAU_PASSWORD': 'university SSO password',
    'SSA_ACTIVE_COURSES': "this semester's course keys from config/courses.yaml",
}
STUDENT_OPTIONAL_ENV = {
    'OPENAI_API_KEY': 'transcription when Panopto has no captions (else those lectures are skipped)',
    'GMAIL_CREDENTIALS_JSON': 'summaries by e-mail (Gmail OAuth desktop client)',
    'RECIPIENT_EMAIL': 'where summaries are e-mailed',
}
PACKAGES = ['dotenv', 'yaml', 'requests', 'google.genai']
STUDENT_PACKAGES = ['playwright', 'streamlit', 'bs4']


def load_env() -> dict:
    env = {}
    p = ROOT / '.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.split(' #', 1)[0].strip().strip('"').strip("'")
    for k in list(REQUIRED_ENV) + list(OPTIONAL_ENV) + list(STUDENT_REQUIRED_ENV) + list(STUDENT_OPTIONAL_ENV) + ['SSA_STUDY']:
        if os.environ.get(k):
            env.setdefault(k, os.environ[k])
    return env


def init(student: bool) -> None:
    pairs = [('.env.example', '.env')]
    if student:
        pairs += [('config/courses.example.yaml', 'config/courses.yaml'),
                  ('config/professors.example.yaml', 'config/professors.yaml')]
    for src, dst in pairs:
        s, d = ROOT / src, ROOT / dst
        if d.exists():
            print(f'{OK} {dst} already exists — left as is')
        else:
            shutil.copyfile(s, d)
            print(f'{OK} created {dst} from {src} — now edit it')
    if student:
        print(f'{WARN} set SSA_STUDY=1 in .env to switch the student domain on')


def _study_on(env: dict) -> bool:
    v = (env.get('SSA_STUDY') or '').strip().lower()
    return v in ('1', 'true', 'yes', 'on')        # unset = off, like config/features.py


def _check_env(env: dict, required: dict, optional: dict) -> int:
    bad = 0
    for k, why in required.items():
        if env.get(k):
            print(f'{OK} {k}')
        else:
            print(f'{BAD} {k} — {why}'); bad += 1
    for k, why in optional.items():
        print(f'{OK} {k}' if env.get(k) else f'{WARN} {k} (optional) — {why}')
    return bad


def _check_courses(env: dict) -> int:
    bad = 0
    cy = ROOT / 'config' / 'courses.yaml'
    if not cy.exists():
        print(f'{BAD} config/courses.yaml missing — `--init --student` copies the example, then put YOUR courses in it')
        return 1
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
    return bad


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8')   # Windows consoles default to a legacy code page
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--init', action='store_true')
    ap.add_argument('--student', action='store_true', help='with --init: also create the student-domain configs')
    a = ap.parse_args()
    if a.init:
        init(a.student)
        print()
    bad = 0
    print(f'Python {sys.version.split()[0]}', OK if sys.version_info >= (3, 11) else f'{BAD} need 3.11+ (3.12 recommended)')
    bad += sys.version_info < (3, 11)

    env = load_env()
    study = _study_on(env)
    print(f"\nmode: {'assistant + student domain (SSA_STUDY=1)' if study else 'assistant only (SSA_STUDY=0)'}")

    print('\n── packages')
    for m in PACKAGES + (STUDENT_PACKAGES if study else []):
        try:
            importlib.import_module(m)
            print(f'{OK} {m}')
        except Exception:
            print(f'{BAD} {m} — pip install -r requirements.txt'); bad += 1
    if study:
        ff = os.environ.get('FFMPEG_PATH') or shutil.which('ffmpeg')
        print(f'{OK} ffmpeg' if ff else f'{WARN} ffmpeg not found — needed for audio/board reading (set FFMPEG_PATH)')

    print('\n── .env: the assistant')
    if not (ROOT / '.env').exists():
        print(f'{BAD} no .env — run: python scripts/setup_check.py --init'); bad += 1
    bad += _check_env(env, REQUIRED_ENV, OPTIONAL_ENV)
    if env.get('TELEGRAM_BOT_TOKEN') and not env.get('TELEGRAM_USER_ID'):
        print(f'{BAD} TELEGRAM_USER_ID is empty while a bot token is set — the bot would answer anyone'); bad += 1
    key = env.get('GEMINI_API_KEY', '')
    if key and not key.startswith('AIza'):
        print(f'{WARN} GEMINI_API_KEY does not look like a Google key (should start with AIza)')
    if env.get('WA_BOT_NUMBER') and env.get('WA_BOT_NUMBER') == env.get('WA_OWNER'):
        print(f'{BAD} WA_BOT_NUMBER == WA_OWNER — the bridge ignores messages from its own account, so the bot would '
              'never hear you: the bot needs a second number (SIM / eSIM), SETUP.md §א3'); bad += 1

    if study:
        print('\n── .env: the student domain')
        bad += _check_env(env, STUDENT_REQUIRED_ENV, STUDENT_OPTIONAL_ENV)
        print('\n── config')
        bad += _check_courses(env)
    else:
        print(f'\n{OK} student domain off — no university login, courses.yaml, Moodle, Panopto or NotebookLM needed')

    print('\n── sessions / tokens (created on first login, all gitignored)')
    checks = [(ROOT / 'config' / 'gcal_token.json', 'Google Calendar (python scripts/gcal_auth.py <client.json>)', True)]
    if study:
        checks += [(ROOT / 'config' / 'gmail_token.pickle', 'Gmail (python scripts/gmail_reauth.py)', False),
                   (Path.home() / '.notebooklm' / 'profiles' / 'default' / 'storage_state.json',
                    'NotebookLM (python -m notebooklm login)', False)]
    for p, what, _core in checks:
        print(f'{OK} {what}' if p.exists() else f'{WARN} not yet: {what}')
    wa = ROOT / 'data' / 'wa_bridge'
    print(f'{OK} WhatsApp bridge session' if wa.exists() and any(wa.iterdir())
          else f'{WARN} not yet: WhatsApp bridge — pair it once (SETUP.md §A3)')

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
