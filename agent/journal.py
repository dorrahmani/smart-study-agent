"""
agent/journal.py — "תבטל את מה שעשית" ("undo what you did") (19/11, secretary item 16). Deterministic, no LLM.

Every action the bot itself takes for the owner is written here (data/action_journal.json) with its inverse:
    reminder     a reminder it created           → that reminder is switched off
    cal_add      events it added to his calendar → those events are deleted again
    cal_move     an event it moved to another day → moved back to its day
    cal_edit     an event it edited (time/place/title, by ✅ or the trusted auto-update) → the old values back
    task_close   a task it closed (✅ / "עשיתי")  → open again
    memory       a fact it saved ("תזכור ש…")     → forgotten
    list_add     items it put on a list           → taken off
    outward      a message / mail sent to someone else — can't be undone, and the reply says so plainly
    cal_delete   an event it deleted              — not restored from here (Google Calendar's bin has it)
"תבטל את מה שעשית" undoes the latest entry. When the original needed his ✅ (calendar add / move / edit by ✅), the
undo asks ✅ too (jund:ok / jund:no); when it didn't (a reminder, a task, a note, an auto-update) it's done at once.
When the latest is outward: it says it can't be undone and offers the latest reversible one behind it, with a ✅.
Only the owner's private chat, only entries from the last UNDO_H hours, each undone once.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / 'data' / 'action_journal.json'
KEEP = 80
UNDO_H = 48
REVERSIBLE = {'reminder', 'cal_add', 'cal_move', 'cal_edit', 'task_close', 'memory', 'list_add'}
UNDO_RX = re.compile(r'^\s*(?:ת?בטל|תחזיר|החזר)\s+(?:את\s+)?(?:מה\s+ש(?:עשית|עשית\s+עכשיו|עשית\s+הרגע)|'
                     r'(?:ה)?פעולה\s+(?:ה)?אחרונה(?:\s+שלך)?|מה\s+שעשית\s+(?:עכשיו|הרגע|לפני\s+רגע))\s*[.!]*\s*$|'
                     r'^\s*(?:undo|אנדו)\s*[.!]*\s*$', re.I)


# ── the store ───────────────────────────────────────────────────────────────

def _load(path: Path | None) -> list[dict]:
    try:
        v = json.loads((path or JOURNAL).read_text(encoding='utf-8'))
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _save(rows: list[dict], path: Path | None) -> None:
    p = path or JOURNAL
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows[-KEEP:], ensure_ascii=False, default=str), encoding='utf-8')
    tmp.replace(p)


def _owner(chat: str) -> bool:
    try:
        from whatsapp import outbox
        return bool(chat) and chat == outbox.owner_chat()
    except Exception:
        return False


def record(chat: str, kind: str, what: str, inverse: dict | None = None, confirm: bool = False,
           path: Path | None = None, now: datetime | None = None, check_owner: bool = True) -> str:
    """Never raises (a journal problem must not break the action itself). → the entry id ('' if not recorded)."""
    try:
        if check_owner and not _owner(chat):
            return ''
        rows = _load(path)
        jid = secrets.token_hex(3)
        rows.append({'id': jid, 'chat': chat, 'kind': kind, 'what': str(what)[:200], 'inverse': inverse or {},
                     'confirm': bool(confirm), 'ts': (now or datetime.now()).isoformat(timespec='seconds')})
        _save(rows, path)
        return jid
    except Exception as e:
        print(f'  ⚠️ journal: {e!r}'[:200])
        return ''


def _recent(chat: str, now: datetime, path: Path | None) -> list[dict]:
    cut = (now - timedelta(hours=UNDO_H)).isoformat(timespec='seconds')
    return [r for r in _load(path) if r.get('chat') == chat and not r.get('undone') and r.get('ts', '') >= cut]


# ── undoing ─────────────────────────────────────────────────────────────────

def _apply(r: dict) -> str:
    """Run the inverse of one entry → a short line of what was undone. Raises on failure."""
    inv, kind = r.get('inverse') or {}, r['kind']
    if kind == 'reminder':
        from agent import life
        d = life._load()
        hit = [x for x in d['reminders'] if x.get('id') == inv.get('rid') and x.get('chat') == r['chat']]
        for x in hit:
            x['active'] = False
        life._save(d)
        return f"ביטלתי את התזכורת: {r['what']}"
    if kind == 'cal_add':
        from integrations import gcal
        n = 0
        for eid in inv.get('ids') or []:
            gcal.delete_event(eid)
            n += 1
        return f"הורדתי מהיומן: {r['what']}" + (f' ({n} אירועים)' if n > 1 else '')
    if kind == 'cal_move':
        from datetime import date
        from integrations import gcal
        ev = inv['event']
        gcal.move_event(ev, date.fromisoformat(inv['day']))
        return f"החזרתי ביומן את {r['what']} ל-{date.fromisoformat(inv['day']):%d/%m}"
    if kind == 'cal_edit':
        from agent import calendar_edit
        n = 0
        for it in inv.get('items') or []:
            old, new = it['old'], it['new']
            st = old.get('start') or {}
            calendar_edit.apply({'event': new, 'title': old.get('summary') or '', 'location': old.get('location') or '',
                                 'date': (st.get('dateTime') or st.get('date') or '')[:10],
                                 'start': (st.get('dateTime') or '')[11:16],
                                 'end': ((old.get('end') or {}).get('dateTime') or '')[11:16]})
            n += 1
        return f"החזרתי ביומן את מה שהיה: {r['what']}"
    if kind == 'task_close':
        from agent import tasks
        d = tasks._load()
        for t in d['tasks']:
            if t.get('id') in (inv.get('ids') or []) and t.get('chat') == r['chat']:
                t['status'] = 'open'
                t.pop('closed', None)
                if t.get('event'):
                    try:
                        tasks._Cal().rename(t['event'], f"📌 {t['text']}")
                    except Exception as e:
                        print(f'  ⚠️ journal task rename: {e!r}'[:200])
        tasks._save(d)
        try:
            from agent import now_note
            now_note.add(r['chat'], f"נפתח מחדש (ביטל את הסגירה): {r['what']}", 'decision')
        except Exception:
            pass
        return f"פתחתי שוב את המשימה: {r['what']}"
    if kind == 'memory':
        from agent import life
        d = life._load()
        facts = d['memory'].get(r['chat'], [])
        if inv.get('fact') in facts:
            facts.remove(inv['fact'])
        life._save(d)
        return f"שכחתי: {r['what']}"
    if kind == 'list_add':
        from agent import life
        life.list_remove(inv.get('list', ''), list(inv.get('items') or []))
        return f"הורדתי מהרשימה: {r['what']}"
    raise ValueError(f'no inverse for {kind}')


def _cant(r: dict) -> str:
    if r['kind'] == 'outward':
        return f"❗ את הפעולה האחרונה אי אפשר לבטל: {r['what']} — זה כבר נשלח, ואין לי דרך להחזיר הודעה שיצאה."
    if r['kind'] == 'cal_delete':
        return f"❗ את המחיקה האחרונה ({r['what']}) אני לא מחזיר מכאן — אפשר לשחזר אותה מהאשפה של Google Calendar."
    return f"❗ את זה אני לא יודע לבטל: {r['what']}."


def _mark(path: Path | None, jid: str, now: datetime) -> None:
    rows = _load(path)
    for x in rows:
        if x.get('id') == jid:
            x['undone'] = now.isoformat(timespec='seconds')
    _save(rows, path)


def _do(r: dict, now: datetime, path: Path | None) -> str:
    try:
        line = _apply(r)
    except Exception as e:
        print(f'  ⚠️ journal undo {r.get("kind")}: {e!r}'[:200])
        return f"⚠️ לא הצלחתי לבטל ({r['what']}) — תבדוק ידנית."
    _mark(path, r['id'], now)
    return f'↩️ {line}.'


def undo(chat: str, now: datetime | None = None, path: Path | None = None) -> tuple[str, list]:
    """"תבטל את מה שעשית" → (reply, buttons). Buttons only when the undo needs his ✅."""
    now = now or datetime.now()
    rows = _recent(chat, now, path)
    if not rows:
        return f'🤷 לא מצאתי פעולה שעשיתי ב-{UNDO_H} השעות האחרונות שאפשר לבטל.', []
    last = rows[-1]
    if last['kind'] not in REVERSIBLE:
        behind = next((x for x in reversed(rows[:-1]) if x['kind'] in REVERSIBLE), None)
        msg = _cant(last)
        if not behind:
            return msg, []
        return (msg + f"\n\nהפעולה האחרונה שכן אפשר לבטל: {behind['what']}. לבטל אותה?",
                [('↩️ כן, לבטל', f"jund:ok:{behind['id']}"), ('❌ לא', f"jund:no:{behind['id']}")])
    if last.get('confirm'):
        return (f"↩️ לבטל את מה שעשיתי: {last['what']}?",
                [('↩️ כן, לבטל', f"jund:ok:{last['id']}"), ('❌ לא', f"jund:no:{last['id']}")])
    return _do(last, now, path), []


def button(chat: str, payload: str, now: datetime | None = None, path: Path | None = None) -> str:
    """jund:ok:<id> / jund:no:<id> — the ✅ for an undo."""
    now = now or datetime.now()
    _, op, jid = (payload.split(':', 2) + ['', ''])[:3]
    if op != 'ok':
        return '👍 השארתי כמו שזה.'
    r = next((x for x in _recent(chat, now, path) if x.get('id') == jid), None)
    if not r:
        return 'כבר לא אפשרי לבטל את זה מכאן.'
    if r['kind'] not in REVERSIBLE:
        return _cant(r)
    return _do(r, now, path)


def route(chat: str, text: str, now: datetime | None = None, path: Path | None = None) -> tuple[str, list] | None:
    if not UNDO_RX.match(text or ''):
        return None
    return undo(chat, now, path)
