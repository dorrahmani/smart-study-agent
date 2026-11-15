"""
bot/reminders.py — Simple JSON-backed reminder store + watcher.

Storage: data/reminders.json — list of {id, chat_id, due_at(ISO), text, fired}
Usage from bot:
  /remind 17/05 23:00 להגיש תרגיל 3 בדידה
  /remind tomorrow 09:00 to read calculus chapter 4
  /remind list
  /remind del <id>

Watcher: asyncio task that polls every 30s, sends Telegram message when due.
"""
from __future__ import annotations
import asyncio, json, re, secrets
from datetime import datetime, timedelta
from pathlib import Path

STORE_PATH = Path(__file__).resolve().parent.parent / 'data' / 'reminders.json'

# ─── Storage ───
def _load() -> list[dict]:
    if not STORE_PATH.exists():
        return []
    try: return json.loads(STORE_PATH.read_text(encoding='utf-8'))
    except: return []

def _save(items: list[dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')

# ─── Date parsing ───
HE_RELATIVES = {
    'מחר': 1, 'tomorrow': 1,
    'מחרתיים': 2, 'overmorrow': 2,
    'היום': 0, 'today': 0,
}
DATE_PATS = [
    # 17/05/2026 14:00, 17/05 14:00, 17.5.2026 14:00
    re.compile(r'(\d{1,2})[/\.](\d{1,2})(?:[/\.](\d{2,4}))?[\s,]+(\d{1,2}):(\d{2})'),
    # 17/05/2026 only (default to 09:00)
    re.compile(r'(\d{1,2})[/\.](\d{1,2})(?:[/\.](\d{2,4}))?(?!\s*\d)'),
    # 14:00 only (today)
    re.compile(r'(\d{1,2}):(\d{2})'),
]

def parse_when(text: str) -> tuple[datetime | None, str]:
    """Parse a date/time substring and return (datetime, remainder_text)."""
    text = text.strip()
    now = datetime.now()
    # Hebrew relatives
    for kw, days in HE_RELATIVES.items():
        if text.lower().startswith(kw + ' ') or text.lower() == kw:
            rest = text[len(kw):].strip()
            base = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days)
            tm = re.match(r'(\d{1,2}):(\d{2})\s+(.*)', rest)
            if tm:
                h, m, rem = int(tm.group(1)), int(tm.group(2)), tm.group(3)
                base = base.replace(hour=h, minute=m)
                return base, rem.strip()
            return base, rest
    # Numeric date
    m = DATE_PATS[0].match(text)
    if m:
        d, mo, y, h, mi = m.groups()
        y = int(y) if y else now.year
        if y < 100: y += 2000
        try:
            dt = datetime(y, int(mo), int(d), int(h), int(mi))
            rest = text[m.end():].strip()
            return dt, rest
        except Exception:
            pass
    m = DATE_PATS[1].match(text)
    if m:
        d, mo, y = m.groups()
        y = int(y) if y else now.year
        if y < 100: y += 2000
        try:
            dt = datetime(y, int(mo), int(d), 9, 0)
            rest = text[m.end():].strip()
            return dt, rest
        except Exception:
            pass
    m = DATE_PATS[2].match(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if dt < now: dt += timedelta(days=1)
        rest = text[m.end():].strip()
        return dt, rest
    return None, text

# ─── Public API ───
def add(chat_id: int, when: datetime, text: str) -> dict:
    items = _load()
    rid = secrets.token_hex(3)
    rec = {
        'id': rid,
        'chat_id': chat_id,
        'due_at': when.isoformat(timespec='minutes'),
        'text': text,
        'fired': False,
        'created_at': datetime.now().isoformat(timespec='minutes'),
    }
    items.append(rec)
    _save(items)
    return rec

def remove(chat_id: int, rid: str) -> bool:
    items = _load()
    new = [x for x in items if not (x['id'] == rid and x['chat_id'] == chat_id)]
    if len(new) == len(items): return False
    _save(new)
    return True

def list_for(chat_id: int) -> list[dict]:
    return [x for x in _load() if x['chat_id'] == chat_id and not x.get('fired')]

# ─── Handler (sync, called by bot's /remind) ───
def handle_remind(args: str, chat_id: int) -> str:
    args = (args or '').strip()
    if not args:
        return ('📋 שימוש:\n'
                '`/remind 17/05 14:00 להגיש תרגיל 3`\n'
                '`/remind מחר 09:00 לקרוא פרק 4`\n'
                '`/remind list`\n'
                '`/remind del <id>`')
    # list
    if args.lower() in ('list', 'רשימה'):
        items = list_for(chat_id)
        if not items: return '_אין תזכורות פעילות._'
        lines = ['*⏰ תזכורות פעילות:*']
        for r in sorted(items, key=lambda x: x['due_at']):
            dt = datetime.fromisoformat(r['due_at']).strftime('%d/%m/%Y %H:%M')
            lines.append(f'• `{r["id"]}` · {dt} · {r["text"]}')
        return '\n'.join(lines)
    # del
    m = re.match(r'(?:del|מחק)\s+([0-9a-f]+)', args, re.IGNORECASE)
    if m:
        if remove(chat_id, m.group(1)): return f'✅ נמחק'
        return f'❌ לא נמצא'
    # add (parse date then text)
    when, rest = parse_when(args)
    if not when:
        return '⚠️ לא הצלחתי להבין את התאריך. נסה: `17/05 14:00 משימה` או `מחר 09:00 משימה`'
    if not rest:
        return '⚠️ חסר טקסט תזכורת. דוגמה: `17/05 14:00 להגיש תרגיל 3`'
    rec = add(chat_id, when, rest)
    return f'✅ תזכורת נשמרה: `{rec["id"]}`\n📅 {when.strftime("%d/%m/%Y %H:%M")}\n📝 {rest}'

# ─── Watcher (async task) ───
async def watcher(bot, allowed_ids: set[int], poll_s: float = 30.0) -> None:
    """Background loop: every 30s, fire any due reminders."""
    while True:
        try:
            now = datetime.now()
            items = _load()
            changed = False
            for r in items:
                if r.get('fired'): continue
                try: due = datetime.fromisoformat(r['due_at'])
                except: continue
                if due > now: continue
                if r['chat_id'] not in allowed_ids: continue
                # Send
                try:
                    msg = f'⏰ תזכורת:\n{r["text"]}'
                    await bot.send_message(chat_id=r['chat_id'], text=msg)
                    r['fired'] = True
                    r['fired_at'] = now.isoformat(timespec='minutes')
                    changed = True
                except Exception:
                    pass
            if changed:
                _save(items)
        except Exception:
            pass
        await asyncio.sleep(poll_s)
