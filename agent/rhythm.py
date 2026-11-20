"""
agent/rhythm.py — the owner's rhythm: when he actually answers what the bot starts, and how fast (17/11, the owner's improvement 4).

Pure statistics over files already on the VM. No LLM, no network:
    data/wa_outgoing.jsonl        everything sent to his chat (outbox._log_sent). A message that is not the answer to a
                                  message of his (none of his within ±2 min) is one the bot started; a burst within 10
                                  minutes counts once.
    data/agent_conversations.jsonl his messages and button taps (role owner) = he responded
    data/wa_reactions.jsonl       his WhatsApp reactions (whatsapp/worker, 17/11) = he responded
    data/proactive_seen.json      which kind of opener agent/proactive said when → the kinds he ignores
→ data/rhythm.json (recomputed at most every 6 hours, on first use): per weekday × hour how many openers, how many he
answered within an hour, the median minutes to his answer, how much he writes there; the warm / cold hours; the opener
kinds he leaves unanswered. `python -m agent.rhythm` prints the grid; schedule_text() is the short Hebrew version (the
weekly bot report shows it).

How it is used (every use falls back to the old behaviour on any error or too little data):
    agent/proactive.pick   a non-urgent opener in a cold hour waits (≤3h) for his next warm hour; an opener kind he
                           consistently ignores speaks at most once every 3 days. Never: prio-0 notes (clash, trip,
                           deadline, an obligation's reminder), his homework/quizzes (oblig), deadlines, escalations.
    agent/escalate         the "🔔 שוב" nudge waits his usual answer time at that hour (15–25 min); the calendar alarm
                           and a deadline's compression are unchanged.
    utils/notify           the overnight "בזמן שישנת" digest waits (≤2h after the quiet hours) for his first warm hour,
                           unless it holds a homework deadline (hw:…) or an urgent item.
"""
from __future__ import annotations

import bisect
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'rhythm.json'
SENT_LOG = ROOT / 'data' / 'wa_outgoing.jsonl'
CONVO = ROOT / 'data' / 'agent_conversations.jsonl'
REACTIONS = ROOT / 'data' / 'wa_reactions.jsonl'
PROACTIVE = ROOT / 'data' / 'proactive_seen.json'

DAYS = 42                                  # the history read
REPLY_GAP = timedelta(seconds=120)         # one of his messages this close to a sent one → it was the answer
BURST = timedelta(minutes=10)              # sent within this of the previous opener → the same opener
FAST = timedelta(minutes=60)               # answered within this = he was reachable then
ANSWERED = timedelta(hours=3)              # answered within this = not ignored
STALE = timedelta(hours=6)                 # rhythm.json older than this is recomputed
K = 3.0                                    # smoothing: a slot's rate leans on its hour's, an hour's on the overall
MIN_HOUR_N = 4                             # openers at an hour before it can be called cold
HOLD_MAX = timedelta(hours=3)              # an opener waits at most this for a warm hour
DAY_END = 22                               # no waiting into the night
IGNORE_N, IGNORE_RATE = 4, 0.2             # ≥4 openers of a kind in 30 days, ≤20% answered → backed off
BACKOFF_EVERY = timedelta(days=3)
PROTECTED = {'oblig', 'deadline', 'escalate', 'calendar'}   # his homework/quizzes, deadlines, what he marked important
NUDGE_MIN, NUDGE_MAX = 15, 25              # agent/escalate's "שוב" nudge, minutes
DAY_HE = ['ב׳', 'ג׳', 'ד׳', 'ה׳', 'ו׳', 'ש׳', 'א׳']    # Python weekday → Hebrew letter (Mon = ב׳)
GROUP_HE = {'mail': 'מיילים שמחכים לך', 'promise': 'דברים שאמרת שתעשה', 'prep': 'הכנה לתורים ולנסיעות',
            'gift': 'מתנות וברכות', 'waiting': 'דברים שמחכים לאישור', 'study': 'חלונות ללמידה'}

try:
    from zoneinfo import ZoneInfo
    _IL = ZoneInfo('Asia/Jerusalem')
except Exception:                          # no tzdata (Windows without the package)
    _IL = None


# ── reading ─────────────────────────────────────────────────────────────────

def _jsonl(path: Path, tail: int = 400_000) -> list[dict]:
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail * 4))
            raw = f.read().decode('utf-8', errors='replace').splitlines()
        if size > tail * 4:
            raw = raw[1:]                  # a cut first line
    except OSError:
        return []
    out = []
    for ln in raw:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _dt(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s)[:19])
    except (TypeError, ValueError):
        return None


