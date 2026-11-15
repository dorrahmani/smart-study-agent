"""
whatsapp/receiver.py — the ONLY internet-facing piece. Deliberately tiny.

It does three things and nothing else:
  1. GET  /wa/webhook — Meta's one-time verification handshake (hub.verify_token)
  2. POST /wa/webhook — verify Meta's X-Hub-Signature-256 (HMAC-SHA256 of the raw body
     with the app secret) → drop anything unsigned or forged
  3. keep only messages FROM allowed numbers and write each to the spool dir (one JSON
     file per message id — Meta's retries dedupe on the file name); return 200 fast
Every other path → 404. No access to API keys, .env or the agent: the worker
(whatsapp/worker.py, not exposed) reads the spool. Env it needs: WA_APP_SECRET,
WA_VERIFY_TOKEN, WA_OWNER/WA_ALLOWED, optional WA_SPOOL_DIR.

Run (systemd: deploy/smart-study-wa-receiver.service), bound to localhost only;
Tailscale Funnel on port 8443 → 127.0.0.1:8090 is what makes it reachable (The owner's call).
    uvicorn whatsapp.receiver:app --host 127.0.0.1 --port 8090
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_BODY = 1_000_000


def spool_dir() -> Path:
    return Path(os.environ.get('WA_SPOOL_DIR') or ROOT / 'data' / 'wa_spool')


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not app_secret or not header or not header.startswith('sha256='):
        return False
    expected = hmac.new(app_secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split('=', 1)[1].strip())


def handshake(params: dict, verify_token: str) -> str | None:
    if verify_token and params.get('hub.mode') == 'subscribe' and \
            hmac.compare_digest(str(params.get('hub.verify_token', '')), verify_token):
        return str(params.get('hub.challenge', ''))
    return None


def extract_messages(payload: dict, allowed: set[str]) -> list[dict]:
    """Inbound messages from allowed senders only (statuses/receipts are dropped)."""
    out = []
    for entry in payload.get('entry') or []:
        for change in entry.get('changes') or []:
            value = change.get('value') or {}
            for m in value.get('messages') or []:
                if str(m.get('from', '')).lstrip('+') in allowed and m.get('id'):
                    out.append(m)
    return out


def spool(messages: list[dict], d: Path | None = None) -> int:
    d = d or spool_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / 'done').mkdir(exist_ok=True)
    n = 0
    for m in messages:
        safe = re.sub(r'[^A-Za-z0-9_.=-]', '_', m['id'])[:120]
        if list(d.glob(f'*_{safe}.json')) or list((d / 'done').glob(f'*_{safe}.json')):
            continue                                          # Meta retry of a message we already have
        tmp = d / f'.{safe}.tmp'
        tmp.write_text(json.dumps(m, ensure_ascii=False), encoding='utf-8')
        tmp.replace(d / f'{int(time.time() * 1000)}_{safe}.json')
        n += 1
    return n


def _allowed() -> set[str]:
    raw = os.environ.get('WA_ALLOWED') or os.environ.get('WA_OWNER') or ''
    return {x.strip().lstrip('+') for x in raw.split(',') if x.strip()}


async def webhook(request):
    from starlette.responses import PlainTextResponse, Response
    if request.method == 'GET':
        ch = handshake(dict(request.query_params), os.environ.get('WA_VERIFY_TOKEN', ''))
        return PlainTextResponse(ch) if ch is not None else Response(status_code=403)
    body = await request.body()
    if len(body) > MAX_BODY:
        return Response(status_code=413)
    if not verify_signature(os.environ.get('WA_APP_SECRET', ''), body, request.headers.get('x-hub-signature-256')):
        return Response(status_code=403)
    try:
        payload = json.loads(body)
    except ValueError:
        return Response(status_code=400)
    spool(extract_messages(payload, _allowed()))
    return PlainTextResponse('ok')


def _build_app():
    from starlette.applications import Starlette
    from starlette.routing import Route
    return Starlette(routes=[Route('/wa/webhook', webhook, methods=['GET', 'POST'])])


try:
    app = _build_app()
except ImportError:                    # starlette missing (dev box) — the pure helpers stay testable
    app = None
