"""
scripts/wa_bridge_watch.py — tells the owner, OUTSIDE WhatsApp, when the WhatsApp bridge is logged out or has been
disconnected for ~10 minutes (19/11/26). If the linked device is gone the bot is deaf and mute, and every other alert
(utils.notify → WhatsApp) is stuck behind the same outage — so this one never touches WhatsApp.

Reads only state the bridge already leaves (whatsapp/bridge.py is untouched):
  * data/wa_bridge/status.json        {"connected": ts} on every (re)connect, {"logged_out": ts} on LoggedOutEv
  * data/wa_bridge/session.sqlite3    gone = the device is not linked (a fresh pairing is pending)
  * journal of smart-study-wa-bridge  "connected to WhatsApp" / "LOGGED OUT" / "Error reading from websocket …"
  * systemctl is-active smart-study-wa-bridge

Incident → ONE alert per incident (state: data/wa_bridge_watch.json), then ONE "חזר" when it is healthy again.
  logged out / no session → alert at once.   Disconnected / service down → alert once it has lasted 10 minutes.
A normal reconnect (the websocket drops and returns within seconds, several times a day) is never an alert.

Channels (every one that is configured; none depends on WhatsApp):
  * email to the owner's own address (config.paths.owner_email() — RECIPIENT_EMAIL / courses.yaml) through the Gmail token
    the summaries already use (scope gmail.send; refreshed in memory, never written, never interactive);
  * ntfy.sh push to his phone when NTFY_TOPIC is set in .env (a long random topic; NTFY_SERVER optional).
If no channel takes it the incident stays un-alerted and the next run retries; it is also written once to
data/undelivered_alerts.jsonl (dashboard). No LLM. Exit code is always 0 (this is the alert).

Cron (every 5 min):  */5 * * * * cd ~/smart_study_agent && venv/bin/python3 scripts/wa_bridge_watch.py >> logs/wa_bridge_watch.log 2>&1
Simulate without touching anything:  python scripts/wa_bridge_watch.py --simulate logged_out|disconnected|service_down|no_session
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = Path(os.environ.get('WA_DATA_DIR') or ROOT / 'data')
BRIDGE = DATA / 'wa_bridge'
STATE = DATA / 'wa_bridge_watch.json'
UNIT = 'smart-study-wa-bridge'
IL = ZoneInfo('Asia/Jerusalem')
GRACE = 600                      # seconds a disconnect / downed service must last before it is an incident
JOURNAL_WINDOW = '-3h'

CONNECTED_RE = re.compile(r'connected to whatsapp', re.I)
LOGGED_OUT_RE = re.compile(r'logged out|loggedout', re.I)
DISCONNECT_RE = re.compile(r'error reading from websocket|failed to connect|connect failure|connectfailure|'
                           r'disconnected|stream replaced|client outdated|temporary ban|dial tcp|no such host|'
                           r'i/o timeout|websocket.*(closed|error)', re.I)
_JLINE = re.compile(r'^(\d+(?:\.\d+)?)\s+\S+\s+\S+?:\s*(.*)$')     # journalctl -o short-unix

KINDS = {
    'logged_out': 'WhatsApp הוציא את המכשיר המקושר (התנתקות / הסרה / חסימה של המספר)',
    'no_session': 'אין חיבור שמור של המכשיר המקושר (הסשן חסר)',
    'service_down': 'שירות הגשר (smart-study-wa-bridge) לא רץ',
    'disconnected': 'הגשר מנותק מוואטסאפ ולא מצליח להתחבר מחדש',
}
IMMEDIATE = ('logged_out', 'no_session')

RELINK = (
    'איך מחברים מחדש (על השרת, ואז בטלפון של מספר הבוט):\n'
    '1. ssh <user>@<your-server>\n'
    '2. cd ~/smart_study_agent/data/wa_bridge && mkdir -p old && mv session.sqlite3* old/   (מזיזים הצידה, לא מוחקים)\n'
    '3. sudo systemctl restart smart-study-wa-bridge ; sleep 40 ; cat ~/smart_study_agent/data/wa_bridge/pair_code.txt\n'
    '   (הגשר לא מדפיס QR — הוא מזווג בקוד של 8 תווים; הקוד מגיע גם כהתראה בטלפון)\n'
    '4. בטלפון של מספר הבוט: WhatsApp ← מכשירים מקושרים (Linked devices) ← קישור מכשיר (Link a device) ←\n'
    '   "קישור עם מספר טלפון במקום" ← מקלידים את הקוד.\n'
    '5. אימות: cat ~/smart_study_agent/data/wa_bridge/status.json  (צריך להופיע "connected"); תוך דקות תגיע הודעת "חזר".\n'
    'פירוט: docs/RUNBOOK.md ← "הגשר של וואטסאפ התנתק".'
)


# ── pure logic ──────────────────────────────────────────────────────────────

def parse_journal(text: str) -> list[tuple[float, str]]:
    """journalctl -o short-unix lines → [(epoch, message)]."""
    out = []
    for ln in (text or '').splitlines():
        m = _JLINE.match(ln.strip())
        if m:
            out.append((float(m.group(1)), m.group(2)))
    return out


def assess(now: float, status: dict, session_exists: bool, service_active: bool | None,
           events: list[tuple[float, str]]) -> tuple[str | None, float | None]:
    """(kind, since) — kind None = healthy. `since` is when the trouble began as far as the evidence shows;
    None for service_down (the caller times that one itself). Pure."""
    last_conn = max([float(status.get('connected') or 0)] + [t for t, m in events if CONNECTED_RE.search(m)])
    out_ts = float(status.get('logged_out') or 0)
    jo = [t for t, m in events if LOGGED_OUT_RE.search(m)]
    if jo:
        out_ts = max(out_ts, max(jo))
    if out_ts and out_ts >= last_conn:
        return 'logged_out', out_ts
    if not session_exists:
        return 'no_session', None
    if service_active is False:
        return 'service_down', None
    bad = sorted(t for t, m in events if t > last_conn and DISCONNECT_RE.search(m))
    if bad and now - bad[0] >= GRACE:
        return 'disconnected', bad[0]
    return None, None


def decide(state: dict, kind: str | None, since: float | None, now: float) -> tuple[dict, str | None]:
    """The new state and what to do: None | 'alert' | 'back'. Pure.
    state = {'incident': {kind, since, alerted} | None, 'pending': {kind, first} | None}"""
    st = {'incident': state.get('incident'), 'pending': state.get('pending')}
    inc = st['incident']
    if kind is None:
        st['pending'] = None
        if inc:
            st['incident'] = None
            return st, ('back' if inc.get('alerted') else None)
        return st, None
    if inc:                                                  # same incident: never a second alert
        if kind != inc.get('kind') and not inc.get('alerted') and kind in IMMEDIATE:
            inc['kind'] = kind
        return st, ('alert' if not inc.get('alerted') else None)
    if since is None:                                        # service_down: time it ourselves
        pend = st['pending'] if (st['pending'] or {}).get('kind') == kind else {'kind': kind, 'first': now}
        st['pending'] = pend
        since = pend['first']
    if kind in IMMEDIATE or now - since >= GRACE:
        st['incident'] = {'kind': kind, 'since': since, 'alerted': None}
        return st, 'alert'
    return st, None


def il(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(IL).strftime('%d/%m %H:%M')


def build_message(kind: str, since: float | None, now: float, back: bool = False) -> tuple[str, str]:
    """(subject, body) — Hebrew, no secrets."""
    if back:
        mins = int(max(0.0, now - (since or now)) // 60)
        return ('✅ גשר הוואטסאפ חזר',
                f'חזר ✅ — הגשר מחובר שוב לוואטסאפ ({il(now)}). הניתוק נמשך כ-{mins} דקות (מ-{il(since or now)}).\n'
                'הודעות שנשלחו בזמן הניתוק לא הגיעו לבוט; אם חיכית לתשובה, תשלח שוב.')
    body = (f'🚨 {KINDS.get(kind, kind)}.\n'
            f'מאז: {il(since or now)} (שעון ישראל). הבוט לא מקבל ולא שולח הודעות עד שמחברים מחדש.\n'
            'ההתראה הזאת נשלחת מחוץ לוואטסאפ, פעם אחת לתקלה; כשיחזור תגיע הודעת "חזר".\n\n' + RELINK)
    return '🚨 גשר הוואטסאפ של הבוט מנותק', body


# ── effects ────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str) -> bool:
    """Gmail API with the existing send-scope token. Never interactive, never writes the token."""
    try:
        import pickle
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from config.paths import owner_email
        to = owner_email()
        tok = ROOT / 'config' / 'gmail_token.pickle'
        if not to or not tok.exists():
            return False
        with open(tok, 'rb') as f:
            creds = pickle.load(f)
        if not creds.valid:
            if not (creds.expired and creds.refresh_token):
                return False
            creds.refresh(Request())
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['To'] = to
        msg['Subject'] = Header(subject, 'utf-8')
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii')
        build('gmail', 'v1', credentials=creds, cache_discovery=False).users().messages().send(
            userId='me', body={'raw': raw}).execute()
        return True
    except Exception as e:                                   # noqa: BLE001 — report the class only, never content
        print(f'  email failed: {type(e).__name__}')
        return False


def send_ntfy(subject: str, body: str, urgent: bool = True) -> bool:
    topic = (os.environ.get('NTFY_TOPIC') or '').strip()
    if not topic:
        return False
    server = (os.environ.get('NTFY_SERVER') or 'https://ntfy.sh').rstrip('/')
    try:
        import urllib.request
        payload = {'topic': topic, 'title': subject, 'message': body[:3500], 'priority': 5 if urgent else 3,
                   'tags': ['rotating_light' if urgent else 'white_check_mark']}
        req = urllib.request.Request(server + '/', data=json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:                                   # noqa: BLE001 — the URL holds the topic: never print it
        print(f'  ntfy failed: {type(e).__name__}')
        return False


def send_all(subject: str, body: str, urgent: bool = True) -> list[str]:
    """Names of the channels that took it."""
    ok = []
    if send_email(subject, body):
        ok.append('email')
    if send_ntfy(subject, body, urgent):
        ok.append('ntfy')
    return ok


def _journal() -> list[tuple[float, str]]:
    try:
        r = subprocess.run(['journalctl', '-u', UNIT, '--no-pager', '-o', 'short-unix', '--since', JOURNAL_WINDOW],
                           capture_output=True, text=True, timeout=30)
        return parse_journal(r.stdout)
    except (OSError, subprocess.SubprocessError):
        return []


def _active() -> bool | None:
    try:
        r = subprocess.run(['systemctl', 'is-active', UNIT], capture_output=True, text=True, timeout=15)
        s = r.stdout.strip()
        return True if s == 'active' else (False if s else None)
    except (OSError, subprocess.SubprocessError):
        return None                                          # can't tell → not an incident


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save(path: Path, st: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(path)


def run(now: float | None = None, state_path: Path = STATE, bridge_dir: Path = BRIDGE, events=None,
        active=None, send=send_all, record_undelivered=None) -> str | None:
    """One watch tick. Returns 'alert' | 'back' | None (what was delivered)."""
    now = time.time() if now is None else now
    status = _load(bridge_dir / 'status.json')
    kind, since = assess(now, status, (bridge_dir / 'session.sqlite3').exists(),
                         _active() if active is None else active, _journal() if events is None else events)
    state = _load(state_path)
    st, act = decide(state, kind, since, now)
    inc = st['incident']
    if act == 'alert':
        subj, body = build_message(inc['kind'], inc['since'], now)
        took = send(subj, body, True)
        if took:
            inc['alerted'] = now
            inc['channels'] = took
            print(f'{il(now)} ALERT {inc["kind"]} sent via {",".join(took)}')
        else:
            print(f'{il(now)} ALERT {inc["kind"]} — no channel took it; will retry')
            if not inc.get('undelivered_logged'):
                inc['undelivered_logged'] = True
                (record_undelivered or _record_undelivered)(f'{subj}: {KINDS.get(inc["kind"], "")}')
            act = None
    elif act == 'back':
        prev = state.get('incident') or {}
        subj, body = build_message(prev.get('kind', ''), prev.get('since'), now, back=True)
        if not send(subj, body, False):
            print(f'{il(now)} back — no channel took it')
        else:
            print(f'{il(now)} BACK sent')
    elif kind:
        print(f'{il(now)} {kind} — waiting/already alerted')
    _save(state_path, st)
    return act


def _record_undelivered(text: str) -> None:
    try:
        from utils.notify import _record_undelivered as rec
        rec(text, kind='wa_bridge_alert', reason='no external channel configured/working')
    except Exception:                                        # noqa: BLE001
        pass


def main(argv: list[str]) -> int:
    from utils.env import load_env_dotfile
    load_env_dotfile()
    if len(argv) >= 3 and argv[1] == '--simulate':           # prints the message; sends nothing, writes nothing
        kind = argv[2]
        now = time.time()
        subj, body = build_message(kind, now - 900, now)
        print(subj + '\n' + body)
        return 0
    try:
        run()
    except Exception as e:                                   # noqa: BLE001
        print(f'wa_bridge_watch error: {type(e).__name__}: {e}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