def _il_offset(t: datetime) -> timedelta:
    if _IL is None:
        return timedelta(hours=2)
    return t.replace(tzinfo=_IL).utcoffset() or timedelta(hours=2)


def sent_times(rows: list[dict], chat: str) -> list[tuple[datetime, str]]:
    """(Israel time, text) of what went to `chat`, in file order. The log is append-only, but a process on UTC (a Claude
    job's report) writes UTC stamps: a row that jumps back by about the UTC offset is moved forward by it."""
    out, top = [], None
    for r in rows:
        if r.get('to') != chat:
            continue
        t = _dt(r.get('ts'))
        if not t:
            continue
        if top and t < top - timedelta(minutes=20):
            fixed = t + _il_offset(t)
            if fixed >= top - timedelta(minutes=20):
                t = fixed
        top = max(top, t) if top else t
        out.append((t, str(r.get('text') or '')))
    return out


def his_times(convo: list[dict], reactions: list[dict], chat: str = '') -> list[datetime]:
    """When the owner wrote, tapped or reacted (sorted). Forwarded texts count too: he was on WhatsApp."""
    ts = [t for r in convo if r.get('role') == 'owner' and (t := _dt(r.get('ts')))]
    ts += [t for r in reactions if (not chat or r.get('chat') == chat) and (t := _dt(r.get('ts')))]
    return sorted(ts)


KIND_RX = [('lesson', re.compile(r'^⏰ (?:עוד מעט|\*בעוד \d+ דקות)|^🌙 \*מחר מוקדם')),
           ('morning', re.compile(r'^☀️')),
           ('reminder', re.compile(r'^⏰ \*תזכורת|^🔔')),
           ('oblig', re.compile(r'בוחן|Quiz|quiz|מטלה|תרגיל \d|הגשה|שיעורי בית|^📌|^⭐')),
           ('univ', re.compile(r'^📬|^🚨 \*חשוב מהאוניברסיטה|מהאוניברסיטה|^📖|^💬 יש משוב')),
           ('learned', re.compile(r'^🧠 \*דברים שלמדתי')),
           ('system', re.compile(r'^⚠️|דוח בריאות|NotebookLM|^📊|^🔒')),
           ('claude', re.compile(r'^🛠️|^✅|^🚀|קלוד|משימה \d{6}'))]
KIND_HE = {'lesson': 'תזכורות לשיעורים', 'morning': 'הודעת הבוקר', 'reminder': 'תזכורות שלך', 'oblig': 'מטלות ובחנים',
           'univ': 'עדכונים מהאוניברסיטה', 'learned': '"דברים שלמדתי עליך"', 'system': 'מצב המערכת',
           'claude': 'תוצאות של קלוד', 'other': 'אחר'}


REASK = '↩️ *עוד שאלה פתוחה:*\n'


def kind_of(text: str) -> str:
    t = text or ''
    while t.startswith(REASK):
        t = t[len(REASK):]
    return next((k for k, rx in KIND_RX if rx.search(t[:200])), 'other')


def openers(sent: list[tuple[datetime, str]], his: list[datetime]) -> list[tuple[datetime, str]]:
    """What the bot started: not within REPLY_GAP of a message of his; a burst counts once (its first)."""
    out: list[tuple[datetime, str]] = []
    last = None
    for t, text in sorted(sent, key=lambda x: x[0]):
        i = bisect.bisect_left(his, t - REPLY_GAP)
        if i < len(his) and his[i] <= t + REPLY_GAP:
            last = t                       # an answer: a burst right after it is part of the same conversation
            continue
        if last and t - last < BURST:
            last = t
            continue
        out.append((t, text))
        last = t
    return out


def latency(t: datetime, his: list[datetime]) -> timedelta | None:
    """Time from an opener to his next message/tap/reaction (None: nothing after it)."""
    i = bisect.bisect_right(his, t)
    return his[i] - t if i < len(his) else None


# ── the statistics ──────────────────────────────────────────────────────────

def _rate(hit: float, n: float, prior: float) -> float:
    return (hit + K * prior) / (n + K)


