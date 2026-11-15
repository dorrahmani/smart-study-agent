"""utils/text_chunks.py — split a long message on line boundaries (WhatsApp bridge / Cloud API limits).

10/11: lived in notifier/telegram.py; moved here so WhatsApp sending doesn't import the (unused) Telegram module.
"""
from __future__ import annotations

MAX_LEN = 4000


def chunks(text: str, limit: int = MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ''
    for line in text.splitlines(keepends=True):
        while len(line) > limit:            # a single monster line
            if cur:
                out.append(cur); cur = ''
            out.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) > limit:
            out.append(cur); cur = ''
        cur += line
    if cur:
        out.append(cur)
    return out
