"""scripts/oauth_health_check.py — Weekly check of all OAuth tokens.

Alerts via WhatsApp (utils.notify; Telegram deleted 10/11) if any token:
  • is already expired
  • expires within 3 days
  • is missing entirely

Cron suggestion (Sunday 09:00 Israel = 06:00 UTC):
  0 6 * * 0  /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "OAuth Health" \
    bash -c "cd /home/ubuntu/smart_study_agent && venv/bin/python3 scripts/oauth_health_check.py"
"""
from __future__ import annotations

import json
import os
import pickle
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass


WARN_DAYS = 3
checks: list[dict] = []


def _push(name: str, status: str, detail: str = '', urgency: int = 0) -> None:
    """urgency: 0=OK (green), 1=warn (yellow), 2=crit (red)."""
    checks.append({'name': name, 'status': status, 'detail': detail, 'urgency': urgency})


def _check_pickle_token(path: Path, name: str) -> None:
    if not path.exists():
        _push(name, '❓', f'קובץ חסר: {path}', urgency=2)
        return
    try:
        with open(path, 'rb') as f:
            tok = pickle.load(f)
    except Exception as e:
        _push(name, '⚠️', f'נכשל לקרוא: {e}', urgency=2)
        return
    expiry = getattr(tok, 'expiry', None)
    has_refresh = bool(getattr(tok, 'refresh_token', None))
    scopes = getattr(tok, 'scopes', [])
    if expiry is None:
        _push(name, '✅' if has_refresh else '⚠️',
              f'אין תפוגה ידועה. refresh_token: {has_refresh}. scopes: {scopes}',
              urgency=0 if has_refresh else 1)
        return
    # expiry may be naive or aware
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = expiry - now
    days = delta.total_seconds() / 86400
    if days < 0 and has_refresh:
        # access-token expired but refresh_token will auto-renew it on next API call
        _push(name, '✅',
              f'Access-token פג ב-{expiry.strftime("%Y-%m-%d %H:%M UTC")} '
              f'אבל יש refresh_token — יתחדש אוטומטית בקריאה הבאה.',
              urgency=0)
    elif days < 0:
        _push(name, '⛔',
              f'פג תוקף ב-{expiry.strftime("%Y-%m-%d %H:%M UTC")} '
              f'(לפני {-int(days)} ימים). אין refresh_token — צריך ראוט ידני!',
              urgency=2)
    elif days < WARN_DAYS and not has_refresh:
        _push(name, '⚠️',
              f'יפוג בעוד {days:.1f} ימים, ואין refresh_token. צריך לראות ידני!',
              urgency=2)
    elif days < WARN_DAYS:
        _push(name, '✅',
              f'יפוג בעוד {days:.1f} ימים (יש refresh_token, יתחדש אוטומטית)',
              urgency=0)
    else:
        _push(name, '✅',
              f'תקף עד {expiry.strftime("%Y-%m-%d")} ({days:.0f} ימים).'
              f' refresh: {has_refresh}',
              urgency=0)


def _check_whatsapp() -> None:
    """10/11: Telegram is gone — the one alert channel is the WhatsApp bridge outbox; check it can take a message."""
    try:
        from whatsapp import outbox
        if not outbox.enabled():
            _push('WhatsApp', '⛔', 'WA_TRANSPORT אינו bridge — התראות לא ייצאו', urgency=2)
        elif not outbox.owner_chat():
            _push('WhatsApp', '⛔', 'אין צ׳אט בעלים רשום לגשר', urgency=2)
        else:
            _push('WhatsApp', '✅', 'הגשר מוגדר, יש צ׳אט בעלים', urgency=0)
    except Exception as e:
        _push('WhatsApp', '⛔', f'בדיקה נכשלה: {e}', urgency=2)


def _check_gemini() -> None:
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        _push('Gemini API', '⛔', 'GEMINI_API_KEY חסר ב-env', urgency=2)
        return
    # Quick token introspection via list models (cheap)
    try:
        req = urllib.request.Request(
            f'https://generativelanguage.googleapis.com/v1beta/models?key={key}',
            method='GET',
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode('utf-8'))
        n = len(data.get('models', []) or [])
        _push('Gemini API', '✅', f'{n} מודלים זמינים', urgency=0)
    except Exception as e:
        _push('Gemini API', '⛔', f'{e}', urgency=2)


