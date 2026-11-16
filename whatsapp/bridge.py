"""
whatsapp/bridge.py — WhatsApp linked-device bridge for the owner's dedicated bot number (neonize/whatsmeow).

The owner chose this over the Meta Cloud API (free, no open port): the server is a LINKED DEVICE of the
dedicated number 052-…, like WhatsApp Web. Unofficial → the number may be banned; it is dedicated
to the bot, so the blast radius is that number only.

Isolation (deploy/systemd/smart-study-wa-bridge.service): its own venv (venv_wa), and the process
sees only this file's directory, venv_wa and its data dirs — no .env, no API keys, no project
code. It does NOT import the project: it only relays.
  inbound  : message from an ALLOWED sender → data/wa_spool/<ts>_<id>.json   (worker → agent)
  outbound : data/wa_outbox/<name>.json → send → receipt in data/wa_outbox/sent/<name>.json
Everyone else is ignored silently (no reply, nothing stored but a counter).

Who is allowed: WA_ALLOWED (phone numbers, e.g. 9725XXXXXXXX), or a chat that sent the one-time
WA_PAIR_SECRET (needed when WhatsApp hides the number behind a LID). Enrolled chats are kept in
data/wa_bridge/allowed.json.

First run (no session): links via pairing code — the code is written to data/wa_bridge/pair_code.txt;
The owner enters it on the bot phone: WhatsApp → Linked devices → Link a device → "Link with phone number".

Env: WA_BOT_NUMBER (972…, required for pairing), WA_ALLOWED, WA_PAIR_SECRET (optional).
    venv_wa/bin/python whatsapp/bridge.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('WA_DATA_DIR') or ROOT / 'data')
BRIDGE = DATA / 'wa_bridge'
SPOOL = DATA / 'wa_spool'
OUTBOX = DATA / 'wa_outbox'
MEDIA = DATA / 'wa_media'
SESSION = BRIDGE / 'session.sqlite3'
ALLOWED_FILE = BRIDGE / 'allowed.json'
GATE_FILE = BRIDGE / 'group_gate.json'   # written by agent/group_mode.py: is it on, which groups, who's in a live debate

log = logging.getLogger('wa_bridge')


# ── pure helpers (unit-tested without neonize) ──────────────────────────────

def digits(s: str) -> str:
    s = re.sub(r'\D', '', s or '')
    return '972' + s[1:] if s.startswith('0') and len(s) == 10 else s     # 05X-XXXXXXX → 9725XXXXXXXX


def load_roles(path: Path | None = None) -> dict[str, str]:
    """{chat or phone: 'owner' | 'partner'}. WA_ALLOWED phones are owners; allowed.json holds
    enrolled chats (an old plain list = owners)."""
    roles = {digits(x): 'owner' for x in (os.environ.get('WA_ALLOWED') or '').split(',') if digits(x)}
    try:
        data = json.loads((path or ALLOWED_FILE).read_text(encoding='utf-8'))
        roles.update(data if isinstance(data, dict) else {c: 'owner' for c in data})
    except (OSError, ValueError):
        pass
    return roles


def load_allowed(path: Path | None = None) -> set[str]:
    return set(load_roles(path))


def role_of(identities: set[str], roles: dict[str, str]) -> str | None:
    hits = [roles[i] for i in identities if i in roles]
    return 'owner' if 'owner' in hits else (hits[0] if hits else None)


def enroll(chat_id: str, path: Path | None = None, role: str = 'owner') -> None:
    p = path or ALLOWED_FILE
    cur: dict = {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        cur = data if isinstance(data, dict) else {c: 'owner' for c in data}
    except (OSError, ValueError):
        pass
    cur[chat_id] = role
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, ensure_ascii=False), encoding='utf-8')
    os.chmod(p, 0o600)


INVITE_RX = re.compile(r'SSA-?([A-Z2-9]{4})-?([A-Z2-9]{4})', re.I)


def redeem_invite(text: str, chat_id: str, path: Path | None = None, now: float | None = None) -> dict | None:
    """03/11 a one-time code the owner got from the bot (whatsapp/invites.py) → that invite, now used; else None."""
    import hashlib
    m = INVITE_RX.search(text or '')
    if not m:
        return None
    p = path or BRIDGE / 'invites.json'
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    key = hashlib.sha256(('SSA' + m.group(1) + m.group(2)).upper().encode()).hexdigest()
    inv = d.get(key)
    now = now or time.time()
    if not inv or inv.get('used') or inv.get('expires', 0) < now:
        return None
    inv['used'] = {'chat': chat_id, 'ts': now}
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    return inv


def is_allowed(identities: set[str], allowed: set[str]) -> bool:
    """identities: the sender's phone digits and/or LID user ids and full chat ids."""
    return bool({i for i in identities if i} & allowed)


