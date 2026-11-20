"""
whatsapp/outbox.py — the main-venv side of the linked-device bridge (whatsapp/bridge.py).

Sending = writing a job file the bridge picks up (data/wa_outbox/), so the process holding the
WhatsApp session never needs the project, its keys or the network beyond WhatsApp.
A linked device can't send interactive buttons → numbered options; the reply digit is mapped
back to the button payload (options are remembered per chat for 30 minutes).
"""
from __future__ import annotations

import json
import re
import os
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTBOX = ROOT / 'data' / 'wa_outbox'
OPTIONS = ROOT / 'data' / 'wa_options.json'
OWNER = ROOT / 'data' / 'wa_bridge' / 'allowed.json'
OPTIONS_TTL = 48 * 3600          # 07/10 08:12 answer to last night; 24/10 a "1" 20h after the question was lost
OPTIONS_FRESH = 1800              # older than this → the reply says which question it answers
REASK_MAX_AGE = 6 * 3600          # 05/11: last night's "🧠 למדתי עליך" came back at 10:09 and 10:12 — stale isn't asked again
DIGITS = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣']


def enabled() -> bool:
    return os.environ.get('WA_TRANSPORT', '').strip().lower() == 'bridge'


def owner_chat() -> str:
    """The enrolled chat to push proactive messages to ('' until the owner has written once)."""
    explicit = os.environ.get('WA_OWNER_CHAT', '').strip()
    if explicit:
        return explicit
    try:
        data = json.loads(OWNER.read_text(encoding='utf-8'))
        roles = data if isinstance(data, dict) else {c: 'owner' for c in data}
        chats = [c for c, r in roles.items() if '@' in c and r == 'owner']
        return chats[0] if chats else ''
    except (OSError, ValueError):
        owner = os.environ.get('WA_OWNER', '').strip()
        return f'{owner}@s.whatsapp.net' if owner else ''


def partner_chat() -> str:
    try:
        data = json.loads(OWNER.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            chats = [c for c, r in data.items() if '@' in c and r == 'partner']
            return chats[0] if chats else ''
    except (OSError, ValueError):
        pass
    return ''


SENT_LOG = ROOT / 'data' / 'wa_outgoing.jsonl'


_GROUP_REPLY: ContextVar[bool] = ContextVar('ssa_group_reply', default=False)


@contextmanager
def group_reply():
    """05/11 the only way anything is sent to a group: a reply to the owner's tag or the debate, from the worker. A reminder,
    a browser-agent result or a "קלוד:" report that carries a group's id stops in _job."""
    t = _GROUP_REPLY.set(True)
    try:
        yield
    finally:
        _GROUP_REPLY.reset(t)


def _log_sent(job: dict, name: str = '') -> None:
    """08/10: the bot didn't know what its own proactive messages said (exam eve, the daily review report),
    so the owner's replies to them were read in a vacuum. Everything sent is logged; the agent reads it as context.
    05/11 not what went to a group: that text grew from friends' words, and it would reach the agent's prompt.
    15/11 with the outbox file's name: the bridge's receipt (sent/<name>.json) gives the WhatsApp id, so a message he
    quotes can be found again (quoted_text)."""
    if str(job.get('to', '')).endswith('@g.us'):
        return
    try:
        row = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'to': job.get('to', ''),
               'text': (job.get('text') or job.get('caption') or '')[:1500]}
        if name:
            row['file'] = name
        with open(SENT_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError:
        pass


def quoted_text(chat: str, msg_id: str | None, outbox: Path | None = None, path: Path | None = None) -> str:
    """15/11 "מה השינוי ?" quoting the calendar notification was answered about something else: the reply's quoted id
    is matched against the bridge's receipts, and the row of the sent log with that file name (older rows, from before
    the name was logged: the same chat within 3 seconds of the file's stamp) is what he quoted. '' = not ours/not found."""
    if not msg_id:
        return ''
    sent = (outbox or OUTBOX) / 'sent'
    name = ''
    try:
        files = sorted(sent.glob('*.json'), key=lambda p: p.name)[-600:]
    except OSError:
        return ''
    for f in reversed(files):
        try:
            r = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get('ok') and r.get('id') == msg_id:
            name = f.name
            break
    if not name:
        return ''
    try:
        lines = (path or SENT_LOG).read_text(encoding='utf-8').splitlines()[-1500:]
    except OSError:
        return ''
    rows = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get('to') == chat:
            if r.get('file') == name:
                return str(r.get('text') or '')
            rows.append(r)
    try:
        at = int(name.split('_', 1)[0]) / 1000
    except ValueError:
        return ''
    best, gap = '', 3.0
    for r in rows:
        if r.get('file'):
            continue
        try:
            t = time.mktime(time.strptime(str(r.get('ts', ''))[:19], '%Y-%m-%dT%H:%M:%S'))
        except ValueError:
            continue
        if abs(t - at) <= gap:
            best, gap = str(r.get('text') or ''), abs(t - at)
    return best


def recent_sent(chat: str, hours: float = 36, n: int = 6, path: Path | None = None) -> list[dict]:
    """The last messages the bot sent to `chat` (newest last)."""
    try:
        lines = (path or SENT_LOG).read_text(encoding='utf-8').splitlines()[-400:]
    except OSError:
        return []
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - hours * 3600))
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get('to') == chat and r.get('ts', '') >= cutoff and r.get('text'):
            out.append(r)
    return out[-n:]


