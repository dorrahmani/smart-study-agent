"""
vault_sync/mine.py — the owner's own notes ("✍️ שלי") come from the PC to the VM, and lines in them can become reminders (16/11).

Today the vault flows one way: VM → Drive → PC (scripts/sync_after.sh, scripts/vault_pull.ps1). Notes he writes by hand
in Obsidian on the PC never reached the assistant. The safe two-way:

    PC   (vault_pull.ps1)   "✍️ שלי\\" → gdrive:smart-study-agent/vault_inbox   (rclone copy --update, nothing else)
                            + .ssa_manifest.txt: the notes that exist there now. The pull of the vault skips "✍️ שלי".
    VM   (pull())           gdrive:…/vault_inbox → data/vault_inbox_staging     (rclone copy: staging only)
         (merge())          staging → <vault>/✍️ שלי:
                              new                 → copied
                              same bytes          → nothing
                              changed on the PC   → replaced; the VM's old copy moved to .replaced/ (never deleted)
                              the VM copy changed too (nobody should write there) → both kept: "<name> (מה-PC <date>).md"
                              gone from the manifest → moved to .replaced/, so the assistant stops answering from it
    The bot never writes into "✍️ שלי"; the PC never writes anywhere else. So no generated note is ever overwritten and a
    hand-written one is never lost.

Inbox: a line in any of his notes like "- [ ] תזכיר לי לקנות מתנה לבת הזוג ביום חמישי" or "לעוזר: תוסיף ליומן…" is DATA.
It becomes something only after the bot asks him in WhatsApp and he taps ✅ (ask() / button(); core routes 'vi:'). Each line is
asked once, ever (data/vault_inbox.json), inside the proactive question budget (outbox.gate). No model here.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from vault_sync.privacy import MINE

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / 'data' / 'vault_inbox_staging'
STATE = ROOT / 'data' / 'vault_mine_state.json'
INBOX_STATE = ROOT / 'data' / 'vault_inbox.json'
REMOTE = 'gdrive:smart-study-agent/vault_inbox'
MANIFEST = '.ssa_manifest.txt'
ALLOWED = {'.md', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf', '.canvas'}
MAX_BYTES = 20 * 1024 * 1024
ASK_PER_RUN = 2


def _sha(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def _load(p: Path, default):
    try:
        return json.loads(Path(p).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _save(p: Path, d) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')


def pull(staging: Path | None = None, remote: str = REMOTE, run=subprocess.run) -> bool:
    """Drive → staging (rclone COPY, never sync). False when rclone isn't there / failed."""
    st = staging or STAGING
    st.mkdir(parents=True, exist_ok=True)
    try:
        r = run(['rclone', 'copy', remote, str(st), '--update', '--max-size', f'{MAX_BYTES // (1024 * 1024)}M',
                 '--quiet'], capture_output=True, text=True, timeout=300)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _aside(vault_mine: Path, rel: str, now: datetime) -> None:
    src = vault_mine / rel
    to = vault_mine / '.replaced' / f'{rel}.{now:%Y%m%d-%H%M%S}'
    n = 1
    while to.exists():                                       # an older copy aside is never overwritten either
        to = to.with_name(f'{Path(rel).name}.{now:%Y%m%d-%H%M%S}.{n}')
        n += 1
    to.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(to))


def merge(staging: Path, vault_mine: Path, state_path: Path | None = None, now: datetime | None = None) -> dict:
    """staging → the vault's "✍️ שלי". → counts. Never deletes; a conflict keeps both."""
    now = now or datetime.now()
    state = _load(state_path or STATE, {})
    stats = {'new': 0, 'updated': 0, 'same': 0, 'conflict': 0, 'retired': 0, 'skipped': 0}
    if not staging.is_dir():
        return stats
    man = staging / MANIFEST
    keep = set()
    if man.exists():                    # the notes that exist on the PC now (written by vault_pull.ps1 before each push)
        keep = {ln.strip().replace('\\', '/') for ln in man.read_text(encoding='utf-8-sig', errors='replace').splitlines()
                if ln.strip()}
    for src in sorted(staging.rglob('*')):
        if not src.is_file():
            continue
        rel = src.relative_to(staging).as_posix()
        if rel == MANIFEST or rel.startswith('.') or '/.' in rel:
            continue
        if src.suffix.lower() not in ALLOWED or src.stat().st_size > MAX_BYTES:
            stats['skipped'] += 1
            continue
        if keep and rel not in keep:        # deleted on the PC (Drive still has it: copy never deletes there)
            stats['skipped'] += 1
            continue
        dst = vault_mine / rel
        h = _sha(src)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            stats['new'] += 1
        elif _sha(dst) == h:
            stats['same'] += 1
        elif _sha(dst) == state.get(rel):                    # only the PC changed it: the newer one wins, the old is kept aside
            _aside(vault_mine, rel, now)
            shutil.copy2(src, dst)
            stats['updated'] += 1
        else:                                                # both changed: keep both
            alt = dst.with_name(f'{dst.stem} (מה-PC {now:%Y-%m-%d %H%M}){dst.suffix}')
            if not any(_sha(p) == h for p in dst.parent.glob(f'{dst.stem} (מה-PC *){dst.suffix}')):
                shutil.copy2(src, alt)
                stats['conflict'] += 1
            continue
        state[rel] = h
    if keep:                                                 # notes he deleted on the PC: out of the assistant's sight
        for rel in list(state):
            if rel not in keep and (vault_mine / rel).exists():
                _aside(vault_mine, rel, now)
                state.pop(rel, None)
                stats['retired'] += 1
    _save(state_path or STATE, state)
    return stats


