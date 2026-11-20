"""
utils/notify.py — the one router for messages the system INITIATES.

    notify('⏰ מטלה 3 מחר', key='hw:probability:123:d1', buttons=[[('✅ הגשתי', 'hw:done:probability:123')]])

What it adds over a raw send:
  * dedup — the same `key` is never delivered twice (re-runs, retries, overlapping crons)
  * quiet hours (default 23:00–07:00) — non-urgent messages are queued and go out as one
    "בזמן שישנת" digest when the quiet hours end (flush_digest)
  * one channel — WhatsApp (the linked-device bridge outbox; the Cloud API only inside its 24h window).
    10/11: Telegram is gone (The owner deleted the bot). When WhatsApp can't take a message it is logged and written to
    data/undelivered_alerts.jsonl, which the dashboard and the weekly bot report show (undelivered()).
  * buttons in one channel-agnostic shape: [[(label, payload), ...], ...]
  * an audit log of everything sent (data/notify_log.jsonl)

Env overrides: SSA_QUIET_HOURS="23:00-07:00" (or "off"). SSA_NOTIFY_CHANNEL is no longer read.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'notify_state.json'       # {'sent': {key: ts}, 'digest': [...]}
LOG = ROOT / 'data' / 'notify_log.jsonl'
UNDELIVERED = ROOT / 'data' / 'undelivered_alerts.jsonl'   # what WhatsApp couldn't take — shown, never re-sent
DEFAULT_QUIET = '23:00-07:00'
KEEP_KEYS_DAYS = 120


def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STATE).read_text(encoding='utf-8'))
        return {'sent': d.get('sent') or {}, 'digest': d.get('digest') or []}
    except (OSError, ValueError):
        return {'sent': {}, 'digest': []}


def _save(d: dict, path: Path | None = None) -> None:
    p = path or STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now().timestamp() - KEEP_KEYS_DAYS * 86400
    d['sent'] = {k: v for k, v in d['sent'].items() if _ts(v) >= cutoff}
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(p)


def _ts(v) -> float:
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except ValueError:
        return 0.0


def quiet_window() -> tuple[time, time] | None:
    raw = os.getenv('SSA_QUIET_HOURS', DEFAULT_QUIET).strip().lower()
    if raw in ('', 'off', 'none', '0'):
        return None
    try:
        a, b = raw.split('-')
        return time.fromisoformat(a.strip()), time.fromisoformat(b.strip())
    except ValueError:
        return time(23, 0), time(7, 0)


def in_quiet_hours(now: datetime | None = None) -> bool:
    w = quiet_window()
    if not w:
        return False
    t = il_time(now)
    start, end = w
    return (start <= t or t < end) if start > end else (start <= t < end)


def il_time(now: datetime | None = None) -> time:
    """The Israel wall-clock time of `now`. 03/11: UTC cron jobs (no TZ= on their line) read 07:00–09:00 Israel as
    quiet. An aware `now` is converted; a naive one that is this process's current clock (a caller's datetime.now(),
    within a minute) is read in the process's zone and converted — so a UTC job can't bring the bug back; any other
    naive time (a stated wall-clock time, e.g. a test's 23:30) is already Israel time."""
    from zoneinfo import ZoneInfo
    il = ZoneInfo('Asia/Jerusalem')
    if now is None:
        return datetime.now(il).time()
    if now.tzinfo is None and abs((now - datetime.now()).total_seconds()) < 60:
        now = now.astimezone()                       # naive local clock → aware in the process's zone
    return (now.astimezone(il) if now.tzinfo else now).time()


def _log(entry: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': datetime.now().isoformat(timespec='seconds'), **entry},
                               ensure_ascii=False) + '\n')
    except OSError:
        pass


# ── channel ─────────────────────────────────────────────────────────────────

def preferred_channel() -> str:
    """Kept for callers that print it; WhatsApp is the only channel since 10/11 (Telegram deleted)."""
    return 'whatsapp'


def _record_undelivered(text: str, kind: str = 'text', reason: str = '', path: Path | None = None) -> None:
    """WhatsApp failed → the message isn't lost: logged + a local alert file the dashboard/weekly report read."""
    print(f'  ⚠️ WhatsApp did not take the {kind} ({reason or "not delivered"}) — kept in {UNDELIVERED.name}')
    try:
        p = path or UNDELIVERED
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': datetime.now().isoformat(timespec='seconds'), 'kind': kind,
                                'reason': str(reason)[:200], 'text': str(text)[:2000]}, ensure_ascii=False) + '\n')
    except OSError:
        pass


def undelivered(days: int = 7, path: Path | None = None) -> list[dict]:
    """The undelivered alerts of the last `days` days, newest first, one per text (a retrying job writes it again)."""
    cutoff = datetime.now().timestamp() - days * 86400
    rows: list[dict] = []
    try:
        for line in (path or UNDELIVERED).read_text(encoding='utf-8').splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and _ts(r.get('ts')) >= cutoff:
                rows.append(r)
    except OSError:
        return []
    seen, out = set(), []
    for r in reversed(rows):
        k = (r.get('kind'), (r.get('text') or '')[:300])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _whatsapp(text: str, buttons) -> bool:
    try:
        from whatsapp import client as wa, outbox
    except ImportError:
        return False
    if outbox.enabled():                      # linked-device bridge: free, no 24h window
        chat = outbox.owner_chat()
        if not chat:
            return False
        flat = [b for row in (buttons or []) for b in row]
        return outbox.send_options(chat, text, flat) if flat else outbox.send_text(chat, text)
    if not (wa.configured() and wa.window_open()):
        return False
    flat = [b for row in (buttons or []) for b in row]
    return bool(wa.send_buttons(wa.owner(), text, flat) if flat else wa.send_text(wa.owner(), text))