_LAST_MS = 0


def _next_ms() -> int:
    """11/11: the bridge sends in name order, and two sends in the same millisecond (a reask right after a question,
    on a fast disk) got a random order from the hex suffix. Within a process the stamp now always moves forward."""
    global _LAST_MS
    _LAST_MS = max(int(time.time() * 1000), _LAST_MS + 1)
    return _LAST_MS


def _job(job: dict, wait: float = 0.0, outbox: Path | None = None) -> str | None:
    if str(job.get('to', '')).endswith('@g.us') and not _GROUP_REPLY.get():
        print('  ⚠️ outbox: a send to a group outside a reply was refused')
        return None
    d = outbox or OUTBOX
    name = f'{_next_ms()}_{secrets.token_hex(3)}.json'
    if (c := _COLLECT.get()) is not None:              # 19/11 send_options keeps its files: a quote binds to the question
        c.append(name)
    if outbox is None:
        _log_sent(job, name)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f'.{name}.tmp'
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    tmp.replace(d / name)
    if not wait:
        return name
    receipt = d / 'sent' / name
    deadline = time.time() + wait
    while time.time() < deadline:
        if receipt.exists():
            try:
                r = json.loads(receipt.read_text(encoding='utf-8'))
                return r.get('id') or None if r.get('ok') else None
            except ValueError:
                pass
        time.sleep(0.3)
    return None


def send_text(chat: str, text: str, outbox: Path | None = None) -> bool:
    from utils.text_chunks import chunks
    return all(_job({'to': chat, 'kind': 'text', 'text': part}, outbox=outbox) for part in chunks(text, 3800))


def send_reply(chat: str, text: str, quote: dict, outbox: Path | None = None) -> bool:
    """17/11 a group answer that quotes the message it answers (the bridge builds WhatsApp's reply; plain if it can't)."""
    from utils.text_chunks import chunks
    parts = chunks(text, 3800)
    return all(_job({'to': chat, 'kind': 'text', 'text': part, **({'quote': quote} if i == 0 and quote else {})},
                    outbox=outbox) for i, part in enumerate(parts))


def send_document(chat: str, path, caption: str = '', wait: float = 30.0, outbox: Path | None = None) -> str | None:
    """Returns the WhatsApp message id (for reply-to-a-practice-set), or None.
    The file is copied into the outbox: the sandboxed bridge can't see the rest of data/."""
    import shutil
    d = (outbox or OUTBOX) / 'files' / secrets.token_hex(4)
    d.mkdir(parents=True, exist_ok=True)
    copy = d / Path(path).name
    shutil.copy2(path, copy)
    return _job({'to': chat, 'kind': 'document', 'path': str(copy), 'text': caption[:1000]}, wait=wait, outbox=outbox)


VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.3gp'}
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp'}
MEDIA_MAX = 16 * 1024 * 1024            # WhatsApp's limit for a playable video / photo — bigger goes as a file


def send_media(chat: str, path, caption: str = '', wait: float = 30.0, outbox: Path | None = None) -> str | None:
    """17/10: a video arrives as a video and a photo as a photo (not as a file to download); anything else, or
    anything over 16MB, as a document. The bridge falls back to a document if WhatsApp rejects the media."""
    import shutil
    p = Path(path)
    ext = p.suffix.lower()
    kind = 'document'
    if p.stat().st_size <= MEDIA_MAX:
        kind = 'video' if ext in VIDEO_EXT else 'image' if ext in IMAGE_EXT else 'document'
    d = (outbox or OUTBOX) / 'files' / secrets.token_hex(4)
    d.mkdir(parents=True, exist_ok=True)
    copy = d / p.name
    shutil.copy2(p, copy)
    return _job({'to': chat, 'kind': kind, 'path': str(copy), 'text': caption[:1000]}, wait=wait, outbox=outbox)


ASKED = ROOT / 'data' / 'wa_proactive_asked.json'
ASKED_DAYS = 7


def _q_key(text: str) -> str:
    t = (text or '')
    while t.startswith(REASK):
        t = t[len(REASK):]
    return re.sub(r'\s+', ' ', t).strip()[:400]


def already_asked(chat: str, text: str, path: Path | None = None, now: float | None = None) -> bool:
    """19/10 — the safety net under every proactive question (The owner: "אתה מתחיל לשאול אותי אותה שאלה כמה פעמים רצוף"):
    the same question to the same chat within 7 days is dropped, whatever source produced it. Records it otherwise."""
    p = path or ASKED
    now = now or time.time()
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    mine = {k: ts for k, ts in (d.get(chat) or {}).items() if now - ts < ASKED_DAYS * 86400}
    k = _q_key(text)
    if k in mine:
        return True
    mine[k] = now
    d[chat] = mine
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    return False


KINDS = ROOT / 'data' / 'wa_question_kinds.json'
DAILY_QUESTIONS = 5               # proactive numbered questions per chat per day; more wait for tomorrow
IGNORED_MUTE = 2                  # this many ignored in a row → that kind of question stops (MUTE_DAYS)
MUTE_DAYS = 14


def _first_line(text: str) -> str:
    t = text or ''
    while t.startswith(REASK):
        t = t[len(REASK):]
    return next((ln.strip() for ln in t.splitlines() if ln.strip()), '')


