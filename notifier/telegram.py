"""
notifier/telegram.py — UNUSED since 10/11 (Telegram bot deleted; utils/notify.py → WhatsApp only).
Kept aside, not deleted. Was: the ONE way to push a Telegram message.

Before this module there were ~14 copies of the same urllib snippet, each
with its own subtly different fallback. Everything now goes through send():

    from notifier.telegram import send
    send('⚠️ something happened')                  # plain text, admin chat
    send('*bold*', markdown=True)                  # Markdown, auto plain fallback
    send(text, chat_id=123, reply_markup={...})    # explicit recipient / buttons

Contract:
  * Never raises (unless raise_errors=True) — a failed alert must not crash
    the job that was trying to report a problem.
  * Markdown that Telegram rejects is retried once as plain text.
  * Messages over Telegram's 4096-char limit are split on line boundaries.
  * Credentials are read at call time (after the caller's load_dotenv).
  * Default recipient = the FIRST id in TELEGRAM_USER_ID (the admin). The
    variable may hold a comma-separated allowlist for the bot; pushing to the
    raw string would fail as soon as it contains a second id.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

API = 'https://api.telegram.org/bot{token}/{method}'
MAX_LEN = 4000  # Telegram hard limit is 4096; keep headroom for entities


def admin_chat_id() -> str:
    """First id from TELEGRAM_USER_ID, or '' if unset."""
    raw = os.environ.get('TELEGRAM_USER_ID', '')
    for part in raw.split(','):
        part = part.strip()
        if part:
            return part
    return ''


def _strip_markdown(text: str) -> str:
    return text.replace('*', '').replace('`', '').replace('_', '')


from utils.text_chunks import chunks as _chunks  # noqa: E402  (moved 10/11; this module is unused)


def _post(token: str, payload: dict, timeout: int) -> dict:
    data = urllib.parse.urlencode(payload).encode('utf-8')
    req = urllib.request.Request(API.format(token=token, method='sendMessage'),
                                 data=data, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode('utf-8'))
    if not resp.get('ok'):
        raise RuntimeError(f'telegram ok=false: {resp.get("description", resp)}')
    return resp


def send_document(path: str, caption: str = '', *, chat_id: str | int | None = None,
                  timeout: int = 60, raise_errors: bool = False) -> dict | None:
    """Send a file (PDF etc.) with an optional caption. Same never-raise contract."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat = str(chat_id) if chat_id is not None else admin_chat_id()
    try:
        if not token or not chat:
            raise RuntimeError('telegram: missing TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID')
        import requests
        from pathlib import Path
        p = Path(path)
        with open(p, 'rb') as f:
            r = requests.post(API.format(token=token, method='sendDocument'),
                              data={'chat_id': chat, 'caption': caption[:1000]},
                              files={'document': (p.name, f)}, timeout=timeout)
        resp = r.json()
        if not resp.get('ok'):
            raise RuntimeError(f'telegram ok=false: {resp.get("description", resp)}')
        return resp
    except Exception as e:
        if raise_errors:
            raise
        print(f'  ⚠️ telegram send_document failed: {e}')
        return None


def send(text: str, *, markdown: bool = False, chat_id: str | int | None = None,
         reply_markup: dict | None = None, timeout: int = 15,
         raise_errors: bool = False) -> dict | None:
    """Send `text`; returns the API response of the last chunk, or None on failure."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat = str(chat_id) if chat_id is not None else admin_chat_id()
    if not token or not chat:
        msg = 'telegram: missing TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID'
        if raise_errors:
            raise RuntimeError(msg)
        print(f'  ⚠️ {msg}')
        return None

    resp = None
    parts = _chunks(text)
    for i, part in enumerate(parts):
        payload = {'chat_id': chat, 'text': part}
        if markdown:
            payload['parse_mode'] = 'Markdown'
        if reply_markup and i == len(parts) - 1:   # buttons on the last chunk
            payload['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
        try:
            resp = _post(token, payload, timeout)
        except Exception as e:
            if markdown:
                payload.pop('parse_mode', None)
                payload['text'] = _strip_markdown(part)
                try:
                    resp = _post(token, payload, timeout)
                    continue
                except Exception as e2:
                    e = e2
            if raise_errors:
                raise
            print(f'  ⚠️ telegram send failed: {e}')
            return None
    return resp
