"""
whatsapp/invites.py — "תוסיף את יוסי לבוט" → a one-time code that goes to the owner; he forwards it, the person sends it to
the bot and is in (The owner 03/11: "הקוד יישלח אליי"). Replaces the permanent pairing secrets removed in the red team.

  create(name, by)  : SSA-XXXX-XXXX, single use, 24 hours, ≤ MAX_OPEN open at once; only its hash is stored
  joined()          : codes used since the last call → "✅ יוסי הצטרף לבוט" for the owner (each once)

The bridge redeems the code itself (whatsapp/bridge.py: redeem_invite — it imports nothing from the project), in
data/wa_bridge/invites.json (600, the bridge's own sandboxed folder).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / 'data' / 'wa_bridge' / 'invites.json'
TTL = 24 * 3600
MAX_OPEN = 3
ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'      # nothing that reads two ways (0/O, 1/I/L)
RX = re.compile(r'(?:תוסיף|תצרף|הוסף|צרף|להוסיף|לצרף|תכניס)\s+(?:את\s+)?(.{1,30}?)\s+(?:ל|אל\s+ה)?בוט|'
                r'קוד\s+(?:צירוף|הצטרפות)(?:\s+(?:ל|בשביל\s+)(.{1,30}))?|קוד\s+הזמנה\s+לבוט')


def _load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save(d: dict, path: Path | None = None) -> None:
    p = path or FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def digest(code: str) -> str:
    return hashlib.sha256(re.sub(r'[^A-Z0-9]', '', code.upper()).encode()).hexdigest()


def wants(text: str) -> str | None:
    """'' = an invite without a name, the name, or None."""
    m = RX.search(text or '')
    if not m:
        return None
    return (m.group(1) or m.group(2) or '').strip(' ?!.,"\'')


def create(name: str, by: str, role: str = 'friend', path: Path | None = None, now: float | None = None) -> str | None:
    now = now or time.time()
    d = {k: v for k, v in _load(path).items() if v.get('used') or v['expires'] > now - 7 * 86400}
    if sum(1 for v in d.values() if not v.get('used') and v['expires'] > now) >= MAX_OPEN:
        return None
    code = 'SSA-' + ''.join(secrets.choice(ALPHABET) for _ in range(4)) + '-' + ''.join(secrets.choice(ALPHABET) for _ in range(4))
    d[digest(code)] = {'name': name or 'מישהו', 'role': role, 'by': by, 'created': now, 'expires': now + TTL, 'used': None}
    _save(d, path)
    return code


def bot_number() -> str:
    """The bot's own number (the bridge's config), for a wa.me link with the code already typed."""
    try:
        for ln in (ROOT / 'config' / 'wa_bridge.env').read_text(encoding='utf-8').splitlines():
            if ln.startswith('WA_BOT_NUMBER='):
                return re.sub(r'\D', '', ln.split('=', 1)[1])
    except OSError:
        pass
    return ''


def messages(name: str, code: str, number: str = '') -> tuple[str, str]:
    """(for the owner, the one to forward)."""
    from urllib.parse import quote
    who = name or 'מי שתרצה'
    to_owner = (f'🔑 *קוד צירוף ל{who}* — פעם אחת, בתוקף 24 שעות:\n*{code}*\n\n'
              f'מה {who} יוכל: לשאול שאלות לימודים כלליות, עד 15 ביום. הוא לא רואה את היומן, התזכורות, הרשימות '
              f'או שום מידע שלך.\nתעביר את ההודעה הבאה 👇 ("תסיר את {who} מהבוט" כדי לבטל)')
    fwd = (f'היי! כדי לדבר עם העוזר של המשתמש, שלח/י לו את ההודעה הזו:\n{code}'
           + (f'\n\nאו פשוט ללחוץ: https://wa.me/{number}?text={quote(code)}' if number else ''))
    return to_owner, fwd


def joined(path: Path | None = None) -> list[tuple[str, str]]:
    """[(The owner's chat, '✅ יוסי הצטרף…')] for codes used since the last call."""
    d, out = _load(path), []
    for v in d.values():
        if v.get('used') and not v.get('notified'):
            v['notified'] = True
            out.append((v['by'], f"✅ {v['name']} הצטרף/ה לבוט עם הקוד ששלחתי לך."))
    if out:
        _save(d, path)
    return out


def remove(name: str, path: Path | None = None, allowed: Path | None = None) -> str:
    """'תסיר את יוסי מהבוט' → the chats that joined with his invites are no longer allowed (moved aside in the file)."""
    d = _load(path)
    chats = [v['used']['chat'] for v in d.values() if v.get('used') and name and name in v.get('name', '')]
    open_ = [k for k, v in d.items() if not v.get('used') and name and name in v.get('name', '')]
    for k in open_:
        d[k]['expires'] = 0
    p = allowed or FILE.parent / 'allowed.json'
    try:
        roles = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        roles = {}
    gone = [c for c in chats if roles.pop(c, None)]
    if gone:
        roles.setdefault('_removed', {}).update({c: name for c in gone})
        p.write_text(json.dumps(roles, ensure_ascii=False), encoding='utf-8')
        os.chmod(p, 0o600)
    _save(d, path)
    if gone or open_:
        return f'🚫 {name} כבר לא יכול/ה לדבר עם הבוט.' + (' (ביטלתי גם קוד שעוד לא נוצל)' if open_ else '')
    return f'לא מצאתי את {name} בין מי שהצטרף עם קוד.'