def kind_of(buttons: list, text: str = '') -> str:
    """The kind of a question: its payload prefix ('cal:ok:ab12' → 'cal') + the start of its first line without
    numbers ('📝 *תרגיל חדש באתר…') — 'cal' alone is every calendar offer, from mail, homework and the partner alike."""
    try:
        pre = str(list(buttons)[0][1]).split(':', 1)[0]
    except (IndexError, TypeError):
        pre = ''
    first = _first_line(text)
    return pre + '|' + re.sub(r'[\d*_/.:,()\-]+|\s+', ' ', first).strip()[:18]


def _kinds(path: Path | None) -> dict:
    try:
        return json.loads((path or KINDS).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _kinds_save(d: dict, path: Path | None) -> None:
    p = path or KINDS
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')


def gate(chat: str, text: str, buttons: list, path: Path | None = None, now: float | None = None) -> str:
    """19/10 message budget (The owner: "פחות הודעות, יותר דיוק") → 'send' | 'defer' | 'mute'.
    A kind of proactive question the owner left unanswered IGNORED_MUTE times in a row stops for MUTE_DAYS (the morning
    brief says so once); at most DAILY_QUESTIONS proactive questions a day — the rest wait in the deferred queue."""
    now = now or time.time()
    d = _kinds(path)
    c = d.setdefault(chat, {})
    kinds = c.setdefault('kinds', {})
    kind = kind_of(buttons, text)
    k = kinds.setdefault(kind, {})
    q = _first_line(text)[:70]
    if k.get('muted_until', 0) > now:
        return 'mute'
    if k.get('open') and now - k.get('ts', 0) > OPEN_WINDOW:      # the last one of this kind was never answered
        k['open'] = False
        k['streak'] = k.get('streak', 0) + 1
        if k['streak'] >= IGNORED_MUTE:
            k.update(muted_until=now + MUTE_DAYS * 86400, streak=0)
            c.setdefault('new_mutes', []).append(k.get('q') or q)
            _kinds_save(d, path)
            return 'mute'
    day = time.strftime('%Y-%m-%d', time.localtime(now))
    if c.get('day') != day:
        c['day'], c['n'] = day, 0
    if c.get('n', 0) >= DAILY_QUESTIONS:
        _kinds_save(d, path)
        return 'defer'
    c['n'] = c.get('n', 0) + 1
    k.update(open=True, ts=now, q=q)
    _kinds_save(d, path)
    return 'send'


def note_answered(chat: str, payload: str = '', question: str = '', path: Path | None = None,
                  now: float | None = None) -> None:
    """A tapped option (that kind) or any written reply (the latest open kind, if asked < 3h ago) = not ignored."""
    now = now or time.time()
    d = _kinds(path)
    kinds = (d.get(chat) or {}).get('kinds') or {}
    if payload:
        k = kinds.get(kind_of([('', payload)], question))
    else:
        opn = [v for v in kinds.values() if v.get('open') and now - v.get('ts', 0) <= OPEN_WINDOW]
        k = max(opn, key=lambda v: v.get('ts', 0)) if opn else None
    if not k:
        return
    k.update(open=False, streak=0)
    _kinds_save(d, path)


def mute_last(chat: str, days: int = 30, path: Path | None = None, now: float | None = None) -> str:
    """"תפסיק לשאול על זה" → the kind of the latest proactive question. → its first line ('' if none)."""
    now = now or time.time()
    d = _kinds(path)
    kinds = (d.get(chat) or {}).get('kinds') or {}
    if not kinds:
        return ''
    k = max(kinds.values(), key=lambda v: v.get('ts', 0))
    k.update(muted_until=now + days * 86400, open=False, streak=0)
    _kinds_save(d, path)
    return k.get('q') or 'השאלה האחרונה'


def unmute_all(chat: str, path: Path | None = None) -> int:
    d = _kinds(path)
    kinds = (d.get(chat) or {}).get('kinds') or {}
    n = sum(1 for v in kinds.values() if v.get('muted_until', 0) > time.time())
    for v in kinds.values():
        v.pop('muted_until', None)
        v['streak'] = 0
    _kinds_save(d, path)
    return n


def pop_new_mutes(chat: str, path: Path | None = None) -> list[str]:
    d = _kinds(path)
    c = d.get(chat) or {}
    out = c.pop('new_mutes', [])
    if out:
        _kinds_save(d, path)
    return out


DEFERRED = ROOT / 'data' / 'deferred_options.json'
# a question asked < 3h ago and not answered yet is still "open": the next proactive question waits for the answer.
# 18/10: with 20 min, an unanswered question released the next one every 20 minutes — a stream of questions.
OPEN_WINDOW = 3 * 3600
REASK = '↩️ *עוד שאלה פתוחה:*\n'


LATE_KEY = '_late'                 # questions past OPTIONS_TTL, still answerable by a bare digit for a week
LATE_TTL = 7 * 86400


def _keep_late(allopts: dict, chat: str, still_open: list[dict]) -> None:
    """The chat's questions that just expired → the late list (newest 5, a week)."""
    v = allopts.get(chat) or []
    v = [v] if isinstance(v, dict) else list(v)
    gone = [o for o in v if o not in still_open and time.time() - o.get('ts', 0) > OPTIONS_TTL]
    if not gone:
        return
    late = allopts.setdefault(LATE_KEY, {})
    late[chat] = [o for o in (late.get(chat) or []) + gone if time.time() - o.get('ts', 0) <= LATE_TTL][-5:]
    allopts[chat] = [o for o in v if o not in gone]


def _open_list(allopts: dict, chat: str) -> list[dict]:
    """16/10: every unanswered numbered question of a chat (oldest first), not only the latest — the owner answered the
    tutorial-room question and then '1' for the lecture-room one, which the newer question had overwritten."""
    v = allopts.get(chat) or []
    v = [v] if isinstance(v, dict) else list(v)
    return [o for o in v if time.time() - o.get('ts', 0) <= OPTIONS_TTL]


def options_open(chat: str, options_path: Path | None = None) -> bool:
    """A question of this chat is waiting for its answer (asked < 3h ago, not held behind a newer one)."""
    try:
        lst = _open_list(json.loads((options_path or OPTIONS).read_text(encoding='utf-8')), chat)
    except (OSError, ValueError):
        return False
    return any(time.time() - o.get('ts', 0) < OPEN_WINDOW for o in _live(lst))


def open_payloads(chat: str, prefix: str = '', options_path: Path | None = None) -> list[str]:
    """Every answer a bare digit could still mean in this chat, the expired-but-late questions included."""
    try:
        allopts = json.loads((options_path or OPTIONS).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    v = allopts.get(chat) or []
    v = [v] if isinstance(v, dict) else list(v)
    late = (allopts.get(LATE_KEY) or {}).get(chat) or []
    return [pl for o in v + late for pl in o.get('payloads') or [] if str(pl).startswith(prefix)]


DEFERRED_MAX_AGE = 48 * 3600      # a question waiting two days is stale — dropped, not sent late


def defer_options(chat: str, text: str, buttons: list, path: Path | None = None, gated: bool = True) -> None:
    """13/10: two numbered questions in one tick — a '1' only answers the latest, so the first was unanswerable.
    A proactive question while another is open waits here; release_deferred() sends it once that one is done.
    gated=False: it waited for the daily budget (not counted yet) — release_deferred asks gate() again."""
    p = path or DEFERRED
    try:
        cur = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cur = []
    if any(i['chat'] == chat and _q_key(i['text']) == _q_key(text) for i in cur):
        return
    cur.append({'chat': chat, 'text': text, 'buttons': [list(b) for b in buttons], 'ts': time.time(), 'gated': gated})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, ensure_ascii=False), encoding='utf-8')


