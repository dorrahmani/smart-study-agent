"""
agent/noise.py — what the bot sends on its own, by kind, and "stop sending me this" (16/11).

16/11 20:18 the owner: "תפסיק לשלוח לי דברים שלא רלוונטיים" → 'לא העברתי לקלוד. אם לפתוח לו משימה, תכתוב "תתקן ש…"'. The
last thing the bot had sent him on its own (19:21) was "⚠️ דוח בריאות pipeline • 🚨 NotebookLM: סיכומים ותמלולים חדשים
לא עלו למחברות" — the server's problem, nothing for him to do; at 10:20 and 15:20 it had tried to tell him "✅ NotebookLM
חזר לעבוד בשרת". A complaint like that is feedback on those messages: say what it was, stop that kind, offer an undo;
when it can't be told which, show the last 2–3 kinds as buttons. Never "open a job for Claude".

    KINDS      every proactive message type, told apart by its fixed first line (the senders' own headers)
    defaults   'system_ok' (recovered / all good) is off unless he turns it on; failures he can act on stay on
    gate       blocked(text) — utils/notify (notify/deliver) and the worker's proactive ticks drop a muted kind
    store      data/noise_mutes.json {kind: 'off' | 'on'}; buttons nz:mute:<kind> / nz:undo:<kind>
Deterministic, no LLM. Only his own messages and the bot's own sent log are read.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'noise_mutes.json'

# key, how it is named to him, the sent text's start, may he mute it from a complaint
KINDS: list[tuple[str, str, re.Pattern, bool]] = [
    ('system_ok', 'עדכוני "חזר לעבוד" של המערכת',
     re.compile(r'^\s*✅[^\n]{0,40}(?:חזר(?:ה)?\s+לעבוד|חזר(?:ה)?\s+לפעול|תקין|עובד\s+שוב|back)'), True),
    ('system', 'עדכוני מערכת (NotebookLM, דוח בריאות, משימות מתוזמנות)',
     re.compile(r'^\s*(?:📬 \*בזמן שישנת\*[^\n]*\s+)?(?:⚠️|⛔|🚨)\s*\*?(?:NotebookLM|דוח בריאות|משימה מתוזמנת|כבר \d+ פעמים|'
                r'Pipeline|pipeline|הגיבוי|Brain Sync|התקציב|budget)'), True),
    ('univ_digest', 'סיכום העדכונים מהאוניברסיטה', re.compile(r'^\s*📬 \*עדכונים מהאוניברסיטה'), True),
    ('univ_urgent', 'הודעות דחופות מהאוניברסיטה', re.compile(r'^\s*🚨 \*(?:חשוב מהאוניברסיטה|מייל שנראה דחוף)'), False),
    ('schedule', 'עדכוני לו״ז מהאוניברסיטה', re.compile(r'^\s*🗓️ \*עדכנתי בלו״ז'), True),
    ('learned', '"דברים שלמדתי עליך"', re.compile(r'^\s*🧠 \*(?:דברים שלמדתי|למדתי עליך)'), True),
    ('preclass', 'תזכורות לפני שיעור', re.compile(r'^\s*⏰ (?:עוד מעט:|\*בעוד \d+ דקות\*)'), True),
    ('tomorrow', '"מחר מוקדם" בערב', re.compile(r'^\s*🌙 \*מחר מוקדם'), True),
    ('morning', 'הודעת הבוקר', re.compile(r'^\s*☀️ \*בוקר טוב'), True),
    ('claude', 'עדכונים על משימות של קלוד', re.compile(r'^\s*(?:🛠️ המשימה של קלוד|🚀 פורס|🔒 לא פורס|🤖 קלוד)'), True),
    ('starter', 'עדכונים על הגרסה לחברים', re.compile(r'^\s*📦'), True),
    ('reminder', 'תזכורות שביקשת', re.compile(r'^\s*⏰ \*תזכורת'), False),
]
NAMES = {k: n for k, n, _, _ in KINDS}
DEFAULT_OFF = {'system_ok'}

# "תפסיק לשלוח לי דברים שלא רלוונטיים" / "די עם ההודעות האלה" / "זה לא רלוונטי אליי" / "למה שלחת לי את זה?"
COMPLAINT_RX = re.compile(
    r'^\s*(?:(?:תפסיק|די|מספיק|אל\s+ת(?:שלח|שלוח))\s*(?:,\s*)?(?:לשלוח|לשלח|עם|לי)?\s*(?:לי\s+)?(?:את\s+)?(?:כל\s+)?(?:ה)?'
    r'(?:דברים|הודעות|עדכונים|שטויות|זבל|ספאם|התראות)(?:\s+(?:ה)?(?:אלה|האלה|האלו|כאלה|כאלו))?'
    r'(?:\s+(?:ש)?(?:לא|אינם)\s+(?:רלוונטי(?:ים|ות)?|מעניינ(?:ים|ות)|חשוב(?:ים|ות)?|קשור(?:ים|ות)?)(?:\s+(?:אליי|לי))?)?'
    r'|(?:זה|הם|ההודע(?:ה|ות)\s+(?:האלה|האלו|האחרונ(?:ה|ות)))\s+(?:לא|ממש\s+לא)\s+(?:רלוונטי(?:ים|ות)?|מעניין|מעניינ(?:ים|ות)|חשוב)'
    r'(?:\s+(?:אליי|לי))?'
    r'|למה\s+(?:אתה\s+)?(?:שלחת|שולח)\s+לי\s+(?:את\s+)?(?:זה|את\s+זה|הודעות\s+כאלה|דברים\s+כאלה|כל\s+(?:ה)?(?:דברים|הודעות)))'
    r'\s*[.!?]*\s*$')
WINDOW_H = 4            # "the last thing it sent" = in the hours before the complaint


def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STORE).read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict, path: Path | None = None) -> None:
    p = path or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    tmp.replace(p)


def kind_of(text: str) -> str:
    t = (text or '').lstrip()
    return next((k for k, _, rx, _ in KINDS if rx.search(t[:300])), '')


def is_off(kind: str, path: Path | None = None) -> bool:
    if not kind:
        return False
    v = _load(path).get(kind)
    return v == 'off' or (v is None and kind in DEFAULT_OFF)


def blocked(text: str, path: Path | None = None) -> str:
    """The kind of this proactive text when he turned it off (or it is off by default) → don't send. '' = send."""
    try:
        k = kind_of(text)
        if k == 'system' and ACTION_RX.search(text or ''):   # a failure only he can fix still reaches him
            return ''
        return k if k and is_off(k, path) else ''
    except Exception:
        return ''


