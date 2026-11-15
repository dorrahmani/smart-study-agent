"""
scripts/panopto_cookie_probe.py — how long does a Panopto session cookie live?

Phase 0 of the friends plan: if friends ever use the system, storing only their
Panopto cookie (never their TAU password) is only viable if the cookie lives
long enough. Nobody has measured it — the pipeline logs in fresh every run.
This measures it on the owner's own account.

    venv/bin/python3 scripts/panopto_cookie_probe.py --capture   # log in once, keep Panopto cookies only
    venv/bin/python3 scripts/panopto_cookie_probe.py             # daily probe (cron), NO re-login

Stores only cookies for the Panopto domain (not the TAU SSO cookies, which
open every TAU service) in data/panopto_cookie_probe.json (600). Each probe
appends to data/panopto_cookie_probe_log.jsonl. On the first failure — or at
30 days alive — it reports the lifetime on WhatsApp and stops probing.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / '.env', override=True)

import requests  # noqa: E402

COOKIE_FILE = ROOT / 'data' / 'panopto_cookie_probe.json'
LOG_FILE = ROOT / 'data' / 'panopto_cookie_probe_log.jsonl'
BASE = 'https://tau.cloud.panopto.eu'
MAX_DAYS = 30


def _save_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.chmod(path, 0o600)


def capture() -> None:
    from collectors.panopto_collector import PanoptoCollector
    pc = PanoptoCollector()
    pc.login()
    cookies = [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path}
               for c in pc.session.cookies if 'panopto' in (c.domain or '')]
    if not cookies:
        sys.exit('no Panopto cookies after login — nothing captured')
    _save_private(COOKIE_FILE, {'captured_at': datetime.now().isoformat(timespec='seconds'),
                                'cookies': cookies, 'done': False})
    print(f'captured {len(cookies)} Panopto cookies (TAU SSO cookies NOT stored)')


def probe_alive(cookies: list[dict]) -> tuple[bool, str]:
    """Read-only call with the stored cookies only (no login)."""
    s = requests.Session()
    s.headers.update({'User-Agent': 'Mozilla/5.0'})
    for c in cookies:
        s.cookies.set(c['name'], c['value'], domain=c['domain'], path=c.get('path') or '/')
    try:
        r = s.post(f'{BASE}/Panopto/Services/Data.svc/GetSessions', timeout=30, json={
            'queryParameters': {'query': '', 'sortColumn': 1, 'sortAscending': False,
                                'maxResults': 1, 'page': 0, 'startDate': None, 'endDate': None,
                                'folderID': None, 'bookmarked': False, 'sessionListOnly': True,
                                'isSharedWithMe': False}})
    except requests.RequestException as e:
        return None, f'network: {e}'           # unknown — don't count as expired
    if r.status_code == 200:
        try:
            if 'd' in r.json():
                return True, 'ok'
        except ValueError:
            pass
    return False, f'HTTP {r.status_code}'


def probe() -> None:
    if not COOKIE_FILE.exists():
        print('no captured cookie — run with --capture first'); return
    st = json.loads(COOKIE_FILE.read_text(encoding='utf-8'))
    if st.get('done'):
        print('measurement finished:', st.get('result')); return
    age = (datetime.now() - datetime.fromisoformat(st['captured_at'])).total_seconds() / 86400
    alive, detail = probe_alive(st['cookies'])
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'ts': datetime.now().isoformat(timespec='seconds'),
                            'age_days': round(age, 2), 'alive': alive, 'detail': detail}) + '\n')
    print(f'age {age:.1f}d → alive={alive} ({detail})')
    if alive is None:
        return
    if alive is False or age >= MAX_DAYS:
        result = (f'עוגיית Panopto החזיקה כ-{age:.1f} ימים' if alive is False
                  else f'עוגיית Panopto עדיין חיה אחרי {age:.0f} ימים — מספיק למצב "עוגייה בלבד"')
        st.update(done=True, result=result, cookies=[])   # drop the cookie values
        _save_private(COOKIE_FILE, st)
        from utils.notify import deliver          # WhatsApp only (10/11: Telegram deleted)
        deliver(f'🍪 מדידת עוגיית Panopto (תוכנית חבר_א וחבר_ב, שלב 0):\n{result}', markdown=False)


if __name__ == '__main__':
    capture() if '--capture' in sys.argv else probe()
