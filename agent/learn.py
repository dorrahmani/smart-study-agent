"""
agent/learn.py — the memory that learns by itself (The owner 19/10: "שיהיה יותר חכם ויותר מזכיר את Instinct",
"שתלמד מתי אני עובד והכל").

On 19/10 the bot's memory of the owner's private chat was EMPTY — facts came only from "תזכור ש…", which he never says.
    learn_tick   once a day (21:30–22:45): The owner's own messages of the last 24h → one flash call → new lasting facts
                 (people, routines, preferences, plans, work), minus what's already known → ONE question:
                 "🧠 למדתי עליך היום: … לשמור?" (✅ before anything is written — lf:ok / lf:no)
    gap_tick     what a planner can't work without — when he works — asked ONCE (a free-text answer, 19:00–21:00);
                 the answer goes through planner.parse_work. Not asked again for 30 days if he ignores it.
Never raises into a reply; one Gemini call a day at most (plus parse_work when he answers).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from utils.gemini_sub import api_only

ROOT = Path(__file__).resolve().parent.parent
MODEL = 'gemini-3.8-flash'
CONVO = ROOT / 'data' / 'agent_conversations.jsonl'
MAX_NEW = 5

PROMPT = """אתה הזיכרון של העוזר האישי של המשתמש (סטודנט בתל אביב, בת זוג: בת הזוג).
הנה ההודעות שהמשתמש כתב לעוזר ב-24 השעות האחרונות (ותשובות העוזר בקיצור):
{turns}

מה שהעוזר כבר יודע עליו:
{known}