def _deferred_for(options_path: Path | None) -> Path:
    """Where a held question waits: the deferred queue, next to a test's own options file."""
    return DEFERRED if options_path is None else options_path.parent / 'deferred.json'


def release_deferred(path: Path | None = None, options_path: Path | None = None, outbox: Path | None = None,
                     kinds_path: Path | None = None, only: str = '') -> int:
    """Send the oldest waiting question of each chat whose previous question is answered / stale. → how many sent.
    only: just that chat (19/11 right after he answered, not on the next minute's tick)."""
    p = path or (_deferred_for(options_path) if options_path is not None else DEFERRED)
    try:
        cur = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0
    sent, keep, done_chats = 0, [], set()
    for item in cur:
        if time.time() - item.get('ts', 0) > DEFERRED_MAX_AGE:
            continue
        if (only and item['chat'] != only) or item['chat'] in done_chats or options_open(item['chat'], options_path):
            keep.append(item)
            continue
        if not item.get('gated', True):                   # waited for the daily budget — ask the gate again
            g = gate(item['chat'], item['text'], item['buttons'], kinds_path)
            if g == 'defer':
                keep.append(item)
                done_chats.add(item['chat'])
                continue
            if g == 'mute':
                continue
        if options_path is None and _settled(item['chat'], item['text']):
            continue                                      # 05/11: The owner closed what this question is about
        send_options(item['chat'], item['text'], [tuple(b) for b in item['buttons']], options_path, outbox)
        done_chats.add(item['chat'])
        sent += 1
    p.write_text(json.dumps(keep, ensure_ascii=False), encoding='utf-8')
    return sent


def _settled(chat: str, text: str) -> bool:
    """The question is about something the owner already closed (agent/closure.py)."""
    try:
        from agent import closure
        return bool(closure.is_closed(chat, text))
    except Exception:
        return False


# ── 19/11 one open numbered question at a time ─────────────────────────────────────────────────────────────────────────
# The owner: a question with 1 2 3, then another one also with 1 2 3, he answered "2" and the bot didn't know which he meant.
# Every question goes through send_options, so the rules live here:
#   • the newest question owns the bare digits; an older one still open is held behind it (o['by'] = its id) and comes
#     back once after it is answered (reask_open). A background question (busy='hold') waits instead, in the deferred queue.
#   • the same question again (same kind and key, or the same first line) closes the old one, said in one short line.
#   • one that can't wait (a due reminder, an escalation: busy='letters') is labelled א/ב/ג beside an open numbered one.
#   • a reply that quotes a question binds to it (the bridge's receipt names the message); one quoting an answered
#     question binds to nothing (GONE).
#   • two questions a bare answer could still mean → 'oq:which:…': the bot asks which, naming both (ask_which).
#   • after he has written about other things for a while, a bare digit no longer binds to an old question.
HEB = 'אבגדהוזחט'
STALE_AFTER = 6 * 3600            # he wrote about other things, and this long passed → a bare digit no longer binds
SAID_MAX = 5                      # this many messages of his since the question → the topic moved on
DONE_KEY = '_done'                # answered / replaced questions: a quote of one of them binds to nothing
CLARIFY_Q = '🤔 התכוונת ל'
GONE = 'oq:gone'
_COLLECT: ContextVar[list | None] = ContextVar('ssa_outbox_collect', default=None)


