"""
agent/catchup.py — "מה פספסתי" (19/11, secretary item 12). Deterministic, no LLM.

When the owner has been away for a while (no message and no button press for AWAY_H hours), the worker's proactive batch
doesn't trickle its non-urgent messages into a silent chat: they wait here (data/catchup_held.json). When he's back
(writes or taps anything), the next batch tick sends ONE ordered digest — the soonest dates first — and the held
questions go back to the usual one-at-a-time question queue (the daily question budget and the mutes still apply).

    urgent     never held: a calendar reminder, the exam-day message, the morning brief, anything marked 🚨,
               anything whose text speaks of today / tonight / tomorrow / "in N hours", or names a date within ~24h.
               A held item whose date comes within ~24h while he's away goes out on its own then.
    caps       nothing new is sent: what's held already passed its source's budget (agent/proactive's ≤3 a day,
               the 7-day same-question rule runs when a held question is released). The batch runs outside quiet
               hours only, so the digest does too. "מה פספסתי" in the chat gives the digest at any hour (he asked).
    activity   data/owner_activity.json, stamped by whatsapp/worker for every private owner message (a digit is one).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELD = ROOT / 'data' / 'catchup_held.json'
ACTIVITY = ROOT / 'data' / 'owner_activity.json'
AWAY_H = 4                                  # no message / tap for this long = away
URGENT_H = 24                               # a date within this many hours = urgent
MAX_AGE_H = 72                              # held longer than this → dropped (logged), never sent days late
MAX_ITEMS = 12                              # lines in one digest; the rest are counted
# sources that are urgent by nature (a reminder before an event, the exam day, the day's brief — itself a digest)
URGENT_SOURCES = {'event_reminder_tick', 'exam_day_messages', 'morning_brief_tick'}
SOON_RX = re.compile(r'🚨|(?<![א-ת])(?:היום|הלילה|הערב|מחר|עכשיו|בעוד\s+(?:\d+\s+)?(?:דקות|שעה|שעות|שעתיים)|'
                     r'עוד\s+(?:\d+\s+)?(?:דקות|שעה|שעתיים))(?![א-ת])')
DATE_RX = re.compile(r'(?<![\d/.])(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?(?![\d/.])(?:\D{0,12}?(\d{1,2}):(\d{2}))?')
ASK_RX = re.compile(r'^\s*(?:ו?מה|מה עוד)\s+(?:פספסתי|פיספסתי|החמצתי|פספסתי בינתיים)\s*[?!.]*\s*$|'
                    r'^\s*(?:תעדכן אותי|עדכן אותי)\s+(?:מה|במה)\s+(?:פספסתי|פיספסתי)\s*[?!.]*\s*$')


# ── activity ────────────────────────────────────────────────────────────────

def note_active(chat: str, now: float | None = None, path: Path | None = None) -> None:
    p = path or ACTIVITY
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    d[chat] = now or time.time()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(d), encoding='utf-8')
        tmp.replace(p)
    except OSError:
        pass


def last_active(chat: str, path: Path | None = None) -> float | None:
    try:
        v = json.loads((path or ACTIVITY).read_text(encoding='utf-8')).get(chat)
        return float(v) if v else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def away(chat: str, now: float | None = None, path: Path | None = None) -> bool:
    """No record yet (the first run) = not away: nothing is held until his activity is known."""
    t = last_active(chat, path)
    return t is not None and (now or time.time()) - t >= AWAY_H * 3600


# ── urgency ─────────────────────────────────────────────────────────────────

def _dates(text: str, now: datetime) -> list[datetime]:
    out = []
    for m in DATE_RX.finditer(text or ''):
        d, mo = int(m.group(1)), int(m.group(2))
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            continue
        y = int(m.group(3)) if m.group(3) else now.year
        if y < 100:
            y += 2000
        timed = bool(m.group(4)) and int(m.group(4)) < 24 and int(m.group(5)) < 60
        try:
            at = datetime(y, mo, d, int(m.group(4)), int(m.group(5))) if timed else datetime(y, mo, d, 23, 59)
        except ValueError:
            continue
        if not m.group(3) and at < now - timedelta(days=180):     # "03/01" said in November = next year
            at = at.replace(year=y + 1)
        out.append(at)
    return out


def soonest(text: str, now: datetime) -> datetime | None:
    """The earliest date the text names that hasn't fully passed (for the digest's order and the 24h rule)."""
    ds = [d for d in _dates(text, now) if d >= now - timedelta(hours=1)]
    return min(ds) if ds else None


def urgent(text: str, source: str = '', now: datetime | None = None, at_hold: bool = True) -> bool:
    """Deadline within ~24h. Words ("מחר", "הערב") count only when the message is new (at_hold): held, they'd age."""
    now = now or datetime.now()
    if source in URGENT_SOURCES:
        return True
    if at_hold and SOON_RX.search(text or ''):
        return True
    d = soonest(text, now)
    return bool(d and d - now <= timedelta(hours=URGENT_H))


# ── the held store ──────────────────────────────────────────────────────────

def _load(path: Path | None) -> list[dict]:
    try:
        v = json.loads((path or HELD).read_text(encoding='utf-8'))
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _save(items: list[dict], path: Path | None) -> None:
    p = path or HELD
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
    tmp.replace(p)


