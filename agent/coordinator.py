"""
agent/coordinator.py — "תתאם עם יוסי פגישה בשבוע הבא אחרי 16:00" → the agent talks to Yossi for the owner.

Flow (The owner 08/10: "אני רוצה להגיד לו שלח הודעה ל… תתאם איתו כך וכך וזה יקרה"):
  1. The owner's request → plan (one Gemini call): who, the goal, the limits, a drafted first message.
  2. The owner approves the goal + first message (✅) — the ONLY approval needed for the back-and-forth.
  3. The bot (its own number, introducing itself as the owner's assistant) messages the person; their replies are
     answered automatically while they stay inside the approved goal, using the owner's real calendar availability.
  4. Agreed → confirmation to the person, and to the owner: "סגרתי … — להוסיף ליומן?" (✅).
     Outside the goal / a question only the owner can answer → "רגע, אני בודק עם המשתמש", and the owner is asked
     ("ליוסי: …" answers). The person says stop / "מי זה?" → polite apology and the task ends.
Safety: the person can only ever reach this conversation (role 'contact' — never the owner's tools); at most
MAX_AUTO automatic replies per task; tasks expire after 3 days; the owner sees every turn on request ("מה המצב עם יוסי?").
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from utils.gemini_sub import api_only

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / 'data' / 'coord_tasks.json'
MODEL = 'gemini-3.8-flash'
MAX_AUTO = 8
TTL_DAYS = 3


def _client():
    import google.genai as genai
    return genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))


@api_only()                                             # the log is the person's words
def _json_call(prompt: str, op: str, client=None) -> dict:
    from google.genai import types as T
    client = client or _client()
    resp = client.models.generate_content(
        model=MODEL, contents=prompt,
        config=T.GenerateContentConfig(temperature=0.3, max_output_tokens=2500, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op=op)
    try:
        return json.loads(resp.text or '{}')
    except ValueError:
        return {}


# ── tasks ──────────────────────────────────────────────────────────────────

def load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or TASKS).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save(d: dict, path: Path | None = None) -> None:
    p = path or TASKS
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def active_for(phone: str, path: Path | None = None) -> tuple[str, dict] | None:
    now = datetime.now().isoformat(timespec='seconds')
    for tid, t in load(path).items():
        if t['phone'] == phone and t['status'] in ('active', 'waiting_owner') and t['expires'] > now:
            return tid, t
    return None


def by_name(name: str, path: Path | None = None) -> tuple[str, dict] | None:
    hits = [(tid, t) for tid, t in load(path).items() if name and name in t['name'] and t['status'] != 'cancelled']
    return max(hits, key=lambda x: x[1]['created']) if hits else None


def availability(days: int = 14) -> str:
    """The owner's busy times (titles hidden) for the next `days` — what the agent may offer."""
    try:
        from integrations import gcal
        if not gcal.configured():
            return '(היומן לא מחובר — אל תציע זמנים, שאל את המשתמש)'
        now = datetime.now()
        busy = []
        for i in range(days):
            day = (now + timedelta(days=i)).date()
            for ev in gcal.events_on(day):
                st, en = ev.get('start') or {}, ev.get('end') or {}
                if 'dateTime' in st:
                    busy.append(f"{day:%a %d/%m} {st['dateTime'][11:16]}–{en.get('dateTime', '')[11:16]} תפוס")
                else:
                    busy.append(f"{day:%a %d/%m} כל היום תפוס")
        return '\n'.join(busy) or '(אין אירועים ביומן — כל הזמנים פנויים, בשעות סבירות)'
    except Exception:
        return '(לא הצלחתי לקרוא את היומן — אל תציע זמנים, שאל את המשתמש)'


# ── planning the owner's request ─────────────────────────────────────────────────

PLAN = """המשתמש (סטודנט, ת"א) מבקש מהעוזר האישי שלו בוואטסאפ לפנות למישהו בשמו. היום {now}.
הבקשה: "{text}"
אנשי קשר שמורים: {contacts}

החזר JSON:
{{"name": "למי (שם כפי שהמשתמש כתב)", "phone": "מספר אם המשתמש כתב אחד, אחרת ריק",
  "kind": "message" (רק להעביר הודעה) | "coordinate" (לתאם/לסגור משהו מול האדם — דורש הלוך-חזור),
  "goal": "המטרה במשפט אחד (למשל: לקבוע פגישת לימוד של שעה בשבוע הבא)",
  "limits": "הגבולות שהמשתמש נתן (ימים/שעות/מקום/מחיר) — ריק אם לא נתן",
  "first_message": "ההודעה הראשונה לאדם, בעברית, קצרה וחמה. חייבת להתחיל ב'היי {{שם}}, כאן העוזר האישי של המשתמש 🤖' "
                   "ולומר בבירור מה רוצים. בלי פרטים פרטיים של המשתמש מעבר לנדרש."}}"""


