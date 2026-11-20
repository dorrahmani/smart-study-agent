"""
vault_sync/privacy.py — what may be written into the vault, and which notes stay out of NotebookLM (16/11).

The vault is the second brain: it lives on the VM, is copied nightly to Drive and pulled to the owner's PC. So:
  • scrub()        every text the assistant exporter writes: secrets / TAU ID / user name (utils/secret_scrub.scrub)
                   + phone numbers + e-mail addresses other than the owner's own university one. Numbers stay in data/contacts.json;
                   a note names the person and links by name.
  • is_private()   a note stays out of NotebookLM uploads (agent/brain.uploadable): its front-matter says `private: true`, or
                   its domain is private (DOMAINS below, overridable in config/vault_privacy.json {"<domain>": {"private": …}}).
                   A front-matter `private: false` lets one note of a private domain through.
Nothing here calls a model or the network.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
import os

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'config' / 'vault_privacy.json'

AREA = '👩‍💼 העוזר'                    # everything the assistant writes about his life
MINE = '✍️ שלי'                       # what the owner writes by hand on the PC (comes back to the VM, never written by the bot)

# domain → default. A domain is a folder under the assistant area (or the hand-written folder).
DOMAINS: dict[str, dict] = {
    '🎓 לימודים': {'private': False},
    '👥 אנשים':   {'private': False},
    '📁 מסמכים':  {'private': False},
    '✅ משימות':  {'private': False},
    '📓 יומן':    {'private': True},     # the daily journal: every turn of the day — stays home
    '🧭 עליך':    {'private': False},
    '🤖 הבוט':    {'private': True},     # security status, self-improvement, mutes: about the bot, not for answers
    MINE:          {'private': True},     # his own notes: read by the assistant on the VM, not uploaded unless he says so
}

_PHONE_RX = re.compile(r'(?<![\d\w])(?:\+?972[\s.-]?|0)(?:[2-9]\d?)[\s.-]?\d{3}[\s.-]?\d{3,4}(?![\d])|'
                       r'waid=\d+|(?<!\d)9725\d{8}(?!\d)')
_PARTIAL_PHONE_RX = re.compile(r'\s*\(\s*5\d…\d{3}\s*\)')          # the bot's own masked form "(50…215)"
_EMAIL_RX = re.compile(r'[\w.+-]+@(?![\w.-]*lid\b)[\w-]+\.[\w.-]+')
_LID_RX = re.compile(r'\b\d{8,}@(?:lid|s\.whatsapp\.net|g\.us)\b')
_KEEP_EMAIL = tuple('@' + d.strip().lstrip('@') for d in os.environ.get('SSA_UNI_MAIL_DOMAIN', 'mail.tau.ac.il').split(',')
                    if d.strip())     # the owner's own university address stays (TAU is the example)


def scrub(text: str) -> str:
    """Text → safe for a note that goes to Drive and the PC."""
    from utils.secret_scrub import scrub as _secrets
    t = _secrets(str(text or ''))
    t = _LID_RX.sub('[…]', t)
    t = _PHONE_RX.sub('[טלפון]', t)
    t = _PARTIAL_PHONE_RX.sub('', t)
    t = _EMAIL_RX.sub(lambda m: m.group(0) if m.group(0).lower().endswith(_KEEP_EMAIL) else '[מייל]', t)
    return t


@lru_cache(maxsize=1)
def _overrides() -> dict:
    try:
        d = json.loads(CONFIG.read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def domain_private(domain: str) -> bool:
    o = _overrides().get(domain)
    if isinstance(o, dict) and 'private' in o:
        return bool(o['private'])
    return bool((DOMAINS.get(domain) or {}).get('private'))


def domain_of(path: Path) -> str:
    parts = Path(path).parts
    for i, p in enumerate(parts):
        if p == MINE:
            return MINE
        if p == AREA and i + 1 < len(parts) and parts[i + 1] in DOMAINS:
            return parts[i + 1]
    return ''


_FM_RX = re.compile(r'^---\s*\n(.*?)\n---', re.S)


def front(path: Path) -> dict:
    try:
        m = _FM_RX.match(Path(path).read_text(encoding='utf-8', errors='replace')[:4000])
        fm = yaml.safe_load(m.group(1)) if m else {}
        return fm if isinstance(fm, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def is_private(path: Path) -> bool:
    """True → keep it out of NotebookLM. The note's own `private:` wins over its domain."""
    fm = front(path)
    if 'private' in fm:
        return bool(fm.get('private'))
    return domain_private(domain_of(path))