ACTION_RX = re.compile(r'לחדש\s+ידנית|תדליק|צריך\s+ממך|רק\s+אתה|תתחבר|להתחבר\s+מחדש|איך\s+לבצע|התקציב|תקרה|נעול|קפוא')


def set_kind(kind: str, on: bool, path: Path | None = None) -> None:
    d = _load(path)
    d[kind] = 'on' if on else 'off'
    _save(d, path)


def recent(chat: str, now: datetime | None = None, hours: float = WINDOW_H, sent_path: Path | None = None) -> list[dict]:
    """The bot's own proactive messages to him in the last hours, by kind, newest first: [{kind, name, first, ts}]."""
    from whatsapp import outbox
    now = now or datetime.now()
    rows = outbox.recent_sent(chat, hours=72, n=60, path=sent_path)
    cut = (now - timedelta(hours=hours)).isoformat(timespec='seconds')
    out, seen = [], set()
    for r in sorted(rows, key=lambda x: str(x.get('ts') or ''), reverse=True):
        ts = str(r.get('ts') or '')
        if ts < cut or ts > now.isoformat(timespec='seconds'):
            continue
        k = kind_of(r.get('text') or '')
        if not k or k in seen:
            continue
        seen.add(k)
        first = next((ln.replace('*', '').strip() for ln in str(r['text']).splitlines() if ln.strip()), '')
        out.append({'kind': k, 'name': NAMES[k], 'first': first[:60], 'ts': ts})
    return out


def _muted_text(it: dict) -> tuple[str, list[tuple[str, str]]]:
    at = it['ts'][11:16]
    extra = '\nרק תקלה שמחכה לך (משהו שרק אתה יכול לעשות) עדיין תגיע.' if it['kind'] == 'system' else ''
    return (f'הבנתי, צודק. ההודעה האחרונה ששלחתי לך ביוזמתי הייתה "{it["first"]}" ({at}).\n'
            f'לא אשלח לך יותר {it["name"]}.{extra}',
            [('↩️ בעצם כן לשלוח', f'nz:undo:{it["kind"]}')])