def spool_write(msg: dict, d: Path | None = None) -> bool:
    d = d or SPOOL
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^A-Za-z0-9_.=-]', '_', str(msg['id']))[:120]
    if list(d.glob(f'*_{safe}.json')) or list((d / 'done').glob(f'*_{safe}.json')):
        return False
    tmp = d / f'.{safe}.tmp'
    tmp.write_text(json.dumps(msg, ensure_ascii=False), encoding='utf-8')
    tmp.replace(d / f'{int(time.time() * 1000)}_{safe}.json')
    return True


MENTION_RX = re.compile(r'@\d{6,}\s*')
GROUP_KEEP_S = 48 * 3600                 # group context kept in memory only — never written to disk
GROUP_KEEP_N = 200


def group_request(sender_role: str | None, mentioned: list[str], own: set[str], text: str) -> str | None:
    """16/10 (The owner: "רק שאני מתייג אותו"): in a group the bot acts ONLY on the owner's own message that tags the bot.
    → the request with the tag removed ('' = just a tag), or None = ignore (anyone else, or the owner without a tag)."""
    if sender_role != 'owner':
        return None
    if not ({(m or '').split('@')[0].split(':')[0] for m in mentioned} & {o for o in own if o}):
        return None
    return MENTION_RX.sub('', text or '').strip()


GROUP_ON_RX = re.compile(r'\s*(?:תפעיל|הפעל)\s+(?:את\s+עצמך\s+)?(?:כאן|פה|בקבוצה(?:\s+הזו)?)\s*[.!]*\s*')
GROUP_OFF_RX = re.compile(r'\s*(?:תכבה|כבה)\s+(?:את\s+עצמך\s+)?(?:כאן|פה|בקבוצה(?:\s+הזו)?)\s*[.!]*\s*')
DEBATE_MAX = 2000                        # what the bridge passes on; the worker cuts further
MAX_AGE_S = 300                          # after a reconnect old messages arrive: a tag from yesterday starts nothing


def stale(timestamp, now: float | None = None) -> bool:
    """Info.Timestamp: Unix seconds, or milliseconds on another library version. Zero or unreadable counts as old."""
    try:
        t = float(timestamp)
        return (now or time.time()) - (t / 1000 if t > 1e11 else t) > MAX_AGE_S
    except (TypeError, ValueError):
        return True


