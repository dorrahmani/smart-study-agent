"""
agent/contacts.py — people the agent may message for the owner (besides the partner). data/contacts.json (600).

The owner adds someone by sharing a WhatsApp contact card with the bot, or by writing the number in a request
("תתאם עם יוסי 050-1234567 …"). Names are matched loosely ("יוסי" finds "יוסי כהן").
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'contacts.json'
PHONE_RX = re.compile(r'(?:\+?972[\s-]?|0)5\d(?:[\s-]?\d){7}')


def normalize(phone: str) -> str:
    """'050-123 4567' / '+972 50 1234567' → '972501234567' ('' if it isn't an Israeli mobile)."""
    d = re.sub(r'\D', '', phone or '')
    if d.startswith('0') and len(d) == 10:
        d = '972' + d[1:]
    return d if re.fullmatch(r'9725\d{8}', d) else ''


def jid(phone: str) -> str:
    return f'{normalize(phone)}@s.whatsapp.net'


def _load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or STORE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def add(name: str, phone: str, path: Path | None = None) -> str:
    p = normalize(phone)
    if not p or not name.strip():
        return ''
    d = _load(path)
    d[name.strip()] = p
    (path or STORE).parent.mkdir(parents=True, exist_ok=True)
    (path or STORE).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    try:
        os.chmod(path or STORE, 0o600)
    except OSError:
        pass
    return p


def find(name: str, path: Path | None = None) -> tuple[str, str] | None:
    """→ (full name, phone digits) or None. Exact first, then 'starts with', then 'contains'."""
    d, n = _load(path), (name or '').strip()
    if not n:
        return None
    for test in (lambda k: k == n, lambda k: k.startswith(n), lambda k: n in k):
        hits = [k for k in d if test(k)]
        if len(hits) == 1:
            return hits[0], d[hits[0]]
    return None


def name_of(phone: str, path: Path | None = None) -> str:
    p = normalize(phone) or phone
    return next((k for k, v in _load(path).items() if v == p), '')


def from_vcard(display: str, vcard: str) -> tuple[str, str]:
    """A shared WhatsApp contact card → (name, phone digits)."""
    name = display.strip() or (re.search(r'(?m)^FN:(.+)$', vcard or '') or [None, ''])[1].strip()
    m = re.search(r'waid=(\d+)', vcard or '') or re.search(r'(?m)^TEL[^:]*:(.+)$', vcard or '')
    return name, normalize(m.group(1)) if m else ''