def compute(now: datetime | None = None, chat: str | None = None, sent_path: Path | None = None,
            convo_path: Path | None = None, reactions_path: Path | None = None,
            proactive_path: Path | None = None) -> dict:
    now = now or datetime.now()
    if chat is None:
        try:
            from whatsapp import outbox
            chat = outbox.owner_chat()
        except Exception:
            chat = ''
    since = now - timedelta(days=DAYS)
    convo = _jsonl(convo_path or CONVO)
    his = [t for t in his_times(convo, _jsonl(reactions_path or REACTIONS), chat) if t >= since - timedelta(days=1)]
    sent = [(t, x) for t, x in sent_times(_jsonl(sent_path or SENT_LOG), chat) if since <= t <= now]
    ops = openers(sent, his)
    slots: dict[str, dict] = {}
    hours = [{'n': 0, 'fast': 0, 'lat': []} for _ in range(24)]
    kinds: dict[str, dict] = {}
    lats = []
    for t, text in ops:
        lat = latency(t, his)
        if lat is not None and t + lat > now:
            lat = None
        open_now = lat is None and now - t < ANSWERED          # too recent to say it was ignored
        if open_now:
            continue
        fast = lat is not None and lat <= FAST
        s = slots.setdefault(f'{t.weekday()}-{t.hour}', {'n': 0, 'fast': 0, 'lat': []})
        for b in (s, hours[t.hour]):
            b['n'] += 1
            b['fast'] += fast
            if lat is not None and lat <= ANSWERED:
                b['lat'].append(lat.total_seconds() / 60)
        if lat is not None and lat <= ANSWERED:
            lats.append(lat.total_seconds() / 60)
        k = kinds.setdefault(kind_of(text), {'n': 0, 'answered': 0})
        k['n'] += 1
        k['answered'] += bool(lat is not None and lat <= ANSWERED)
    active = [0] * 168
    for t in his:
        if t >= since:
            active[t.weekday() * 24 + t.hour] += 1
    n_all = sum(h['n'] for h in hours)
    g = (sum(h['fast'] for h in hours) / n_all) if n_all else 0.0
    hour_rate = [_rate(h['fast'], h['n'], g) for h in hours]
    grid, warm, cold = {}, [], []
    for wd in range(7):
        for h in range(24):
            s = slots.get(f'{wd}-{h}', {'n': 0, 'fast': 0, 'lat': []})
            r = _rate(s['fast'], s['n'], hour_rate[h])
            act = active[wd * 24 + h]
            cell = {'n': s['n'], 'fast': s['fast'], 'rate': round(r, 2), 'active': act}
            if s['lat']:
                cell['med_min'] = round(median(s['lat']))
            grid[f'{wd}-{h}'] = cell
            if n_all < 10:
                continue
            # one weekday-hour has 0–10 openers: it is warm on its own only with 2+ fast answers; otherwise the hour of
            # day across the week decides (warm: ≥3 fast answers and 1.5× the overall rate; cold: ≥6 openers, ≤0.6×)
            hr, hn, hf = hour_rate[h], hours[h]['n'], hours[h]['fast']
            if (s['fast'] >= 2 and r >= max(g * 1.25, 0.3)) or (hf >= 3 and hr >= max(g * 1.5, 0.25)):
                warm.append(f'{wd}-{h}')
            elif hn >= 6 and hr <= g * 0.6 and s['fast'] == 0 and act < 2:
                cold.append(f'{wd}-{h}')
    groups = ignored_groups(now, his, proactive_path)
    return {'computed': now.isoformat(timespec='seconds'), 'days': DAYS, 'openers': n_all, 'fast_rate': round(g, 2),
            'median_min': round(median(lats)) if lats else None,
            'hours': [{'h': h, 'n': hours[h]['n'], 'fast': hours[h]['fast'], 'rate': round(hour_rate[h], 2),
                       **({'med_min': round(median(hours[h]['lat']))} if hours[h]['lat'] else {})} for h in range(24)],
            'grid': grid, 'warm': warm, 'cold': cold, 'kinds': kinds, 'groups': groups}