def _style(o: dict) -> str:
    return o.get('style') or 'num'


def _live(lst: list[dict]) -> list[dict]:
    """The entries not held behind a newer question that is still open."""
    ids = {o.get('id') for o in lst if o.get('id')}
    # from before 19/11 (no id): the newest one owned the digits, as it did then; the older ones wait behind it
    return [o for i, o in enumerate(lst) if not (o.get('by') and o['by'] in ids and o['by'] != o.get('id'))
            and not (not o.get('id') and i < len(lst) - 1)]


def _bindable(o: dict, now: float | None = None) -> bool:
    """A bare digit may still answer it: nothing written since, or a little, not long ago."""
    said = o.get('said', 1 if o.get('replied') else 0)      # 'replied' from before the count: at least one message
    if not said:
        return True
    return said < SAID_MAX and (now or time.time()) - o.get('ts', 0) <= STALE_AFTER


def _strip_reask(text: str) -> str:
    t = text or ''
    while t.startswith(REASK):
        t = t[len(REASK):]
    return t


def _first(text: str) -> str:
    return next((ln for ln in _strip_reask(text).splitlines() if ln.strip()), '')[:80]


def topic(o: dict | str) -> str:
    """'🔔 תזכורת נוספת לפאדל? אזכיר כאן…' → 'תזכורת נוספת לפאדל': what a question is about, in a few words."""
    t = o if isinstance(o, str) else (o.get('q') or _first(o.get('text') or ''))
    t = _first(t).replace('*', '').replace('_', '')
    t = re.sub(r'^[^\wא-ת"]+', '', t)
    t = re.split(r'\?|:|\s[—–-]\s|\(', t)[0].strip()
    if len(t) > 40:
        t = t[:40].rsplit(' ', 1)[0] + '…'
    return t or 'השאלה הקודמת'


def _fam_key(payload) -> tuple[str, str]:
    parts = str(payload).split(':')
    return parts[0], (parts[-1] if len(parts) >= 3 else '')


def _same_subject(o: dict, q: str, buttons: list) -> bool:
    """The new question asks what this one asked: same kind of answer, and the same thing (its key, or its first line)."""
    if not o.get('payloads') or not buttons:
        return False
    (f1, k1), (f2, k2) = _fam_key(o['payloads'][0]), _fam_key(buttons[0][1])
    return f1 == f2 and ((bool(k1) and k1 == k2) or _q_key(o.get('q', '')) == _q_key(q))


def _done_add(allopts: dict, chat: str, o: dict, moved: bool = False) -> None:
    rows = (allopts.setdefault(DONE_KEY, {}).get(chat) or [])
    rows.append({'files': o.get('files') or [], 'q': o.get('q', ''), 'ts': time.time(),
                 'key': _q_key(o.get('text') or o.get('q', '')), **({'moved': True} if moved else {})})
    allopts[DONE_KEY][chat] = [r for r in rows if time.time() - r.get('ts', 0) <= LATE_TTL][-12:]


def _receipt_name(msg_id: str | None, outbox: Path | None = None) -> str:
    """The bridge's receipt (sent/<name>.json) with this WhatsApp id → the outbox file's name ('' = not ours)."""
    if not msg_id:
        return ''
    try:
        files = sorted(((outbox or OUTBOX) / 'sent').glob('*.json'), key=lambda p: p.name)[-600:]
    except OSError:
        return ''
    for f in reversed(files):
        try:
            r = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get('ok') and r.get('id') == msg_id:
            return f.name
    return ''