def hold(chat: str, kind: str, text: str, buttons: list | None = None, source: str = '',
         now: float | None = None, path: Path | None = None) -> None:
    items = _load(path)
    if any(i['chat'] == chat and i['text'] == text for i in items):
        return
    items.append({'chat': chat, 'kind': kind, 'text': text, 'buttons': [list(b) for b in buttons or []],
                  'source': source, 'ts': now or time.time()})
    _save(items, path)


def held(chat: str, path: Path | None = None) -> list[dict]:
    return [i for i in _load(path) if i['chat'] == chat]


def should_hold(chat: str, out, source: str, owner: str, now: datetime | None = None,
                activity_path: Path | None = None) -> bool:
    """The worker's question for each proactive message of the batch: hold it (The owner away, not urgent)?"""
    if not chat or chat != owner or getattr(out, 'kind', '') not in ('text', 'buttons'):
        return False
    now = now or datetime.now()
    if not away(chat, now.timestamp(), activity_path):
        return False
    return not urgent(getattr(out, 'text', ''), source, now)


def _line(text: str) -> str:
    """One held message as digest lines: its text without the numbered-options footer, compact."""
    t = re.sub(r'\n+השב במספר\.?\s*$', '', (text or '').strip())
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t[:700] + ('…' if len(t) > 700 else '')


def digest_text(texts: list[str], n_questions: int, hours: float | None = None) -> str:
    head = '📬 *מה פספסת*' + (f' (ב-{int(hours)} השעות האחרונות)' if hours and hours >= 1 else '') + ':'
    body = '\n\n'.join(f'• {_line(t)}' for t in texts[:MAX_ITEMS])
    more = f'\n\nועוד {len(texts) - MAX_ITEMS} עדכונים קטנים.' if len(texts) > MAX_ITEMS else ''
    q = ''
    if n_questions:
        q = ('\n\n' if texts else '') + (f'❓ יש עוד {n_questions} שאלות שמחכות לך — אשלח אותן אחת-אחת.'
                                         if n_questions > 1 else '❓ יש עוד שאלה אחת שמחכה לך — שולח אותה עכשיו.')
    return f'{head}\n\n{body}{more}{q}'.replace(':\n\n\n\n', ':\n\n')


def take(chat: str, now: datetime | None = None, path: Path | None = None) -> tuple[str, list[dict]]:
    """Everything held for `chat`, removed → (the digest text or '', the held questions in order). Ordered: the
    soonest named date first, then the order they were held. Older than MAX_AGE_H → dropped."""
    now = now or datetime.now()
    items = _load(path)
    mine = [i for i in items if i['chat'] == chat]
    if not mine:
        return '', []
    _save([i for i in items if i['chat'] != chat], path)
    fresh = [i for i in mine if now.timestamp() - i.get('ts', 0) <= MAX_AGE_H * 3600]
    if len(fresh) < len(mine):
        print(f'  🗑️ catchup: {len(mine) - len(fresh)} held message(s) older than {MAX_AGE_H}h dropped')
    far = datetime(9999, 1, 1)
    fresh.sort(key=lambda i: (soonest(i['text'], now) or far, i.get('ts', 0)))
    texts = [i['text'] for i in fresh if i['kind'] == 'text']
    questions = [i for i in fresh if i['kind'] == 'buttons']
    if not texts and not questions:
        return '', []
    first = min(i.get('ts', 0) for i in fresh) if fresh else 0
    hours = (now.timestamp() - first) / 3600 if first else None
    if not texts and len(questions) == 1:
        return '', questions                       # one question alone needs no digest around it
    return digest_text(texts, len(questions), hours), questions


def due_now(chat: str, now: datetime | None = None, path: Path | None = None) -> list[dict]:
    """Held items whose named date came within ~24h while he was away → removed, to be sent on their own now."""
    now = now or datetime.now()
    items = _load(path)
    go = [i for i in items if i['chat'] == chat and urgent(i['text'], i.get('source', ''), now, at_hold=False)]
    if go:
        _save([i for i in items if i not in go], path)
    return go


def release(chat: str, now: datetime | None = None, path: Path | None = None,
            activity_path: Path | None = None) -> tuple[str, list[dict]]:
    """The batch's step: he's back (not away) → the digest + questions; still away → ('', items now urgent)."""
    now = now or datetime.now()
    if not chat or not held(chat, path):
        return '', []
    if away(chat, now.timestamp(), activity_path):
        return '', due_now(chat, now, path)
    return take(chat, now, path)


def answer(chat: str, text: str, now: datetime | None = None, path: Path | None = None) -> tuple[str, list[dict]] | None:
    """"מה פספסתי" → (reply, held questions to queue) or None when it isn't that question."""
    if not ASK_RX.match(text or ''):
        return None
    body, qs = take(chat, now, path)
    if body:
        return body, qs
    if qs:
        return '📬 לא פספסת עדכונים — רק שאלה אחת שמחכה לך, הנה היא.', qs
    return '👍 לא פספסת כלום — כל מה שהיה כבר נשלח אליך.', []