# ── the inbox: a line in his notes → a question in WhatsApp → only his ✅ makes it real ──

ITEM_RX = re.compile(r'^\s*(?:[-*]\s*\[ \]\s*)?(?P<what>(?:תזכיר(?:י)?\s+לי|תזכורת\s*:)\s*.+)$|'
                     r'^\s*(?:[-*]\s*(?:\[ \]\s*)?)?(?:לעוזר|@עוזר)\s*[:,-]?\s*(?P<ask>.+)$')


def candidates(vault_mine: Path) -> list[dict]:
    """Every open line addressed to the assistant: [{id, text, file}]. A ticked "- [x]" line is not open."""
    out = []
    if not vault_mine.is_dir():
        return out
    for p in sorted(vault_mine.rglob('*.md')):
        rel = p.relative_to(vault_mine).as_posix()
        if rel.startswith('.') or '/.' in rel:
            continue
        try:
            lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for ln in lines:
            if re.match(r'^\s*[-*]\s*\[[xX]\]', ln):
                continue
            m = ITEM_RX.match(ln)
            if not m:
                continue
            text = re.sub(r'\s+', ' ', (m.group('what') or m.group('ask') or '')).strip()[:300]
            rest = re.sub(r'^(?:תזכיר(?:י)?\s+לי|תזכורת\s*:)\s*', '', text)
            if len(re.sub(r'[\W_]', '', rest)) < 3:          # the template's "- [ ] תזכיר לי ..." is not a request
                continue
            iid = hashlib.sha1(f'{rel}|{text}'.encode('utf-8')).hexdigest()[:10]
            out.append({'id': iid, 'text': text, 'file': rel})
    return out


def ask(chat: str, vault_mine: Path, state_path: Path | None = None, send=None, gate=None, now: datetime | None = None) -> int:
    """New inbox lines → at most ASK_PER_RUN questions with ✅ / ❌. Each line is asked once. → how many asked."""
    if not chat:
        return 0
    now = now or datetime.now()
    st = _load(state_path or INBOX_STATE, {})
    if send is None or gate is None:
        from whatsapp import outbox
        send = send or (lambda t, b: outbox.send_options(chat, t, b))
        gate = gate or (lambda t, b: outbox.gate(chat, t, b))
    asked = 0
    for c in candidates(vault_mine):
        if c['id'] in st or asked >= ASK_PER_RUN:
            continue
        text = (f'✍️ *כתבת ב-Obsidian* ({Path(c["file"]).stem}):\n«{c["text"]}»\n\n'
                'לבצע את זה כאילו כתבת לי עכשיו?')
        buttons = [('✅ כן', f'vi:ok:{c["id"]}'), ('❌ לא, זו רק הערה', f'vi:no:{c["id"]}')]
        g = gate(text, buttons)
        if g == 'defer':
            break
        if g == 'mute':
            st[c['id']] = {**c, 'status': 'muted', 'ts': now.isoformat(timespec='seconds')}
            continue
        if send(text, buttons):
            st[c['id']] = {**c, 'status': 'asked', 'chat': chat, 'ts': now.isoformat(timespec='seconds')}
            asked += 1
    _save(state_path or INBOX_STATE, st)
    return asked


def button(chat: str, payload: str, state_path: Path | None = None) -> tuple[str, str]:
    """'vi:ok:<id>' / 'vi:no:<id>' → (reply, text to route as his own words or ''). Only an asked line, only once."""
    _, op, iid = (payload.split(':', 2) + ['', ''])[:3]
    st = _load(state_path or INBOX_STATE, {})
    rec = st.get(iid)
    if not rec or rec.get('status') != 'asked' or rec.get('chat') != chat:      # his chat only, once
        return 'זה כבר לא בתוקף.', ''
    rec['status'] = 'ok' if op == 'ok' else 'no'
    rec['answered'] = datetime.now().isoformat(timespec='seconds')
    _save(state_path or INBOX_STATE, st)
    if op != 'ok':
        return '👍 נשאר הערה, לא עשיתי כלום.', ''
    return '✍️ מבצע מה שכתבת (אפשר לסמן [x] ב-Obsidian):', rec['text']
