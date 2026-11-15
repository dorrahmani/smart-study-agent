"""
whatsapp/client.py — send/receive through the official WhatsApp Cloud API (Meta Graph API).

Env (in .env, 600): WA_TOKEN (System User permanent token), WA_PHONE_NUMBER_ID,
WA_OWNER (The owner's number, international digits, e.g. 9725XXXXXXXX), optional WA_GRAPH_VERSION.
Unofficial WhatsApp libraries are deliberately NOT used (ToS → banned numbers).

Pricing note (Oct 2026): replies inside the 24h customer-service window are free up to
1,000/month per bot number; business-initiated messages outside it need paid templates.
window_open(): outside the 24h window utils/notify can't use the Cloud API (no Telegram fallback since 10/11).
"""
from __future__ import annotations

import json
import mimetypes
import os
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINDOW = ROOT / 'data' / 'wa_window.json'
MEDIA_DIR = ROOT / 'data' / 'wa_media'
MAX_TEXT = 4000
WINDOW_HOURS = 23.5            # a little under Meta's 24h, so a reply never lands just outside it


def _cfg(name: str) -> str:
    return os.environ.get(name, '').strip()


def configured() -> bool:
    return bool(_cfg('WA_TOKEN') and _cfg('WA_PHONE_NUMBER_ID') and _cfg('WA_OWNER'))


def owner() -> str:
    return _cfg('WA_OWNER')


def allowed_numbers() -> set[str]:
    raw = _cfg('WA_ALLOWED') or _cfg('WA_OWNER')
    return {x.strip().lstrip('+') for x in raw.split(',') if x.strip()}


def _base() -> str:
    return f"https://graph.facebook.com/{_cfg('WA_GRAPH_VERSION') or 'v23.0'}"


def _headers() -> dict:
    return {'Authorization': f"Bearer {_cfg('WA_TOKEN')}"}


# ── 24h window ──────────────────────────────────────────────────────────────

def record_inbound(now: datetime | None = None, path: Path | None = None) -> None:
    p = path or WINDOW
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({'last_inbound': (now or datetime.now()).isoformat(timespec='seconds')}),
                 encoding='utf-8')


def window_open(now: datetime | None = None, path: Path | None = None) -> bool:
    try:
        last = datetime.fromisoformat(json.loads((path or WINDOW).read_text(encoding='utf-8'))['last_inbound'])
    except (OSError, ValueError, KeyError):
        return False
    return (now or datetime.now()) - last < timedelta(hours=WINDOW_HOURS)


# ── sending ─────────────────────────────────────────────────────────────────

def _post_message(payload: dict, timeout: int = 30) -> str | None:
    """POST /messages → the new message id (wamid), or None. Never raises."""
    import requests
    try:
        r = requests.post(f"{_base()}/{_cfg('WA_PHONE_NUMBER_ID')}/messages",
                          headers=_headers(), json={'messaging_product': 'whatsapp', **payload}, timeout=timeout)
        data = r.json()
        if r.status_code >= 400 or 'error' in data:
            print(f"  ⚠️ whatsapp send failed: {(data.get('error') or {}).get('message', r.status_code)}")
            return None
        return ((data.get('messages') or [{}])[0]).get('id')
    except Exception as e:
        print(f'  ⚠️ whatsapp send failed: {e}')
        return None


def _chunks(text: str) -> list[str]:
    from utils.text_chunks import chunks
    return chunks(text, MAX_TEXT)


def send_text(to: str, text: str, reply_to: str | None = None) -> str | None:
    last = None
    for i, part in enumerate(_chunks(text or '')):
        payload = {'to': to, 'type': 'text', 'text': {'body': part, 'preview_url': False}}
        if reply_to and i == 0:
            payload['context'] = {'message_id': reply_to}
        last = _post_message(payload)
        if not last:
            return None
    return last


def send_buttons(to: str, text: str, buttons: list[tuple[str, str]]) -> str | None:
    """Up to 3 reply buttons (title ≤20 chars, id = payload). Longer bodies go first as text."""
    buttons = buttons[:3]
    body = text
    if len(body) > 1000:
        if not send_text(to, body):
            return None
        body = 'מה הלאה?'
    return _post_message({'to': to, 'type': 'interactive', 'interactive': {
        'type': 'button', 'body': {'text': body},
        'action': {'buttons': [{'type': 'reply', 'reply': {'id': p[:256], 'title': lbl[:20]}}
                               for lbl, p in buttons]}}})


def upload_media(path: Path) -> str | None:
    import requests
    mime = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
    try:
        with open(path, 'rb') as f:
            r = requests.post(f"{_base()}/{_cfg('WA_PHONE_NUMBER_ID')}/media", headers=_headers(),
                              data={'messaging_product': 'whatsapp', 'type': mime},
                              files={'file': (Path(path).name, f, mime)}, timeout=120)
        return r.json().get('id')
    except Exception as e:
        print(f'  ⚠️ whatsapp upload failed: {e}')
        return None


def send_document(to: str, path: Path | str, caption: str = '', reply_to: str | None = None) -> str | None:
    path = Path(path)
    mid = upload_media(path)
    if not mid:
        return None
    payload = {'to': to, 'type': 'document',
               'document': {'id': mid, 'filename': path.name, 'caption': caption[:1000]}}
    if reply_to:
        payload['context'] = {'message_id': reply_to}
    return _post_message(payload)


def mark_read(message_id: str) -> None:
    _post_message({'status': 'read', 'message_id': message_id})


def download_media(media_id: str, suffix: str = '') -> Path | None:
    """Inbound voice/image → a local file (data/wa_media/)."""
    import requests
    try:
        meta = requests.get(f'{_base()}/{media_id}', headers=_headers(), timeout=30).json()
        r = requests.get(meta['url'], headers=_headers(), timeout=120)
        r.raise_for_status()
        ext = suffix or mimetypes.guess_extension((meta.get('mime_type') or '').split(';')[0]) or ''
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        out = MEDIA_DIR / f'{media_id}{ext}'
        out.write_bytes(r.content)
        return out
    except Exception as e:
        print(f'  ⚠️ whatsapp media download failed: {e}')
        return None
