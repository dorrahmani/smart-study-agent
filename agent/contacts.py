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


def normalize(phone: str, landline: bool = False) -> str:
    """'050-123 4567' / '+972 50 1234567' → '972501234567' ('' if it isn't an Israeli mobile). landline=True also takes
    an Israeli landline or 07x number ('+972 3-519-7112' → '97235197112'): WhatsApp Business runs on those, and a shared
    contact card's waid= proves the number is on WhatsApp."""
    d = re.sub(r'\D', '', phone or '')
    if d.startswith('0') and len(d) in (9, 10):
        d = '972' + d[1:]
    if re.fullmatch(r'9725\d{8}', d):
        return d
    return d if landline and re.fullmatch(r'972(?:[2-489]\d{7}|7\d{8})', d) else ''


def jid(phone: str) -> str:
    return f'{normalize(phone) or normalize(phone, landline=True)}@s.whatsapp.net'   # a saved business card's landline


def _load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or STORE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def add(name: str, phone: str, path: Path | None = None) -> str:
    p = normalize(phone) or normalize(phone, landline=bool(re.fullmatch(r'972[2-9]\d{7,8}', phone or '')))
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


def _vfield(vcard: str, key: str) -> str:
    """FN / X-WA-BIZ-NAME / ORG, with or without parameters (FN;CHARSET=UTF-8:…), unescaped, ORG's first part."""
    m = re.search(rf'(?mi)^(?:item\d+\.)?{key}(?:;[^:\n]*)?:(.+)$', vcard or '')
    return re.split(r'(?<!\\);', m.group(1).strip())[0].replace('\\,', ',').replace('\\;', ';').strip() if m else ''


def from_vcard(display: str, vcard: str) -> tuple[str, str]:
    """A shared WhatsApp contact card → (name, phone digits). 15/11 a business card ("רשת מלונות לדוגמה",
    TEL;type=CELL;type=VOICE;waid=9723…:+972 3-…) got "לא הצלחתי לקרוא": only Israeli mobiles were taken. Now: the name from
    the display, FN, the business name or ORG; the number from waid= (that's WhatsApp's own id: a landline counts), else
    the first TEL that is a number we can message."""
    vcard = (vcard or '').replace('\r\n', '\n').replace('\n ', '')          # folded lines
    name = (display or '').strip().split('\n')[0].strip() or _vfield(vcard, 'FN') or _vfield(vcard, 'X-WA-BIZ-NAME') \
        or _vfield(vcard, 'ORG')
    for wa in re.findall(r'(?i)waid=(\d+)', vcard):
        if p := normalize(wa, landline=True):
            return name, p
    for tel in re.findall(r'(?mi)^(?:item\d+\.)?TEL[^:\n]*:(.+)$', vcard):
        if p := normalize(tel):
            return name, p
    return name, ''