def _check_openai_quota() -> None:
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        _push('OpenAI (Whisper)', '⚠️', 'OPENAI_API_KEY חסר — Whisper לא יעבוד', urgency=1)
        return
    # Touch a cheap endpoint
    try:
        req = urllib.request.Request(
            'https://api.openai.com/v1/models',
            headers={'Authorization': f'Bearer {key}'},
            method='GET',
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            json.loads(r.read().decode('utf-8'))
        _push('OpenAI (Whisper)', '✅', 'מפתח תקף', urgency=0)
    except Exception as e:
        s = str(e)
        if '429' in s or 'insufficient_quota' in s.lower():
            _push('OpenAI (Whisper)', '⛔', 'מכסה מוצתה — Whisper לא יעבוד!', urgency=2)
        elif '401' in s:
            _push('OpenAI (Whisper)', '⛔', 'מפתח לא תקף', urgency=2)
        else:
            _push('OpenAI (Whisper)', '⚠️', s, urgency=1)


def _check_notebooklm() -> None:
    # notebooklm-py >= 0.7 stores the session per profile
    # (~/.notebooklm/profiles/default/storage_state.json); ask the library first,
    # or a stale 0.3.x file at the old path raises a false "expired" alarm.
    candidates = []
    try:
        from notebooklm.paths import get_storage_path
        candidates.append(Path(get_storage_path()))
    except Exception:
        pass
    candidates += [
        Path.home() / '.notebooklm' / 'storage_state.json',
        ROOT / 'config' / 'notebooklm_storage_state.json',
        ROOT / 'config' / 'storage_state.json',
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        _push('NotebookLM', '⚠️',
              f'storage_state חסר ב-{candidates[0]}', urgency=1)
        return
    # Ground truth: a real, read-only API call. Cookie-expiry math misleads —
    # Google keeps short-lived cookies that roll over on their own.
    try:
        import asyncio
        from notebooklm import NotebookLMClient

        async def _probe():
            async with NotebookLMClient.from_storage() as client:
                return len(await client.notebooks.list())

        n = asyncio.run(asyncio.wait_for(_probe(), timeout=60))
        _push('NotebookLM', '✅', f'חיבור פעיל ({n} מחברות נגישות)', urgency=0)
        return
    except Exception as e:
        msg = str(e)
        if 'auth' in msg.lower() or 'login' in msg.lower() or 'accounts.google' in msg:
            _push('NotebookLM', '⛔',
                  'החיבור פג — במחשב: `python -m notebooklm login`, ואז להעתיק לשרת (RUNBOOK)',
                  urgency=2)
            return
        # network/library hiccup → fall back to the cookie heuristic below
        print(f'  NotebookLM live probe failed ({msg[:80]}); falling back to cookie check')
    try:
        st = json.loads(path.read_text(encoding='utf-8'))
        cookies = st.get('cookies', []) or []
        # Find Google session cookies and earliest expiry
        google_cookies = [c for c in cookies
                          if 'google' in c.get('domain', '').lower()
                          or 'notebooklm' in c.get('domain', '').lower()]
        if not google_cookies:
            _push('NotebookLM', '⚠️', 'אין cookies של Google', urgency=1)
            return
        expiries = [c.get('expires', 0) for c in google_cookies if c.get('expires', 0) > 0]
        if not expiries:
            _push('NotebookLM', '✅',
                  f'{len(google_cookies)} cookies, ללא תפוגה (session)', urgency=0)
            return
        earliest = min(expiries)
        days = (earliest - datetime.now(timezone.utc).timestamp()) / 86400
        if days < 0:
            _push('NotebookLM', '⛔', f'cookie פג {-days:.0f} ימים — צריך VNC reauth!', urgency=2)
        elif days < WARN_DAYS:
            _push('NotebookLM', '⚠️', f'cookie יפוג בעוד {days:.1f} ימים', urgency=1)
        else:
            _push('NotebookLM', '✅',
                  f'תקף עד {datetime.fromtimestamp(earliest, tz=timezone.utc).strftime("%Y-%m-%d")} '
                  f'({days:.0f} ימים, {len(google_cookies)} cookies)',
                  urgency=0)
    except Exception as e:
        _push('NotebookLM', '⚠️', f'שגיאה בקריאה: {e}', urgency=1)


# ─── Run all checks ───────────────────────────────────────────────────
_check_pickle_token(ROOT / 'config' / 'gmail_token.pickle', 'Gmail OAuth')
# Calendar token if exists
cal_path = ROOT / 'config' / 'calendar_token.pickle'
if cal_path.exists():
    _check_pickle_token(cal_path, 'Calendar OAuth')
_check_whatsapp()
_check_gemini()
_check_openai_quota()
_check_notebooklm()

# ─── Build report ──────────────────────────────────────────────────────
max_urgency = max((c['urgency'] for c in checks), default=0)
header = {0: '✅ *בדיקת OAuth שבועית — הכל תקין*',
          1: '⚠️ *בדיקת OAuth — דרושה תשומת לב*',
          2: '⛔ *בדיקת OAuth — שגיאות קריטיות!*'}[max_urgency]

lines = [header, '']
for c in checks:
    lines.append(f'{c["status"]} *{c["name"]}*')
    lines.append(f'   {c["detail"]}')
    lines.append('')

msg = '\n'.join(lines).strip()
print(msg)

# Only push if there's anything to warn about, or once a week confirmation — WhatsApp only (10/11: Telegram deleted)
if max_urgency >= 1 or os.environ.get('OAUTH_HEALTH_ALWAYS_NOTIFY') == '1':
    from utils.notify import deliver
    if deliver(msg, markdown=True):
        print('✅ Pushed to WhatsApp')
    else:
        print('⚠️ WhatsApp push failed (kept in data/undelivered_alerts.jsonl)')

sys.exit(2 if max_urgency == 2 else 0)