def _muted(text: str) -> str:
    """16/11 a kind the owner turned off ("תפסיק לשלוח לי דברים שלא רלוונטיים"), or off by default ("✅ … חזר לעבוד"):
    agent/noise. → the kind, or ''. Never breaks a send."""
    try:
        from agent import noise
        return noise.blocked(text)
    except Exception:
        return ''


def deliver(text: str, buttons=None, markdown: bool = True) -> str | None:
    """Send now on WhatsApp → 'whatsapp', or None (logged + kept in the undelivered-alerts file). No Telegram."""
    reason = ''
    if (k := _muted(text)):
        _log({'event': 'muted', 'kind': k, 'text': text[:200]})
        return 'muted'
    try:
        if _whatsapp(text, buttons):
            return 'whatsapp'
    except Exception as e:
        reason = f'{type(e).__name__}: {e}'
    _record_undelivered(text, 'text', reason)
    return None


def deliver_document(path, caption: str = '') -> tuple[str, str, object] | None:
    """A file on WhatsApp → ('whatsapp', chat, message id), or None (kept in the undelivered-alerts file).
    15/10: the weekly practice set went to Telegram although the owner lives in WhatsApp; 10/11 Telegram is gone."""
    reason = ''
    try:
        from whatsapp import outbox
        chat = outbox.owner_chat() if outbox.enabled() else ''
        if chat and (mid := outbox.send_document(chat, path, caption)):
            return 'whatsapp', chat, mid
        reason = 'bridge off or no owner chat' if not chat else 'outbox refused'
    except Exception as e:
        reason = f'{type(e).__name__}: {e}'
    _record_undelivered(f'{caption}\n[file: {Path(str(path)).name}]', 'document', reason)
    return None


# ── the router ──────────────────────────────────────────────────────────────

def notify(text: str, *, key: str | None = None, priority: str = 'normal', buttons=None,
           markdown: bool = True, now: datetime | None = None, state_path: Path | None = None) -> str:
    """'sent' | 'queued' (quiet hours → digest) | 'dup' (key already delivered/queued) | 'failed'."""
    st = _load(state_path)
    if key and key in st['sent']:
        return 'dup'
    if (k := _muted(text)):                               # not queued for the morning either
        _log({'event': 'muted', 'kind': k, 'key': key, 'text': text[:200]})
        return 'muted'
    stamp = (now or datetime.now()).isoformat(timespec='seconds')
    if priority != 'urgent' and in_quiet_hours(now):
        st['digest'].append({'text': text, 'key': key, 'ts': stamp})
        if key:
            st['sent'][key] = stamp
        _save(st, state_path)
        _log({'event': 'queued', 'key': key, 'text': text[:200]})
        return 'queued'
    channel = deliver(text, buttons, markdown)
    if not channel:
        _log({'event': 'failed', 'key': key, 'text': text[:200]})
        return 'failed'                                   # not marked → the next run retries
    if key:
        st['sent'][key] = stamp
    _save(st, state_path)
    _log({'event': 'sent', 'channel': channel, 'key': key, 'text': text[:200]})
    return 'sent'


def failure_key(name: str, tail: str, today=None) -> str:
    """A failing cron job's alert key: the same error again is the same alert for a week. 11–16/11 "Brain Sync נכשלה:
    AuthError" came every morning for six days (NotebookLM's login, which has its own alert). A different error is new."""
    import hashlib
    from datetime import date as _date
    today = today or _date.today()
    m = re.findall(r'\b([A-Z]\w*(?:Error|Exception|Exit))\b', tail or '')
    sig = m[-1] if m else re.sub(r'[\d\W_]+', ' ', (tail or '').strip().splitlines()[-1] if (tail or '').strip() else '')[:60]
    y, w, _ = today.isocalendar()
    return f"cron-fail:{name}:{hashlib.sha1(sig.encode()).hexdigest()[:8]}:{y}-W{w:02d}"


def was_sent(key: str, state_path: Path | None = None) -> bool:
    return key in _load(state_path)['sent']


def _rhythm_wait(items: list[dict], now: datetime | None = None) -> bool:
    """17/11 (agent/rhythm): the overnight digest waits for the owner's first warm hour — at most until 2h after the quiet
    hours end, and never when it holds a homework deadline (hw:…). Any error → it goes now."""
    try:
        if any(str(i.get('key') or '').startswith('hw:') for i in items):
            return False
        w = quiet_window()
        t = il_time(now)
        if not w or (t.hour * 60 + t.minute) - (w[1].hour * 60 + w[1].minute) >= 120:
            return False
        from agent import rhythm
        n = now or datetime.now()
        if rhythm.prefer_later(n.replace(tzinfo=None) if n.tzinfo else n):
            _log({'event': 'digest_wait', 'n': len(items)})
            return True
    except Exception:
        pass
    return False


def flush_digest(now: datetime | None = None, state_path: Path | None = None) -> int:
    """Outside quiet hours: send everything queued overnight as ONE message. Returns items sent."""
    if in_quiet_hours(now):
        return 0
    st = _load(state_path)
    items = st['digest']
    if not items:
        return 0
    if state_path is None and _rhythm_wait(items, now):
        return 0
    body = '\n\n'.join(i['text'] for i in items)
    text = f'📬 *בזמן שישנת* ({len(items)})\n\n{body}' if len(items) > 1 else items[0]['text']
    if not deliver(text, None, True):
        return 0
    st['digest'] = []
    _save(st, state_path)
    _log({'event': 'digest', 'n': len(items)})
    return len(items)