def load_gate(path: Path | None = None) -> dict:
    """No file or a broken one = everything off."""
    try:
        d = json.loads((path or GATE_FILE).read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def group_event(role: str | None, mentioned: list[str], own: set[str], text: str, sender: set[str], gid: str, gate: dict,
                kind: str = 'text', forwarded: bool = False, edited: bool = False,
                now: float | None = None, old: bool = False) -> tuple[str, str] | None:
    """05/11 the only ways a group message gets past the bridge. ('owner', request): The owner's own new message with a real
    tag, in a group he turned on (or turning it on). ('debate', text): the one person of a live debate. Else None, and
    nothing is written, so no worker, no model. Names and the message's words never decide who someone is."""
    if forwarded or edited or old or not gate.get('on'):
        return None
    on = gid in (gate.get('groups') or [])
    req = group_request(role, mentioned, own, text)
    if req is not None:
        return ('owner', req) if on or GROUP_ON_RX.fullmatch(req) else None
    s = (gate.get('sessions') or {}).get(gid)
    if on and s and role != 'owner' and kind == 'text' and (text or '').strip() and set(sender) & set(s.get('target') or []) \
            and (now or time.time()) < s.get('until', 0):
        return 'debate', text[:DEBATE_MAX]
    return None


def group_targets(mentioned: list[str], own: set[str], quoted_sender: str = '') -> list[str]:
    """Who the owner means: the people tagged besides the bot, else the author of the message he replied to."""
    users = [(m or '').split('@')[0].split(':')[0] for m in mentioned]
    out = list(dict.fromkeys(u for u in users if u and u not in own))
    q = (quoted_sender or '').split('@')[0].split(':')[0]
    return out or ([q] if q and q not in own else [])


def group_context(buf: list, now: float | None = None, limit: int = 150) -> str:
    """The group's recent messages (≤48h) as '[HH:MM] name: text' lines, for "@בוט תסכם"."""
    now = now or time.time()
    rows = [r for r in buf if now - r[0] <= GROUP_KEEP_S][-limit:]
    return '\n'.join(f"[{time.strftime('%d/%m %H:%M', time.localtime(t))}] {n or '?'}: {x}" for t, n, x in rows)


# ── neonize glue ────────────────────────────────────────────────────────────

def quoted_message(m):
    """The message a reply quotes (a waE2E.Message), or None."""
    for f in ('extendedTextMessage', 'imageMessage', 'documentMessage'):
        if m.HasField(f) and getattr(m, f).HasField('contextInfo') and getattr(m, f).contextInfo.HasField('quotedMessage'):
            return getattr(m, f).contextInfo.quotedMessage
    return None


def _mentions(m) -> list[str]:
    """The JIDs tagged in a message (text, or a caption of an image / document)."""
    for f in ('extendedTextMessage', 'imageMessage', 'documentMessage', 'videoMessage'):
        if m.HasField(f) and getattr(m, f).HasField('contextInfo'):
            return list(getattr(m, f).contextInfo.mentionedJID)
    if m.HasField('documentWithCaptionMessage'):
        dm = m.documentWithCaptionMessage.message.documentMessage
        return list(dm.contextInfo.mentionedJID) if dm.HasField('contextInfo') else []
    return []



def _jid_str(jid) -> str:
    return f'{jid.User}@{jid.Server}' if jid and jid.User else ''


def _identities(info) -> set[str]:
    src = info.MessageSource
    ids = {src.Chat.User, src.Sender.User, _jid_str(src.Chat), _jid_str(src.Sender)}
    for alt in ('SenderAlt', 'RecipientAlt'):
        j = getattr(src, alt, None)
        if j is not None and getattr(j, 'User', ''):
            ids |= {j.User, _jid_str(j)}
    return {i for i in ids if i}


def sender_ids(info) -> set[str]:
    """Who wrote this, from the sender fields only: in a group the chat id says nothing about the person."""
    src = info.MessageSource
    ids = {src.Sender.User, _jid_str(src.Sender)}
    j = getattr(src, 'SenderAlt', None)
    if j is not None and getattr(j, 'User', ''):
        ids |= {j.User, _jid_str(j)}
    return {i for i in ids if i}


def _context_info(m):
    for f in ('extendedTextMessage', 'imageMessage', 'documentMessage', 'videoMessage', 'audioMessage'):
        if m.HasField(f) and getattr(m, f).HasField('contextInfo'):
            return getattr(m, f).contextInfo
    return None


def _is_forwarded(m) -> bool:
    """Text, a caption or a voice note: `_context_info` looks in each of them."""
    info = _context_info(m)
    return bool(info is not None and (info.isForwarded or info.forwardingScore))


def _text_and_context(m) -> tuple[str, str, str]:
    """(kind, text/caption, quoted message id) from a waE2E.Message."""
    if m.conversation:
        return 'text', m.conversation, ''
    if m.HasField('extendedTextMessage'):
        e = m.extendedTextMessage
        return 'text', e.text, e.contextInfo.stanzaID if e.HasField('contextInfo') else ''
    if m.HasField('imageMessage'):
        im = m.imageMessage
        return 'image', im.caption, im.contextInfo.stanzaID if im.HasField('contextInfo') else ''
    if m.HasField('audioMessage'):
        am = m.audioMessage
        return 'voice', '', am.contextInfo.stanzaID if am.HasField('contextInfo') else ''
    if m.HasField('contactMessage'):                       # a shared contact card (vCard)
        cm = m.contactMessage
        return 'contact', f'{cm.displayName}\n{cm.vcard}', ''
    if m.HasField('documentMessage'):
        dm = m.documentMessage
        return 'document', dm.caption, dm.contextInfo.stanzaID if dm.HasField('contextInfo') else ''
    if m.HasField('documentWithCaptionMessage'):          # a document sent with a caption is wrapped
        inner = m.documentWithCaptionMessage.message
        if inner.HasField('documentMessage'):
            dm = inner.documentMessage
            return 'document', dm.caption, dm.contextInfo.stanzaID if dm.HasField('contextInfo') else ''
    # 15/11 a message with no text got the help menu. What we can't read says so in one line (agent/core); an edit, a
    # reaction or a sticker stays 'other' and gets nothing
    for field, label in UNREADABLE:
        try:
            if m.HasField(field):
                return 'unsupported', label, ''
        except ValueError:                                 # a field this neonize build doesn't have
            continue
    return 'other', '', ''


UNREADABLE = (('videoMessage', 'סרטון'), ('ptvMessage', 'סרטון'), ('locationMessage', 'מיקום'),
              ('liveLocationMessage', 'מיקום'), ('pollCreationMessage', 'סקר'), ('pollCreationMessageV3', 'סקר'))


DOC_EXT = {'.zip', '.pdf', '.docx', '.xlsx', '.pptx', '.txt', '.csv', '.md', '.json', '.html', '.htm', '.xml', '.py', '.java',
           '.c', '.cpp', '.js', '.ts', '.yaml', '.yml', '.jpg', '.jpeg', '.png', '.webp'}
DOC_MAX = 15 * 1024 * 1024


def _doc_name(m) -> tuple[str, int]:
    dm = m.documentMessage if m.HasField('documentMessage') else m.documentWithCaptionMessage.message.documentMessage
    return (dm.fileName or dm.title or 'file'), int(dm.fileLength or 0)


def group_message(c, ev, groups: dict, own_ids: set[str], attach_media) -> None:
    """One group message. At module level so it runs in a test: inside main() it needs a live neonize client, and on 06/11 an
    undefined name here made every tag of the owner's in a group fail with no reply."""
    src = ev.Info.MessageSource
    gid = _jid_str(src.Chat)
    kind, text, quoted = _text_and_context(ev.Message)
    name = getattr(ev.Info, 'Pushname', '') or src.Sender.User[-4:]
    sender = sender_ids(ev.Info)
    if sender & own_ids:                             # the bot's own message, however it's addressed
        return
    role = role_of(sender, load_roles())
    mentioned = _mentions(ev.Message)
    forwarded = _is_forwarded(ev.Message)
    edited = bool(getattr(ev, 'IsEdit', False) or getattr(ev.Info, 'Edit', ''))
    gate = load_gate()
    event = group_event(role, mentioned, own_ids, text, sender, gid, gate, kind, forwarded, edited,
                        old=stale(ev.Info.Timestamp))
    req = event[1] if event and event[0] == 'owner' else None
    if event and event[0] == 'debate':                   # the one person of a live debate: their words, nothing else
        spool_write({'bridge': True, 'id': ev.Info.ID, 'chat': gid, 'role': 'group_member', 'group': True, 'debate': True,
                     'sender': sorted(u.split('@')[0] for u in sender), 'type': 'text', 'text': event[1]})
        return
    if req is None and role == 'owner' and (mentioned or '@' in (text or '')):
        # The owner tagged someone/something but not (recognisably) the bot — ids only, never the text
        if not gate.get('on'):
            log.info('group %s: groups are off (SSA_GROUPS)', gid[:8])
        elif gid not in (gate.get('groups') or []):
            log.info('group %s: not turned on here', gid[:8])
        else:
            log.info('group %s: owner tag not matched (mentioned=%s own=%s)', gid[:8],
                     [m.split('@')[0][-6:] for m in mentioned], [o[-6:] for o in own_ids])
    if req is not None:
        log.info('group %s: owner tagged the bot', gid[:8])
    if req is None:
        if kind == 'text' and text and gate.get('on') and gid in (gate.get('groups') or []):
            buf = groups.setdefault(gid, [])
            buf.append((time.time(), name, text[:1000]))
            del buf[:-GROUP_KEEP_N]
        return
    ctx_info = _context_info(ev.Message)
    msg = {'bridge': True, 'id': ev.Info.ID, 'chat': gid, 'role': 'owner', 'group': True, 'from': src.Sender.User,
           'type': kind, 'text': req, 'reply_to': quoted, 'context': group_context(groups.get(gid, [])),
           'targets': group_targets(mentioned, own_ids, ctx_info.participant if ctx_info is not None else '')}
    q = quoted_message(ev.Message) if kind == 'text' else None
    # a friend's voice note would become a command once transcribed: a tag can't pick it up
    if q is not None and (qkind := _text_and_context(q)[0]) in ('image', 'document'):
        msg['type'] = qkind                             # "@bot תעבור על זה" as a reply to a file → that file
        attach_media(c, ev, qkind, msg, message=q)
        log.info('group %s: tag replies to a %s — using it', gid[:8], qkind)
    else:
        attach_media(c, ev, kind, msg)
    spool_write(msg)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    from neonize.client import NewClient
    from neonize.events import ConnectedEv, MessageEv, PairStatusEv, LoggedOutEv
    from neonize.utils.jid import build_jid

    for d in (BRIDGE, SPOOL, OUTBOX / 'sent', MEDIA):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(BRIDGE, 0o700)
    fresh = not SESSION.exists()
    client = NewClient(str(SESSION))
    secrets_by_role = {r: (os.environ.get(k) or '').strip() for r, k in
                       (('owner', 'WA_PAIR_SECRET'), ('partner', 'WA_PAIR_SECRET_PARTNER'))}
    ignored = {'n': 0}
    tries: dict[str, int] = {}                             # wrong invite codes per unknown chat

    @client.event(ConnectedEv)
    def _connected(c, ev):
        log.info('connected to WhatsApp')
        (BRIDGE / 'status.json').write_text(json.dumps({'connected': time.time()}), encoding='utf-8')

    @client.event(PairStatusEv)
    def _paired(c, ev):
        log.info('pairing finished')
        (BRIDGE / 'pair_code.txt').unlink(missing_ok=True)

    @client.event(LoggedOutEv)
    def _logged_out(c, ev):
        log.warning('LOGGED OUT — the linked device was removed or the number was banned')
        (BRIDGE / 'status.json').write_text(json.dumps({'logged_out': time.time()}), encoding='utf-8')

    groups: dict[str, list] = {}                          # group → recent (ts, name, text), memory only
    own: set[str] = set()

    def _own_ids(c) -> set[str]:
        if not own:
            try:
                me = c.get_me()
                own.update(x for x in (me.JID.User, me.LID.User) if x)
            except Exception as e:
                log.warning('get_me failed: %s', e)
        return own | {digits(os.environ.get('WA_BOT_NUMBER', ''))}

    def _group(c, ev):
        group_message(c, ev, groups, _own_ids(c), _attach_media)

    def _attach_media(c, ev, kind, msg, message=None):
        """message: the waE2E.Message to download from (default: this event's) — a quoted file in a group."""
        message = message if message is not None else ev.Message
        if kind in ('image', 'voice'):
            ext = '.jpg' if kind == 'image' else '.ogg'
            path = MEDIA / f"{re.sub(r'[^A-Za-z0-9]', '_', ev.Info.ID)}{ext}"
            try:
                c.download_any(message, str(path))
                msg['media'] = str(path)
            except Exception as e:
                log.warning('media download failed: %s', e)
        if kind == 'document':
            try:
                name, size = _doc_name(message)
                ext = os.path.splitext(name)[1].lower()
                safe = re.sub(r'[^\w.\- ]', '_', os.path.basename(name))[:80] or 'file'
                path = MEDIA / f"{re.sub(r'[^A-Za-z0-9]', '_', ev.Info.ID)}__{safe}"
                if ext in DOC_EXT and size <= DOC_MAX:
                    c.download_any(message, str(path))
                    msg['media'] = str(path)
                else:                                    # the agent explains what it can't read
                    msg['media'] = str(path)
                    path.write_bytes(b'')
            except Exception as e:
                log.warning('document download failed: %s', e)

    @client.event(MessageEv)
    def _message(c, ev):
        src = ev.Info.MessageSource
        if src.IsFromMe:
            return
        if src.IsGroup:                                   # only the owner's own message that tags the bot
            try:
                _group(c, ev)
            except Exception as e:
                log.warning('group message failed: %s', e)
            return
        ids = _identities(ev.Info)
        kind, text, quoted = _text_and_context(ev.Message)
        chat = _jid_str(src.Chat)
        role = role_of(ids, load_roles())
        if role is None:
            inv = None
            if kind == 'text' and INVITE_RX.search(text or '') and tries.get(chat, 0) < 5:   # ≤5 guesses per chat
                inv = redeem_invite(text, chat)
                tries[chat] = tries.get(chat, 0) + (0 if inv else 1)
            if inv:
                enroll(chat, role=inv.get('role') or 'friend')
                log.info('enrolled a new %s chat via a one-time invite', inv.get('role'))
                c.send_message(src.Chat, f"✅ היי {inv.get('name', '')}! מעכשיו אפשר לדבר איתי — אני העוזר של המשתמש. "
                                         'כתוב/כתבי "היי" כדי לראות מה אפשר.')
                return
            new_role = next((r for r, sec in secrets_by_role.items()
                             if sec and kind == 'text' and text.strip() == sec), None)
            if new_role:
                enroll(chat, role=new_role)
                log.info('enrolled a new %s chat via its pairing secret', new_role)
                c.send_message(src.Chat, '✅ נרשמת. כתוב "היי" כדי לראות מה אפשר.')
            else:
                ignored['n'] += 1
                (BRIDGE / 'ignored.json').write_text(json.dumps({'count': ignored['n']}), encoding='utf-8')
            return
        msg = {'bridge': True, 'id': ev.Info.ID, 'chat': chat, 'role': role, 'from': src.Chat.User, 'type': kind,
               'text': text, 'reply_to': quoted}
        try:                                               # a forwarded message is content, never a command
            if _is_forwarded(ev.Message):
                msg['forwarded'] = True
        except Exception:                                  # unknown is treated as someone else's: it commands nothing
            msg['forwarded'] = True
        if role == 'contact':                              # which enrolled phone this is (their chat may be a LID)
            roles = load_roles()
            msg['key'] = next((i for i in ids if roles.get(i) == 'contact'), '')
        _attach_media(c, ev, kind, msg)
        spool_write(msg)

    def _outbox_loop():
        while True:
            for f in sorted(OUTBOX.glob('*.json')):
                try:
                    job = json.loads(f.read_text(encoding='utf-8'))
                    user, _, server = job['to'].partition('@')
                    to = build_jid(user, server or 's.whatsapp.net')
                    kind = job.get('kind')
                    if kind in ('video', 'image'):              # playable media; WhatsApp refused it → as a file
                        try:
                            send = client.send_video if kind == 'video' else client.send_image
                            resp = send(to, job['path'], caption=job.get('text') or None)
                        except Exception as e:
                            log.warning('%s send failed, sending as a document: %s', kind, e)
                            kind = 'document'
                    if kind == 'document':
                        resp = client.send_document(to, job['path'], caption=job.get('text') or None,
                                                    filename=Path(job['path']).name)
                    elif kind not in ('video', 'image'):
                        resp = client.send_message(to, job.get('text', ''))
                    receipt = {'ok': True, 'id': getattr(resp, 'ID', '')}
                except Exception as e:
                    log.warning('send failed: %s', e)
                    receipt = {'ok': False, 'error': str(e)[:200]}
                (OUTBOX / 'sent' / f.name).write_text(json.dumps(receipt), encoding='utf-8')
                f.unlink(missing_ok=True)
                try:                                      # the per-job copy of a document
                    dp = Path(job.get('path', ''))
                    if dp.is_file() and (OUTBOX / 'files') in dp.parents:
                        dp.unlink()
                        dp.parent.rmdir()
                except (OSError, NameError):
                    pass
            time.sleep(0.5)

    threading.Thread(target=_outbox_loop, daemon=True).start()

    number = digits(os.environ.get('WA_BOT_NUMBER', ''))
    if fresh and not number:
        log.error('no session and no WA_BOT_NUMBER — cannot pair')
        return 2
    # connect() blocks for the whole session and creates the Go client; pairing needs that client,
    # so run connect in a thread and request the phone code once WhatsApp offers a login (QR event).
    awaiting_login = threading.Event()
    client.event.qr(lambda c, data: awaiting_login.set())          # never print a QR; we pair by code
    conn = threading.Thread(target=client.connect, daemon=True)
    conn.start()
    if fresh or not SESSION.exists() or awaiting_login.wait(20):
        if awaiting_login.wait(60):
            code = client.PairPhone(number, show_push_notification=True)
            (BRIDGE / 'pair_code.txt').write_text(code, encoding='utf-8')
            log.info('pairing code written to %s', BRIDGE / 'pair_code.txt')
        else:
            log.error('WhatsApp did not offer a login within 60s')
    conn.join()
    return 1                         # connect() returned → session ended; systemd restarts us


if __name__ == '__main__':
    raise SystemExit(main())