def send_options(chat: str, text: str, buttons: list, options_path: Path | None = None,
                 outbox: Path | None = None, reasked_from: float | None = None, busy: str = 'park',
                 files: list | None = None) -> bool:
    """Buttons → '1️⃣ label' lines; remember payloads so a digit reply maps back. Never in a group: nothing is
    confirmed there (05/11). busy (another question is open): 'park' = this one is the answer to what he just wrote, it
    takes the digits and the open one waits behind it · 'hold' = this one waits (the deferred queue) · 'letters' = it can't
    wait: א/ב/ג beside the numbered one."""
    if chat.endswith('@g.us'):
        return False
    buttons = list(buttons)[:9]
    p = options_path or OPTIONS
    try:
        allopts = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        allopts = {}
    lst = _open_list(allopts, chat)
    _keep_late(allopts, chat, lst)
    now = time.time()
    waiting = [o for o in _live(lst) if now - o.get('ts', 0) < OPEN_WINDOW]
    if busy == 'hold' and waiting:
        defer_options(chat, text, buttons, _deferred_for(options_path))
        print(f'  ⏸️ outbox: "{topic(text)}" waits until "{topic(waiting[-1])}" is answered')
        return True
    style = 'abc' if busy == 'letters' and any(_style(o) == 'num' for o in waiting) else 'num'
    body, q = _strip_reask(text), _first(text)
    line = ''
    for o in list(lst):                                   # the same question again: the old one is closed, and said so
        if not body.startswith(CLARIFY_Q) and _same_subject(o, q, buttons):
            lst.remove(o)
            _done_add(allopts, chat, o)
            if _q_key(_strip_reask(o.get('text') or '')) != _q_key(body) and not line:
                line = f'↪️ במקום השאלה הקודמת ({topic(o)}):'
    nid = secrets.token_hex(3)
    for o in _live(lst):                                  # what was open waits behind this one, and comes back once after
        if _style(o) == style:
            o['by'] = nid
            o['requeue'] = now - o.get('first_ts', o.get('ts', 0)) <= REASK_MAX_AGE
    if files is None:                                     # 17/11 pulled by "כן פאדל" and asked again: a quote of the
        done = (allopts.get(DONE_KEY) or {}).get(chat) or []       # first message still binds to it
        row = next((r for r in reversed(done) if r.get('moved') and r.get('key') == _q_key(body)), None)
        if row:
            files = list(row.get('files') or [])
            done.remove(row)
    entry = {'payloads': [pl for _, pl in buttons], 'labels': [lbl for lbl, _ in buttons], 'ts': now, 'q': q,
             'text': text[:3000], 'id': nid, 'files': list(files or [])}
    if style == 'abc':
        entry['style'] = 'abc'
    if body.startswith(CLARIFY_Q):
        entry['clarify'] = True
    if reasked_from:
        entry.update(reasked=True, first_ts=reasked_from)
    lst.append(entry)
    allopts[chat] = lst[-6:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
    marks = DIGITS if style == 'num' else [f'*{h})*' for h in HEB]
    foot = 'השב במספר.' if style == 'num' else 'השב באות (א, ב…).'
    lines = ([line] if line else []) + [text, ''] + [f'{marks[i]} {lbl}' for i, (lbl, _) in enumerate(buttons)] + ['', foot]
    names: list = []
    tok = _COLLECT.set(names)
    try:
        ok = send_text(chat, '\n'.join(lines), outbox=outbox)
    finally:
        _COLLECT.reset(tok)
    if names:                                             # the outbox files → the WhatsApp ids a quote carries
        try:
            cur = json.loads(p.read_text(encoding='utf-8'))
            for o in cur.get(chat) or []:
                if isinstance(o, dict) and o.get('id') == nid:
                    o['files'] = o.get('files', []) + names
            p.write_text(json.dumps(cur, ensure_ascii=False), encoding='utf-8')
        except (OSError, ValueError):
            pass
    return ok


# 17/11 13:52 "1 2" and 14:00 "2 ו1" to "🔔 תזכורת נוספת? 1️⃣ יום לפני 2️⃣ שעתיים לפני…": both were lost, then the model said
# "הגדרתי תזכורות" and nothing was set. Several digits, one question: only where the options add up (ADDITIVE), the
# picked values join into one payload ('rm:1440+120:key'); anywhere else several digits stay unanswered (which one?).
MANY_RX = re.compile(r'(?:גם\s*)?\d(?:\s*(?:,|\+|&|וגם|ו-?|גם|\s)\s*(?:גם\s*)?\d)+')
ADDITIVE = ('rm:',)
KEYCAP_RX = re.compile('[️⃣]')
LETTER_RX = re.compile(r"(?:אות\s+)?([א-ט])['׳]?\)?")
MANY_ABC_RX = re.compile(r"[א-ט](?:\s*(?:,|\+|&|וגם|\s)\s*ו?[א-ט])+")


def _combine(payloads: list, picks: list[int]) -> str | None:
    """'rm:1440:k' + 'rm:120:k' → 'rm:1440+120:k'. None when any pick is out of range, a 'none' option, or not additive."""
    if not all(0 < i <= len(payloads) for i in picks):
        return None
    chosen = [str(payloads[i - 1]) for i in picks]
    parts = [c.split(':') for c in chosen]
    if not all(c.startswith(ADDITIVE) and len(x) == 3 for c, x in zip(chosen, parts)):
        return None
    if len({(x[0], x[2]) for x in parts}) != 1 or any(x[1] == 'none' or not x[1].isdigit() for x in parts):
        return None
    return f'{parts[0][0]}:{"+".join(x[1] for x in parts)}:{parts[0][2]}'


def _picks(text: str) -> tuple[list[int], str]:
    """His answer → (the 1-based picks, 'num' | 'abc'). '2', '2️⃣', 'אני 2', '2 לא…', '1 2', 'ב', "ב'", 'א וב'."""
    t = KEYCAP_RX.sub('', text or '').strip().strip('.')
    # "2 לא להוסיף ליומן": the digit plus a yes/no gloss; 05/11 "אני 1" (speech to text): the same digit
    m = re.match(r'^(\d)[\s.:)\-]+(?:לא|כן)\b', t) or re.fullmatch(r'(?:אני|מספר|אופציה)\s+(\d)', t)
    if m:
        t = m.group(1)
    if t.isdigit():
        return [int(t)], 'num'
    if MANY_RX.fullmatch(t):
        picks = [int(x) for x in re.findall(r'\d', t)]
        return (picks if len(set(picks)) >= 2 else []), 'num'
    if (m := LETTER_RX.fullmatch(t)):
        return [HEB.index(m.group(1)) + 1], 'abc'
    if MANY_ABC_RX.fullmatch(t):
        toks = [x[1:] if len(x) == 2 and x[0] == 'ו' else x for x in re.split(r'\s*(?:,|\+|&|וגם|\s)\s*', t) if x]
        if all(len(x) == 1 and x in HEB for x in toks) and len(set(toks)) >= 2:
            return [HEB.index(x) + 1 for x in toks], 'abc'
    return [], ''


def _answer(o: dict, picks: list[int]) -> str | None:
    pls = o.get('payloads') or []
    if len(picks) == 1:
        return pls[picks[0] - 1] if 0 < picks[0] <= len(pls) else None
    return _combine(pls, list(dict.fromkeys(picks)))


def _candidates(lst: list[dict], picks: list[int], style: str) -> list[tuple[dict, str]]:
    """The open questions a bare answer could mean: the live ones of its label kind (a digit to the א/ב one only when no
    numbered question is open), still bindable, where the answer fits."""
    live = _live(lst)
    pool = [o for o in live if _style(o) == style]
    if not pool and style == 'num':
        pool = live
    return [(o, pl) for o in pool if _bindable(o) and (pl := _answer(o, picks)) is not None]


def _consume(allopts: dict, chat: str, o: dict, pl: str, p: Path, options_path: Path | None,
             meta: dict | None, late: bool = False) -> str:
    lst = _open_list(allopts, chat)
    q, ts = o.get('q', ''), o.get('ts', 0)
    if late:
        lt = allopts.setdefault(LATE_KEY, {})
        lt[chat] = [x for x in lt.get(chat) or [] if x != o]
    else:
        lst = [x for x in lst if x != o]
    _done_add(allopts, chat, o)
    if o.get('clarify'):                                  # "which one?" answered: that question is answered too
        fk = _fam_key(pl)
        src = [x for x in lst if pl in (x.get('payloads') or [])
               or (fk[1] and fk in {_fam_key(y) for y in x.get('payloads') or []})]
        for x in src:
            lst.remove(x)
            _done_add(allopts, chat, x)
        if src:
            q, ts = src[0].get('q', q), src[0].get('ts', ts)
    if lst:
        allopts[chat] = lst
    else:
        allopts.pop(chat, None)
    p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
    if options_path is None:                              # the budget: this kind of question isn't being ignored
        try:
            note_answered(chat, pl, o.get('text') or o.get('q', ''))
        except Exception:
            pass
    if meta is not None:
        meta.update(q=q, stale=late or time.time() - ts > OPTIONS_FRESH)
        if lst:
            meta['more'] = len(lst)
    return pl


def take_option(chat: str, text: str, options_path: Path | None = None, meta: dict | None = None,
                reply_to: str | None = None, outbox: Path | None = None) -> str | None:
    """A bare answer ('2', '1 2', 'ב') to an open question → that payload (consumed once); GONE for a quote of a
    question already answered; 'oq:which:<picks>:<kind>' when two open questions could be meant (ask_which).
    reply_to: the WhatsApp id he quoted — that question, and only that one.
    `meta` (if given) gets {'q': first line of that question, 'stale': answered after 30+ min, 'more': still open}."""
    picks, style = _picks(text)
    if not picks:
        return None
    p = options_path or OPTIONS
    try:
        allopts = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    lst = _open_list(allopts, chat)
    if reply_to and (name := _receipt_name(reply_to, outbox)):    # 19/11 a quoted question wins
        late = [o for o in (allopts.get(LATE_KEY) or {}).get(chat) or [] if time.time() - o.get('ts', 0) <= LATE_TTL]
        o = next((x for x in lst + late if name in (x.get('files') or [])), None)
        if o is not None:
            pl = _answer(o, picks)
            return _consume(allopts, chat, o, pl, p, options_path, meta, late=o not in lst) if pl else None
        if any(name in (r.get('files') or []) for r in (allopts.get(DONE_KEY) or {}).get(chat) or []):
            return GONE
        return None                                       # he quoted another message of ours: not an open question
    cands = _candidates(lst, picks, style)
    late = False
    if not cands and not lst and style == 'num':          # 03/11: a "1" days later — the latest expired question, said so
        _keep_late(allopts, chat, [])
        old = [o for o in (allopts.get(LATE_KEY) or {}).get(chat) or []
               if time.time() - o.get('ts', 0) <= LATE_TTL and not o.get('said')]     # 19/11 only if nothing came since
        if old and (pl := _answer(old[-1], picks)):
            cands, late = [(old[-1], pl)], True
    if len(cands) == 1:
        return _consume(allopts, chat, cands[0][0], cands[0][1], p, options_path, meta, late=late)
    if len(cands) > 1:
        print(f'  🤔 "{text}" fits {len(cands)} open questions: asking which')
        return f'oq:which:{"+".join(map(str, picks))}:{style}'
    return None


def ask_which(chat: str, picks: str, style: str = 'num', options_path: Path | None = None) -> tuple[str, list] | None:
    """19/11: a bare answer two open questions could mean → 'התכוונת ל"X" או ל"Y"?', each option the real answer to one
    of them (its payload): nothing is picked for him. None = it's no longer ambiguous."""
    ps = [int(x) for x in str(picks).split('+') if x.isdigit()]
    try:
        lst = _open_list(json.loads((options_path or OPTIONS).read_text(encoding='utf-8')), chat)
    except (OSError, ValueError):
        return None
    cands = _candidates(lst, ps, style or 'num')
    if len(cands) < 2 or not ps:
        return None
    cands = cands[-3:]                                    # the newest three: a longer "which one?" is no help
    names = [topic(o) for o, _ in cands]
    text = CLARIFY_Q + ' או ל'.join(f'"{n}"' for n in names) + '?'
    buttons = [(f'{n}: {" + ".join(str(o["labels"][i - 1]) for i in ps if 0 < i <= len(o.get("labels") or []))}', pl)
               for n, (o, pl) in zip(names, cands)]
    return text, buttons


def pull_question(chat: str, words: str, options_path: Path | None = None) -> dict | None:
    """17/11 "כן פאדל" → "רק לוודא, על מה מדובר?" twice, with the 🔔 question still open. The open numbered question his
    words name (or the only one) comes off the list and is returned to be asked again; several and none named → None."""
    from agent import closure
    p = options_path or OPTIONS
    try:
        allopts = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    lst = [o for o in _open_list(allopts, chat) if o.get('labels')]
    said = closure.stems(words or '')
    named = [o for o in lst if said & closure.stems(o.get('text') or o.get('q', ''))]
    o = named[-1] if named else (lst[0] if len(lst) == 1 else None)
    if o is None:
        return None
    allopts[chat] = [x for x in _open_list(allopts, chat) if x is not o]
    if not allopts[chat]:
        allopts.pop(chat, None)
    _done_add(allopts, chat, o, moved=True)              # asked again right away: its old message still binds to it
    p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
    text = _strip_reask(o.get('text') or o.get('q', ''))
    return {'text': text, 'buttons': list(zip(o['labels'], o['payloads']))}


def open_questions(chat: str, options_path: Path | None = None) -> list[dict]:
    """The chat's numbered questions still waiting for an answer (oldest first) — for the assistant's context."""
    try:
        return [o for o in _open_list(json.loads((options_path or OPTIONS).read_text(encoding='utf-8')), chat)
                if o.get('labels')]
    except (OSError, ValueError):
        return []


def mark_replied(chat: str, options_path: Path | None = None) -> None:
    """05/11: the owner wrote something while a question was open. Maybe it was the answer ("אני לא ניגש למבחן", three re-asks on
    03/11; "אני 1"), maybe not — the question stays answerable, but it no longer comes back by itself.
    19/11 counted: after a few messages about other things (and a few hours), a bare digit no longer binds to it."""
    p = options_path or OPTIONS
    try:
        allopts = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    lst = _open_list(allopts, chat)
    late = (allopts.get(LATE_KEY) or {}).get(chat) or []
    if lst or late:
        for o in lst + late:
            o.update(replied=True, said=o.get('said', 0) + 1, requeue=False)
        if lst:
            allopts[chat] = lst
        p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')


def _reaskable(o: dict) -> bool:
    return bool(o.get('text') and o.get('labels')) and not o.get('reasked') \
        and (not o.get('replied') or o.get('requeue')) \
        and time.time() - o.get('first_ts', o.get('ts', 0)) <= REASK_MAX_AGE


def close_open(chat: str, n: int, options_path: Path | None = None) -> dict | None:
    """03/11 ("אני לא ניגש למבחן" in words → the review offer came back 3h later as "עוד שאלה פתוחה"): a question
    The owner answered in words is closed. `n` = its 1-based place in open_questions(). → that question (or None)."""
    p = options_path or OPTIONS
    try:
        allopts = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    lst = _open_list(allopts, chat)
    shown = [o for o in lst if o.get('labels')]
    if not 1 <= n <= len(shown):
        return None
    o = shown[n - 1]
    lst.remove(o)
    if lst:
        allopts[chat] = lst
    else:
        allopts.pop(chat, None)
    _done_add(allopts, chat, o)
    p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
    if options_path is None:
        try:
            note_answered(chat, o['payloads'][0] if o.get('payloads') else '', o.get('text') or o.get('q', ''))
        except Exception:
            pass
    return o


def reask_open(chat: str, options_path: Path | None = None, outbox: Path | None = None) -> bool:
    """After an answer, the next still-open question comes back to the bottom of the chat (so a '1' is never lost).
    05/11: once per question, within REASK_MAX_AGE, and only if the owner hasn't written anything since it was asked (19/11: or
    it was held behind the question he just answered); the others stay answerable, they're just not sent again."""
    p = options_path or OPTIONS
    try:
        allopts = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    lst = _open_list(allopts, chat)
    o = next((x for x in reversed(_live(lst)) if _reaskable(x)), None)
    if o is None:
        return False
    text = _strip_reask(o['text'])                       # 18/10: "↩️ עוד שאלה פתוחה: ↩️ עוד שאלה פתוחה: …"
    if options_path is None and _settled(chat, text):
        o['replied'], o['requeue'] = True, False
        allopts[chat] = lst
        p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
        return False
    lst.remove(o)
    allopts[chat] = lst
    p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
    return send_options(chat, REASK + text, list(zip(o['labels'], o['payloads'])), p, outbox,
                        o.get('first_ts', o.get('ts')), busy='letters' if _style(o) == 'abc' else 'park',
                        files=o.get('files') or [])