def plan(text: str, contact_names: list[str], client=None) -> dict:
    p = _json_call(PLAN.format(now=datetime.now().strftime('%A %d/%m/%Y %H:%M'), text=text,
                               contacts=', '.join(contact_names) or '(אין)'), 'coord.plan', client)
    p.setdefault('kind', 'coordinate')
    return p


def create(owner_chat: str, name: str, phone: str, p: dict, path: Path | None = None) -> str:
    d = load(path)
    tid = secrets.token_hex(3)
    now = datetime.now()
    d[tid] = {'owner': owner_chat, 'name': name, 'phone': phone, 'kind': p.get('kind', 'coordinate'),
              'goal': p.get('goal', ''), 'limits': p.get('limits', ''), 'status': 'draft', 'auto': 0,
              'created': now.isoformat(timespec='seconds'), 'expires': (now + timedelta(days=TTL_DAYS)).isoformat(timespec='seconds'),
              'log': [], 'first_message': p.get('first_message', '')}
    save(d, path)
    return tid


# ── the conversation with the person ───────────────────────────────────────

TURN = """אתה העוזר האישי של המשתמש ומתאם בשמו מול {name} בוואטסאפ. היום {now}.
המטרה שהמשתמש אישר: {goal}
הגבולות של המשתמש: {limits}
הזמנים התפוסים של המשתמש (מהיומן — כל השאר פנוי בשעות סבירות 08:00–22:00):
{busy}

השיחה עד עכשיו (bot = אתה, them = {name}, owner = הנחיה מהמשתמש):
{log}

החזר JSON:
{{"action": "reply" | "agreed" | "ask_owner" | "stop",
  "message": "ההודעה ל{name} (עברית, קצרה, אדיבה)",
  "to_owner": "עדכון קצר למשתמש (ב-agreed / ask_owner / stop)",
  "event": {{"title": "...", "date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM", "location": "..."}} או null}}
כללים:
- reply: רק אם התשובה בתוך המטרה והגבולות (למשל להציע/לאשר זמן פנוי). אל תתחייב לשום דבר אחר בשם המשתמש.
- agreed: כשנסגר דבר מוסכם וברור — message = אישור קצר ל{name}; מלא event אם זה מפגש/אירוע.
- ask_owner: שאלה/בקשה מחוץ למטרה, מחיר, משהו אישי, או אם אין זמן מתאים — message = "רגע, אני בודק עם המשתמש ואחזור אליך 🙏".
- stop: אם האדם לא מעוניין, מבקש להפסיק, או לא מבין מי זה — message = התנצלות קצרה.
- אסור להמציא מידע על המשתמש. אסור לשתף פרטים מהיומן מעבר ל"פנוי/לא פנוי"."""


def next_step(t: dict, client=None) -> dict:
    if t['auto'] >= MAX_AUTO:                                       # a long back-and-forth → the owner decides, and no model
        return {'action': 'ask_owner', 'message': 'רגע, אני בודק עם המשתמש ואחזור אליך 🙏',
                'to_owner': f"התכתבתי {t['auto']} פעמים ועוד לא נסגר. הנה ההודעה האחרונה שלו."}
    log = '\n'.join(f"{who}: {txt}" for who, txt in t['log'][-20:])
    d = _json_call(TURN.format(name=t['name'], now=datetime.now().strftime('%A %d/%m/%Y %H:%M'), goal=t['goal'],
                               limits=t['limits'] or '(לא צוינו)', busy=availability(), log=log), 'coord.turn', client)
    if d.get('action') not in ('reply', 'agreed', 'ask_owner', 'stop'):
        d = {'action': 'ask_owner', 'message': 'רגע, אני בודק עם המשתמש ואחזור אליך 🙏', 'to_owner': 'לא הבנתי את התשובה.'}
    return d
