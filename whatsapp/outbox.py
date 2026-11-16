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
    try:
        lst = _open_list(json.loads((options_path or OPTIONS).read_text(encoding='utf-8')), chat)
    except (OSError, ValueError):
        return False
    return bool(lst) and time.time() - lst[-1].get('ts', 0) < OPEN_WINDOW


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


def release_deferred(path: Path | None = None, options_path: Path | None = None, outbox: Path | None = None,
                     kinds_path: Path | None = None) -> int:
    """Send the oldest waiting question of each chat whose previous question is answered / stale. → how many sent."""
    p = path or DEFERRED
    try:
        cur = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0
    sent, keep, done_chats = 0, [], set()
    for item in cur:
        if time.time() - item.get('ts', 0) > DEFERRED_MAX_AGE:
            continue
        if item['chat'] in done_chats or options_open(item['chat'], options_path):
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


def send_options(chat: str, text: str, buttons: list, options_path: Path | None = None,
                 outbox: Path | None = None, reasked_from: float | None = None) -> bool:
    """Buttons → '1️⃣ label' lines; remember payloads so a digit reply maps back. Never in a group: nothing is
    confirmed there (05/11)."""
    if chat.endswith('@g.us'):
        return False
    buttons = list(buttons)[:9]
    lines = [text, ''] + [f'{DIGITS[i]} {lbl}' for i, (lbl, _) in enumerate(buttons)] + ['', 'השב במספר.']
    p = options_path or OPTIONS
    try:
        allopts = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        allopts = {}
    lst = _open_list(allopts, chat)
    _keep_late(allopts, chat, lst)
    lst.append({'payloads': [pl for _, pl in buttons], 'labels': [lbl for lbl, _ in buttons], 'ts': time.time(),
                'q': next((ln for ln in text.splitlines() if ln.strip()), '')[:80], 'text': text[:3000]})
    if reasked_from:
        lst[-1].update(reasked=True, first_ts=reasked_from)
    allopts[chat] = lst[-5:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
    return send_text(chat, '\n'.join(lines), outbox=outbox)


def take_option(chat: str, text: str, options_path: Path | None = None, meta: dict | None = None) -> str | None:
    """A bare digit answering the latest numbered options → that payload (consumed once).
    `meta` (if given) gets {'q': first line of that question, 'stale': answered after 30+ min}."""
    t = (text or '').strip().strip('.')
    # "2 לא להוסיף ליומן": the digit plus a yes/no gloss; 05/11 "אני 1" (speech to text): the same digit
    m = re.match(r'^(\d)[\s.:)\-]+(?:לא|כן)\b', t) or re.fullmatch(r'(?:אני|מספר|אופציה)\s+(\d)', t)
    if m:
        t = m.group(1)
    if not t.isdigit():
        return None
    p = options_path or OPTIONS
    try:
        allopts = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    lst = _open_list(allopts, chat)
    late = False
    if not lst:                                         # 03/11: a "1" days later — the latest expired question, said so
        _keep_late(allopts, chat, [])
        old = [o for o in (allopts.get(LATE_KEY) or {}).get(chat) or [] if time.time() - o.get('ts', 0) <= LATE_TTL]
        if not old:
            return None
        lst, late = old, True
    o = lst[-1]                                         # a bare digit answers the latest open question
    i = int(t) - 1
    if not 0 <= i < len(o['payloads']):
        return None
    rest = lst[:-1]
    if late:
        allopts.setdefault(LATE_KEY, {})[chat] = rest
        rest = []
    if rest:
        allopts[chat] = rest
    else:
        allopts.pop(chat, None)
    p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
    if options_path is None:                            # the budget: this kind of question isn't being ignored
        try:
            note_answered(chat, o['payloads'][i], o.get('text') or o.get('q', ''))
        except Exception:
            pass
    if meta is not None:
        meta.update(q=o.get('q', ''), stale=late or time.time() - o.get('ts', 0) > OPTIONS_FRESH)
        if rest:
            meta['more'] = len(rest)
    return o['payloads'][i]


def open_questions(chat: str, options_path: Path | None = None) -> list[dict]:
    """The chat's numbered questions still waiting for an answer (oldest first) — for the assistant's context."""
    try:
        return [o for o in _open_list(json.loads((options_path or OPTIONS).read_text(encoding='utf-8')), chat)
                if o.get('labels')]
    except (OSError, ValueError):
        return []


def mark_replied(chat: str, options_path: Path | None = None) -> None:
    """05/11: The owner wrote something while a question was open. Maybe it was the answer ("אני לא ניגש למבחן", three re-asks on
    03/11; "אני 1"), maybe not — the question stays answerable, but it no longer comes back by itself."""
    p = options_path or OPTIONS
    try:
        allopts = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    lst = _open_list(allopts, chat)
    if lst:
        for o in lst:
            o['replied'] = True
        allopts[chat] = lst
        p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')


def _reaskable(o: dict) -> bool:
    return bool(o.get('text') and o.get('labels')) and not o.get('reasked') and not o.get('replied') \
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
    p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
    if options_path is None:
        try:
            note_answered(chat, o['payloads'][0] if o.get('payloads') else '', o.get('text') or o.get('q', ''))
        except Exception:
            pass
    return o


def reask_open(chat: str, options_path: Path | None = None, outbox: Path | None = None) -> bool:
    """After an answer, the next still-open question comes back to the bottom of the chat (so a '1' is never lost).
    05/11: once per question, within REASK_MAX_AGE, and only if the owner hasn't written anything since it was asked; the
    others stay answerable, they're just not sent again."""
    p = options_path or OPTIONS
    try:
        allopts = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    lst = _open_list(allopts, chat)
    o = next((x for x in reversed(lst) if _reaskable(x)), None)
    if o is None:
        return False
    text = o['text']
    while text.startswith(REASK):                       # 18/10: "↩️ עוד שאלה פתוחה: ↩️ עוד שאלה פתוחה: …"
        text = text[len(REASK):]
    if options_path is None and _settled(chat, text):
        o['replied'] = True
        allopts[chat] = lst
        p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
        return False
    lst.remove(o)
    allopts[chat] = lst
    p.write_text(json.dumps(allopts, ensure_ascii=False), encoding='utf-8')
    return send_options(chat, REASK + text, list(zip(o['labels'], o['payloads'])), p, outbox, o.get('first_ts', o.get('ts')))