def ignored_groups(now: datetime, his: list[datetime], path: Path | None = None) -> dict:
    """agent/proactive's openers of the last 30 days by group: said, answered within 3h, backed off or not."""
    try:
        said = json.loads((path or PROACTIVE).read_text(encoding='utf-8')).get('said') or {}
    except (OSError, ValueError, AttributeError):
        return {}
    try:
        from agent.proactive import group_of
    except Exception:
        group_of = lambda k: k                                   # noqa: E731
    out: dict[str, dict] = {}
    for key, ts in said.items():
        t = _dt(ts)
        if not t or t < now - timedelta(days=30) or now - t < ANSWERED:
            continue
        g = group_of(str(key).split('|', 1)[0])
        lat = latency(t, his)
        d = out.setdefault(g, {'n': 0, 'answered': 0})
        d['n'] += 1
        d['answered'] += bool(lat is not None and lat <= ANSWERED)
    for g, d in out.items():
        d['backoff'] = g not in PROTECTED and d['n'] >= IGNORE_N and d['answered'] / d['n'] <= IGNORE_RATE
    return out


# ── the store ───────────────────────────────────────────────────────────────

def load(now: datetime | None = None, path: Path | None = None, refresh: bool = True) -> dict:
    """data/rhythm.json, recomputed when missing or older than 6h. {} if nothing can be read."""
    p = path or STORE
    now = now or datetime.now()
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        if not refresh or (now - (_dt(d.get('computed')) or datetime.min)) < STALE:
            return d
    except (OSError, ValueError):
        if not refresh:
            return {}
    try:
        d = compute(now)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(p)
        return d
    except Exception as e:
        print(f'  ⚠️ rhythm: {e!r}'[:200])
        return {}


def _slot(t: datetime) -> str:
    return f'{t.weekday()}-{t.hour}'


def is_cold(now: datetime, d: dict | None = None) -> bool:
    d = load(now) if d is None else d
    return _slot(now) in set(d.get('cold') or [])


def next_warm(now: datetime, d: dict | None = None, within: timedelta = HOLD_MAX) -> datetime | None:
    """The start of his next warm hour after now, within `within` and before DAY_END (None: none)."""
    d = load(now) if d is None else d
    warm = set(d.get('warm') or [])
    t = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while t <= now + within:
        if t.hour >= DAY_END or t.date() != now.date():
            return None
        if _slot(t) in warm:
            return t
        t += timedelta(hours=1)
    return None


def _away() -> bool:
    """agent/catchup (19/11): the owner hasn't written for 4h+ → its "מה פספסתי" digest holds non-urgent messages until he's
    back. The rhythm only shifts openers while he's around, so the two never hold the same message."""
    try:
        from agent import catchup
        from whatsapp import outbox
        chat = outbox.owner_chat()
        return bool(chat) and catchup.away(chat)
    except Exception:
        return False


def hold(kind: str, prio: int, now: datetime, d: dict | None = None) -> bool:
    """agent/proactive: wait with this opener — he rarely answers at this hour and usually does soon after."""
    try:
        from agent.proactive import group_of
        if prio == 0 or group_of(kind) in PROTECTED:
            return False
        d = load(now) if d is None else d
        if not (is_cold(now, d) and next_warm(now, d) is not None):
            return False
        return not _away()                 # away (agent/catchup holds it for his return): not held twice
    except Exception as e:
        print(f'  ⚠️ rhythm hold: {e!r}'[:200])
        return False


def backed_off(group: str, said: dict, now: datetime, d: dict | None = None) -> bool:
    """A kind he consistently leaves unanswered (and that isn't protected): once every BACKOFF_EVERY at most."""
    try:
        if group in PROTECTED:
            return False
        d = load(now) if d is None else d
        if not ((d.get('groups') or {}).get(group) or {}).get('backoff'):
            return False
        from agent.proactive import group_of
        cut = (now - BACKOFF_EVERY).isoformat()
        return any(group_of(str(k).split('|', 1)[0]) == group and v >= cut for k, v in (said or {}).items())
    except Exception as e:
        print(f'  ⚠️ rhythm backoff: {e!r}'[:200])
        return False


def nudge_after(sent: datetime, d: dict | None = None) -> timedelta:
    """agent/escalate: his median minutes to answer at that hour, clamped to 15–25 (15 with no data)."""
    try:
        d = load(sent) if d is None else d
        cell = (d.get('grid') or {}).get(_slot(sent)) or {}
        med = cell.get('med_min') or ((d.get('hours') or [{}] * 24)[sent.hour] or {}).get('med_min')
        if not med:
            return timedelta(minutes=NUDGE_MIN)
        return timedelta(minutes=max(NUDGE_MIN, min(NUDGE_MAX, int(med))))
    except Exception:
        return timedelta(minutes=NUDGE_MIN)


