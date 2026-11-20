"""
agent/approvals.py — a ✅ that never came (19/11, secretary item 15). Deterministic, no LLM.

Actions waiting for the owner's ✅ live in agent/core's state (data/agent_state.json), per chat:
    pending  calendar additions   (cal:ok / cal:no)      — from mail, homework, the partner's proposals, his own requests
    edits    calendar edit / move / delete (ce:ok / ce:no)
    msgs     a message to someone else (msg:send / msg:no)
After STALE_H hours without an answer: ONE reminder — the same question again, with the same buttons. After EXPIRE_H
more: it expires — the item is removed (its button then answers "כבר לא בתוקף") with one short note.
Never executes anything: this module only reminds and removes. A ✅ is the button (a digit mapped by the outbox) —
words never count as one, here or anywhere: nothing here reads his text at all.
A calendar addition whose events already passed (core refuses it anyway) is removed quietly, and so is anything never
reminded that is already older than both windows (the weeks-old offers in the state from before this existed).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

STALE_H = 24
EXPIRE_H = 48                     # after the reminder
STORES = {'pending': ('cal', 'ok', 'no', '✅ הוסף'), 'edits': ('ce', 'ok', 'no', '✅ לעדכן'),
          'msgs': ('msg', 'send', 'no', '📤 שלח')}
REMIND_HEAD = '⏳ *עדיין מחכה ל✅ שלך* (מלפני יום):\n\n'


def _ts(item: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(item.get('ts', '')))
    except ValueError:
        return None


def title(store: str, item: dict) -> str:
    if store == 'msgs':
        return f"הודעה ל{item.get('who') or 'מישהו'}: \"{str(item.get('text', ''))[:60]}\""
    if store == 'edits':
        ev = item.get('event') or {}
        verb = {'move': 'להזיז', 'delete': 'למחוק', 'edit': 'לעדכן'}.get(item.get('kind'), 'לעדכן')
        return f"{verb} ביומן: {ev.get('summary', '')}"
    evs = item.get('events') or ([item['event']] if item.get('event') else [])
    names = ', '.join(str(e.get('title') or e.get('summary') or '') for e in evs[:3])
    return f'להוסיף ליומן: {names}' + (f' ועוד {len(evs) - 3}' if len(evs) > 3 else '')


def _past(item: dict, now: datetime) -> bool:
    evs = item.get('events') or []
    try:
        return bool(evs) and all(datetime.fromisoformat(str(e.get('start', ''))[:16]) < now for e in evs)
    except ValueError:
        return False


def _question(chat: str, pid: str, options: dict) -> dict | None:
    """The numbered question this pending item was asked with (its text and buttons), if the outbox still has it."""
    v = options.get(chat) or []
    late = (options.get('_late') or {}).get(chat) or []
    for o in list(v if isinstance(v, list) else [v]) + list(late):
        if any(str(p).endswith(f':{pid}') for p in o.get('payloads') or []) and o.get('labels'):
            return o
    return None


def _strip(text: str) -> str:
    from whatsapp import outbox
    t = text or ''
    while t.startswith(outbox.REASK):
        t = t[len(outbox.REASK):]
    return re.sub(r'\n+השב במספר\.?\s*$', '', t)


def _drop_options(options: dict, chat: str, pid: str) -> bool:
    changed = False
    for holder in (options, options.get('_late') or {}):
        v = holder.get(chat)
        if isinstance(v, list):
            keep = [o for o in v if not any(str(p).endswith(f':{pid}') for p in o.get('payloads') or [])]
            if len(keep) != len(v):
                holder[chat] = keep
                changed = True
    return changed


def tick(now: datetime | None = None, st: dict | None = None, options: dict | None = None,
         owner: str | None = None) -> tuple[list[tuple[str, str, list]], list[tuple[str, str]], bool, bool]:
    """Pure over the given state/options (both mutated in place) →
    (reminders [(chat, text, buttons)], notes [(chat, text)], state changed, options changed)."""
    now = now or datetime.now()
    st = st or {}
    options = options if options is not None else {}
    reminders, notes = [], []
    s_changed = o_changed = False
    for chat, u in st.items():
        if not isinstance(u, dict) or '@' not in str(chat) or (owner and chat != owner):
            continue
        expired = []
        for store, (pre, ok, no, ok_label) in STORES.items():
            items = u.get(store)
            if not isinstance(items, dict):
                continue
            for pid, item in list(items.items()):
                if not isinstance(item, dict) or not (t := _ts(item)):
                    continue
                age = now - t
                if store == 'pending' and _past(item, now) and age > timedelta(hours=2):
                    items.pop(pid)
                    o_changed = _drop_options(options, chat, pid) or o_changed
                    s_changed = True
                    continue
                reminded = _ts({'ts': item.get('stale_reminded', '')})
                if not reminded and age >= timedelta(hours=STALE_H + EXPIRE_H):
                    # never reminded and already past both windows: from before 19/11 (weeks-old offers in the state)
                    # or a worker that was down — removed quietly, not a burst of "expired" about things long gone
                    items.pop(pid)
                    o_changed = _drop_options(options, chat, pid) or o_changed
                    s_changed = True
                    continue
                if reminded and now - reminded >= timedelta(hours=EXPIRE_H):
                    items.pop(pid)
                    o_changed = _drop_options(options, chat, pid) or o_changed
                    s_changed = True
                    expired.append(title(store, item))
                    continue
                if not reminded and age >= timedelta(hours=STALE_H):
                    item['stale_reminded'] = now.isoformat(timespec='seconds')
                    s_changed = True
                    q = _question(chat, pid, options)
                    if q:
                        text, buttons = _strip(q.get('text') or q.get('q', '')), list(zip(q['labels'], q['payloads']))
                        o_changed = _drop_options(options, chat, pid) or o_changed     # it's asked again below
                    else:
                        text = title(store, item) + '?'
                        buttons = [(ok_label, f'{pre}:{ok}:{pid}'), ('❌ לא', f'{pre}:{no}:{pid}')]
                    reminders.append((chat, REMIND_HEAD + text + '\n\n(בלי תשובה עד מחרתיים — אבטל את זה.)', buttons))
        if expired:
            notes.append((chat, '⌛ פג תוקף — לא קיבלתי ✅, אז לא עשיתי כלום:\n' + '\n'.join(f'• {x}' for x in expired)
                          + '\nאם עדיין רלוונטי — תבקש שוב.'))
    return reminders, notes, s_changed, o_changed


def run(now: datetime | None = None) -> list[tuple[str, object]]:
    """The worker's batch tick: read core's state and the outbox options, apply, save → [(chat, Out)]."""
    from agent import core
    from whatsapp import outbox
    st = core._load()
    try:
        options = json.loads(outbox.OPTIONS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        options = {}
    reminders, notes, s_changed, o_changed = tick(now, st, options, outbox.owner_chat() or None)
    if s_changed:
        core._save(st)
    if o_changed:
        outbox.OPTIONS.write_text(json.dumps(options, ensure_ascii=False), encoding='utf-8')
    return [(c, core.Out('buttons', t, buttons=b)) for c, t, b in reminders] + \
        [(c, core.Out('text', t)) for c, t in notes]