def handle(chat: str, text: str, quoted: str = '', now: datetime | None = None, path: Path | None = None,
           sent_path: Path | None = None) -> tuple[str, list[tuple[str, str]]] | None:
    """His complaint about what the bot sent → (reply, buttons), or None when the message isn't one."""
    t = (text or '').strip()
    if not t or len(t) > 90 or not COMPLAINT_RX.match(t):
        return None
    if quoted and (k := kind_of(quoted)):                  # he replied to that very message
        items = [{'kind': k, 'name': NAMES[k], 'first': next((ln.replace('*', '').strip() for ln in quoted.splitlines() if ln.strip()), '')[:60],
                  'ts': (now or datetime.now()).isoformat(timespec='seconds')}]
    else:
        items = recent(chat, now, sent_path=sent_path)
    items = [i for i in items if i['kind'] not in ('reminder',)]
    if len(items) == 1 or (items and quoted):
        it = items[0]
        if not dict((k, m) for k, _, _, m in KINDS)[it['kind']]:
            return (f'את {it["name"]} אני שולח כי הן נוגעות ישירות לשיעורים ולמבחנים שלך. אם משהו ספציפי שם לא היה '
                    f'רלוונטי, תגיד לי מה ואדייק.', [])
        set_kind(it['kind'], False, path)
        text, btns = _muted_text(it)
        if quoted:
            text = text.replace(f'ההודעה האחרונה ששלחתי לך ביוזמתי הייתה "{it["first"]}" ({it["ts"][11:16]})',
                                f'זו הייתה "{it["first"]}"')
        return text, btns
    if items:
        btns = [(f'🔕 {i["name"]}'[:40], f'nz:mute:{i["kind"]}') for i in items[:3]]
        lines = '\n'.join(f'• {i["ts"][11:16]} — {i["first"]}' for i in items[:3])
        return f'הבנתי. על מה מדובר? אלה הדברים האחרונים ששלחתי לך:\n{lines}', btns
    from agent import proactive                             # a secretary note (agent/proactive) of the last hours
    said = proactive.commands('פחות מזה', now=now)
    if said:
        return said, []
    return 'הבנתי. לא שלחתי לך משהו ביוזמתי בשעות האחרונות — על איזו הודעה מדובר? אפשר לענות עליה (ציטוט) ולכתוב "לא רלוונטי".', []


def answer(op: str, kind: str, path: Path | None = None) -> str:
    """A tap: nz:mute:<kind> / nz:undo:<kind>."""
    if kind not in NAMES:
        return 'לא מצאתי את סוג ההודעה הזה.'
    if op == 'mute':
        set_kind(kind, False, path)
        return f'🔕 הבנתי, לא אשלח לך יותר {NAMES[kind]}. "תחזיר {NAMES[kind]}" מחזיר.'
    set_kind(kind, True, path)
    return f'🔔 בסדר, ממשיך לשלוח {NAMES[kind]}.'


BACK_RX = re.compile(r'^\s*(?:תחזיר|תחזור\s+לשלוח)\s+(?:את\s+)?(.{3,60}?)\s*[.!]*\s*$')


def back(text: str, path: Path | None = None) -> str | None:
    """"תחזיר עדכוני מערכת" → on again. None when it names no kind of ours."""
    m = BACK_RX.match(text or '')
    if not m:
        return None
    k = next((k for k, rx in BACK_WORDS if rx.search(m.group(1))), '')
    if not k:
        return None
    set_kind(k, True, path)
    return f'🔔 בסדר, ממשיך לשלוח {NAMES[k]}.'


BACK_WORDS = [('system_ok', re.compile(r'חזר\s+לעבוד')), ('system', re.compile(r'(?:עדכוני|הודעות|התראות)\s+(?:ה)?מערכת|תקלות')),
              ('univ_digest', re.compile(r'עדכונים\s+מהאוניברסיטה|סיכום\s+העדכונים')), ('learned', re.compile(r'למדתי')),
              ('preclass', re.compile(r'לפני\s+(?:ה)?שיעור')), ('tomorrow', re.compile(r'מחר\s+מוקדם')),
              ('morning', re.compile(r'(?:הודעת|את)\s+(?:ה)?בוקר')), ('schedule', re.compile(r'(?:עדכוני|שינויי)\s+(?:ה)?לו["״]?ז')),
              ('claude', re.compile(r'משימות\s+(?:של\s+)?קלוד')), ('starter', re.compile(r'הגרסה\s+לחברים'))]
