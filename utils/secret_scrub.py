"""
utils/secret_scrub.py — secrets and IDs out of a text before it leaves for a note or a model.

Patterns (key = value, Google / OpenAI / GitHub keys, IPv4 addresses, a 9-digit Israeli ID) plus the owner's own
university ID and user name by value (TAU_ID / TAU_USERNAME in .env), wherever they sit: a pattern can't know them.
Read when called, so a script that loads .env later still gets them.
"""
from __future__ import annotations

import os
import re

SECRET_RX = re.compile(r'(?i)(?:api[_-]?key|token|password|secret|passwd)\s*[:=]\s*\S{6,}|AIza[0-9A-Za-z_-]{20,}|'
                       r'sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|\b\d{1,3}(?:\.\d{1,3}){3}\b|'
                       r'(?<!\d)\d{9}(?!\d)')                  # an Israeli ID (a phone has 10; a Hebrew prefix isn't a \b)


def scrub(text: str) -> str:
    text = SECRET_RX.sub('[…]', text or '')
    for v in (os.environ.get('TAU_ID'), os.environ.get('TAU_USERNAME')):
        v = (v or '').strip()
        if len(v) >= 4:
            text = re.sub(rf'(?i)(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])', '[…]', text)
    return text