חלץ עד {n} עובדות *חדשות ומתמשכות* על המשתמש שיעזרו לעוזר בעתיד: אנשים בחייו (מי זה מי), שגרה וזמנים קבועים,
העדפות (איך הוא אוהב לקבל דברים, מתי לומד), מטרות, עבודה, תחביבים, מקומות קבועים.
לא: משימות חד-פעמיות, אירועים שכבר ביומן, תוכן קורסים, דברים שהעוזר כבר יודע, ניחושים, מידע רפואי/כספי רגיש.
כל עובדה — משפט קצר בגוף שלישי ("המשתמש עובד בבר ברביעי בערב"). אם אין — רשימה ריקה.
החזר JSON בלבד: {{"facts": ["..."]}}"""


def turns_since(since: datetime, path: Path | None = None) -> list[dict]:
    """The owner's own text turns (not button taps) since `since`."""
    try:
        lines = (path or CONVO).read_text(encoding='utf-8').splitlines()[-400:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            t = json.loads(ln)
        except ValueError:
            continue
        if t.get('role') != 'owner' or t.get('forwarded') or t.get('kind') not in ('text', 'voice') or t.get('ts', '') < since.isoformat():
            continue
        if len((t.get('in') or '').strip()) < 4:
            continue
        out.append(t)
    return out


def _norm(s: str) -> set[str]:
    return set(re.findall(r'\w{2,}', (s or '').replace('המשתמש ', '')))


def new_only(cands: list[str], known: list[str]) -> list[str]:
    """Drop what's already known (or nearly: ≥60% of the words) and duplicates within the batch."""
    out: list[str] = []
    for c in cands:
        c = (c or '').strip(' .•-')
        if len(c) < 6 or len(c) > 200:
            continue
        w = _norm(c)
        if not w:
            continue
        if any(len(w & _norm(k)) / max(1, len(w | _norm(k))) >= 0.6 for k in known + out):
            continue
        out.append(c)
    return out[:MAX_NEW]


@api_only()                                             # the bot's lines in there may quote a mail
def extract(turns: list[dict], known: list[str], client=None) -> list[str]:
    if not turns:
        return []
    if client is None:
        import google.genai as genai
        client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    from google.genai import types as T
    txt = '\n'.join(f"[{t['ts'][11:16]}] המשתמש: {t['in'][:400]}\n   עוזר: {' / '.join(x for _, x in t.get('out') or [])[:200]}"
                    for t in turns[-40:])
    resp = client.models.generate_content(
        model=MODEL, contents=PROMPT.format(turns=txt, known='\n'.join(f'- {k}' for k in known[-60:]) or '(כלום)', n=MAX_NEW),
        config=T.GenerateContentConfig(temperature=0.1, max_output_tokens=1500, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    try:
        from utils.usage_tracker import log_gemini
        log_gemini(resp, MODEL, op='learn.extract')
    except Exception:
        pass
    try:
        facts = json.loads(resp.text or '{}').get('facts') or []
    except ValueError:
        return []
    return new_only([f for f in facts if isinstance(f, str)], known)


def learn_tick(u: dict, chat: str, now: datetime | None = None, convo: Path | None = None, client=None):
    """→ an Out('buttons') with today's new facts, or None. Once a day, 21:30–22:45."""
    from agent.core import Out
    from agent import life
    now = now or datetime.now()
    if not ((now.hour, now.minute) >= (21, 30) and now.hour < 23) or u.get('learn_day') == now.date().isoformat():
        return None
    u['learn_day'] = now.date().isoformat()
    turns = turns_since(now - timedelta(hours=24), convo)
    if not turns:
        return None
    try:
        facts = extract(turns, life.facts(chat), client)
    except Exception as e:
        print(f'  ⚠️ learn extract failed: {e!r}'[:200])
        return None
    if not facts:
        return None
    u['learn_pending'] = {'facts': facts, 'ts': now.isoformat(timespec='seconds')}
    return Out('buttons', '🧠 *דברים שלמדתי עליך היום:*\n' + '\n'.join(f'• {f}' for f in facts)
               + '\n\nלשמור בזיכרון? (אחר כך אפשר "תשכח ש…")',
               buttons=[('✅ לשמור', 'lf:ok'), ('❌ לא לשמור', 'lf:no')])


def button(u: dict, chat: str, payload: str) -> str:
    from agent import life
    pend = u.pop('learn_pending', None)
    if not pend:
        return 'זה כבר לא בתוקף.'
    if payload != 'lf:ok':
        return '👍 לא שמרתי.'
    for f in pend['facts']:
        life.remember(chat, f)
    return f"🧠 שמרתי {len(pend['facts'])} דברים. \"מה אתה זוכר?\" מראה הכול."


# ── gaps: what the planner needs and nobody told it ─────────────────────────

WORK_ASK = ('💼 שאלה אחת כדי שאוכל לתכנן לך את הימים: *מתי אתה עובד?*\n'
            'כתוב במילים, למשל "ראשון ורביעי 16-22, שישי 8-14" — או "לא עובד".')
ANSWER_RX = re.compile(r'ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת|\d{1,2}(?::\d{2})?\s*[-–]\s*\d{1,2}|לא עובד|בוקר|ערב|משמרת')


def gap_tick(u: dict, chat: str, now: datetime | None = None, plan_path: Path | None = None):
    """→ Out('text') asking when the owner works, once (again only after 30 days without an answer), 19:00–21:00."""
    from agent.core import Out
    from agent import planner
    now = now or datetime.now()
    if not 19 <= now.hour < 21 or now.weekday() == 5 or planner.work_known(chat, plan_path):
        return None
    last = u.get('work_asked', '')
    if last and now - datetime.fromisoformat(last) < timedelta(days=30):
        return None
    u['work_asked'] = now.isoformat(timespec='seconds')
    u['learn_wait'] = {'k': 'work', 'ts': now.isoformat(timespec='seconds')}
    return Out('text', WORK_ASK)


def awaiting_work(u: dict, text: str, now: datetime | None = None) -> bool:
    """Is this message the answer to the work question (asked < 14h ago, and it reads like days / hours)?"""
    w = u.get('learn_wait') or {}
    if w.get('k') != 'work':
        return False
    now = now or datetime.now()
    if now - datetime.fromisoformat(w['ts']) > timedelta(hours=14):
        u.pop('learn_wait', None)
        return False
    return bool(ANSWER_RX.search(text or '')) and len(text) < 300