def prefer_later(now: datetime, within: timedelta = timedelta(hours=2), d: dict | None = None) -> bool:
    """utils/notify's digest: this hour is cold and a warm one comes within `within`."""
    try:
        d = load(now) if d is None else d
        return is_cold(now, d) and next_warm(now, d, within) is not None
    except Exception:
        return False


# ── shown to the owner ────────────────────────────────────────────────────────────

def _ranges(hours: list[int]) -> str:
    hs = sorted(set(hours))
    out, i = [], 0
    while i < len(hs):
        j = i
        while j + 1 < len(hs) and hs[j + 1] == hs[j] + 1:
            j += 1
        out.append(f'{hs[i]:02d}–{hs[j] + 1:02d}')
        i = j + 1
    return ', '.join(out)


def _by_day(slots: list[str]) -> str:
    per: dict[int, list[int]] = {}
    for s in slots:
        wd, h = (int(x) for x in s.split('-'))
        if 7 <= h < 23:
            per.setdefault(wd, []).append(h)
    order = [6, 0, 1, 2, 3, 4, 5]                    # Sunday first
    return ' · '.join(f'{DAY_HE[wd]} {_ranges(per[wd])}' for wd in order if wd in per)


def schedule_text(d: dict | None = None) -> str:
    """The short Hebrew version (the weekly bot report)."""
    d = load() if d is None else d
    if not d or (d.get('openers') or 0) < 10:
        return '⏱️ הקצב שלך: עוד אין מספיק נתונים (פחות מ-10 הודעות יזומות)'
    lines = [f"⏱️ *הקצב שלך* ({d['openers']} הודעות שהבוט יזם ב-{d.get('days', DAYS)} יום; "
             f"ענית תוך שעה על {round(100 * d.get('fast_rate', 0))}%"
             + (f", חציון {d['median_min']} דק׳" if d.get('median_min') is not None else '') + ')']
    if (w := _by_day(d.get('warm') or [])):
        lines.append(f'   עונה מהר: {w}')
    if (c := _by_day(d.get('cold') or [])):
        lines.append(f'   כמעט לא עונה: {c} — הודעות לא דחופות מחכות שם לשעה טובה יותר')
    off = [g for g, v in (d.get('groups') or {}).items() if v.get('backoff')]
    if off:
        lines.append('   פחות מזה (כמעט לא ענית): ' + ', '.join(
            f"{GROUP_HE.get(g, g)} ({v['answered']}/{v['n']})" for g, v in d['groups'].items() if v.get('backoff'))
            + ' — פעם ב-3 ימים לכל היותר')
    return '\n'.join(lines)


def grid_text(d: dict | None = None) -> str:
    """The whole grid: rows = hours 07–23, columns = א׳…ש׳; each cell 'answered fast/openers' and a mark (+ warm, - cold)."""
    d = load() if d is None else d
    if not d:
        return 'no data'
    order = [6, 0, 1, 2, 3, 4, 5]
    warm, cold = set(d.get('warm') or []), set(d.get('cold') or [])
    rows = ['hour  ' + '  '.join(f'{DAY_HE[wd]:>6}' for wd in order)]
    for h in range(7, 24):
        cells = []
        for wd in order:
            c = (d.get('grid') or {}).get(f'{wd}-{h}') or {}
            mark = '+' if f'{wd}-{h}' in warm else '-' if f'{wd}-{h}' in cold else ' '
            cells.append(f"{c.get('fast', 0)}/{c.get('n', 0)}{mark}".rjust(6) + ' ')
        hr = (d.get('hours') or [{}] * 24)[h]
        rows.append(f'{h:02d}    ' + ' '.join(cells) + f"  rate {hr.get('rate', 0):.2f}"
                    + (f" med {hr['med_min']}m" if hr.get('med_min') is not None else ''))
    kinds = ', '.join(f"{KIND_HE.get(k, k)} {v['answered']}/{v['n']}" for k, v in
                      sorted((d.get('kinds') or {}).items(), key=lambda x: -x[1]['n']))
    groups = ', '.join(f"{g} {v['answered']}/{v['n']}{' BACKOFF' if v.get('backoff') else ''}"
                       for g, v in (d.get('groups') or {}).items())
    return '\n'.join(rows + ['', f'kinds (answered≤3h/sent): {kinds}', f'proactive groups: {groups or "-"}'])


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    d = compute()
    if '--write' in sys.argv:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    print(schedule_text(d))
    print()
    print(grid_text(d))
