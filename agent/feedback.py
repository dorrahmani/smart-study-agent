"""
agent/feedback.py — the owner marks a bad answer with 👎 ("לא נכון", "לא הבנת"…) → it reaches the next daily review first.

No LLM. The exchange he reacted to (his message + the bot's answer) comes from the conversation log; an optional
"ציפיתי ש…" within 10 minutes is attached. scripts/agent_daily_review.py puts these at the top ("fix first") and
runs on any day that has one — even after the daily-review period ends.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'feedback.jsonl'
CONVO = ROOT / 'data' / 'agent_conversations.jsonl'
FEEDBACK_RX = re.compile(r'^\s*(?:👎+\s*(.*)|(?:לא נכון|לא הבנת(?:י)?|לא הבנת אותי|טעות|זה לא מה שביקשתי|זה לא מה שרציתי|'
                         r'לא עבד|תשובה לא טובה|לא קשור)\s*[.!?]*)\s*$', re.S)
COMMENT_RX = re.compile(r'^\s*(?:ציפיתי|רציתי|התכוונתי|הכוונה שלי|הכוונה היתה|ביקשתי)\b')


def is_feedback(text: str) -> bool:
    return bool(FEEDBACK_RX.match(text or ''))


def _last_exchange(role: str, path: Path | None = None) -> dict:
    """The latest logged exchange of this role (The owner's message + the bot's answer) — the one he's reacting to."""
    try:
        lines = (path or CONVO).read_text(encoding='utf-8').splitlines()[-60:]
    except OSError:
        return {}
    for raw in reversed(lines):
        try:
            t = json.loads(raw)
        except ValueError:
            continue
        if t.get('role') == role and not is_feedback(t.get('in', '')):
            return {'in': t.get('in', ''), 'out': ' ⏎ '.join(x for _, x in t.get('out') or [])[:1500], 'at': t.get('ts', '')}
    return {}


def record(u: dict, text: str, role: str = 'owner', path: Path | None = None, convo: Path | None = None) -> str:
    m = FEEDBACK_RX.match(text or '')
    comment = ((m.group(1) or '') if m else '').strip()
    ex = _last_exchange(role, convo)
    rec = {'ts': datetime.now().isoformat(timespec='seconds'), 'role': role, **ex, 'comment': comment}
    p = path or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    u['fb_wait'] = rec['ts']
    if comment:
        return '📝 תודה — סימנתי, וזה יגיע לסקירה של מחר בבוקר כדבר הראשון לתקן.'
    return ('📝 תודה, סימנתי — זה יגיע לסקירה של מחר בבוקר כדבר הראשון לתקן.\n'
            'רוצה להוסיף מה ציפית? כתוב "ציפיתי ש…" (לא חובה).')


def add_comment(u: dict, text: str, path: Path | None = None) -> str | None:
    """'ציפיתי ש…' within 10 minutes of a 👎 → attached to it. None if this isn't that."""
    ts = u.get('fb_wait')
    if not ts or not COMMENT_RX.match(text or '') or datetime.now() - datetime.fromisoformat(ts) > timedelta(minutes=10):
        return None
    p = path or STORE
    try:
        rows = [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
    except (OSError, ValueError):
        return None
    for r in reversed(rows):
        if r.get('ts') == ts:
            r['comment'] = (r.get('comment', '') + ' ' + text.strip()).strip()
            break
    p.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
    u.pop('fb_wait', None)
    return '📝 רשמתי. מחר בבוקר זה הדבר הראשון שקלוד יתקן.'


CORRECTION_RX = re.compile(r'^\s*(?:לא[,.!]?\s+(?:התכוונתי|אני מתכוו[ןנ]|הכוונה|רציתי|ביקשתי|שאלתי|זה לא|לא זה|אמרתי)|'
                           r'התכוונתי ל|אמרתי (?:לך )?ש|שאלתי (?:על|את|מה|אם)|עוד פעם[:,]|שוב[:,]|אתה לא מבין|את לא מבינה)')


def _similar(a: str, b: str) -> bool:
    from difflib import SequenceMatcher
    wa, wb = set(re.findall(r'\w+', a)), set(re.findall(r'\w+', b))
    if len(wa) < 3 or len(wb) < 3:
        return False                                        # "תודה", "1", "כן" repeat innocently
    return len(wa & wb) / len(wa | wb) >= 0.5 or SequenceMatcher(None, a, b).ratio() >= 0.7


def implicit(text: str, role: str = 'owner', path: Path | None = None, convo: Path | None = None) -> str:
    """15/10 (The owner: "שתלמד מטעויות"): a correction ("לא, התכוונתי…") or the same question again within 5 minutes =
    the last answer missed — logged for the daily review like a 👎, silently (the message is handled as usual).
    → the reason, or '' if this isn't one."""
    ex = _last_exchange(role, convo)
    if not ex or not (text or '').strip():
        return ''
    why = ''
    if CORRECTION_RX.match(text):
        why = 'תיקן את הבוט'
    else:
        try:
            recent = datetime.now() - datetime.fromisoformat(ex.get('at', '')[:19]) < timedelta(minutes=5)
        except ValueError:
            recent = False
        if recent and _similar(text.strip(), ex.get('in', '').strip()):
            why = 'שאל שוב כמעט אותו דבר'
    if not why:
        return ''
    rec = {'ts': datetime.now().isoformat(timespec='seconds'), 'role': role, **ex, 'comment': text.strip()[:500],
           'implicit': why}
    p = path or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    return why


def since(when: datetime, path: Path | None = None) -> list[dict]:
    try:
        rows = [json.loads(l) for l in (path or STORE).read_text(encoding='utf-8').splitlines() if l.strip()]
    except (OSError, ValueError):
        return []
    return [r for r in rows if r.get('ts', '') >= when.isoformat()]
