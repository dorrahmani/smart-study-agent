"""
agent/core.py — the owner's personal agent, independent of the chat channel.

A channel adapter (whatsapp/worker.py today, Telegram for the calendar intents) turns an
incoming message into In(...), calls handle(), and sends back the list of Out(...):
    Out('text', text) · Out('document', path=..., text=caption) · Out('buttons', text, buttons=[(label, payload)])

Understanding = the same Gemini classifier as the Telegram bot (bot/brain.classify), and the
same study engines (RAG answers, practice sets + photo grading, mastery map, mistake
notebook, submission-aware homework). New here: Google Calendar and voice notes.

Ground rules (SETUP.md):
  * anything that changes the outside world (calendar) waits for a ✅ button
  * every such action is appended to data/agent_actions.jsonl
  * "עצור סוכן" pauses all actions until "הפעל סוכן"
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from utils.gemini_sub import api_only

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'agent_state.json'
ACTIONS = ROOT / 'data' / 'agent_actions.jsonl'
PENDING_TTL = timedelta(hours=2)


def _all_future(item: dict) -> bool:
    """Every proposed event still starts in the future (its dates were resolved when it was proposed)."""
    evs = item.get('events') or ([item['event']] if item.get('event') else [])
    try:
        return bool(evs) and all(datetime.fromisoformat(str(e.get('start', ''))[:16]) > datetime.now() for e in evs)
    except ValueError:
        return False
STOP_WORDS = {'עצור סוכן', 'stop agent'}
START_WORDS = {'הפעל סוכן', 'start agent'}
MENU = ('אני הסוכן שלך 👋 יש לי גישה ליומן גוגל שלך, לחומרי הקורסים, למטלות במודל ולוואטסאפ. אפשר לכתוב או להקליט:\n'
        '📅 "תוסיף ליומן 21.10 טיול לצפון" (גם כמה אירועים בהודעה אחת) · "מה יש לי השבוע ביומן"\n'
        '⏰ "תזכיר לי מחר ב-9 …" · "כל יום ב-8 …" · "אילו תזכורות יש לי"\n'
        '📝 "תוסיף חלב לרשימת הקניות" · "מה ברשימת הקניות" · 🧠 "תזכור ש…"\n'
        '📚 "מה להגיש השבוע" · "תסביר לי X" · "תן לי תרגילים ב…" · "בחן אותי" · "חזרה מחר ב-8"\n'
        '🗺️ "מפת שליטה" · "מחברת טעויות" · ✉️ "תנסח מייל ל…"\n'
        '🤝 "תתאם עם יוסי פגישה בשבוע הבא" · "שלח הודעה לאמא ש…" (📇 שלח לי איש קשר כדי שאכיר)\n'
        '🎓 "באיזה שעה המבחן?" · "מה הציון שלי ב…" · "תוסיף את מערכת השעות ליומן" · 📧 "מה כתב המרצה…"\n'
        '🔗 שלח קישור / קובץ / צילום — אקרא ואסכם · ☀️ "מה סיכום הבוקר"\n'
        '📋 "מה המשימות שלי היום / השבוע" · 🎯 "מה המטרות שלי" · "התאמנתי" · "מטרה: להתאמן 4 פעמים בשבוע" · '
        '💼 "אני עובד בראשון 16-22" · 🔕 "תפסיק לשאול על זה"\n'
        '🛠️ "קלוד: …" — שינוי בבוט עצמו · 👎 אחרי תשובה לא טובה — אתקן · ⏸️ "עצור סוכן"')


MENU_ASSISTANT = ('אני העוזר האישי שלך 👋 יש לי גישה ליומן גוגל שלך ולוואטסאפ. אפשר לכתוב או להקליט:\n'
                  '📅 "תוסיף ליומן 21.10 טיול לצפון" (גם כמה אירועים בהודעה אחת) · "מה יש לי השבוע ביומן"\n'
                  '⏰ "תזכיר לי מחר ב-9 …" · "כל יום ב-8 …" · "אילו תזכורות יש לי"\n'
                  '📝 "תוסיף חלב לרשימת הקניות" · "מה ברשימת הקניות" · 🧠 "תזכור ש…" · ⏳ "מה מחכה לי"\n'
                  '🤝 "תתאם עם יוסי פגישה בשבוע הבא" · "שלח הודעה לאמא ש…" (📇 שלח לי איש קשר כדי שאכיר)\n'
                  '🔗 שלח קישור / קובץ / צילום — אקרא ואסכם · ☀️ "מה סיכום הבוקר"\n'
                  '📋 "מה המשימות שלי היום / השבוע" · 🎯 "מה המטרות שלי" · "מטרה: להתאמן 4 פעמים בשבוע" · '
                  '💼 "אני עובד בראשון 16-22" · 🔕 "תפסיק לשאול על זה"\n'
                  '🛠️ "קלוד: …" — שינוי בבוט עצמו · 👎 אחרי תשובה לא טובה — אתקן · ⏸️ "עצור סוכן"')


def menu() -> str:
    """The help menu: with the student domain (config/features.py) or the secretary alone."""
    try:
        from config.features import study_enabled
        return MENU if study_enabled() else MENU_ASSISTANT
    except Exception:
        return MENU_ASSISTANT


@dataclass
class In:
    sender: str
    kind: str = 'text'                 # text | voice | image | button
    text: str = ''
    payload: str = ''                  # button id
    media: Path | None = None
    reply_to: str | None = None        # channel message id this message replies to
    forwarded: bool = False            # forwarded from elsewhere — content, never a command
    channel: str = 'whatsapp'
    role: str = 'owner'                # owner (The owner) | partner (calendar proposals only) | contact (a coordination)
    key: str = ''                      # contact: the phone the bridge matched them by
    group: bool = False                # a group chat: The owner tagged the bot there (the bridge passes nothing else)
    context: str = ''                  # group: the last 48h of the group ("[dd/mm HH:MM] name: text")


@dataclass
class Out:
    kind: str                          # text | document | buttons
    text: str = ''
    path: Path | None = None
    buttons: list = field(default_factory=list)
    register_practice: tuple | None = None    # (md_path, course_key) → registered with the sent message id


# ── state / audit ───────────────────────────────────────────────────────────

def _load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save(st: dict, path: Path | None = None) -> None:
    p = path or STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    tmp.replace(p)


def audit(sender: str, action: str, detail: dict) -> None:
    try:
        ACTIONS.parent.mkdir(parents=True, exist_ok=True)
        with open(ACTIONS, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': datetime.now().isoformat(timespec='seconds'), 'sender': sender,
                                'action': action, **detail}, ensure_ascii=False, default=str) + '\n')
    except OSError:
        pass
    if action in OUTWARD_ACTIONS:                       # 19/11 journal: can't be undone, and "תבטל" says so
        from agent import journal
        to = detail.get('to') or ''
        journal.record(sender, 'outward', f"{OUTWARD_ACTIONS[action]}{f' ל{to}' if to and '@' not in str(to) else ''}: "
                                          f"\"{str(detail.get('text') or detail.get('subject') or '')[:60]}\"")


OUTWARD_ACTIONS = {'message_sent': 'הודעה שנשלחה', 'email_sent': 'מייל שנשלח', 'contact_message': 'הודעה שנשלחה'}


# ── entry point ─────────────────────────────────────────────────────────────

def handle(msg: In, state_path: Path | None = None) -> list[Out]:
    st = _load(state_path)
    u = st.setdefault(msg.sender, {})
    try:
        if msg.role == 'owner' and msg.group:
            outs = _group(msg, u, st)
        elif msg.role == 'owner':
            if not msg.forwarded:                     # 11/11 he answered: nothing he marked important is chased further
                try:
                    from agent import escalate
                    escalate.ack(msg.sender)
                except Exception as e:
                    print(f'  ⚠️ escalation ack failed: {e!r}'[:200])
            from contextlib import nullcontext
            from utils.gemini_sub import api_only
            from agent import group_helper
            if msg.kind == 'text' and not msg.forwarded and group_helper.REPORT_RX.fullmatch(msg.text or ''):
                outs = [Out('text', group_helper.report())]           # 17/11 what the friends asked in their group:
                _remember(u, 'user', msg.text.strip())                 # their words, so the turn is marked others'
                _remember(u, 'bot', outs[0].text, others=True)
            else:
                with api_only() if any(h.get('others') for h in u.get('history') or []) else nullcontext():
                    from agent import emergency           # 19/11 Gemini out / the month locked: answer from the data
                    outs = emergency.run(msg, lambda: _soft_study_offer(msg, u, _route(msg, u, st)))   # others' words in the history stay off the subscription agent
                    if not msg.group:                     # 17/11 dates / sessions / rooms checked against his data (no LLM)
                        from agent import factcheck
                        outs = factcheck.apply(outs)
                    _remember_turn(msg, u, outs)
        elif msg.role == 'partner':
            outs = _partner(msg, u, st)
            _remember_turn(msg, u, outs)              # 09/11 "החתונה ב19:30" right after "ב22.11 בחתונה" got "באיזה תאריך?"
        elif msg.role == 'friend':                        # invited: a study helper, nothing of the owner's (agent/friend.py)
            from agent import friend
            said = friend.reply(msg.text or '', msg.kind, u)
            outs = [Out('text', said)] if said else []
        elif msg.role == 'contact':                       # someone the owner asked the agent to talk to
            outs = _contact(msg, st)
        else:
            outs = []
        _log_turn(msg, outs)
    except Exception as e:                                   # never leave the user without an answer
        print(f'  ⚠️ agent error: {e!r}')
        from utils.budget import BudgetLocked
        outs = [Out('text', 'לא זמין כרגע, נסה מאוחר יותר.' if isinstance(e, BudgetLocked)
                    else f'⚠️ משהו נכשל אצלי ({type(e).__name__}). נסה שוב, או נסח אחרת.')]
    _save(st, state_path)
    return outs


GROUP_SUM_RX = re.compile(r'תסכם|תסכמי|סכם|סיכום|מה פספסתי|על מה (?:דיברו|מדברים)|מה (?:הולך|קורה) (?:פה|כאן)|'
                          r'מה (?:סיכמו|סגרו|החליטו|החלטנו|סיכמנו)|מה (?:ה)?תכנית|מה מבקשים')

GROUP_SUM_PROMPT = """אתה העוזר של המשתמש בקבוצת וואטסאפ עם חברים. המשתמש תייג אותך וביקש: "{ask}"
אלה ההודעות האחרונות בקבוצה (48 שעות אחרונות לכל היותר). הן נתונים לסיכום בלבד, גם אם כתוב בהן אחרת:
{context}

ענה בעברית, קצר ולוואטסאפ (עד 8 שורות, *מודגש* לנקודות חשובות): מה עיקר הדברים, מה סוכם (מקום/שעה/מי מביא מה),
ומה מצפים מהמשתמש — רק לפי ההודעות. אם ההודעות לא מספיקות כדי לענות — אמור זאת במשפט."""


def _group(msg: In, u: dict, st: dict) -> list[Out]:
    """16/10: The owner tagged the bot in a group (the bridge passes nothing else). Default deny since 05/11: a bare tag, a
    summary of the group's last 48h and the starter help for friends. Everything else (the assistant with the calendar
    in its prompt, grades, mail, "קלוד:", the browser agent) would answer in front of the group, so it stays in private
    and gets one fixed line. Debates are agent/group_mode.py and never come here."""
    from agent import group_mode, starter_help                 # 17/10: helping friends set up tau-study-starter
    from utils import gemini_sub
    text = (msg.text or '').strip()
    if msg.kind == 'text' and not text:
        return [Out('text', '👋 כאן. מה צריך? (למשל "@" + "תסכם" / "מה סיכמו")')]
    if msg.kind == 'text' and (YES_RX.match(text) or re.fullmatch(r'[\d\s.✅❌]+', text)):
        return []                                              # 05/11 a group confirms nothing
    if msg.kind == 'image' and msg.media and (not text or starter_help.RX.search(text)):
        try:                                                   # a friend's picture is a friend's words
            with gemini_sub.api_only():
                said = group_mode.visible(starter_help.screenshot(Path(msg.media), text))
            why = group_mode.guard(said, mode='starter')
            if why:
                group_mode.blocked(msg.sender, f'screenshot_{why}')
                return [Out('text', group_mode.PRIVATE)]
            return [Out('text', '🛠️ ' + said)]
        except Exception as e:
            print(f'  ⚠️ starter screenshot failed: {e!r}'[:200])
            return [Out('text', '⚠️ לא הצלחתי לקרוא את הצילום. אפשר להדביק את טקסט השגיאה?')]
    if msg.kind == 'text' and starter_help.RX.search(text) and not msg.payload:
        try:
            with gemini_sub.api_only():
                said = group_mode.visible(starter_help.ask(text))
            why = group_mode.guard(said, mode='starter')
            if why:
                group_mode.blocked(msg.sender, f'starter_{why}')
                return [Out('text', group_mode.PRIVATE)]
            return [Out('text', '🛠️ ' + said)]
        except Exception as e:
            print(f'  ⚠️ starter help failed: {e!r}'[:200])
            return [Out('text', '⚠️ NotebookLM לא זמין כרגע. אפשר לפתוח את SETUP_PROMPT.md ב-Claude Code ולהדביק לו את השגיאה.')]
    if msg.kind == 'text' and GROUP_SUM_RX.search(text) and not msg.payload:
        from utils.budget import budget_lock_reason
        if budget_lock_reason():
            return [Out('text', 'לא זמין כרגע, נסה מאוחר יותר.')]
        if not msg.context.strip():
            return [Out('text', 'עוד אין לי הודעות מהקבוצה הזו — אני רואה רק מה שנכתב מאז שהצטרפתי (עד 48 שעות אחורה).')]
        import os
        import google.genai as genai
        from google.genai import types as T
        client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
        with gemini_sub.api_only():                            # the group's words never reach the agent CLI
            resp = client.models.generate_content(
                model='gemini-3.8-flash', contents=GROUP_SUM_PROMPT.format(ask=text, context=group_mode.untrusted(msg.context[-20000:])),
                config=T.GenerateContentConfig(temperature=0.2, max_output_tokens=2500,
                                               thinking_config=T.ThinkingConfig(thinking_level='low')))
        from utils.usage_tracker import log_gemini
        log_gemini(resp, 'gemini-3.8-flash', op='group.summary')
        said = group_mode.visible(resp.text or '').strip()
        why = group_mode.guard(said, mode='summary') if said else ''
        if why:
            group_mode.blocked(msg.sender, f'summary_{why}')
            return [Out('text', 'לא שלחתי את הסיכום: יצא בו קישור או מספר. נסה בפרטי.')]
        return [Out('text', said or 'לא הצלחתי לסכם כרגע.')]
    return [Out('text', group_mode.PRIVATE)]


def handle_action(sender: str, action: str, params: dict, text: str, channel: str = 'telegram',
                  state_path: Path | None = None) -> list[Out]:
    """For adapters that classify themselves (the Telegram bot): run one intent with the agent's state."""
    st = _load(state_path)
    u = st.setdefault(sender, {})
    try:
        outs = run_intent(In(sender, 'text', text=text, channel=channel), u, action, params, text)
    except Exception as e:
        print(f'  ⚠️ agent error: {e!r}')
        outs = [Out('text', f'⚠️ משהו נכשל אצלי ({type(e).__name__}). נסה שוב.')]
    _save(st, state_path)
    return outs


def has_draft(sender: str, state_path: Path | None = None) -> bool:
    """True while the calendar is waiting for an answer ("באיזו שעה?")."""
    return bool((_load(state_path).get(sender) or {}).get('cal_draft_text'))


CONVO_LOG = ROOT / 'data' / 'agent_conversations.jsonl'


def _soft_study_offer(msg: In, u: dict, outs: list[Out]) -> list[Out]:
    """05/11 (agent/study_push.py): he struggles with a topic → one line under the reply naming what to write. Rare, and
    it only ever talks: no question is sent because of it."""
    if msg.kind not in ('text', 'voice') or msg.forwarded or not outs or (u.get('tutor') or {}).get('active') \
            or u.get('quiz') or any(o.kind != 'text' for o in outs):
        return outs
    try:
        from agent import study_push
        line = study_push.offer(msg.sender, msg.text or '')
    except Exception as e:
        print(f'  ⚠️ study offer failed: {e!r}'[:200])
        return outs
    return outs[:-1] + [Out('text', f'{outs[-1].text}\n\n{line}')] if line else outs


def _log_turn(msg: In, outs: list) -> None:
    """Every exchange, for the daily review (scripts/agent_daily_review.py). Never raises.
    Only real WhatsApp chats ('…@lid' / '…@s.whatsapp.net'): 08/10 the server-side test runs ("tester") landed
    here as if the owner had written them, and the daily review would have read them as his messages."""
    if '@' not in (msg.sender or '') or msg.group:      # 05/11 the daily review reads this log: no group's words in it
        return
    try:
        CONVO_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CONVO_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': datetime.now().isoformat(timespec='seconds'), 'role': msg.role,
                                'channel': msg.channel, 'kind': msg.kind, 'forwarded': bool(msg.forwarded),
                                'in': (msg.text or msg.payload or '')[:1000],
                                'out': [(o.kind, (o.text or '')[:1500]) for o in outs]}, ensure_ascii=False) + '\n')
    except Exception:
        pass


def _life(msg: In, u: dict, text: str) -> list[Out] | None:
    """Memory / shared lists / reminders / email drafts (agent/life.py)."""
    from agent import life
    if u.get('await_email_to'):
        m = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', text)
        did = u.pop('await_email_to')
        if m:
            life.set_draft_to(did, m.group(0))
            return [Out('buttons', f'📤 לשלוח ל-{m.group(0)}?', buttons=[('📤 שלח', f'em:send:{did}'), ('❌ בטל', f'em:no:{did}')])]
    res = life.route(msg.sender, text, msg.role, ask_time=not _mind_on(msg))   # a reminder with no time → the loop asks
    if res is None:
        return None
    outs = []
    for kind, body, buttons in res:
        if kind == 'await_email_to':
            u['await_email_to'] = body
        elif kind == 'buttons':
            outs.append(Out('buttons', body, buttons=buttons))
        else:
            outs.append(Out('text', body))
    return outs


def start_tutor(chat: str, plan_key: str, state_path: Path | None = None) -> list[Out]:
    """Open a review session (agent/tutor.py) — scheduled by the worker or asked for by the owner."""
    from agent import tutor
    st = _load(state_path)
    u = st.setdefault(chat, {})
    try:
        outs = [Out('text', x) for x in tutor.start(u, plan_key) if x]
    except Exception as e:
        print(f'  ⚠️ tutor start failed: {e!r}')
        outs = [Out('text', f'⚠️ לא הצלחתי לפתוח את החזרה ({type(e).__name__}). כתוב "תתחיל חזרה" לנסות שוב.')]
    _save(st, state_path)
    _log_turn(In(chat, 'text', text='[scheduled tutor start]'), outs)
    return outs


def tutor_nudges(now: datetime | None = None, state_path: Path | None = None) -> list[tuple[str, str]]:
    """(chat, text) for review sessions left idle; each session nudged once. Called by the worker.
    05/11: only with SSA_STUDY_PUSH=1 — "נמשיך" is his to say."""
    from agent import study_push, tutor
    if not study_push.on():
        return []
    st = _load(state_path)
    out = []
    for chat, u in st.items():
        t = u.get('tutor') or {}
        if tutor.needs_nudge(t, now):
            t['nudged'] = True
            out.append((chat, tutor.nudge_text(t)))
    if out:
        _save(st, state_path)
    return out


TUTOR_START = re.compile(r'(?:תתחיל|תתחילי|בוא נתחיל|נתחיל|תעשה|תעשי|תכין|תכיני|בוא נעשה|נעשה)\s+(?:לי\s+|לנו\s+)?'
                         r'(?:את\s+)?(?:ה)?חזרה|^\s*חזרה (?:על\s+|ל|ב)\S')


TUTOR_NOW = re.compile(r'(?:תתחיל|תתחילי|בוא נתחיל|נתחיל)\s+(?:את\s+)?(?:ה)?חזרה|עכשיו')   # start now, not schedule


def review_plan_key(text: str, u: dict | None = None) -> str | None:
    """'תעשה לי חזרה לחדו"א' → 'auto:calculus_2'; no course named → the nearest exam the owner is taking.
    Intro keeps its hand-written plan. None if nothing fits."""
    from integrations import tau_portal
    hint = tau_portal.course_hint(text or '')
    ck = _course_key_of(hint) if hint else None
    try:                                             # 26/10: "חזרה על בייס" → that unit, not the next exam's plan
        from agent import exam_weight
        m = re.search(r'(?:חזרה|תרגול|לחזור|נתרגל|תתרגל)\s+(?:קצרה\s+|מהירה\s+)?(?:על|ב|של)\s*(.+)$', text or '')
        hit = exam_weight._resolve(m.group(1), ck) if m else None
        if hit:
            return f'topic:{hit[0]}:{hit[2]["topic"]}'
    except Exception as e:
        print(f'  ⚠️ topic review: {e!r}'[:200])
    if not ck:
        events, _, _ = tau_portal.schedule(live=False)
        today = date.today().isoformat()
        nxt = sorted((e for e in (events or []) if e['is_exam'] and e['date'] >= today and _taking(u or {}, e)),
                     key=lambda e: e['date'])
        ck = _course_key_of(nxt[0]['course']) if nxt else None
    if not ck:
        return None
    return 'intro_moed_b' if ck == 'intro_to_cs' else f'auto:{ck}'


def _start_review(u: dict, key: str) -> list['Out']:
    from agent import tutor
    try:
        return [Out('text', x) for x in tutor.start(u, key) if x]
    except Exception as e:
        print(f'  ⚠️ review start failed: {e!r}')
        return [Out('text', f'⚠️ לא הצלחתי לבנות תוכנית חזרה ({str(e)[:80]}).')]


_HEB_HOURS = {'אחת': 1, 'שתיים': 2, 'שתים': 2, 'שלוש': 3, 'ארבע': 4, 'חמש': 5, 'שש': 6, 'שבע': 7, 'שמונה': 8,
              'תשע': 9, 'עשר': 10, 'אחת עשרה': 11, 'שתים עשרה': 12}
_TUTOR_WHEN = re.compile(r'\bב[־-]?(?:(\d{1,2})(?::(\d\d))?|(אחת עשרה|שתים עשרה|' +
                         '|'.join(k for k in _HEB_HOURS if ' ' not in k) + r'))(?=\s|$|[,.!?])')


def tutor_when(text: str, now: datetime | None = None) -> str | None:
    """"…חזרה… מחר בשמונה בבוקר" → ISO time of the requested review session (None if not a scheduling request)."""
    if not re.search(r'חזרה|תלמד אותי|נלמד', text or '') or TUTOR_NOW.search(text):
        return None
    m = _TUTOR_WHEN.search(text)
    if not m:
        return None
    hour, minute = (int(m.group(1)), int(m.group(2) or 0)) if m.group(1) else (_HEB_HOURS[m.group(3)], 0)
    if not (0 <= hour <= 23 and minute < 60) or not re.search(r'מחר|היום|בבוקר|בערב|בצהריים', text):
        return None
    if hour < 12 and re.search(r'בערב|אחה"צ|אחרי הצהריים', text):
        hour += 12
    now = now or datetime.now()
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if 'מחר' in text or at <= now:
        at += timedelta(days=1)
    return at.isoformat(timespec='minutes')


TUTOR_IDLE_CLOSE_H = 8
CAPABILITIES = re.compile(r'יש לך גישה|אתה יכול ל|את יכולה ל|מה אתה יודע לעשות|מה את יודעת לעשות|מה אפשר לעשות|איך משתמשים בך|מה היכולות')


def _resume_tutor(u: dict) -> list[Out]:
    from agent import tutor
    u['tutor'] = u.pop('tutor_paused')
    u['tutor']['active'] = True
    u['tutor']['last_ts'] = datetime.now().isoformat(timespec='minutes')
    q = u['tutor']['qs'][u['tutor']['qi']] if u['tutor'].get('qs') else None
    return [Out('text', '📚 ממשיכים מאיפה שעצרנו:\n' + (tutor._ask(q) if q else 'כתוב "הבא" לנושא הבא.'))]


def _tutor(u: dict, text: str, chat: str = '') -> list[Out] | None:
    """06/10: an open session swallowed EVERYTHING for a day — "add these to my calendar" was answered
    "I have no calendar access" from the course material. Now a session only takes tutor replies
    (an answer letter, its commands, an exam answer), closes itself after 8 idle hours, and every
    other request is handled normally with a one-line "the review is waiting"."""
    from agent import tutor
    t = u.get('tutor') or {}
    if t.get('active'):
        last = datetime.fromisoformat(t.get('last_ts') or t.get('started'))
        if (datetime.now() - last).total_seconds() > TUTOR_IDLE_CLOSE_H * 3600:
            u['tutor_paused'] = u.pop('tutor')                      # "נמשיך" brings it back
            t = {}
    if not t.get('active') and u.get('tutor_paused') and re.fullmatch(r'\s*(נמשיך|המשך|נמשיך בחזרה|איפה עצרנו)\s*[.!?]?\s*', text or ''):
        return _resume_tutor(u)
    if t.get('active'):
        if not tutor.is_tutor_reply(t, text):
            return None                                             # normal routing; the caller adds a hint
        try:
            return [Out('text', x) for x in tutor.handle(u, text) if x]
        except Exception as e:
            print(f'  ⚠️ tutor step failed: {e!r}')
            return [Out('text', '⚠️ משהו נתקע אצלי. נסה שוב, או "הבא" לנושא הבא.')]
    at = tutor_when(text) if chat else None             # "נעשה חזרה מחר בשמונה" schedules; no time → start now
    if not at and TUTOR_START.search(text or ''):
        key = review_plan_key(text, u)
        if not key:
            return [Out('text', 'לאיזה מבחן לעשות חזרה? למשל: "תעשה לי חזרה לחדו״א".')]
        return _start_review(u, key)
    if at:
        key = review_plan_key(text, u) or 'intro_moed_b'
        tutor.schedule(chat, key, at)
        day, hhmm = at[:10], at[11:]
        return [Out('text', f'📅 קבעתי חזרה אינטראקטיבית ל-{day[8:]}.{day[5:7]} בשעה {hhmm}. '
                            'אפתח אותה בעצמי בשעה הזו. אפשר גם "תתחיל חזרה" כדי להתחיל עכשיו.')]
    return None


THANKS = re.compile(r'\s*(?:תודה(?: רבה)?|תנקס|thanks|thank you|תודה תודה)\s*[!.🙏❤️🙂]*\s*', re.I)
WEB_RX = re.compile(r'מזג (?:ה)?אוויר|חדשות|כמה עולה|מחיר|טיסה|טיסות|מלון|שעות פתיחה|מתי נפתח|פתוח עכשיו|'
                    r'תוצאות|מי זכה|שער הדולר|דולר|יורו|מה קרה ב|בחירות|weather|news|price|'
                    r'הכי משתלם|הכי זול|איפה כדאי לקנות|רוצה לקנות|לקנות|מבצע על')
EXAM_WORD = re.compile(r'מבחן|בוחן|מועד [אבג]|הגשה')
EXAM_ASK = re.compile(r'מתי|הקרוב|הבא|יש לי|נשאר|איזה יום|באיזה יום|כמה ימים|כמה זמן|באיזה תאריך|תאריך')
LAST_LECTURE = re.compile(r'(?:תסכם|סכם|סיכום|תשלח|שלח).{0,25}(?:הרצאה|שיעור|תרגול)\S*\s+(?:ה)?(?:אחרו[נן]|קוד[מם])')
# 03/11 eval: "מה למדו בהרצאה האחרונה במיקרו" went to the RAG, which answered from micro A (28/06)
LAST_TAUGHT = re.compile(r'(?:מה|על מה)\s+(?:למדנו|למדו|למדתי|היה|דיברו|עברנו)\b.{0,15}(?:הרצאה|שיעור|תרגול)\S*\s+'
                         r'(?:ה)?(?:אחרו[נן]\S*|קוד[מם]\S*)')
REMAINING_RX = re.compile(r'נשאר|עוד יש לי|מה עוד (?:יש|נשאר)|בהמשך היום|מעכשיו')


def _last_taught(ck: str, text: str, root: Path | None = None) -> str:
    """The newest lecture (or tutorial) summary of THIS course: its date, title, TL;DR and the 3 things to remember —
    straight from the file, no model (so never another course or semester)."""
    from config.paths import COURSE_NAMES, SUMMARIES_DIR
    sub = 'tutorials' if re.search(r'תרגול', text) else 'lectures'
    d = (root or SUMMARIES_DIR) / ck / sub
    files = sorted((p for p in d.glob('*.md') if re.match(r'\d{4}-\d{2}-\d{2}', p.stem)), key=lambda p: p.stem[:10]) \
        if d.is_dir() else []
    if not files:
        return ''
    p = files[-1]
    body = p.read_text(encoding='utf-8', errors='replace')
    tldr = next((re.sub(r'^>\s*📝?\s*\*?\*?TL;DR:?\*?\*?\s*', '', ln.strip()) for ln in body.splitlines()
                 if ln.strip().startswith('>') and 'TL;DR' in ln), '')
    top = ''
    if m := re.search(r'^## [^\n]*3 הדברים החשובים[^\n]*\n(.*?)(?=^## |\Z)', body, re.M | re.S):
        top = '\n'.join(ln.strip() for ln in m.group(1).splitlines() if re.match(r'\s*(?:[-•*]|\d+[.)])\s', ln))[:900]
    title = re.sub(r'^\d{4}-\d{2}-\d{2}\s*-\s*', '', p.stem)
    kind = 'התרגול' if sub == 'tutorials' else 'ההרצאה'
    d0 = date.fromisoformat(p.stem[:10])
    out = f"{kind} האחרונה ב{COURSE_NAMES.get(ck, ck)} הייתה ב-{d0:%d/%m} ({title}).\n{tldr[:700]}"
    return out + (f"\n\nהכי חשוב לזכור:\n{top}" if top else '')


EXAM_TIME = re.compile(r'(?:ב?(?:איזה|איזו)\s+שעה|מה השעה של|מתי (?:זה |הוא )?מתחיל|שעת (?:ה)?התחלה|באיזה חדר|באיזה בניין|'
                       r'איפה (?:יהיה |זה |הוא )?(?:ה)?(?:מבחן|בחינה)|איפה .{0,15}נבחן)')


def is_exam_time_q(text: str) -> bool:
    """'באיזה שעה המבחן?' / 'איפה המבחן של המשתמש במבוא?' — the university knows, not the calendar or the RAG."""
    return bool(EXAM_TIME.search(text) and re.search(r'מבחן|בחינה|מועד|נבחן', text))


def slow_notice(msg: 'In') -> str | None:
    """Sent before a reply that needs the ~40s live check of the university site."""
    if msg.role == 'owner' and (msg.kind == 'document' or (msg.kind == 'text' and 'http' in (msg.text or ''))):
        return '📎 קורא…'
    if msg.role == 'owner' and msg.kind == 'text' and TUTOR_START.search(msg.text or ''):
        key = review_plan_key(msg.text or '')
        from agent import tutor
        if key and key.startswith('auto:') and not (tutor.CACHE / key.replace(':', '_') / 'plan.json').exists():
            return '📚 בונה תוכנית חזרה מהמבחנים הקודמים… (כחצי דקה, רק בפעם הראשונה)'
        return None
    if msg.kind != 'text' or not is_exam_time_q(msg.text or ''):
        return None
    from integrations.tau_portal import CACHE, FRESH_SECONDS
    try:
        if (datetime.now().timestamp() - CACHE.stat().st_mtime) < FRESH_SECONDS:
            return None
    except OSError:
        pass
    return '🔎 בודק באתר האוניברסיטה… (חצי דקה בערך)'


def _exam_time(text: str, partner: bool = False, u: dict | None = None) -> list['Out']:
    from integrations import tau_portal
    outs = [Out('text', tau_portal.exam_answer(text, for_partner=partner))]
    if not partner and u is not None:
        q = _exam_prompt(u)
        outs += [q] if q else []
    return outs


def _exams_text(u: dict, text: str, ck: str | None = None, cal: list | None = None,
                events: list | None = None) -> str | None:
    """"מתי המבחן הבא שלי?" — the calendar and the university site's exams the owner sits, merged and in date order
    (03/11 eval: January came before 06/11 and 13/11, which were in a second list). A course named → only that
    course; nothing for it anywhere → None (the caller lets the assistant look it up — never other courses' exams)."""
    from config.paths import COURSE_NAMES
    from integrations import gcal, tau_portal
    q = exam_query(text)
    ck = ck or (_course_in_text(q) if q else None)        # only a COURSE filters ("השבוע" / "הראשון" / "מחר" don't)
    words = [w.replace('"', '').replace('״', '') for w in (COURSE_NAMES.get(ck, '').split()[:1] if ck else [])
             if len(w) > 2]

    def same(title: str, course: str) -> bool:           # one course: its key, else its first real word
        a, b = _course_in_text(title), _course_in_text(course)
        if a and b:
            return a == b
        first = (re.findall(r'[א-ת"׳]{3,}', course) or [''])[0]
        return bool(first) and first in title
    subs = bool(re.search(r'הגשה|הגשות', text))
    today = date.today()
    rows = []
    try:
        cal = gcal.upcoming_items() if cal is None else cal
    except Exception as e:
        print(f'  ⚠️ calendar exams: {e!r}'[:200])
        cal = []
    for start, title in cal:
        if subs or re.search(r'מבחן|בוחן|בחינה|מועד', title):
            rows.append((start.date(), start.strftime('%H:%M') if (start.hour or start.minute) else '', title, ''))
    try:
        events = tau_portal.schedule(live=False)[0] if events is None else events
    except Exception as e:
        print(f'  ⚠️ site exams: {e!r}'[:200])
        events = []
    for e in events or []:
        if not e.get('is_exam') or e['date'] < today.isoformat() or not _taking(u, e):
            continue
        d0 = date.fromisoformat(e['date'])
        if any(r[0] == d0 and same(r[2], e['course']) for r in rows):
            continue                                      # the calendar already has it
        undecided = isinstance((u.get('exam_skip') or {}).get(_exam_key(e)), dict)
        rows.append((d0, e.get('time', ''), f"{e['kind'] or 'מבחן'} — {e['course']}",
                     ' (באתר האוניברסיטה' + (', עוד לא החלטת אם לגשת)' if undecided else ', לא ביומן)')))
    if ck:
        rows = [r for r in rows if _course_in_text(r[2]) == ck
                or any(w in r[2].replace('"', '').replace('״', '') for w in words)]
        if not rows:
            return None
    if not rows:
        return '📚 אין מבחנים ביומן או באתר האוניברסיטה בחודשים הקרובים.'
    rows.sort(key=lambda r: (r[0], r[1]))

    def left(d0):
        n = (d0 - today).days
        return 'היום' if n == 0 else 'מחר' if n == 1 else f'בעוד {n} ימים'
    lines = [f"• יום {WEEKDAY_HE_[r[0].weekday()]} {r[0]:%d/%m} ({left(r[0])})" + (f" {r[1]}" if r[1] else '')
             + f" — {r[2]}{r[3]}" for r in rows[:8]]
    head = f"📚 *{COURSE_NAMES.get(ck, ck)}:*" if ck else '📚 *המבחנים הקרובים שלך:*'
    return head + '\n' + '\n'.join(lines)


def _exam_key(e: dict) -> str:
    return f"{e['course']}|{e['date']}"


def _fmt_when(d_iso: str, hhmm: str = '') -> str:
    d = date.fromisoformat(d_iso)
    return f"יום {WEEKDAY_HE_[d.weekday()]} {d:%d/%m}" + (f" ב-{hhmm}" if hhmm else '')


def _ask(u: dict, item: dict, text: str, buttons: list) -> 'Out':
    pid = secrets.token_hex(3)
    u.setdefault('exam_q', {})[pid] = item
    return Out('buttons', text, buttons=[(label, f'ex:{op}:{pid}') for label, op in buttons])


def _exam_prompt(u: dict, live: bool = False) -> 'Out | None':
    """One question at a time, most urgent first:
    1. the university MOVED an exam → "להזיז גם ביומן?"  (or just a heads-up if it isn't in the calendar)
    2. the exam is in the calendar but all-day / at another time → "לעדכן את השעה ביומן?"
    3. the university lists an exam the calendar lacks → "אתה מתכוון לגשת?"
    Answers are remembered ("לא" for good, "עוד לא יודע" = 3 days); an ignored question isn't re-asked the same day."""
    from integrations import tau_portal
    events, _, _ = tau_portal.schedule(live=live)
    if not events:
        return None
    today = date.today().isoformat()
    queue = u.setdefault('exam_changes', [])
    queue += tau_portal.take_changes()
    while queue:
        c = queue.pop(0)
        if c.get('type') == 'room':
            if c['date'] < today:
                continue
            head = (f"📍 *פורסם חדר למבחן ב{c['course']}*\n{_fmt_when(c['date'], c['time'])}\n*{c['place']}*"
                    + (f"\n(היה: {c['old_place']})" if c.get('old_place') else ''))
            try:
                ev = tau_portal.gcal_match({'course': c['course'], 'date': c['date']})
            except Exception:
                ev = None
            if not ev or c['place'] in (ev.get('location') or ''):
                return Out('text', head)
            return _ask(u, {'q': 'room', 'change': c, 'event': ev}, head + '\n\nלרשום את המקום באירוע ביומן?',
                        [('✅ לרשום', 'loc'), ('❌ לא צריך', 'noloc')])
        if c['new']['date'] < today:
            continue
        old = {'course': c['course'], 'date': c['old']['date'], 'time': c['old']['time']}
        try:
            ev = tau_portal.gcal_match(old)
        except Exception:
            ev = None
        head = (f"⚠️ *האוניברסיטה שינתה את המבחן ב{c['course']}*\n"
                f"היה: {_fmt_when(c['old']['date'], c['old']['time'])}\n"
                f"עכשיו: *{_fmt_when(c['new']['date'], c['new']['time'])}*")
        if not ev:
            return Out('text', head + '\n(המבחן הזה לא ביומן שלך.)')
        return _ask(u, {'q': 'move', 'change': c, 'event': ev}, head + '\n\nלהזיז גם ביומן?',
                    [('✅ להזיז ביומן', 'mv'), ('❌ להשאיר', 'keep')])
    skip = u.setdefault('exam_skip', {})
    asked = u.setdefault('exam_asked', {})
    times = u.setdefault('exam_asked_n', {})                 # 18/10: Discrete 2 was asked 6 times in 4 days

    def is_open(key: str) -> bool:
        v = skip.get(key)
        if isinstance(v, str) or (v or {}).get('until', '') > today:
            return False
        n = times.get(key, 1 if asked.get(key) else 0)
        if n >= EXAM_ASK_MAX:
            return False                                     # asked enough — it stays in the morning brief, quietly
        last = asked.get(key, '')
        return not last or (date.fromisoformat(today) - date.fromisoformat(last[:10])).days >= EXAM_ASK_GAP_DAYS
    todo = [e for e in events if e['is_exam'] and e['date'] > today]
    if not todo:
        return None
    try:
        issues = tau_portal.calendar_issues(todo)
    except Exception:
        return None
    for it in sorted(issues, key=lambda i: i['type'] != 'time'):
        e = it['exam']
        key = ('time|' if it['type'] == 'time' else '') + _exam_key(e)
        if not is_open(key):
            continue
        prev = times.get(key, 1 if asked.get(key) else 0)    # asked before this counter existed = once
        asked[key] = today
        times[key] = prev + 1
        if it['type'] == 'time':
            from integrations import gcal
            ev = it['event']
            allday = ' (כל היום)' if 'date' in (ev.get('start') or {}) else ''
            return _ask(u, {'q': 'time', 'exam': e, 'event': ev},
                        f"⏰ *{ev.get('summary', '')}* — ביומן: {gcal.when_text(ev)}{allday}\n"
                        f"באתר האוניברסיטה: שעת התחלה *{e['time']}*.\nלעדכן את השעה ביומן?",
                        [('✅ לעדכן', 'time'), ('❌ להשאיר', 'notime')])
        return _ask(u, {'q': 'missing', 'exam': e},
                    f"🎓 באתר האוניברסיטה אתה רשום ל*{e['course']}* ({e['kind'] or 'בחינה'}) — "
                    f"{_fmt_when(e['date'], e['time'])}.\nזה לא ביומן שלך. אתה מתכוון לגשת?",
                    [('✅ כן — להוסיף ליומן', 'yes'), ('❌ לא ניגש', 'no'), ('🤷 עוד לא יודע', 'later')])
    return None


def _place(e: dict) -> str:
    return ' '.join(x for x in (e.get('building'), f"חדר {e['room']}" if e.get('room') else '') if x)


def _exam_answer_button(msg: 'In', u: dict, p: str) -> list['Out']:
    _, op, pid = (p.split(':', 2) + ['', ''])[:3]
    item = (u.get('exam_q') or {}).pop(pid, None)
    if not item:
        return [Out('text', 'השאלה כבר לא בתוקף.')]
    if 'q' not in item:                               # asked before 07/10: a bare exam dict
        item = {'q': 'missing', 'exam': item}
    skip = u.setdefault('exam_skip', {})
    if op in ('yes', 'time', 'mv', 'loc', 'on') and u.get('paused'):
        return [Out('text', '⏸️ הסוכן עצור — לא משנה את היומן.')]
    if item['q'] == 'eve':
        return _exam_eve_button(msg, u, item, op)
    if item['q'] == 'review':
        return _review_button(msg, u, item, op)
    if item['q'] == 'tt':
        return _timetable_button(u, op)
    from integrations import gcal
    if item['q'] == 'room':
        c, ev = item['change'], item['event']
        if op != 'loc':
            outs = [Out('text', '👍 בסדר.')]
        else:
            gcal.set_location(ev, c['place'])
            audit(msg.sender, 'calendar_location', {'id': ev.get('id'), 'title': ev.get('summary'), 'place': c['place']})
            outs = [Out('text', f"✅ רשמתי ביומן: {ev.get('summary', '')} — 📍 {c['place']}")]
        nxt_q = _exam_prompt(u)
        return outs + ([nxt_q] if nxt_q else [])
    if item['q'] == 'move':
        c, ev = item['change'], item['event']
        if op != 'mv':
            outs = [Out('text', '👍 השארתי את היומן כמו שהוא.')]
        else:
            gcal.set_event_time(ev, date.fromisoformat(c['new']['date']), c['new']['time'])
            skip[_exam_key({'course': c['course'], 'date': c['new']['date']})] = 'yes'
            audit(msg.sender, 'calendar_move', {'id': ev.get('id'), 'title': ev.get('summary'), 'to': c['new'],
                                                'source': 'tau_portal'})
            outs = [Out('text', f"✅ הזזתי ביומן: {ev.get('summary', '')} → "
                                f"{_fmt_when(c['new']['date'], c['new']['time'])}")]
    elif item['q'] == 'time':
        e, ev = item['exam'], item['event']
        key = 'time|' + _exam_key(e)
        if op != 'time':
            skip[key] = 'no'
            outs = [Out('text', '👍 השארתי — לא אשאל שוב על השעה הזו.')]
        else:
            gcal.set_event_time(ev, date.fromisoformat(e['date']), e['time'], location=_place(e))
            skip[key] = 'done'
            audit(msg.sender, 'calendar_time', {'id': ev.get('id'), 'title': ev.get('summary'), 'time': e['time'],
                                                'source': 'tau_portal'})
            outs = [Out('text', f"✅ עודכן ביומן: {ev.get('summary', '')} — {_fmt_when(e['date'], e['time'])}")]
    else:
        e = item['exam']
        if op == 'no':
            skip[_exam_key(e)] = 'no'
            outs = [Out('text', f"👍 רשמתי שאתה לא ניגש ל{e['course']} — לא אשאל שוב.")]
        elif op == 'later':
            skip[_exam_key(e)] = {'until': (date.today() + timedelta(days=3)).isoformat()}
            outs = [Out('text', '👌 אשאל שוב בעוד 3 ימים.')]
        else:
            if e['time']:
                h, m = map(int, e['time'].split(':'))
                ev = {'title': f"מבחן ב{e['course']}", 'start': f"{e['date']}T{e['time']}",
                      'end': f"{e['date']}T{min(h + 3, 23):02d}:{m:02d}"}
            else:
                nxt = (date.fromisoformat(e['date']) + timedelta(days=1)).isoformat()
                ev = {'title': f"מבחן ב{e['course']}", 'start': e['date'], 'end': nxt, 'all_day': True}
            if _place(e):
                ev['location'] = _place(e)
            ev['notes'] = 'נוסף מאתר האוניברסיטה (my.tau.ac.il) באישור של המשתמש.'
            gcal.add_event(ev)
            skip[_exam_key(e)] = 'yes'
            audit(msg.sender, 'calendar_add', {'title': ev['title'], 'start': ev['start'], 'source': 'tau_portal'})
            outs = [Out('text', f"✅ נוסף ליומן: {ev['title']} — {_fmt_when(e['date'], e['time'])}. בהצלחה! 💪")]
    nxt_q = _exam_prompt(u)
    return outs + ([nxt_q] if nxt_q else [])


def _course_key_of(course: str) -> str | None:
    """University course title → our course key ('מתמטיקה בדידה 2' → 'discrete_math_2')."""
    from config.paths import COURSE_NAMES
    words = set(re.findall(r'[א-ת"׳]{3,}', course))
    best = max(COURSE_NAMES.items(), key=lambda kv: len(words & set(re.findall(r'[א-ת"׳]{3,}', kv[1]))), default=None)
    if best and words & set(re.findall(r'[א-ת"׳]{3,}', best[1])):
        return best[0]
    return None


def _taking(u: dict, e: dict) -> bool:
    return (u.get('exam_skip') or {}).get(_exam_key(e)) != 'no'


def exam_day_messages(state_path: Path | None = None, now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """Evening before an exam (from 20:00): time + room + an offer of a wake-up reminder and a quick review.
    Exam morning (07:00–10:00): good luck + the room, flagged if it changed or was published overnight.
    Uses the cached university schedule (scripts/tau_watch.py refreshes it at 19:30 and 06:45) — no login here."""
    from integrations import tau_portal
    from whatsapp import outbox
    now = now or datetime.now()
    chat = outbox.owner_chat()
    if not chat:
        return []
    events, at, _ = tau_portal.schedule(live=False)
    if not events:
        return []
    st = _load(state_path)
    u = st.setdefault(chat, {})
    sent = u.setdefault('exam_day_sent', {})
    out = []
    today, tomorrow = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
    for e in events:
        if not e['is_exam'] or not _taking(u, e):
            continue
        key = _exam_key(e)
        place = _place(e) or (u.get('exam_place') or {}).get(key, '')
        if e['date'] == tomorrow and now.hour >= 20 and not sent.get('eve|' + key):
            sent['eve|' + key] = place or '-'
            text = (f"🎯 *מחר מבחן ב{e['course']}*\n"
                    f"🕘 שעת התחלה: *{e['time'] or 'לא מופיעה באתר'}*\n"
                    f"📍 {place or 'החדר עוד לא פורסם באתר — אבדוק שוב בבוקר ואעדכן'}\n"
                    f"🪪 תעודה מזהה / כרטיס סטודנט\n\nבהצלחה! 💪")
            buttons = [('⏰ תזכורת לצאת בבוקר', 'wake')]
            from agent import study_push
            if _course_key_of(e['course']) and study_push.on():     # 05/11: "חזרה" is on request only
                buttons.append(('📚 חזרה מהירה עכשיו', 'review'))
            buttons.append(('🙏 לא צריך', 'none'))
            out.append((chat, _ask(u, {'q': 'eve', 'exam': e}, text, buttons)))
        elif e['date'] == today and 7 <= now.hour < 10 and not sent.get('morning|' + key) and u.get('brief_off'):
            # 12/10: this came at 07:00 and the brief at 07:30 — now it's the top of the brief; alone only if it's off
            sent['morning|' + key] = True
            out.append((chat, Out('text', _exam_today_text(u, e, place))))
    if out:
        _save(st, state_path)
    return out


def _exam_eve_button(msg: 'In', u: dict, item: dict, op: str) -> list['Out']:
    e = item['exam']
    if op == 'wake':
        if not e['time']:
            return [Out('text', 'אין באתר שעת התחלה, אז לא קבעתי. תכתוב לי "תזכיר לי מחר ב-…".')]
        from agent import life
        h, m = map(int, e['time'].split(':'))
        start = datetime.fromisoformat(f"{e['date']}T{h:02d}:{m:02d}")
        when = start - timedelta(minutes=90)
        life.add_reminder(msg.sender, {'text': f"לצאת למבחן ב{e['course']} ({e['time']})", 'when': when.isoformat()})
        return [Out('text', f"⏰ אזכיר לך ב-{when:%H:%M} (שעה וחצי לפני). אם צריך שעה אחרת — \"תזכיר לי מחר ב-…\".")]
    if op == 'review':
        ck = _course_key_of(e['course'])
        if not ck:
            return [Out('text', 'אין לי חומר לקורס הזה.')]
        return _start_review(u, 'intro_moed_b' if ck == 'intro_to_cs' else f'auto:{ck}')
    return [Out('text', 'לילה טוב ובהצלחה מחר! 🍀')]


TT_ON = re.compile(r'מערכת (?:ה)?שעות.{0,20}(?:ליומן|ביומן|לסנכרן|סנכרון|תוסיף|תכניס)|'
                   r'(?:תוסיף|תכניס|תסנכרן|לסנכרן).{0,20}(?:מערכת (?:ה)?שעות|השיעורים שלי|את השיעורים)')
TT_OFF = re.compile(r'(?:תפסיק|תעצור|בטל|תבטל).{0,15}(?:סנכרון|לסנכרן|מערכת (?:ה)?שעות)')
TT_OFFER_FROM = date(2026, 10, 13)            # after the 08/10 + 12/10 exams, before the semester (18/10)
EXAM_ASK_MAX = 2                              # an unanswered "are you taking this exam?" — at most twice…
EXAM_ASK_GAP_DAYS = 5                         # …and days apart (18/10: Discrete 2 was asked daily, 6 times)


def _timetable_offer(u: dict) -> 'Out':
    from agent import timetable
    from integrations import tau_portal
    events, _, _ = tau_portal.schedule(live=False)
    if not events or not timetable.lessons(events):
        return Out('text', 'עוד אין לי מערכת שעות מאתר האוניברסיטה (בודק אותו 3 פעמים ביום). אנסה שוב בקרוב.')
    u['timetable_offered'] = 'asked'
    return _ask(u, {'q': 'tt'}, timetable.preview_text(events) +
                '\n\nלהכניס את כל השיעורים והתרגולים ליומן? אעדכן לבד חדרים, שינויי שעה וביטולים לפי אתר האוניברסיטה.',
                [('✅ כן, לסנכרן', 'on'), ('❌ לא עכשיו', 'off')])


def _timetable_run(u: dict) -> tuple[dict, str]:
    from agent import timetable
    from integrations import tau_portal
    events, _, _ = tau_portal.schedule(live=False)
    res = timetable.sync(events or [])
    u['tt_day'] = date.today().isoformat()
    return res, timetable.changes_text(res)


def timetable_tick(state_path: Path | None = None, now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """Daily after 10:00 (the 09:45 site refresh is in the cache): sync if the owner opted in, else offer once from 13/10."""
    from whatsapp import outbox
    now = now or datetime.now()
    chat = outbox.owner_chat()
    if not chat or now.hour < 10:
        return []
    st = _load(state_path)
    u = st.setdefault(chat, {})
    if u.get('paused') or u.get('tt_day') == now.date().isoformat():
        return []
    u['tt_day'] = now.date().isoformat()                     # mark first — a failure must not retry every minute
    _save(st, state_path)
    out = []
    if u.get('timetable_sync'):
        try:
            _, text = _timetable_run(u)
        except Exception as e:
            print(f'  ⚠️ timetable sync failed: {e}')
            text = ''
        if text:
            out.append((chat, Out('text', text)))
    elif not u.get('timetable_offered') and now.date() >= TT_OFFER_FROM \
            and not any(c.get('kind') == 'lessons' for c in __import__('integrations.gcal', fromlist=['x']).read_calendars()):
        out.append((chat, _timetable_offer(u)))
    _save(st, state_path)
    return out


def _timetable_button(u: dict, op: str) -> list['Out']:
    if op != 'on':
        u['timetable_offered'] = 'no'
        return [Out('text', '👍 בסדר. אם תרצה בהמשך — "תוסיף את מערכת השעות ליומן".')]
    u['timetable_sync'] = True
    try:
        res, text = _timetable_run(u)
    except Exception as e:
        print(f'  ⚠️ timetable sync failed: {e}')
        return [Out('text', '⚠️ משהו נכשל בהוספה ליומן — אנסה שוב מחר בבוקר לבד.')]
    n = len(res['added'])
    out = [Out('text', f"✅ הכנסתי ליומן {n} שיעורים ותרגולים (החודש הקרוב — האוניברסיטה מפרסמת חודש קדימה).\n"
                       "מעכשיו כל בוקר אוסיף את הבאים בתור ואעדכן חדרים / שינויי שעה / ביטולים.\n"
                       "לעצור: \"תפסיק לסנכרן מערכת שעות\".")]
    return out + ([Out('text', text)] if text else [])


_WORD_SKIP = frozenset('המשתמש אני אתה של עם על את לא הוא היא זה זו יש אין כי גם רק אם או מה מי'.split())
FEATURE_ASK = re.compile(r'תוסיף|תוסיפו|להוסיף|תעשה ש|תעשו ש|תשפר|לשפר|תתקן|לתקן|תשנה|לשנות|תבנה|אשמח ש|הייתי רוצה|רוצה ש|'
                         r'כדאי ש|אפשר ש|שהבוט|שתדע|שתוכל|פיצ.ר|באג|תקלה|לא עובד|לא עבד|נתקע|טעות|באיחור|פעמיים')


def _stems(text: str) -> set[str]:
    words = (w for w in re.findall(r'[א-תa-z0-9]{2,}', (text or '').lower()) if w not in _WORD_SKIP)
    return {re.sub(r'^[הבלמושכ]{1,2}(?=[א-ת]{3})', '', w) for w in words}


def _owner_asked(msg: 'In', text: str, value: str, need: float = 0.5) -> bool:
    """Audit A4: the model reads mail, documents and the web in the same call that fills `learned`, `fact`, `correction`
    and `thread`. What it writes there lasts, so it counts only when it rests on the owner's own message: his own words, not a
    group's or a forward's, carrying at least this share of what's being written. No LLM: this is one tap of a helper."""
    if msg.forwarded or msg.group or msg.kind not in ('text', 'voice'):
        return False
    want = _stems(value)
    return bool(want) and len(want & _stems(text)) >= need * len(want)


def _assistant(msg: 'In', u: dict, text: str, thought: dict | None = None) -> list['Out'] | None:
    """Not a recognised command → read it in context (agent/assistant.py). None = a study question (→ summaries).
    thought: the same decision, already made and checked by the agent loop (agent/mind.py), which looked for itself."""
    from agent import assistant
    d = thought
    pre = _PREFETCH.pop(id(msg), None)                  # started with the classifier (_prefetch_decide)
    if d is None and pre and pre[0] == text and pre[1] == _history(u):
        began = _TURN_START.get(id(msg))                 # the worker is one thread: after the loop failed, the rest is bounded
        try:
            d = pre[2].result(timeout=120 if began is None else max(5.0, TURN_SECONDS - (time.monotonic() - began)))
        except Exception as e:
            print(f'  ⚠️ prefetched decide failed: {e!r}'[:160])
            if began is not None and time.monotonic() - began >= TURN_SECONDS:
                return [Out('text', '⏳ זה לוקח לי יותר מדי זמן עכשיו. נסה שוב עוד רגע.')]
    if d is None:
        d = assistant.decide(text, msg.sender, _history(u))
    kind = d['kind']
    u.pop('_resolved', None)
    # 05/11 "כמה ספרים?": each source got the bare words. The same call now says what they mean in context.
    q = d.get('resolved').strip() if isinstance(d.get('resolved'), str) and 0 < len(d['resolved'].strip()) <= 300 else text
    if isinstance(d.get('correction'), str) and d['correction'].strip() and msg.role == 'owner' \
            and _owner_asked(msg, text, d['correction']):
        from agent import now_note                            # "אמרתי לך כבר": said once is enough
        now_note.add(msg.sender, d['correction'].strip(), 'correction')
    learned = (d.get('learned') or '').strip() if isinstance(d.get('learned'), str) else ''
    if learned and kind != 'fact' and msg.role == 'owner' and _owner_asked(msg, text, learned):   # 15/10: learn about the owner in passing (same call, no cost)
        from agent import life
        life.remember(msg.sender, learned)
    if kind == 'study':
        if q != text:
            u['_resolved'] = q
        return None
    if kind == 'lookup' and (d.get('lookup') or '').strip():
        # 03/11 (The owner: "לא רוצה שינחש, אני רוצה שיחפש וידע — אם אני יכול לפתוח את אתר האוניברסיטה ולגלות, גם הוא"):
        # a fact the bot's data doesn't have → it goes and searches, like an assistant would
        # first the course's own sources (Moodle, syllabus, the lecturer's site), remembering where it found it; then the web
        from config.paths import ACTIVE_COURSES
        found = _mail_lookup(q, msg.sender) if msg.role == 'owner' and (d.get('lookup_where') == 'mail' or _about_mail_task(msg, q)) else None
        if found:
            return [Out('text', found)]
        ck = d.get('lookup_course') if d.get('lookup_course') in ACTIVE_COURSES else ''
        if ck:
            from agent import course_lookup
            found = course_lookup.find(q, ck, (d.get('lookup_topic') or '').strip())
            if found:
                return [Out('text', found)]
        from integrations import websearch
        return [Out('text', websearch.ask_web(d['lookup'].strip() + f'\n(השאלה המקורית של המשתמש: {q})'))]
    found = None if thought else _dig(msg, d, q)
    if found:
        return [Out('text', found)]
    reply = d.get('reply', '').strip()
    follow = d.get('follow_up') if isinstance(d.get('follow_up'), dict) else {}
    if msg.role == 'owner' and (d.get('thread') or '').strip() and _owner_asked(msg, text, d['thread']):   # an open thread this week → the "now" note
        from agent import now_note
        now_note.add(msg.sender, d['thread'].strip(), 'thread')
    done = _answered_in_words(msg, u, d) if msg.role == 'owner' else []
    if done:
        # 03/11 ("אני לא ניגש למבחן" → "לא אציק לך", yet the same offer came back 3h later — "אמרתי לך כבר"):
        # the answer was carried out (a decline / an exam he isn't taking); the confirmations say what was done, so
        # a reply claiming anything else is dropped
        keep = reply and kind != 'unclear' and not CLAIM_RX.search(reply)
        return ([Out('text', reply)] if keep else []) + done
    if kind == 'feature' and (d.get('feature') or '').strip() and not msg.forwarded \
            and (FEATURE_ASK.search(text) or YES_RX.match(text) or _owner_asked(msg, text, d['feature'], 0.34)):
        # 15/11 the job got one sentence and nothing it was about ("אין לי מספיק מידע"): his words + the fenced context
        # (agent/feature_request), the whole of it shown here; a second request within minutes replaces the first
        from agent import feature_request as fr
        earlier = fr.pending_same(u)
        title = d['feature'].strip()
        task = fr.build(msg.sender, title if YES_RX.match(text) else text, list(u.get('history') or []),
                        d.get('feature_related'), earlier)
        pid = secrets.token_hex(3)
        u.setdefault('feat', {})[pid] = title
        u.setdefault('feat_ctx', {})[pid] = {'task': task, 'text': text.strip()[:600]}
        u.setdefault('feat_ts', {})[pid] = datetime.now().isoformat(timespec='seconds')
        for k in ('feat_ctx', 'feat_ts'):                 # only live confirmations are kept
            u[k] = {p: v for p, v in u[k].items() if p in u['feat']}
        merged = '\n(מחליף את הבקשה הקודמת שלא נשלחה)' if earlier else ''
        return [Out('buttons', f"{reply}\n\n🛠️ *בקשת שיפור* — זה מה שקלוד יקבל:{merged}\n\n{fr.shown(task)}\n\n"
                               f"להעביר לקלוד שיפתח את זה? (שום דבר לא עולה לשרת בלי אישור שלך אחרי שהוא מסיים)",
                    buttons=[('✅ כן, שיפתח', f'ft:go:{pid}'), ('❌ לא עכשיו', f'ft:no:{pid}')])]
    if kind == 'fact' and (d.get('fact') or '').strip() and not _owner_asked(msg, text, d['fact']):
        kind = 'chat'                                    # nothing was written, so its "רשמתי" meets the claim guard below
    if kind == 'fact':
        from agent import life
        if (d.get('fact') or '').strip():
            life.remember(msg.sender, d['fact'].strip())
        place = d.get('exam_place') or {}
        if isinstance(place, dict) and (place.get('place') or '').strip():
            q = _exam_place_offer(u, place.get('course') or '', place['place'].strip())
            if q is not None:
                return [Out('text', reply or '👍 רשמתי.'), q]
    if kind != 'fact' and not thought and CLAIM_RX.search(reply):   # the loop's answers passed mind.review
        # 25/10: "מעביר לבת הזוג עכשיו! 🎉" ×3 and nothing was sent; 22/10 "עדכנתי שהתאמנת" and nothing was recorded.
        # This path only talks — a reply that says it did something is replaced by what to write to get it done.
        # 03/11: "ההודעה הקודמת נשלחה במקביל…" (a real answer to "מה?") was swapped twice — the passive is no longer
        # a claim (CLAIM_RX: first person only). Always on: the model that invents the action can't switch it off.
        print(f'  ⚠️ assistant claimed an action it cannot do: {reply[:120]!r}')
        if re.search(rf'{PARTNER_NAME}|\bלה\b', text) and (pm := _partner_message(u, f'הודעה ל{PARTNER_NAME} {text}')):
            return pm
        how = (d.get('how_to') or '').strip()                # 03/11: the exact words for THIS request, not a menu
        return [Out('text', f'{NOT_DONE}. כדי שזה יקרה כתוב: "{how}"' if how else
                    f'{NOT_DONE}. תכתוב לי את זה כבקשה (למשל "תוסיף ליומן …" / "תזכיר לי …") ואבצע.')]
    if follow.get('ask') and follow.get('command') and msg.role == 'owner' and not msg.group \
            and _follow_ok(str(follow['command'])) and not _study_follow(follow):
        # 03/11 (The owner: "שאני ארגיש שאני מדבר עם בן אדם"): one natural next step; a "כן" to it runs that command
        # through the normal router — read / study requests only (_follow_ok)
        u['follow_up'] = {'command': follow['command'].strip()[:300], 'ask': follow['ask'].strip()[:200],
                          'ts': datetime.now().isoformat(timespec='seconds')}
        reply = f"{reply}\n{follow['ask'].strip()}" if reply else follow['ask'].strip()
    return [Out('text', reply)]


# first person only: the passive "נשלח/נשלחה" describes what already happened ("ההודעה נשלחה באיחור"), not a claim
CLAIM_RX = re.compile(r'(?:^|\s)(?:מעביר|מעבירה|העברתי|שלחתי|אשלח|אני\s+שולח|שולח\s+(?:לה|לו|לך|ל[א-ת])|עדכנתי|הוספתי|קבעתי|ביצעתי|'
                      r'תזמנתי|הזמנתי|ביטלתי|מחקתי|סימנתי|רשמתי|אדאג|אתקן)(?=\s|$|[.!,])')
NOT_DONE = 'רגע — את זה עוד לא עשיתי בפועל'          # the claim guard's opening (03/11: one voice, not "⚠️ …")
# a numbered option the owner may answer in words: only the declines (every ❌ / 🙏 / 🤷 label in the bot is one) are
# carried out from words — a ✅ (send, add to the calendar…) still needs his digit
DECLINE_LABEL = re.compile(r'^\s*(?:❌|🙏|🤷)')
# …and only the silent ones: 'cal:no' of the partner's proposal tells her "המשתמש לא אישר", a coordination decline answers a
# contact — words read by the model must never message anyone
SILENT_DECLINE = ('ex:', 'ft:no:', 'em:no:', 'msg:no:', 'ce:no:', 'pp:no:', 'lf:no')
OWN_DONE = ('rmd:done:', 'task:done:')                  # closing his own reminder / task: inward, like a decline


def _silent_decline(u: dict, label: str, payload: str) -> bool:
    if not DECLINE_LABEL.match(label or ''):
        return False
    if payload.startswith('cal:no:'):                    # a calendar offer from mail / homework — not the partner's
        return not ((u.get('pending') or {}).get(payload.split(':', 2)[2]) or {}).get('proposed_by')
    return payload.startswith(SILENT_DECLINE)


def _answered_in_words(msg: 'In', u: dict, d: dict) -> list['Out']:
    """The assistant read the owner's words as an answer to an open numbered question / an exam he isn't taking →
    carry out what's safe: close the question (a decline runs its own button), record the exam decision."""
    out: list[Out] = []
    closed_exam_q = False
    ans = d.get('answers_open') if isinstance(d.get('answers_open'), dict) else {}
    try:
        qn, on = int(ans.get('q') or 0), int(ans.get('option') or 0)
    except (TypeError, ValueError):
        qn = on = 0
    shown = d.get('_open_q') or []                       # the questions as the model saw them (numbering = this list)
    if 1 <= qn <= len(shown):
        from whatsapp import outbox
        o = shown[qn - 1]
        now = outbox.open_questions(msg.sender)          # still open? (expired / answered meanwhile → leave it)
        n = next((i for i, x in enumerate(now, 1) if x.get('ts') == o.get('ts') and x.get('payloads') == o.get('payloads')), 0)
        pay = o['payloads'][on - 1] if n and 1 <= on <= len(o.get('payloads') or []) else ''
        if pay.startswith(OWN_DONE) and not msg.group:
            # 15/11 "ביטלתי את המלון" to "⏰ תזכורת: בטל את המלון… 1️⃣ ✅ בוצע": his own item, closed on his own report —
            # nothing goes out, so words may do what the ✅ button does
            from agent import open_items
            kind, iid = ('reminder' if pay.startswith('rmd:') else 'task'), pay.split(':', 2)[2]
            text_of = re.sub(r'^\s*⏰\s*\*?תזכורת:?\*?\s*|^\s*📌\s*', '', o.get('q') or o.get('text') or '').strip('* ')
            out.append(Out('text', open_items.close(msg.sender, {'kind': kind, 'id': iid, 'text': text_of, 'raised': True})))
        elif pay and _silent_decline(u, o['labels'][on - 1], pay):
            outbox.close_open(msg.sender, n)
            before = dict(u.get('exam_skip') or {})
            out += _button(In(msg.sender, 'button', payload=pay, channel=msg.channel, role=msg.role), u)
            closed_exam_q = (u.get('exam_skip') or {}) != before    # that button already recorded the exam decision
    ex = d.get('exam_decision') if isinstance(d.get('exam_decision'), dict) else {}
    # 03/11 review: closing an exam question already recorded THAT exam; marking again would hit the next sitting
    if ex and not closed_exam_q and isinstance(ex.get('taking'), bool) and \
            (t := _exam_decision(u, ex.get('course') or '', ex['taking'], str(ex.get('when') or ''))):
        out = [o for o in out if not o.text.startswith('👍')] + [Out('text', t)]   # one confirmation, the specific one
        from agent import now_note                     # the assistant keeps it in mind this week
        now_note.add(msg.sender, t.lstrip('👍 ').split(' — ')[0], 'decision')
    return out


def _exam_decision(u: dict, course: str, taking: bool, when: str = '') -> str:
    """"אני לא ניגש למבחן" → that exam is marked as not taken: no more questions, offers or exam-eve messages.
    "אני כן ניגש" undoes it. 03/11 review: the exam is picked among ALL that course's sittings (not only the ones still
    marked as taken — that skipped to מועד ב), by the sitting / date the words name ("מועד ב", "13/11"), else the nearest
    one whose state would change."""
    from integrations import tau_portal
    events, _, _ = tau_portal.schedule(live=False)
    today = date.today().isoformat()
    hint = (tau_portal.course_hint(course) or course).strip()
    exams = sorted((e for e in events or [] if e.get('is_exam') and e['date'] >= today and hint
                    and (hint in e['course'] or e['course'] in hint)), key=lambda e: (e['date'], e.get('time', '')))
    skip = u.setdefault('exam_skip', {})
    if m := re.search(r'מועד\s*([אב])', when):
        named = [e for e in exams if f"מועד {m.group(1)}" in e.get('kind', '') + e['course']] or \
                exams[{'א': 0, 'ב': 1}[m.group(1)]:][:1]
    elif m := re.search(r'(\d{1,2})[./](\d{1,2})', when):
        named = [e for e in exams if e['date'][5:] == f'{int(m.group(2)):02d}-{int(m.group(1)):02d}']
    else:
        named = [e for e in exams if (skip.get(_exam_key(e)) == 'no') != (not taking)]   # the nearest that changes
    if not named:
        return ''
    e = named[0]
    if taking:
        skip.pop(_exam_key(e), None)
        (u.get('review_resume') or {}).pop(_exam_key(e), None)
        return f"👍 רשמתי שאתה כן ניגש ל{e['course']} ({_fmt_when(e['date'], e.get('time', ''))}) — אזכיר כרגיל."
    skip[_exam_key(e)] = 'no'
    u.setdefault('review_resume', {})[_exam_key(e)] = 'no'
    return f"👍 רשמתי שאתה לא ניגש ל{e['course']} ({_fmt_when(e['date'], e.get('time', ''))}) — לא אשאל ולא אזכיר עליו."


def _exam_place_offer(u: dict, course: str, place: str) -> 'Out | None':
    """The owner told us where an exam is → remember it for the exam-day messages and offer it for the calendar event."""
    from integrations import tau_portal
    events, _, _ = tau_portal.schedule(live=False)
    today = date.today().isoformat()
    exams = [e for e in (events or []) if e['is_exam'] and e['date'] >= today and _taking(u, e)]
    hint = tau_portal.course_hint(course) or course.strip()
    match = [e for e in exams if hint and hint in e['course']] or exams[:1]
    if not match:
        return None
    e = match[0]
    u.setdefault('exam_place', {})[_exam_key(e)] = place
    try:
        ev = tau_portal.gcal_match(e)
    except Exception:
        ev = None
    if not ev or place in (ev.get('location') or ''):
        return None
    c = {'type': 'room', 'course': e['course'], 'date': e['date'], 'time': e['time'], 'place': place}
    return _ask(u, {'q': 'room', 'change': c, 'event': ev},
                f"📍 {e['course']} — {_fmt_when(e['date'], e['time'])}\nלרשום *{place}* כמקום באירוע ביומן?",
                [('✅ לרשום', 'loc'), ('❌ לא צריך', 'noloc')])


def _feature_button(msg: 'In', u: dict, p: str) -> list['Out']:
    _, op, pid = (p.split(':', 2) + ['', ''])[:3]
    feat = (u.get('feat') or {}).pop(pid, None)
    ctx = (u.get('feat_ctx') or {}).pop(pid, None) or {}
    (u.get('feat_ts') or {}).pop(pid, None)
    if not feat:
        return [Out('text', 'הבקשה כבר לא בתוקף.')]
    if op != 'go':
        return [Out('text', '👍 לא העברתי. אפשר תמיד לכתוב "קלוד: …".')]
    from agent import claude_job, feature_request
    task = ctx.get('task') or f'{feature_request.PREFIX} (מהוואטסאפ, {datetime.now():%d/%m %H:%M}): {feat}'
    # 15/11 two jobs (1115130461e9, 1115131072ea) for one request six minutes apart: the newer one takes the older's place
    gone = claude_job.replace_recent(msg.sender, feature_request.PREFIX, feature_request.SAME_MINUTES * 60)
    if gone:
        task += '\n\n(מחליף משימה קודמת באותו נושא שעוד לא הסתיימה: ' + ' · '.join(g[:120] for g in gone) + ')'
    jid, reply = claude_job.start(msg.sender, task)
    audit(msg.sender, 'claude_job_start', {'id': jid, 'task': feat[:300], 'via': 'assistant', 'replaced': len(gone)})
    from agent import now_note                                 # the assistant remembers it asked for this
    now_note.add(msg.sender, f'ביקש מקלוד: {feat[:150]}', 'request')
    return [Out('text', reply)]


MAIL_DRAFT = re.compile(r'תנסח|תכתוב|לנסח|לכתוב|תשלח (?:לו |לה )?מייל|שלח מייל|טיוטה')
MAIL_DIGEST_HOURS = (13, 20)


LESSON_TITLE_RX = re.compile(r'^(?:📚\s*)?(?:ה)?(?:שיעור|הרצאה|תרגול|תרגיל(?!\s*בית)|lecture|recitation)\b|'
                             r'(?:^|\s)(?:שיעור|הרצאה|תרגול)\s+(?:ב|של\s+)?(?:מיקרו|מאקרו|הסתברות|תוכנה)', re.I)
_DAYS_HE = {'ראשון': 6, 'שני': 0, 'שלישי': 1, 'רביעי': 2, 'חמישי': 3, 'שישי': 4}


def _mail_dedupe(u: dict, rows: list[dict], now: datetime | None = None) -> list[dict]:
    """26/10: every Moodle announcement came twice — Moodle's notification mail ("📧 noreply-moodle — …") and the
    announcement watcher ("📧 מודל — …"), hours apart. The same subject (≥60% of its words) within 36h → once."""
    now = now or datetime.now()
    seen = [s for s in (u.get('mail_subj_seen') or []) if now - datetime.fromisoformat(s['ts']) < timedelta(hours=36)]

    def words(r):
        subj = re.sub(r'^\s*\d{6,}\s*-\s*[^:]+:\s*', '', r.get('subject', ''))   # "1011210199 - מאקרו כלכלה: …"
        return set(re.findall(r'[\w֐-׿]{3,}', subj.lower()))
    out = []
    for r in rows:
        w = words(r)
        if w and any(len(w & set(s['w'])) / max(1, len(w | set(s['w']))) >= 0.6 for s in seen):
            continue
        out.append(r)
        if w:
            seen.append({'w': sorted(w), 'ts': now.isoformat(timespec='seconds')})
    u['mail_subj_seen'] = seen[-200:]
    return out


def _not_my_lesson(r: dict, now: datetime | None = None, events_on=None) -> bool:
    """26/10: "השיעור היום בכיתה 012" (micro, Monday — the owner's micro lecture is on Tuesday) came as 🚨 urgent. A lesson
    change on a day the owner has no lesson of that course is another group's → not urgent, no calendar question.
    False when the day or the course can't be told (then it's handled as before)."""
    from config.paths import ACTIVE_COURSES, COURSE_NAMES
    text = f"{r.get('subject', '')} {r.get('summary', '')} {r.get('lesson_change', '')}"
    now = now or datetime.now()
    ck = next((k for k in ACTIVE_COURSES
               if any(w in text for w in re.findall(r'[א-ת]{4,}', COURSE_NAMES.get(k, ''))[:1])), None)
    day = None
    if re.search(r'\bהיום\b|this\s+week|today', text, re.I):
        day = now.date()
    elif re.search(r'\bמחר\b|tomorrow', text, re.I):
        day = now.date() + timedelta(days=1)
    else:
        for name, wd in _DAYS_HE.items():
            if re.search(rf'(?:ביום|יום)\s+{name}\b', text):
                day = now.date() + timedelta(days=(wd - now.weekday()) % 7)
                break
    if not ck or not day:
        return False
    if events_on is None:
        from integrations import gcal
        if not gcal.configured():
            return False
        events_on = gcal.events_on
    words = re.findall(r'[א-ת]{4,}', COURSE_NAMES.get(ck, ''))[:1]
    try:
        mine = [e for e in events_on(day) or [] if e.get('_cal_kind') == 'lessons'
                and all(w in (e.get('summary') or '') for w in words)]
    except Exception:
        return False
    return not mine


def _mail_calendar_offer(u: dict, dated: list[str]) -> 'Out | None':
    """All the dates from these mails → ONE calendar question (only the latest numbered options are answerable)."""
    from integrations import gcal
    if not dated or u.get('paused') or not gcal.configured():
        return None
    try:
        events, _q = gcal.parse_events('\n'.join(dated))
    except Exception:
        return None
    # 19/10: the special-exam date came in 3 mails (faculty, approval, reminder) → asked 4 times, and it was already in
    # the calendar since the first ✅. One question per date+time, ever; and nothing that's already in the calendar.
    seen = u.setdefault('mail_cal_seen', [])
    # 26/10: "הרצאה במאקרו כלכלה 26/10 12:00" (another group's lecture, from a room-change mail) was offered as a new
    # event — lessons live in the studies calendar and their changes go through the lesson-change path, never here
    events = [ev for ev in events if not LESSON_TITLE_RX.search(ev.get('title') or '')]
    events = [ev for ev in events if not _seen_mail_event(ev, seen) and not _in_calendar(ev)]
    if not events:
        return None
    seen.extend(_mail_event_key(ev) for ev in events)
    del seen[:-300]
    pid = secrets.token_hex(4)
    u.setdefault('pending', {})[pid] = {'events': events, 'ts': datetime.now().isoformat(timespec='seconds')}
    body = '\n\n'.join(gcal.describe(ev) for ev in events)
    ask = 'להוסיף ליומן?' if len(events) == 1 else f'להוסיף את כל {len(events)} ליומן?'
    return Out('buttons', f'📅 מהמיילים:\n\n{body}\n\n{ask}', buttons=[('✅ הוסף', f'cal:ok:{pid}'), ('❌ לא צריך', f'cal:no:{pid}')])


_GENERIC = {'מבחן', 'בחינה', 'מועד', 'מיוחד', 'נוסף', 'שיעור', 'הרצאה', 'תרגול', 'הגשה', 'מטלה', 'פגישה', 'בקורס'}


def _sig_words(title: str) -> set[str]:
    words = {w.strip('"׳״\'') for w in re.findall(r'[֐-׿\w"׳״\']{2,}', title or '')}
    return {w[1:] if w[:1] in 'בלהומ' and len(w) > 3 else w for w in words} - _GENERIC


def _mail_event_key(ev: dict) -> str:
    """Same date and time = the same thing, however each mail words it."""
    st = str(ev.get('start', ''))
    return st[:16] if 'T' in st else f"{st[:10]}|{'|'.join(sorted(_sig_words(ev.get('title', ''))))[:60]}"


def _seen_mail_event(ev: dict, seen: list[str]) -> bool:
    """28/10: "סיום תקופת הרישום המאוחר לקורסים" (29/10, all day) was asked again 3 days after the owner answered ❌ to
    "סיום תקופת רישום/ביטול קורסים" (29/10) — different words, so a different key. An all-day item on the same date
    sharing most of its words with one already asked is the same thing."""
    key = _mail_event_key(ev)
    if key in seen:
        return True
    if '|' not in key:
        return False
    day, words = key.split('|', 1)[0], _sig_words(ev.get('title', ''))
    for k in seen:
        if not k.startswith(day + '|'):
            continue
        prev = {w for w in k.split('|')[1:] if w}
        common = len(words & prev)
        if common >= 2 and common * 2 >= min(len(words), len(prev)):
            return True
    return False


def _in_calendar(ev: dict) -> bool:
    """The calendar already has it: that day, the same start time (timed) or shared title words (all-day)."""
    from integrations import gcal
    st = str(ev.get('start', ''))
    try:
        day = date.fromisoformat(st[:10])
        items = gcal.events_on(day)
    except Exception:
        return False
    want = _sig_words(ev.get('title', ''))
    for it in items:
        s = (it.get('start') or {}).get('dateTime', '')
        same_time = 'T' in st and s[11:16] == st[11:16]
        if (same_time and (want & _sig_words(it.get('summary', '')) or not want)) or \
                ('T' not in st and want & _sig_words(it.get('summary', ''))):
            return True
    return False


def _edit_offer(u: dict, p: dict, intro: str = '', also: list | None = None) -> 'Out':
    """A planned calendar change (agent/calendar_edit.plan) → before → after + ✅ (ce:ok, kind 'edit').
    also: more events (the rest of a lesson series) that get the same patch with the same ✅."""
    from agent import calendar_edit
    pid = secrets.token_hex(3)
    u.setdefault('edits', {})[pid] = {'kind': 'edit', 'event': p['event'], 'ts': datetime.now().isoformat(timespec='seconds'),
                                      'patch': {k: p[k] for k in ('title', 'location', 'date', 'start', 'end')},
                                      'also': also or []}
    head = f'{intro}\n\n' if intro else ''
    more = f'\n(וגם ב-{len(also)} המפגשים הבאים שלו)' if also else ''
    return Out('buttons', f"{head}📅 *{p['event'].get('summary', '')}*\n{calendar_edit.describe(p)}{more}\n\nלעדכן ביומן?",
               buttons=[('✅ לעדכן', f'ce:ok:{pid}'), ('❌ לא', f'ce:no:{pid}')])


def _lesson_change_offer(u: dict, change: str, source: str, official: bool = False) -> 'Out | None':
    """15/10: "the lecture on Monday is cancelled / moved / in another room" (a lecturer's mail, Moodle, the site) →
    the matching lesson in the owner's calendar, ready to update with one ✅. None if no lesson clearly matches.
    07/11 audit: any address can write this, so only an official source (tau_mail.official) may skip the ✅."""
    from agent import calendar_edit
    try:
        p = calendar_edit.plan(change, calendar_edit.upcoming(days=60))
    except Exception as e:
        print(f'  ⚠️ lesson change plan failed: {e}')
        return None
    if not p or not calendar_edit.describe(p):
        return None
    # 18/10: the same change arrived by Moodle mail AND the Moodle watcher (and was copied into other courses'
    # items) → the owner was asked 6 times. One question per event+change, ever.
    key = change_key(p)
    seen = u.setdefault('lc_seen', [])
    if key in seen:
        return None
    seen.append(key)
    del seen[:-200]
    if not official:
        return _edit_offer(u, p, f'📢 *{source}:* {change}')
    return _auto_edit(u, p, source, change)


def _course_word_match(ev: dict, text: str) -> bool:
    """The lesson's course (the start of its title) is named in the source/change — Moodle mail used to copy one
    course's change into the others (18/10); an automatic edit needs that to agree."""
    course = re.split(r'\s+[–-]\s+', re.sub(r'^❌ בוטל — ', '', ev.get('summary') or ''))[0]
    words = [w for w in re.findall(r'[א-ת]{3,}|\d', course) if w not in ('כלכלה',)][:2]
    return bool(words) and any(w in text for w in words)


def _auto_edit(u: dict, p: dict, source: str, change: str = '', also: list | None = None, sure: bool = False) -> 'Out | None':
    """20/10 "סמוך עליי" (The owner: "תלוי מה, בכל מקרה אשמח לעדכון אבל לא צריך לחפור"): a lesson change from an official
    source (the university site; a Moodle / lecturer mail naming that course) is written straight away and reported
    in one short message with an undo ("בטל" within 12h). Off ("תשאל לפני שינויים ביומן") or unsure → the ✅ offer.
    Only the owner's studies-calendar lessons — never messages to people, never money."""
    ev = p['event']
    if u.get('trust_cal') is False or u.get('paused') or ev.get('_cal_kind') != 'lessons' \
            or not (sure or _course_word_match(ev, f'{source} {change}')):
        return _edit_offer(u, p, f'📢 *{source}:* {change}' if change else source, also=also)
    from agent import calendar_edit
    patch = {k: p[k] for k in ('title', 'location', 'date', 'start', 'end')}
    if _already_applied(ev, patch):
        return None
    try:
        new = calendar_edit.apply({'event': ev, **patch})
        done = [(ev, new)]
        for other in also or []:
            try:
                done.append((other, calendar_edit.apply({'event': other, **patch})))
            except Exception as e:
                print(f'  ⚠️ series edit failed: {e}')
    except Exception as e:
        print(f'  ⚠️ auto edit failed: {e!r}'[:200])
        return _edit_offer(u, p, f'📢 *{source}:* {change}' if change else source, also=also)
    u['undo_cal'] = {'ts': datetime.now().isoformat(timespec='seconds'),
                     'items': [{'old': {k: o.get(k) for k in ('summary', 'location', 'start', 'end')}, 'new': n}
                               for o, n in done]}
    from agent import journal                           # 19/11 the trusted auto-update: undone without a ✅
    journal.record(outbox_owner(), 'cal_edit', ev.get('summary', ''), inverse={'items': u['undo_cal']['items']})
    audit(outbox_owner(), 'calendar_auto_edit', {'id': ev.get('id'), 'source': source[:80], 'patch': patch,
                                                 'also': len(also or [])})
    what = calendar_edit.describe(p).replace('\n', ' · ')
    more = f' (+{len(done) - 1} הבאים)' if len(done) > 1 else ''
    name = re.sub(r'\s*\(קב׳ \d+\)', '', ev.get('summary', ''))
    src = source.split(':')[0].strip('* ')
    return Out('text', f'📅 עדכנתי ביומן ({src}): {name}{more} — {what}\n↩️ "בטל" מחזיר')


def outbox_owner() -> str:
    try:
        from whatsapp import outbox
        return outbox.owner_chat()
    except Exception:
        return ''


UNDO_RX = re.compile(r'^\s*(?:בטל|תבטל|תחזיר|החזר|בטל את זה|תבטל את (?:זה|העדכון|השינוי)|תחזיר את (?:זה|מה שהיה))\s*[.!]*\s*$')
TRUST_OFF_RX = re.compile(r'(?:תשאל|שאל) (?:אותי )?(?:לפני|קודם) (?:שאתה )?(?:משנה|שינויים|עדכונים|מעדכן)')
TRUST_ON_RX = re.compile(r'^\s*(?:סמוך עליי|תעדכן לבד|אל תשאל(?: לפני)? (?:על )?(?:שינויים|עדכונים)(?: ביומן)?)\s*[.!]*\s*$')


def _undo_cal(u: dict) -> list['Out'] | None:
    rec = u.get('undo_cal')
    if not rec or datetime.now() - datetime.fromisoformat(rec['ts']) > timedelta(hours=12):
        return None
    from agent import calendar_edit
    n = 0
    for it in rec['items']:
        old, new = it['old'], it['new']
        st = (old.get('start') or {})
        p = {'event': new, 'title': old.get('summary') or '', 'location': old.get('location') or '',
             'date': (st.get('dateTime') or st.get('date') or '')[:10], 'start': (st.get('dateTime') or '')[11:16],
             'end': ((old.get('end') or {}).get('dateTime') or '')[11:16]}
        try:
            calendar_edit.apply(p)
            n += 1
        except Exception as e:
            print(f'  ⚠️ undo failed: {e!r}'[:200])
    u.pop('undo_cal', None)
    return [Out('text', f'↩️ החזרתי ביומן ({n}).' if n else '⚠️ לא הצלחתי להחזיר — תבדוק ביומן.')]


def change_key(p: dict) -> str:
    """event id + what changes — the same calendar change, whichever mail / post / site check proposed it."""
    return '|'.join(str(x) for x in (p['event'].get('id'), p.get('title'), p.get('location'), p.get('date'),
                                     p.get('start'), p.get('end')))


def _already_applied(ev: dict, patch: dict) -> bool:
    """The calendar already shows this change (another copy of the question was answered first)."""
    try:
        from integrations import gcal
        cur = gcal._service().events().get(calendarId=gcal.cal_of(ev), eventId=ev['id']).execute()
    except Exception:
        return False
    st = (cur.get('start') or {}).get('dateTime') or (cur.get('start') or {}).get('date') or ''
    checks = [(patch.get('title'), cur.get('summary')), (patch.get('location'), cur.get('location')),
              (patch.get('date'), st[:10]), (patch.get('start'), st[11:16])]
    wanted = [(w, c) for w, c in checks if w]
    return bool(wanted) and all((c or '') == w for w, c in wanted)


def office_hours_tick(now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """02/11 (agent/office_hours.py): the lecturer's Zoom office hours → the calendar, one short line when added."""
    from agent import office_hours
    from whatsapp import outbox
    chat = outbox.owner_chat()
    if not chat:
        return []
    line = office_hours.tick(now)
    return [(chat, Out('text', line))] if line else []


def tasks_tick(now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """02/11 (agent/tasks.py): "📌 להחזיר ספרים…" — nudges with ✅ / 🌙 / 🗑️ until the owner closes it."""
    from agent import tasks
    try:
        return [(chat, Out('buttons', text, buttons=b)) for chat, text, b in tasks.tick(now)]
    except Exception as e:
        print(f'  ⚠️ tasks tick failed: {e!r}'[:200])
        return []


def loops_tick(now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """20/10 (agent/loops.py): what's already submitted gets closed in the calendar — one short line, no question."""
    from agent import loops
    from whatsapp import outbox
    chat = outbox.owner_chat()
    if not chat:
        return []
    try:
        lines = loops.close_tick(now)
    except Exception as e:
        print(f'  ⚠️ loops tick failed: {e!r}'[:200])
        return []
    return [(chat, Out('text', '\n'.join(lines)))] if lines else []


def learn_tick(state_path: Path | None = None, now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """19/10 (agent/learn.py): the evening "🧠 what I learned about you today — save?" and, once, "when do you work?"."""
    from agent import learn
    from whatsapp import outbox
    now = now or datetime.now()
    chat = outbox.owner_chat()
    if not chat or not 19 <= now.hour < 23:
        return []
    st = _load(state_path)
    u = st.setdefault(chat, {})
    if u.get('paused'):
        return []
    out = []
    try:
        if (q := learn.gap_tick(u, chat, now)) is not None:
            out.append((chat, q))
        elif (q := learn.learn_tick(u, chat, now)) is not None:
            out.append((chat, q))
    except Exception as e:
        print(f'  ⚠️ learn tick failed: {e!r}'[:200])
    _save(st, state_path)
    return out


MUTE_RX = re.compile(r'^\s*(?:די|מספיק|תפסיק|אל ת(?:שאל|חזור))\s*(?:,\s*)?(?:לשאול|לשאל|לשלוח)?\s*(?:אותי\s*)?'
                     r'(?:על\s*)?(?:זה|את זה|ה?שאלה הזו|שאלות כאלה|כאלה|על זה)\s*[.!]*\s*$')
UNMUTE_RX = re.compile(r'(?:תחזור|תחזיר|אפשר שוב)\s*(?:ל)?(?:שאול|לשאול|את השאלות)')


def _closable_note(chat: str) -> str:
    """The bot's latest task / mail / reminder note, if it's from the last 3 hours: what a bare "כבר עשיתי" is about."""
    try:
        from whatsapp import outbox
        for r in reversed(outbox.recent_sent(chat, hours=3, n=4)):
            t = r.get('text', '')
            while t.startswith(outbox.REASK):
                t = t[len(outbox.REASK):]
            if t.startswith(('📬 ', '📌 ', '⏰ *תזכורת')):
                return t.split('כבר טיפלת?')[0].split('\n\n1️⃣')[0].strip()
    except Exception:
        pass
    return ''


# the bot asked if something of his is done: "תרצה לסמן אותו כבוצע?" / "כבר קנית?" / "בוצע?"
CLOSE_Q_RX = re.compile(r'(?:לסמן|שאסמן|אסמן|נסמן)\s+(?:את\s+)?(?:זה|אותו|אותה|אותם)?\s*(?:כ|ש)(?:בוצע|סגור|הושלם|נעשה|טופל)|'
                        r'(?<![א-ת])(?:כבר\s+)?(?:טיפלת|סיימת|קנית|החזרת|שילמת|עשית|הגשת)(?![א-ת])[^?\n.]{0,60}\?|'
                        r'(?<![א-ת])(?:בוצע|נסגר|טופל|הושלם)\s*\?')
YES_DONE_RX = re.compile(r'\s*(?:(?:כן|נכון|אכן|בטח)[\s,]*)?(?:בוצע|סיימתי|עשיתי|קניתי|החזרתי|שילמתי|טיפלתי|הגשתי|סגור|נסגר|הושלם)'
                         r'[\s!.🙂👍✅]*')
CLOSE_Q_SECONDS = 30 * 60


def _yes_closes(msg: 'In', u: dict, text: str) -> str | None:
    """10/11 "כן בוצע" to the bot's own "תרצה לסמן אותו כבוצע?" got "לא ביצעתי את זה, כי לא הייתי בטוח שזו בקשה שלך".
    A yes closes it, only: the question must be the bot's very last turn (nothing sent since), at most 30 minutes old, and
    name one open task or one reminder of his. Closing only (tasks.action / closure.record): it never opens, sends or books."""
    if not (YES_RX.match(text) or YES_DONE_RX.fullmatch(text)):
        return None
    hist = u.get('history') or []
    last = hist[-1] if hist else {}
    if last.get('role') != 'bot' or not (m := CLOSE_Q_RX.search(last.get('text') or '')):
        return None
    try:
        if (datetime.now() - datetime.fromisoformat(last['ts'])).total_seconds() > CLOSE_Q_SECONDS:
            return None
        from whatsapp import outbox                      # a note sent after that question: the yes may be for it
        said = {h.get('text', '')[:60] for h in hist[-8:] if h.get('role') == 'bot'}
        if any(r.get('ts', '') > last['ts'] and r.get('text', '')[:60] not in said for r in outbox.recent_sent(msg.sender, hours=1)):
            return None
    except (KeyError, ValueError, TypeError):
        return None
    body = last['text']
    start = max(body.rfind(c, 0, m.start()) for c in '.\n!?') + 1      # the sentence that asks
    asked = body[start:m.end()].strip()
    from agent import closure, life, tasks
    hits = [t for t in tasks.open_tasks(msg.sender) if closure.same(t['text'], asked)]
    if len(hits) == 1:
        return tasks.action(msg.sender, 'done', hits[0]['id'])
    if hits:
        return None
    lo, hi = (datetime.now() - timedelta(days=7)).isoformat(), (datetime.now() + timedelta(days=60)).isoformat()
    rems = [r for r in life._load()['reminders'] if r.get('chat') == msg.sender and lo <= str(r.get('due', '')) <= hi
            and closure.same(r.get('text', ''), asked)]
    if len({r.get('text') for r in rems}) != 1:
        return None
    what = rems[0]['text']
    closure.record(msg.sender, what, 'done')
    life.close_matching(msg.sender, what)                # a one-off still waiting goes with it
    from agent import now_note
    now_note.add(msg.sender, f'סגור, לא להזכיר שוב: {what}', 'decision')
    return f'✅ סגרתי: {what}. לא אזכיר יותר.'


def _fast(msg: 'In', u: dict, text: str) -> list['Out'] | None:
    """17/11 "מה הלוז שלי מחר" / "מתי ההגשה הבאה" / "איפה השיעור" — answered from the data, no Gemini (agent/fastpath).
    The owner's own words in his private chat only, a whole short question, nothing open that it could be an answer to."""
    if msg.role != 'owner' or msg.group or msg.forwarded or msg.kind != 'text' or msg.reply_to or u.get('quiz')             or (u.get('tutor') or {}).get('active') or u.get('await_email_to'):
        return None
    d = u.get('cal_draft_text')
    if isinstance(d, dict) and (datetime.now() - datetime.fromisoformat(d.get('ts') or '2000-01-01')).total_seconds() < 900:
        return None                                      # "מחר" may be the answer to the draft's "באיזה יום?"
    from agent import fastpath
    t0 = time.monotonic()
    got = fastpath.answer(text, msg.sender)
    if not got:
        return None
    audit(msg.sender, 'fastpath', {'intent': got[0], 'text': text[:80], 'ms': int((time.monotonic() - t0) * 1000)})
    return [Out('text', got[1])]


def _queue_questions(chat: str, items: list[dict]) -> None:
    """Held questions (agent/catchup) back into the outbox's one-at-a-time queue — the daily budget asks again."""
    from whatsapp import outbox
    for q in items:
        try:
            buttons = [tuple(b) for b in q.get('buttons') or []]
            nag = bool(buttons) and str(buttons[0][1]).startswith(('task:', 'sec:odone:'))
            if not buttons or (not nag and outbox.already_asked(chat, q['text'])):
                continue
            outbox.defer_options(chat, q['text'], buttons, gated=False)
        except Exception as e:
            print(f'  ⚠️ catchup queue: {e!r}'[:200])


def _plan_and_learn(msg: 'In', u: dict, text: str) -> list['Out'] | None:
    """Goals / work shifts / the task page / "stop asking about this" (agent/planner, agent/learn, the outbox budget)."""
    from agent import learn, planner
    from whatsapp import outbox
    if msg.role == 'owner' and not msg.forwarded and not msg.group:
        from agent import catchup, journal              # 19/11 "מה פספסתי" / "תבטל את מה שעשית"
        if (got := catchup.answer(msg.sender, text)) is not None:
            audit(msg.sender, 'catchup', {'questions': len(got[1])})
            _queue_questions(msg.sender, got[1])
            return [Out('text', got[0])]
        if (got := journal.route(msg.sender, text)) is not None:
            audit(msg.sender, 'undo_asked', {'confirm': bool(got[1])})
            return [Out('buttons', got[0], buttons=got[1])] if got[1] else [Out('text', got[0])]
    if UNDO_RX.match(text) and (undo := _undo_cal(u)) is not None:
        return undo
    from agent import loops
    if re.match(r'^\s*הגשתי בכל זאת', text) and u.get('submit_last'):
        from utils import deadlines as dl
        dl.mark_done(u.pop('submit_last'))
        return [Out('text', '✅ סימנתי כהוגש.')]
    if loops.SUBMIT_RX.match(text) and len(text) < 80:
        item = loops.find_item(text)
        if item:
            from utils import deadlines as dl
            u['submit_last'] = dl.key_of(item)
            return [Out('text', loops.verify(item))]
    if msg.role == 'owner' and not msg.forwarded and (done := _yes_closes(msg, u, text)) is not None:
        return [Out('text', done)]
    if msg.role == 'owner' and not msg.forwarded and not msg.group:
        from agent import open_items                     # 15/11 "ביטלתי את המלון" ×4 → "לא ביצעתי…": his own item closes
        if (st := open_items.handle(msg.sender, text)) is not None:
            audit(msg.sender, 'open_item', {'how': st[0], 'text': text[:120]})
            return [Out(st[0], st[1], buttons=st[2] or None)] if st[0] == 'buttons' else [Out('text', st[1])]
    from agent import tasks                              # 02/11 "החזרתי את הספרים" closes that task
    if msg.role == 'owner' and not msg.forwarded and \
            (done := tasks.close_text(msg.sender, text, context=_closable_note(msg.sender))) is not None:
        return [Out('text', done)]
    if TRUST_OFF_RX.search(text):
        u['trust_cal'] = False
        return [Out('text', '👍 מעכשיו אשאל לפני כל שינוי ביומן. "סמוך עליי" מחזיר.')]
    if TRUST_ON_RX.match(text):
        u['trust_cal'] = True
        return [Out('text', '👍 שינויים רשמיים בשיעורים (ביטול / חדר / שעה) אעדכן לבד ואעדכן אותך בשורה.')]
    if MUTE_RX.match(text):
        q = outbox.mute_last(msg.sender)
        return [Out('text', f'🔕 בסדר — לא אשאל יותר על "{q}" בחודש הקרוב. "תחזור לשאול" מחזיר.' if q
                     else '🔕 אין כרגע שאלה שהשתקתי. על מה להפסיק לשאול?')]
    if UNMUTE_RX.search(text):
        n = outbox.unmute_all(msg.sender)
        return [Out('text', f'🔔 חוזר לשאול ({n} סוגי שאלות הושתקו).' if n else '🔔 שום דבר לא היה מושתק.')]
    if msg.role == 'owner' and not msg.forwarded and not msg.group:
        from agent import noise                          # 16/11 "תפסיק לשלוח לי דברים שלא רלוונטיים": about what was sent
        quoted = ''
        if msg.reply_to and msg.channel == 'whatsapp':
            try:
                quoted = outbox.quoted_text(msg.sender, msg.reply_to)
            except Exception:
                quoted = ''
        if (said := noise.handle(msg.sender, text, quoted)) is not None:
            audit(msg.sender, 'noise_feedback', {'text': text[:120], 'buttons': [b[1] for b in said[1]]})
            return [Out('buttons', said[0], buttons=said[1])] if said[1] else [Out('text', said[0])]
        if (said := noise.back(text)) is not None:
            return [Out('text', said)]
    if msg.role == 'owner' and not msg.forwarded:          # 11/11 "פחות" / "תפסיק עם המיילים" / "תחזיר את ההכנות"
        from agent import proactive
        if (said := proactive.commands(text)) is not None:
            return [Out('text', said)]
        from agent import obligations                    # 14/11 "מה חסר לי?" / "מה טעיתי בבחנים" / "תראה לי את תרגיל 3
        try:                                             # במיקרו" / "יצא פתרון לתרגיל 2?" — from the store, read only
            ans = obligations.route(text, msg.sender, _course_in_text(text) if len(text) <= 80 else None)
            if isinstance(ans, tuple):
                body, files = ans
                return [Out('text', body)] + [Out('document', f'📎 {Path(f).name}', path=Path(f)) for f in files
                                              if Path(f).exists()]
            if ans is not None:
                return [Out('text', ans)]
        except Exception as e:
            print(f'  ⚠️ obligations route: {e!r}'[:200])
    answering = learn.awaiting_work(u, text)
    if answering or (planner.WORK_RX.search(text) and planner.WORK_HINT_RX.search(text)
                     and not planner.WORK_Q_RX.search(text) and '?' not in text):
        try:
            work = planner.parse_work(text)
        except Exception as e:
            print(f'  ⚠️ parse work failed: {e!r}'[:200])
            work = {'shifts': [], 'none': False}
        if work['shifts'] or work['none']:
            u.pop('learn_wait', None)
            return [Out('text', planner.set_work(msg.sender, work))]
        if answering and planner.WORK_HINT_RX.search(text):
            return [Out('text', '🤔 לא הבנתי באילו ימים ושעות. נסה למשל: "ראשון ורביעי 16-22, שישי 8-14" — או "לא עובד".')]
    r = planner.route(msg.sender, text)
    return [Out('text', r)] if r else None


def homework_site_tick(state_path: Path | None = None, now: datetime | None = None, items_path: Path | None = None) -> list[tuple[str, 'Out']]:
    """19/10: exercises posted on a lecturer's site (collectors/lecturer_site.homework_sync, twice a day) → ONE question
    "add the submission to the calendar?" per new exercise, never again. The reminders come from utils/deadlines."""
    from utils import deadlines
    from whatsapp import outbox
    now = now or datetime.now()
    chat = outbox.owner_chat()
    if not chat or not 9 <= now.hour < 21:
        return []
    try:
        items = json.loads((items_path or deadlines.LECTURER).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    st = _load(state_path)
    u = st.setdefault(chat, {})
    offered = u.setdefault('hw_site_offered', [])
    new = [a for a in items if a.get('due_date') and a['assign_id'] not in offered and a['due_date'] > now.isoformat()]
    if not new or u.get('paused'):
        return []
    offered.extend(a['assign_id'] for a in new)
    events = [{'title': f"📝 הגשת {a['title']} — {a.get('course_name', '')}", 'start': a['due_date'][:10] + 'T22:00',
               'end': a['due_date'][:16], 'all_day': False, 'notes': f"להגשה במודל. התרגיל: {a.get('url', '')}"} for a in new]
    events = [e for e in events if not _in_calendar(e)]
    _save(st, state_path)
    if not events:
        return []
    from integrations import gcal
    pid = secrets.token_hex(4)
    u.setdefault('pending', {})[pid] = {'events': events, 'ts': now.isoformat(timespec='seconds')}
    _save(st, state_path)
    head = '📝 *תרגיל חדש באתר של המרצה*' if len(events) == 1 else f'📝 *{len(events)} תרגילים חדשים באתר של המרצה*'
    body = '\n\n'.join(gcal.describe(e) for e in events)
    return [(chat, Out('buttons', f'{head}\n\n{body}\n\nלהוסיף את מועד ההגשה ליומן?',
                       buttons=[('✅ הוסף', f'cal:ok:{pid}'), ('❌ לא צריך', f'cal:no:{pid}')]))]


def lesson_sync_tick(state_path: Path | None = None, now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """Once a day after 10:00 (the site refresh at 09:45 is in): the university site vs the studies calendar —
    a new room / hour / day, or a lesson that's gone → one ✅ offer each, never the same one twice (agent/lesson_sync)."""
    from agent import lesson_sync, calendar_edit
    from whatsapp import outbox
    now = now or datetime.now()
    chat = outbox.owner_chat()
    if not chat or now.hour < 10:
        return []
    st = _load(state_path)
    u = st.setdefault(chat, {})
    if u.get('paused') or u.get('ls_day') == now.date().isoformat():
        return []
    u['ls_day'] = now.date().isoformat()                     # mark first — a failure must not retry every minute
    _save(st, state_path)
    try:
        plans = lesson_sync.check(now.date())
    except Exception as e:
        print(f'  ⚠️ lesson sync failed: {e}')
        return []
    seen = u.setdefault('ls_seen', [])
    out, series = [], None
    for p in plans:
        k = lesson_sync.key(p)
        lc = u.setdefault('lc_seen', [])
        if k in seen or change_key(p) in lc:                 # already asked (from the site, a mail or Moodle)
            continue
        lc.append(change_key(p))
        ev, also = p['event'], []
        if p.get('location') and not any(p.get(f) for f in ('title', 'date', 'start')):
            # a new room is usually for the whole semester: the rest of that series, offered once together
            if series is None:
                try:
                    series = [e for e in calendar_edit.upcoming(days=100) if e.get('_cal_kind') == 'lessons']
                except Exception:
                    series = []
            t0 = (ev.get('start') or {}).get('dateTime', '')
            also = [e for e in series if e.get('id') != ev.get('id') and e.get('summary') == ev.get('summary')
                    and (e.get('location') or '') == (ev.get('location') or '')
                    and (e.get('start') or {}).get('dateTime', '') > t0]
            ids = {e.get('id') for e in also}
            seen += [lesson_sync.key(q) for q in plans if q['event'].get('id') in ids and q.get('location') == p['location']]
        seen.append(k)
        if (o := _auto_edit(u, p, 'אתר האוניברסיטה', p['explain'], also=also, sure=True)) is not None:
            out.append((chat, o))
    del seen[:-300]
    _save(st, state_path)
    return out


def _open_mails(chat: str, rows: list[dict], now: datetime) -> list[dict]:
    """05/11: a mail about something the owner already closed isn't delivered. An urgent mail or a lesson change always is."""
    from agent import closure
    from integrations import tau_mail
    try:
        closed = closure.items(chat)
    except Exception:
        return rows
    if not closed:
        return rows
    return [r for r in rows if r.get('level') == 'urgent' or r.get('lesson_change') or not closure.match(
        closed, ' '.join(str(r.get(k) or '') for k in ('subject', 'task', 'action', 'summary')),
        tau_mail.sender_name(r.get('from', '')), r.get('id', ''), r.get('thread', ''), now, 'digest')]


def schedule_changed_text(rows: list[dict]) -> str:
    """"🗓️ עדכנתי בלו״ז שלך: שלישי 17/11 11:00 תרגיל הסתברות — ❌ מבוטל (הודעה במודל)"."""
    from agent import schedule
    from config.paths import COURSE_NAMES
    from integrations.gcal import WEEKDAY_HE
    lines = []
    for o in rows[:6]:
        d = date.fromisoformat(o['date'])
        head = schedule.suffix({**o, 'note': ''})
        lines.append(f"• {WEEKDAY_HE[d.weekday()]} {d:%d/%m} {o['start']} {schedule.KIND_HE.get(o['kind'], '')} "
                     f"{COURSE_NAMES.get(o['ck'], o['ck'])}{head}")
    return '🗓️ *עדכנתי בלו״ז שלך:*\n' + '\n'.join(lines)


@api_only()                                             # mail is anyone's words: not for the subscription agent
def mail_tick(state_path: Path | None = None, now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """Deliver the university-mail queue (scripts/tau_mail_watch.py fills it). Called outside quiet hours only."""
    from integrations import tau_mail
    from whatsapp import outbox
    now = now or datetime.now()
    chat = outbox.owner_chat()
    if not chat:
        return []
    new = tau_mail.take_queue()
    if new:
        try:                                                   # the Obsidian brain: this month's mail note
            from agent import brain
            brain.note_mail(new, when=now)
        except Exception as e:
            print(f'  ⚠️ brain mail note failed: {e!r}'[:200])
    st = _load(state_path)
    u = st.setdefault(chat, {})
    pending = u.setdefault('mail_pending', [])
    out = []
    new = _open_mails(chat, _mail_dedupe(u, new, now), now)
    for r in new:
        if r.get('lesson_change') and _not_my_lesson(r, now):
            r['level'] = 'normal' if r['level'] == 'urgent' else r['level']
            r['lesson_change'] = ''                     # another group's lesson: no calendar question either
            r['dated'] = ''
    changed = []                                        # 16/11 his schedule knows the change, ✅ or not (agent/schedule)
    for r in new:
        try:
            from agent import schedule
            changed += schedule.from_update(r, None if r.get('ref') else now.date())
        except Exception as e:
            print(f'  ⚠️ schedule override: {e!r}'[:200])
    if changed:
        out.append((chat, Out('text', schedule_changed_text(changed))))
    for r in new:                                       # 02/11 "something to do by a date" → calendar + nudges till ✅
        if r.get('task') and r.get('task_due') and r['level'] != 'low' and tau_mail.official(r):
            try:                                        # 07/11 audit: others' mail gets no calendar entry of its own
                from agent import tasks
                who = tau_mail.sender_name(r.get('from', ''))
                ids = {'mail_id': r.get('id', ''), 'thread': r.get('thread', '')}
                if tasks.add(chat, r['task'], r['task_due'], src=who, now=now, **ids):
                    r['task_added'] = True
                    r['dated'] = ''                     # it's in the calendar already — no second question
                elif tasks.covered(chat, r['task'], r['task_due'], src=who, now=now, **ids):
                    r['dated'] = ''                     # 05/11 a task of this thread exists, open or closed
            except Exception as e:
                print(f'  ⚠️ mail task: {e!r}'[:200])
    try:                                                # 16/11 a mail that looks like the answer he's waiting for
        from agent import open_items
        for rec, m in open_items.answered(chat, new):
            what = rec.get('text', '').replace(open_items.TRACK_PREFIX, '')
            out.append((chat, Out('buttons', f"📬 נראה שהגיעה תשובה לגבי *{what[:90]}*:\n"
                                             f"\"{(m.get('subject') or '')[:120]}\" (מ{tau_mail.sender_name(m.get('from', ''))})\n\nלסגור?",
                                  buttons=[('✅ כן, נסגר', f"oi:done:r:{rec['id']}"), ('⏳ עוד לא', 'noop:r')])))
    except Exception as e:
        print(f'  ⚠️ waiting answered: {e!r}'[:200])
    urgent = [r for r in new if r['level'] == 'urgent']
    pending += [r for r in new if r['level'] != 'urgent']
    if urgent:
        head = '🚨 *חשוב מהאוניברסיטה*' if all(tau_mail.official(r) for r in urgent) else '🚨 *מייל שנראה דחוף*'
        out.append((chat, Out('text', head + '\n\n' + '\n\n'.join(tau_mail.item_text(r) for r in urgent))))
        q = _mail_calendar_offer(u, [r['dated'] for r in urgent if r.get('dated')])
        if q:
            out.append((chat, q))
    for r in new:                                       # 15/10: "lesson cancelled / moved / new room" → update the calendar?
        if r.get('lesson_change'):
            src = r.get('from', '') if r.get('source') == 'moodle' else f"מייל מ{tau_mail.sender_name(r.get('from', ''))}"
            if (q := _lesson_change_offer(u, r['lesson_change'], src, tau_mail.official(r))) is not None:
                out.append((chat, q))
    slot = next((f'{now.date()}|{h}' for h in MAIL_DIGEST_HOURS if now.hour >= h), '')
    if pending and slot and u.get('mail_digest_slot') != slot:
        u['mail_digest_slot'] = slot
        pending = _open_mails(chat, pending, now)       # 05/11 closed while it waited for the digest
        normal = [r for r in pending if r['level'] == 'normal']
        low = [r for r in pending if r['level'] == 'low']
        if normal or low:
            text = '📬 *עדכונים מהאוניברסיטה* (מייל + מודל)' + ('\n\n' + '\n\n'.join(tau_mail.item_text(r) for r in normal[:8]) if normal else '')
            if len(normal) > 8:
                text += f'\n\n…ועוד {len(normal) - 8}'
            if low:
                text += f"\n\n🗂️ ועוד {len(low)} פחות חשובים: " + ', '.join(tau_mail.sender_name(r['from']) for r in low[:5])
            out.append((chat, Out('text', text)))
            q = _mail_calendar_offer(u, [r['dated'] for r in normal if r.get('dated')])
            if q:
                out.append((chat, q))
        u['mail_pending'] = []
    if new or out:
        _save(st, state_path)
    return out


def _exam_today_text(u: dict, e: dict, place: str) -> str:
    before = (u.get('exam_day_sent') or {}).get('eve|' + _exam_key(e))
    if place and before and before != place:
        note = f"📍 *שים לב — {'פורסם החדר' if before == '-' else 'החדר השתנה'}:* {place}"
    elif place:
        note = f"📍 {place}"
    else:
        note = '📍 החדר עדיין לא מופיע באתר — כדאי לבדוק בלוח בכניסה לבניין.'
    return f"🎯 *היום מבחן ב{e['course']}* — {e['time'] or ''}\n{note}\nבהצלחה! 🍀"


def morning_brief_text(now: datetime | None = None, chat: str | None = None, u: dict | None = None) -> str:
    """One morning message on WhatsApp (the old briefing went to Telegram, which the owner doesn't read): today +
    what's coming this week. No LLM — calendar, the university cache, Moodle deadlines, reminders."""
    from agent.daytools import day_overview
    from config.paths import COURSE_NAMES
    now = now or datetime.now()
    today = now.date()
    parts = [day_overview(today, chat)]
    try:                                                       # 19/10: the task page + goals (agent/planner)
        from agent import planner
        if chat:
            parts.append(planner.daily_block(chat, today, u=u, deadlines=False))
            if now.weekday() == 6:
                parts.append(planner.weekly_block(chat, today, u=u))
    except Exception as e:
        print(f'  ⚠️ planner block failed: {e!r}'[:200])
    try:                                                       # the budget muted a kind of question → say so once
        from whatsapp import outbox
        muted = outbox.pop_new_mutes(chat) if chat else []
        if muted:
            parts.append('🔕 הפסקתי לשאול (לא ענית פעמיים): ' + '; '.join(f'"{q}"' for q in muted[:3])
                         + ' — "תחזור לשאול" מחזיר.')
    except Exception:
        pass
    week = []
    try:
        from utils.deadlines import pending
        for a in pending(horizon_days=7):
            if a['due'].date() > today:
                left = (a['due'].date() - today).days
                week.append(f"• 📝 {COURSE_NAMES.get(a.get('course_key'), '')} — {a.get('title', '')} "
                            f"({'מחר' if left == 1 else f'בעוד {left} ימים'}, {a['due']:%d/%m %H:%M})")
    except Exception:
        pass
    try:
        from integrations import tau_portal
        events, _, _ = tau_portal.schedule(live=False)
        for e in (events or []):
            if e['is_exam'] and e['date'] == today.isoformat() and _taking(u or {}, e):    # exam today → on top
                place = _place(e) or ((u or {}).get('exam_place') or {}).get(_exam_key(e), '')
                parts.insert(0, _exam_today_text(u or {}, e, place))
                continue
            if e['is_exam'] and today.isoformat() < e['date'] <= (today + timedelta(days=14)).isoformat() \
                    and _taking(u or {}, e):
                left = (date.fromisoformat(e['date']) - today).days
                week.append(f"• 🎯 מבחן ב{e['course']} — {_fmt_when(e['date'], e['time'])} "
                            f"({'מחר' if left == 1 else f'בעוד {left} ימים'})")
    except Exception:
        pass
    if week:
        parts.append('*בהמשך:*\n' + '\n'.join(week))
    try:                                                       # 14/11 new in Moodle since yesterday (agent/obligations)
        from agent import obligations
        obligations.refresh(now, chat)
        new = obligations.take_new(now)
        if new:
            parts.append('🆕 *חדש במודל:*\n' + '\n'.join(f'• {x}' for x in new[:8]) + '\n(קישורים: "מה חסר לי")')
    except Exception as e:
        print(f'  ⚠️ obligations brief: {e!r}'[:200])
    if now.weekday() == 6:                                     # Sunday: the week ahead + system health + cost
        try:
            from agent import weekly
            parts.append(weekly.sunday_extra(now, u))
        except Exception as e:
            print(f'  ⚠️ weekly extra failed: {e}')
    return f"☀️ *בוקר טוב!*\n\n" + '\n\n'.join(p for p in parts if p.strip())


def morning_brief_tick(state_path: Path | None = None, now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """07:30–11:00, Sunday–Friday, once a day."""
    from whatsapp import outbox
    now = now or datetime.now()
    chat = outbox.owner_chat()
    if not chat or now.weekday() == 5 or not (now.hour, now.minute) >= (7, 30) or now.hour >= 11:
        return []
    st = _load(state_path)
    u = st.setdefault(chat, {})
    if u.get('brief_day') == now.date().isoformat() or u.get('brief_off'):
        return []
    u['brief_day'] = now.date().isoformat()
    _save(st, state_path)
    try:
        return [(chat, Out('text', morning_brief_text(now, chat, u)))]
    except Exception as e:
        print(f'  ⚠️ morning brief failed: {e}')
        return []


BRIEF_OFF = re.compile(r'(?:תפסיק|בטל|תבטל|בלי).{0,12}(?:סיכום|תדריך|הודעת) ?(?:ה)?בוקר')
BRIEF_ON = re.compile(r'(?:תחזיר|תפעיל).{0,12}(?:סיכום|תדריך|הודעת) ?(?:ה)?בוקר')


_NOT_A_NAME = (r'(?:ה)?(?:יום|יומן|מחר|מחרתיים|שבוע|חודש|ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת|בוקר|ערב|צהריים|'
               r'לילה|מייל|קבוצה|כולם|רשימה|תזכורת)')      # "תקבע לחמישי…" is a time, not a person
COORD_RX = re.compile(r'^\s*(?:תשלח|שלח|תכתוב|כתוב|תגיד|תתאם|תקבע|תסגור|תשאל)\s+(?:הודעה\s+)?'
                      r'(?:ל(?!י\b|בת הזוג\b|' + _NOT_A_NAME + r')|עם\s+(?!בת הזוג\b|' + _NOT_A_NAME + r'))([א-ת][א-ת\'"]+)')
CAN_MSG_RX = re.compile(r'^\s*(?:אתה יכול|תוכל|אפשר|תוכלי|את יכולה)\s+(?:ל)?(?:שלוח|לשלוח|לכתוב|להגיד|לשלח)\s+'
                        r'(?:הודעה\s+)?ל(?!י\b|בת הזוג\b|' + _NOT_A_NAME + r')([א-ת][א-ת\'" ]{1,30}?)\s*\??\s*$')
COORD_RELAY = re.compile(r'^\s*(?:תענה\s+|תגיד\s+)?ל([א-ת][א-ת\'"]+)\s*[:：]\s*(.+)$', re.S)
COORD_STATUS = re.compile(r'מה (?:ה)?מצב(?:ים)? (?:עם|מול) ([א-ת][א-ת\'"]+)|מה ([א-ת][א-ת\'"]+) ענה')
COORD_CANCEL = re.compile(r'(?:תבטל|בטל|תעצור|תפסיק)\s+(?:את\s+)?(?:ה)?(?:תיאום|שיחה|הודעות)\s+(?:עם|מול|ל)\s*([א-ת][א-ת\'"]+)')


def _coord_draft(msg: 'In', u: dict, text: str) -> list['Out']:
    """The owner: "תתאם עם יוסי פגישה בשבוע הבא" → plan + the first message, for ONE ✅."""
    from agent import contacts, coordinator
    p = coordinator.plan(text, list(contacts._load().keys()))
    name = (p.get('name') or '').strip()
    phone = contacts.normalize(p.get('phone') or '') or contacts.normalize((contacts.PHONE_RX.search(text) or [''])[0])
    if phone and name:
        contacts.add(name, phone)
    found = contacts.find(name) if not phone else (name, phone)
    if not found:
        u['coord_wait'] = {'text': text, 'name': name, 'ts': datetime.now().isoformat(timespec='seconds')}
        return [Out('text', f'📇 אין לי את המספר של {name or "האדם הזה"}. שלח לי את איש הקשר שלו '
                            f'(📎 ← איש קשר) או כתוב את המספר, ואמשיך מאיפה שעצרנו.')]
    full, phone = found
    tid = coordinator.create(msg.sender, full, phone, p)
    first = p.get('first_message', '').strip()
    if p.get('kind') == 'message':
        how = 'אם יענה — אעביר לך את התשובה.'
    else:
        how = (f"🎯 {p.get('goal', '')}" + (f"\n📏 {p['limits']}" if p.get('limits') else '') +
               '\n\nמעכשיו אענה לו לבד בתוך המטרה הזו (לפי היומן שלך), ואחזור אליך כשנסגר — או אם משהו לא ברור.')
    return [Out('buttons', f"🤝 *{full}* ({phone[3:5]}…{phone[-3:]})\n\nההודעה הראשונה (מהמספר של הבוט):\n\"{first}\"\n\n{how}\n\nלשלוח?",
                buttons=[('✅ לשלוח', f'co:go:{tid}'), ('❌ לא', f'co:no:{tid}')])]


# 15/11 13:15 a hotel's card, then "אני רוצה שתיצור איתם קשר ותבטל את המלון יש לך מספר הזמנה" → "אין לי אפשרות ליצור
# איתם קשר… גם אין לי את מספר ההזמנה". The bot can write to a contact for him (one ✅ on the first message), and the
# booking was in a PDF he sent on 10/11.
REACH_RX = re.compile(r'(?<![א-ת])(?:(?:ש?תיצור|צור|ש?תצור|ליצור)\s+(?:איתם|איתו|איתה|עמם|קשר)|ש?תפנה\s+(?:אליהם|אליו)|'
                      r'(?:ש?תכתוב|ש?תשלח(?:\s+הודעה)?|ש?תגיד)\s+(?:להם|לו)|ש?תבקש\s+(?:מהם|ממנו)|ש?תדבר\s+(?:איתם|איתו)|'
                      r'(?:ש?תבטל|ש?תסגור)\s+(?:מולם|מולו|איתם|איתו))(?![א-ת])')
REACH_MINUTES = 60
REACH_FILL_RX = re.compile(r'\d{3,}|(?:מספר|שם|תאריך)\s+(?:ה)?(?:הזמנה|מלון)')


def _reach(msg: 'In', u: dict, text: str) -> list['Out'] | None:
    """"תיצור איתם קשר ו…" right after a contact card → a drafted WhatsApp message to that contact, with what he told us
    before (facts, his files' summaries), for his ✅. Missing details → the exact ones are asked. Nothing is sent here."""
    from agent import contacts, coordinator
    if msg.role != 'owner' or msg.group or msg.forwarded or msg.kind != 'text':
        return None
    wait = u.get('reach_wait')
    if wait and datetime.now() - datetime.fromisoformat(wait['ts']) < timedelta(minutes=30) and len(text) < 200 \
            and '?' not in text and REACH_FILL_RX.search(text):
        u.pop('reach_wait', None)                         # he wrote the missing detail
        return _reach_draft(msg, u, wait['name'], f"{wait['text']}\n(המשתמש הוסיף: {text})")
    last = u.get('last_contact') or {}
    if not REACH_RX.search(text) or not last.get('name'):
        return None
    try:
        if datetime.now() - datetime.fromisoformat(last['ts']) > timedelta(minutes=REACH_MINUTES):
            return None
    except (KeyError, ValueError):
        return None
    if not contacts.find(last['name']):
        return None
    return _reach_draft(msg, u, last['name'], text)


def _reach_draft(msg: 'In', u: dict, name: str, text: str) -> list['Out']:
    from agent import contacts, coordinator
    from utils.gemini_sub import api_only
    full, phone = contacts.find(name) or (name, '')
    if not phone:
        return [Out('text', f'📇 אין לי מספר של {name}. שלח לי שוב את איש הקשר.')]
    details = _known_details(msg.sender, f'{text} {full}')
    with api_only():                                      # his files' text is in the prompt: not for the subscription agent
        p = coordinator.plan(f'שלח הודעה ל{full}: {text}', [full], details=details)
    first = (p.get('first_message') or '').strip()
    missing = (p.get('missing') or '').strip()
    if missing or not first:
        u['reach_wait'] = {'name': full, 'text': text, 'ts': datetime.now().isoformat(timespec='seconds')}
        return [Out('text', f"✉️ אכין הודעה ל{full}, חסר לי רק: *{missing or 'מה לכתוב להם'}*. תכתוב לי את זה ואראה לך "
                            f"את ההודעה לפני שליחה.")]
    p['kind'] = 'message'
    tid = coordinator.create(msg.sender, full, phone, p)
    used = f"\n\n📎 לקחתי מ: {details['from']}" if details.get('from') else ''
    return [Out('buttons', f"🤝 *{full}* ({phone[3:5]}…{phone[-3:]})\n\nההודעה (מהמספר של הבוט, בשמך):\n\"{first}\"{used}\n\n"
                           f"אם יענו — אעביר לך את התשובה.\n\nלשלוח?",
                buttons=[('✅ לשלוח', f'co:go:{tid}'), ('❌ לא', f'co:no:{tid}')])]


def _known_details(chat: str, about: str, days: int = 45, path: Path | None = None) -> dict:
    """What he told us before that this is about: his facts, and the summaries of files he sent (a booking PDF). Data for
    the draft only: {'text': …, 'from': 'what it came from'}. Only his own, unforwarded rows."""
    from agent import life, open_items
    want = set().union(*open_items._words(about)) - open_items._GENERIC if open_items._words(about) else set()
    if not want:
        return {}
    cut = (datetime.now() - timedelta(days=days)).isoformat()
    rows = []
    try:
        for ln in (path or CONVO_LOG).read_text(encoding='utf-8').splitlines()[-3000:]:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get('role') != 'owner' or r.get('forwarded') or str(r.get('ts', '')) < cut or r.get('kind') != 'document':
                continue
            said = ' '.join(str(o[1]) for o in r.get('out') or [] if isinstance(o, (list, tuple)) and len(o) > 1)
            score = len(want & set().union(*open_items._words(said))) if open_items._words(said) else 0
            if score >= 2:
                rows.append((score, r.get('ts', ''), said[:1500]))
    except OSError:
        pass
    try:                                                   # 16/11 the kept files (agent/my_files): summary + identifiers
        from agent import my_files
        for rec in my_files.search(chat, about, k=3):
            said = f"📎 *{rec.get('name', '')}*\n{rec.get('summary', '')}\n{rec.get('keys', '')}"
            score = len(want & set().union(*open_items._words(said))) if open_items._words(said) else 0
            if score >= 2 and str(rec.get('ts', '')) >= cut[:16] and not any(said[:200] in s for _, _, s in rows):
                rows.append((score + 1, rec.get('ts', ''), said[:1500]))
    except Exception as e:
        print(f'  ⚠️ my_files details: {e!r}'[:160])
    facts = [f for f in life.facts(chat) if open_items._words(f) and want & set().union(*open_items._words(f))][-5:]
    best = sorted(rows, key=lambda x: (x[0], x[1]))[-2:]
    if not best and not facts:
        return {}
    title = [re.sub(r'\*', '', s.split('\n')[0])[:80] for _, _, s in best]
    return {'text': '\n\n'.join([s for _, _, s in best] + (['מה שהמשתמש סיפר: ' + '; '.join(facts)] if facts else [])),
            'from': ', '.join(title + (['מה שסיפרת לי'] if facts else []))}


def _coord_button(msg: 'In', u: dict, p: str) -> list['Out']:
    from agent import coordinator, contacts
    from whatsapp import outbox, bridge
    _, op, tid = (p.split(':', 2) + ['', ''])[:3]
    d = coordinator.load()
    t = d.get(tid)
    if not t or t['status'] != 'draft':
        return [Out('text', 'הבקשה כבר לא בתוקף.')]
    if op != 'go' or u.get('paused'):
        t['status'] = 'cancelled'
        coordinator.save(d)
        return [Out('text', '👍 לא נשלח כלום.')]
    bridge.enroll(t['phone'], role='contact')                 # their replies reach ONLY this conversation
    ok = outbox.send_text(contacts.jid(t['phone']), t['first_message'])
    t['status'] = 'active' if ok else 'failed'
    t['log'].append(['bot', t['first_message']])
    coordinator.save(d)
    audit(msg.sender, 'contact_message', {'to': t['name'], 'task': tid, 'text': t['first_message'][:300]})
    return [Out('text', f"📤 נשלח ל{t['name']}. אעדכן אותך." if ok else '⚠️ השליחה נכשלה.')]


def _coord_owner(msg: 'In', u: dict, text: str, st: dict) -> list['Out'] | None:
    """The owner's side: new requests, "ליוסי: …" answers, status, cancel, a number for a waiting request."""
    from agent import contacts, coordinator
    wait = u.get('coord_wait')
    if wait and (datetime.now() - datetime.fromisoformat(wait['ts'])).total_seconds() < 1800 \
            and (m := contacts.PHONE_RX.search(text)) and len(text) < 40:
        u.pop('coord_wait')
        contacts.add(wait['name'], m.group(0))
        return _coord_draft(msg, u, wait['text'])
    if (r := _reach(msg, u, text)) is not None:
        return r
    if m := COORD_CANCEL.search(text):
        hit = coordinator.by_name(m.group(1))
        if hit:
            d = coordinator.load()
            d[hit[0]]['status'] = 'cancelled'
            coordinator.save(d)
            return [Out('text', f"⏹️ הפסקתי את התיאום עם {hit[1]['name']}. לא אענה לו יותר.")]
    if m := COORD_STATUS.search(text):
        hit = coordinator.by_name(m.group(1) or m.group(2))
        if hit:
            t = hit[1]
            lines = '\n'.join(f"{'🤖' if w == 'bot' else '👤' if w == 'them' else '🧑'} {x[:200]}" for w, x in t['log'][-8:])
            status = {'active': 'פעיל', 'waiting_owner': 'מחכה לתשובה שלך', 'done': 'נסגר', 'cancelled': 'בוטל',
                      'draft': 'טיוטה', 'failed': 'נכשל', 'expired': 'פג תוקף'}.get(t['status'], t['status'])
            return [Out('text', f"🤝 *{t['name']}* — {status}\n🎯 {t['goal']}\n\n{lines or '(עוד אין הודעות)'}")]
    if m := COORD_RELAY.match(text):
        hit = coordinator.by_name(m.group(1))
        if hit and hit[1]['status'] in ('active', 'waiting_owner'):
            d = coordinator.load()
            t = d[hit[0]]
            said = m.group(2).strip()
            t['log'].append(['owner', said])
            t['status'] = 'active'
            if t['kind'] == 'message':                          # a plain message → the owner's words, as they are
                from whatsapp import outbox
                outbox.send_text(contacts.jid(t['phone']), f'מהמשתמש: {said}')
                t['log'].append(['bot', f'מהמשתמש: {said}'])
                coordinator.save(d)
                return [Out('text', f"📤 נשלח ל{t['name']}.")]
            coordinator.save(d)
            return _coord_act(hit[0], t, st, reply_to_contact=False)
    if COORD_RX.search(text):
        return _coord_draft(msg, u, text)
    if (m := CAN_MSG_RX.match(text)):                         # "אתה יכול לשלוח הודעה לפלוני?" → yes, what to say?
        name = m.group(1).strip()
        u['coord_compose'] = {'name': name, 'ts': datetime.now().isoformat(timespec='seconds')}
        return [Out('text', f'✉️ כן. מה לכתוב ל{name}?')]
    cc = u.get('coord_compose')
    if cc and msg.kind == 'text' and datetime.now() - datetime.fromisoformat(cc['ts']) < timedelta(minutes=15):
        u.pop('coord_compose', None)                          # the next message is what to say
        return _coord_draft(msg, u, f"שלח הודעה ל{cc['name']} ש{text}")
    return None


def _coord_act(tid: str, t: dict, st: dict, reply_to_contact: bool = True) -> list['Out']:
    """Ask the model for the next move and carry it out. Returns what goes to the CONTACT when this runs in
    the contact's turn (reply_to_contact) — otherwise it's sent to them through the outbox."""
    from agent import coordinator, contacts
    from whatsapp import outbox
    step = coordinator.next_step(t)
    msg_out = (step.get('message') or '').strip()
    owner = t['owner']
    ou = st.setdefault(owner, {})
    if step['action'] == 'reply':
        t['auto'] += 1
    elif step['action'] == 'agreed':
        t['status'] = 'done'
        ev = step.get('event') or {}
        outbox.send_text(owner, f"✅ *סגרתי עם {t['name']}:* {(step.get('to_owner') or '')}")
        if ev.get('date') and ev.get('start'):
            from integrations import gcal
            cal = {'title': ev.get('title') or f"פגישה עם {t['name']}", 'start': f"{ev['date']}T{ev['start']}",
                   'end': f"{ev['date']}T{ev.get('end') or ev['start']}", 'location': ev.get('location', '')}
            pid = secrets.token_hex(4)
            ou.setdefault('pending', {})[pid] = {'events': [cal], 'ts': datetime.now().isoformat(timespec='seconds')}
            outbox.send_options(owner, f"{gcal.describe(cal)}\n\nלהוסיף ליומן?",
                                [('✅ הוסף', f'cal:ok:{pid}'), ('❌ לא', f'cal:no:{pid}')], busy='hold')
    elif step['action'] == 'ask_owner':
        t['status'] = 'waiting_owner'
        last = next((x for w, x in reversed(t['log']) if w == 'them'), '')
        outbox.send_text(owner, f"❓ *{t['name']}:* \"{last[:400]}\"\n{(step.get('to_owner') or '')}\n\n"
                                f"מה לענות? כתוב: \"ל{t['name'].split()[0]}: …\"")
    elif step['action'] == 'stop':
        t['status'] = 'done'
        outbox.send_text(owner, f"🛑 {t['name']}: {(step.get('to_owner') or 'סיים את השיחה')}")
    if msg_out:
        t['log'].append(['bot', msg_out])
    d = coordinator.load()
    d[tid] = t
    coordinator.save(d)
    if not msg_out:
        return []
    if reply_to_contact:
        return [Out('text', msg_out)]
    outbox.send_text(contacts.jid(t['phone']), msg_out)
    return [Out('text', f"📤 עניתי ל{t['name']}: \"{msg_out}\"")]


@api_only()
def _contact(msg: 'In', st: dict) -> list['Out']:
    """Someone the owner asked the agent to talk to. They reach ONLY their conversation — never any other tool."""
    from agent import coordinator, contacts
    from whatsapp import outbox
    phone = contacts.normalize(msg.key) or msg.key
    hit = coordinator.active_for(phone)
    name = contacts.name_of(phone) or 'איש קשר'
    text = (msg.text or '').strip()
    if msg.kind != 'text' or not text:
        return [Out('text', 'כרגע אני קורא רק הודעות טקסט 🙏')]
    if not hit:                                                     # no open task → a secretary passes it on
        recent = [t for t in coordinator.load().values() if t['phone'] == phone]
        if not recent:
            return []
        owner = recent[-1]['owner']
        outbox.send_text(owner, f"📩 *{name}* כתב לעוזר: \"{text[:500]}\"")
        return [Out('text', 'תודה! העברתי למשתמש 🙏')]
    tid, t = hit
    t['log'].append(['them', text])
    if t['kind'] == 'message' or t['status'] == 'waiting_owner':
        d = coordinator.load()
        d[tid] = t
        coordinator.save(d)
        outbox.send_text(t['owner'], f"📩 *{t['name']}:* \"{text[:500]}\"" +
                         (f"\n\nלענות? כתוב: \"ל{t['name'].split()[0]}: …\"" if t['kind'] == 'message' else ''))
        return [Out('text', 'תודה! העברתי למשתמש 🙏')] if t['kind'] == 'message' else []
    return _coord_act(tid, t, st)


_LEAD_WORDS = r'(?:\d{1,3}\s*(?:דקות|דקה|דק[\'׳]|שעות|שעה|ימים|יום)|חצי שעה|רבע שעה|שעה וחצי|שעתיים|שעה|יומיים|יום|שבוע|ערב)'
EVREM_ABOUT = re.compile(r'^\s*(?:תזכיר|תזכירי)\s+לי\s+(?:על|לגבי)\s+(.+?)\s+(' + _LEAD_WORDS + r')\s+לפני\s*[.!?]*$')
EVREM_BEFORE = re.compile(r'^\s*(?:תזכיר|תזכירי)\s+לי\s+(' + _LEAD_WORDS + r')\s+לפני\s+(?!כל\b)(.+?)\s*[.!?]*$')
EVREM_ALL = re.compile(r'(?:תזכיר|תזכירי)\s+לי\s+(.{0,25}?)\s*לפני\s+כל\s+(?:אירוע|פגישה|דבר)(.{0,25})')
EVREM_OFF = re.compile(r'(?:בלי|תפסיק|תבטל|בטל)\s+(?:את\s+)?(?:ה)?תזכורות\s+(?:ל|על\s+)?(?:ה)?אירועים|תפסיק להזכיר לי על אירועים')
EVREM_ON = re.compile(r'(?:תחזיר|תפעיל)\s+(?:את\s+)?(?:ה)?תזכורות\s+(?:ל|על\s+)?(?:ה)?אירועים')
EVREM_NONE = re.compile(r'^\s*בלי\s+תזכורת\s+(?:ל|על\s+)(.+?)\s*[.!?]*$')
# 19/11 improvement 14: the "כדאי לצאת עכשיו" line before a session across the campus (off unless he says so)
LEAVE_ON = re.compile(r'^\s*(?:תזכיר\s+לי\s+(?:מתי\s+)?לצאת\s+בין\s+(?:ה)?שיעורים|תפעיל\s+(?:את\s+)?(?:ה)?(?:תזכורת|שורת)\s+(?:ה)?יציאה)\s*[.!]*\s*$')
LEAVE_OFF = re.compile(r'^\s*(?:בלי|תפסיק|תבטל|די)\s+(?:עם\s+)?(?:את\s+)?(?:ה)?(?:תזכור(?:ת|ות)\s+(?:ה)?יציאה|כדאי\s+לצאת(?:\s+עכשיו)?|להזכיר\s+לי\s+לצאת)\s*[.!]*\s*$')


def _evrem_find(what: str):
    from integrations import gcal
    if not gcal.configured():
        return None
    q = re.sub(r'^(?:ה|את\s+ה?)', '', what.strip())
    hits = gcal.find_events(q, days_back=0, days_ahead=120)
    return hits[0] if hits else None


def _event_reminder_cmd(text: str) -> list['Out'] | None:
    """'תזכיר לי על המסעדה יום לפני' · 'תזכיר לי שעתיים לפני הפגישה עם יוסי' · 'תזכיר לי שעה לפני כל אירוע' ·
    'בלי תזכורות לאירועים' · 'בלי תזכורת למסעדה'. None → not about a calendar event (life.py reminders)."""
    from agent import event_reminders as er
    from integrations import gcal
    if LEAVE_ON.match(text):
        er.set_setting(leave=True)
        return [Out('text', '🚶 סגור. לפני שיעור שצריך ללכת אליו מבניין אחר בלי מספיק הפסקה, אשלח "כדאי לצאת עכשיו". '
                            '"בלי תזכורת יציאה" כדי להפסיק.')]
    if LEAVE_OFF.match(text):
        er.set_setting(leave=False)
        return [Out('text', '🔕 בלי "כדאי לצאת עכשיו".')]
    if EVREM_OFF.search(text):
        er.set_setting(on=False)
        return [Out('text', '🔕 לא אזכיר יותר לפני אירועים ביומן (תזכורות שביקשת במפורש — כן). "תחזיר תזכורות לאירועים" כדי להחזיר.')]
    if EVREM_ON.search(text):
        er.set_setting(on=True)
        return [Out('text', '🔔 חזרתי להזכיר לך לפני אירועים ביומן.')]
    if m := EVREM_ALL.search(text):
        lead = er.parse_lead(m.group(1) + ' ' + m.group(2))
        if lead:
            er.set_setting(lead=lead, on=True)
            return [Out('text', f'🔔 מעכשיו אזכיר לך כאן {er._lead_words(lead).replace("בעוד ", "")} לפני כל אירוע ביומן.')]
    if m := EVREM_NONE.match(text):
        ev = _evrem_find(m.group(1))
        if ev:
            er.set_override(ev['id'], [])
            return [Out('text', f"🔕 בלי תזכורת ל{ev.get('summary', '')}.")]
    m = EVREM_ABOUT.match(text)
    what, lead_txt = (m.group(1), m.group(2)) if m else (None, None)
    if not m and (m2 := EVREM_BEFORE.match(text)):
        lead_txt, what = m2.group(1), m2.group(2)
    if not what:
        return None
    ev = _evrem_find(what)
    lead = er.parse_lead(lead_txt + ' לפני')
    if not ev or not lead:
        return None
    start, all_day = er._start(ev)
    if all_day and lead < 1440:
        lead = 240                                       # "יום/ערב לפני" for an all-day event → 20:00 the evening before
    cur = ev['id'] in er._load()['override'] and er._load()['override'][ev['id']] or er.leads_for(ev, er._load())
    er.set_override(ev['id'], list(cur) + [lead])
    at = (start - timedelta(minutes=lead))
    return [Out('text', f"⏰ אזכיר לך כאן ב-{at:%d/%m %H:%M} על *{ev.get('summary', '')}* ({gcal.when_text(ev)}).")]


def event_reminder_tick(now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """Reminders before calendar events, in the chat (agent/event_reminders.py). Outside quiet hours only."""
    from agent import event_reminders as er
    from whatsapp import outbox
    chat = outbox.owner_chat()
    if not chat:
        return []
    try:
        return [(chat, Out('text', t)) for t in er.due(now)]
    except Exception as e:
        print(f'  ⚠️ event reminders failed: {e}')
        return []


def merge_outs(items: list[tuple[str, 'Out']]) -> list[tuple[str, 'Out']]:
    """Several proactive texts to the same chat in one tick → ONE message (09/10: exam eve + mail + brief arrived
    as a burst). Questions with numbered options stay separate and go last (only the latest is answerable)."""
    by_chat: dict[str, list] = {}
    for chat, o in items:
        by_chat.setdefault(chat, []).append(o)
    out = []
    for chat, outs in by_chat.items():
        texts = [o for o in outs if o.kind == 'text']
        rest = [o for o in outs if o.kind != 'text']
        if len(texts) > 1:
            texts = [Out('text', '\n\n— — —\n\n'.join(o.text for o in texts))]
        out += [(chat, o) for o in texts + rest]
    return out


def _booking(msg: 'In', u: dict, text: str) -> list['Out']:
    """'תזמין לי ולבת הזוג מקום למסעדת a ב-29.10 ב-20:00' → find it, check real availability, offer to book."""
    from agent import booking, contacts
    try:
        req = booking.parse_request(text)
    except Exception as e:
        print(f'  ⚠️ booking parse failed: {e}')
        return [Out('text', '⚠️ לא הצלחתי להבין את הבקשה. נסה: "תזמין מקום במסעדת X ב-29/10 ב-20:00 לשניים".')]
    if not (req.get('restaurant') or '').strip():
        return [Out('text', '🍽️ לאיזו מסעדה להזמין?')]
    try:
        info = booking.lookup(req['restaurant'], req.get('city', ''))
    except Exception as e:
        print(f'  ⚠️ booking lookup failed: {e}')
        info = {}
    if not info.get('name') or not info.get('sure'):
        return [Out('text', f"🍽️ לא מצאתי בוודאות את \"{req['restaurant']}\". באיזו עיר היא, או מה השם המלא?")]
    date_s, time_s, size = req.get('date') or '', req.get('time') or '', req['size']
    lines = [f"🍽️ *{info['name']}*" + (f" · {info['city']}" if info.get('city') else '')]
    if info.get('address'):
        lines.append(f"📍 {info['address']}")
    if info.get('phone'):
        lines.append(f"📞 {info['phone']}")
    link = info.get('url', '') or info.get('site', '')
    try:
        page = booking.find_ontopo(info)
    except Exception:
        page = ''
    prefilled = False
    if page and re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_s) and re.fullmatch(r'\d{1,2}:\d{2}', time_s):
        try:
            av = booking.ontopo_availability(page, date_s, time_s.zfill(5), size)
            d = date.fromisoformat(date_s)
            lines.append(f"\n📅 יום {WEEKDAY_HE_[d.weekday()]} {d:%d/%m} · {time_s} · {size} אנשים\n"
                         + booking.availability_text(av, time_s.zfill(5)))
            link, prefilled = av['link'], True
        except Exception as e:
            print(f'  ⚠️ ontopo availability failed: {e}')
    if link:
        lines.append(f"\n🔗 להזמנה{' (התאריך, השעה ומספר האנשים כבר ממולאים)' if prefilled else ''}:\n{link}")
    pid = secrets.token_hex(3)
    slots = booking.free_slots(av, time_s.zfill(5)) if prefilled else []
    u.setdefault('book', {})[pid] = {'req': req, 'info': info, 'page': page, 'slots': slots,
                                     'ts': datetime.now().isoformat(timespec='seconds')}
    buttons = [(f'⏰ {t} ({area})', f'bk:t{i}:{pid}') for i, (t, area) in enumerate(slots)
               if t != time_s.zfill(5)]
    wa = contacts.normalize(info.get('whatsapp', ''))
    if wa:
        buttons.append(('📲 אזמין בוואטסאפ על שמך', f'bk:wa:{pid}'))
    if date_s and time_s:
        buttons.append(('📅 להוסיף ליומן', f'bk:cal:{pid}'))
    buttons.append(('🙏 לא צריך', f'bk:no:{pid}'))
    note = ('' if wa else '\n\n(אין להם וואטסאפ — בחר שעה ואשלח קישור שנפתח ישר עליה. '
                          'שם, טלפון ופרטי אשראי (אם המסעדה דורשת) ממלא רק אתה — את אלה אני לא ממלא.)')
    return [Out('buttons', '\n'.join(lines) + note, buttons=buttons)]


ME_RX = re.compile(r'([א-ת][א-ת\'"\- ]{1,40}?)\s*[,:-]?\s*((?:\+?972|0)5\d[\d\s-]{7,10})')


def _booking_button(msg: 'In', u: dict, p: str) -> list['Out']:
    from agent import contacts, coordinator
    _, op, pid = (p.split(':', 2) + ['', ''])[:3]
    item = (u.get('book') or {}).get(pid)
    if not item:
        return [Out('text', 'הבקשה כבר לא בתוקף.')]
    req, info = item['req'], item['info']
    if op == 'no':
        u['book'].pop(pid, None)
        return [Out('text', '👍 בסדר.')]
    if re.fullmatch(r't\d', op) and item.get('page'):              # The owner picked one of the free times
        from agent import booking
        t, area = item['slots'][int(op[1:])]
        req['time'] = t
        link = booking.slot_link(item['page'], req['date'], t, req['size'])
        return [Out('buttons', f"🍽️ *{info['name']}* · {req['date'][8:]}/{req['date'][5:7]} · *{t}* ({area}) · {req['size']} אנשים\n"
                               f"🔗 {link}\n\nבקישור: 'מצאו לי שולחן' ← {t} ← השם והטלפון שלך (ואשראי אם נדרש). "
                               f"אחרי שסגרת — להוסיף ליומן?",
                    buttons=[('📅 להוסיף ליומן', f'bk:cal:{pid}'), ('🙏 לא צריך', f'bk:no:{pid}')])]
    if op == 'cal':
        u['book'].pop(pid, None)
        h, m = map(int, req['time'].split(':'))
        ev = {'title': f"🍽️ {info['name']}" + (f" — {req['occasion']}" if req.get('occasion') else ''),
              'start': f"{req['date']}T{h:02d}:{m:02d}", 'end': f"{req['date']}T{min(h + 2, 23):02d}:{m:02d}",
              'location': ', '.join(x for x in (info.get('address'), info.get('city')) if x)}
        ids, added, _ = _add_events(msg.sender, [ev])
        return [Out('text', f"✅ נוסף ליומן: {ev['title']} — {req['date'][8:]}/{req['date'][5:7]} {req['time']}"
                            if added else 'זה כבר ביומן.')]
    me = u.get('me') or {}
    if not me.get('phone'):
        u['book_me'] = pid
        return [Out('text', '📲 על איזה שם ומספר להזמין? (למשל: "ישראל ישראלי 050-1234567" — אזכור לפעם הבאה)')]
    u['book'].pop(pid, None)
    wa = contacts.normalize(info.get('whatsapp', ''))
    contacts.add(info['name'], wa)
    d = date.fromisoformat(req['date'])
    first = (f"היי, כאן העוזר האישי של {me['name']} 🤖 רציתי להזמין שולחן ל-{req['size']} "
             f"ביום {WEEKDAY_HE_[d.weekday()]} {d:%d/%m} ב-{req['time']} על שם {me['name']} ({me['phone']})"
             + (f" — {req['occasion']} 🎉" if req.get('occasion') else '') + '. יש מקום?')
    plan = {'kind': 'coordinate', 'first_message': first,
            'goal': f"להזמין שולחן ל-{req['size']} ב-{req['date']} {req['time']} על שם {me['name']} ({me['phone']})",
            'limits': 'עד חצי שעה לפני/אחרי השעה; אם אין מקום — לשאול את המשתמש. לא למסור פרטי אשראי.'}
    tid = coordinator.create(msg.sender, info['name'], wa, plan)
    return _coord_button(msg, u, f'co:go:{tid}')


def _booking_me(msg: 'In', u: dict, text: str) -> list['Out'] | None:
    """The answer to 'על איזה שם ומספר להזמין?' → remembered, and the booking goes on."""
    pid = u.get('book_me')
    if not pid:
        return None
    m = ME_RX.search(text)
    if not m:
        return None
    from agent import contacts
    u.pop('book_me', None)
    u['me'] = {'name': m.group(1).strip(), 'phone': contacts.normalize(m.group(2)) and m.group(2).strip()}
    return [Out('text', f"👍 זכרתי: {u['me']['name']} · {u['me']['phone']}")] + _booking_button(msg, u, f'bk:wa:{pid}')


CANCEL_RX = re.compile(r'(?:לבטל|ביטול)\D{0,20}?(\d{1,3}\s*(?:שעות|שעה|ימים|יום)|יומיים|שבוע|יום)\s*(?:לפני|מראש)')
_CANCEL_WORDS = {'יומיים': 48, 'שבוע': 168, 'יום': 24}
WITH_PARTNER_RX = re.compile(r'(?:תזכיר|תזכירי|להזכיר)[^.]{0,30}(?:ול?בת הזוג\b|גם לבת הזוג|לשנינו|לשנינו|לי ולה)')


def event_extras(text: str) -> dict:
    """10/10: "הזמנו לשעה 21, אפשר לבטל רק 24 שעות לפני, תזכיר לי ולבת הזוג" — what to set up after the add."""
    out = {}
    if m := CANCEL_RX.search(text or ''):
        g = m.group(1)
        if g in _CANCEL_WORDS:
            out['cancel_hours'] = _CANCEL_WORDS[g]
        else:
            n = int(re.match(r'\d+', g).group(0))
            out['cancel_hours'] = n * (24 if re.search(r'ימים|יום', g) else 1)
    if WITH_PARTNER_RX.search(text or ''):
        out['partner'] = True
    return out


def _daytime(dt: datetime) -> datetime:
    """A reminder moment the bot can actually send (it's quiet 23:00–07:00)."""
    if dt.hour < 9:
        return dt.replace(hour=9, minute=0)
    if dt.hour >= 22:
        return dt.replace(hour=21, minute=0)
    return dt


def _apply_extras(msg: 'In', extras: dict, ids: list[str], events: list[dict]) -> str:
    """Cancellation-deadline reminder for the owner; the partner reminded too; the owner's chat reminders the day before + 1.5h."""
    from agent import life, event_reminders as er
    from whatsapp import outbox
    lines = []
    for eid, ev in zip(ids, events):
        if ev.get('all_day') or 'T' not in ev.get('start', ''):
            continue
        start = datetime.fromisoformat(ev['start'])
        title = ev['title']
        er.set_override(eid, [1440, 90])
        if h := extras.get('cancel_hours'):
            deadline = start - timedelta(hours=h)
            if deadline > datetime.now():
                due = _daytime(deadline - timedelta(hours=9))
                life.add_reminder(msg.sender, {'when': due.isoformat(timespec='minutes'),
                                               'text': f"⚠️ אחרון לבטל בחינם את {title}: עד {deadline:%d/%m %H:%M}. "
                                                       f"אם הכל בסדר — לא צריך לעשות כלום 🙂"})
                lines.append(f"⚠️ תזכורת לביטול: {due:%d/%m %H:%M} (הביטול אפשרי עד {deadline:%d/%m %H:%M})")
        if extras.get('partner') and (partner := outbox.partner_chat()):
            eve = (start - timedelta(days=1)).replace(hour=20, minute=0)
            before = start - timedelta(hours=2)
            for due, txt in ((eve, f"מחר ב-{start:%H:%M} — {title} עם {OWNER_NAME} 🎉"),
                             (_daytime(before), f"בעוד שעתיים ({start:%H:%M}) — {title} עם {OWNER_NAME}")):
                if due > datetime.now():
                    life.add_reminder(partner, {'when': due.isoformat(timespec='minutes'), 'text': txt +
                                             (f" · 📍 {ev['location']}" if ev.get('location') else '')})
            lines.append(f'💬 גם {PARTNER_NAME} תקבל תזכורת: ערב לפני ושעתיים לפני')
        lines.append('🔔 ואני אזכיר לך כאן: יום לפני ושעה וחצי לפני')
    return '\n'.join(dict.fromkeys(lines))


def review_offer_tick(state_path: Path | None = None, now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """6–10 days before an exam the owner is taking (with past exams on file): one offer of a review plan built from
    what recurs in those exams. Building happens only if he says yes (a few cents, cached).
    05/11: only with SSA_STUDY_PUSH=1 — the owner asks for a review ("תעשה לי חזרה ל…"), the bot doesn't offer."""
    from agent import study_push, tutor
    from config.paths import VAULT_DIR
    from integrations import tau_portal
    from whatsapp import outbox
    if not study_push.on():
        return []
    now = now or datetime.now()
    chat = outbox.owner_chat()
    if not chat or now.hour < 10:
        return []
    events, _, _ = tau_portal.schedule(live=False)
    st = _load(state_path)
    u = st.setdefault(chat, {})
    offered = u.setdefault('review_offered', {})
    out = []
    for e in events or []:
        if not e['is_exam'] or not _taking(u, e):
            continue
        left = (date.fromisoformat(e['date']) - now.date()).days
        key = _exam_key(e)
        ck = _course_key_of(e['course'])
        if not (6 <= left <= 10) or key in offered or not ck or \
                not (VAULT_DIR / '🧾 מבחנים' / tutor.EXAM_FOLDERS.get(ck, '-')).is_dir():
            continue
        offered[key] = now.date().isoformat()
        out.append((chat, _ask(u, {'q': 'review', 'exam': e, 'course': ck},
                               f"📚 בעוד {left} ימים מבחן ב{e['course']} ({_fmt_when(e['date'], e['time'])}).\n"
                               f"לבנות לך תוכנית חזרה לפי מה שחוזר הכי הרבה במבחנים הקודמים? "
                               f"5 נושאים, בכל אחד: שיעור קצר, 2 שאלות אמריקאיות ושאלת מבחן אמיתית עם פתרון.",
                               [('📚 כן, מתחילים עכשיו', 'go'), ('⏰ מחר ב-9:00', 'tmr'), ('🙏 לא צריך', 'no')])))
        break                                                  # one offer per tick
    if out:
        _save(st, state_path)
    return out


def review_resume_tick(state_path: Path | None = None, now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """16/10 (real): the calculus review opened 14/10 and stopped at question 1; the exam is a week later. A week
    before and again 2–3 days before an exam with a paused review: one "continue?" each — never more.
    05/11: only with SSA_STUDY_PUSH=1."""
    from agent import study_push, tutor
    from integrations import tau_portal
    from whatsapp import outbox
    if not study_push.on():
        return []
    now = now or datetime.now()
    chat = outbox.owner_chat()
    if not chat or not 10 <= now.hour < 21:
        return []
    st = _load(state_path)
    u = st.get(chat) or {}
    t = u.get('tutor_paused') or {}
    plan = t.get('plan', '')
    if not plan.startswith('auto:') or (u.get('tutor') or {}).get('active'):
        return []
    ck = plan.split(':', 1)[1]
    events, _, _ = tau_portal.schedule(live=False)
    e = next((e for e in events or [] if e['is_exam'] and _taking(u, e) and _course_key_of(e['course']) == ck
              and e['date'] >= now.date().isoformat()), None)
    if not e:
        return []
    left = (date.fromisoformat(e['date']) - now.date()).days
    stage = 'week' if 5 <= left <= 7 else 'final' if 2 <= left <= 3 else ''
    key = _exam_key(e)
    done = u.setdefault('review_resume', {})
    if not stage or done.get(key) == 'no' or stage in (done.get(key) or ''):
        return []
    done[key] = (done.get(key) or '') + stage + ','
    topics = []
    try:                                                       # the cached plan only — never a paid rebuild from a tick
        topics = json.loads((tutor.CACHE / plan.replace(':', '_') / 'plan.json').read_text(encoding='utf-8')).get('topics') or []
    except (OSError, ValueError, AttributeError):
        pass
    i = t.get('i', 0)
    where = f"נושא {i + 1} מתוך {len(topics) or 5}"
    title = (topics[i].get('title', '') if i < len(topics) and isinstance(topics[i], dict) else '')
    out = _ask(u, {'q': 'review', 'exam': e, 'course': ck, 'key': key},
               f"📚 בעוד {left} ימים מבחן ב{e['course']} ({_fmt_when(e['date'], e['time'])}).\n"
               f"החזרה עצרה ב{where}" + (f" ({title})" if title else '') + '. להמשיך?',
               [('▶️ ממשיכים עכשיו', 'resume'), ('⏰ מחר ב-9:00', 'tmr'), ('🙏 לא צריך', 'stop')])
    _save(st, state_path)
    return [(chat, out)]


def _review_button(msg: 'In', u: dict, item: dict, op: str) -> list['Out']:
    ck = item['course']
    key = 'intro_moed_b' if ck == 'intro_to_cs' else f'auto:{ck}'
    if op == 'go':
        return _start_review(u, key)
    if op == 'resume':
        if (u.get('tutor_paused') or {}).get('plan') == key:
            return _resume_tutor(u)
        return _start_review(u, key)
    if op == 'stop':                                           # no more "continue the review?" for this exam
        u.setdefault('review_resume', {})[item.get('key', '')] = 'no'
        return [Out('text', '👍 לא אזכיר יותר. אם תרצה — "נמשיך".')]
    if op == 'tmr':
        from agent import tutor
        at = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        tutor.schedule(msg.sender, key, at.isoformat(timespec='minutes'))
        return [Out('text', f'⏰ מחר ב-9:00 אפתח את החזרה. אפשר גם "תתחיל חזרה" מתי שבא לך.')]
    return [Out('text', '👍 בסדר. אם תרצה בהמשך — "תעשה לי חזרה ל…".')]


def exam_sync_prompts(state_path: Path | None = None, now: datetime | None = None) -> list[tuple[str, 'Out']]:
    """Once a day (after 10:00): live check of the university, then ask the owner about exams missing from his calendar."""
    from whatsapp import outbox
    now = now or datetime.now()
    chat = outbox.owner_chat()
    if not chat or now.hour < 10:
        return []
    st = _load(state_path)
    u = st.setdefault(chat, {})
    if u.get('exam_check_day') == now.date().isoformat():
        return []
    u['exam_check_day'] = now.date().isoformat()
    _save(st, state_path)                       # mark first — a failing site must not be retried every 5 min
    q = _exam_prompt(u, live=False)
    if not q:
        return []
    _save(st, state_path)
    return [(chat, q)]


def exam_query(text: str) -> str:
    """'כמה ימים נשארו למבחן בבדידה?' → 'בדידה'; '' when no course is named ('המבחן הקרוב')."""
    m = re.search(r'(?:מבחן|בוחן|הגשה)\s+(?:של\s+|ב|ל)?([א-ת][א-ת"׳״]+)', text)
    w = m.group(1) if m else ''
    w = w[1:] if w.startswith('ה') and w[1:] in ('קרוב', 'בא', 'באה', 'קרובה', 'אחרון') else w
    return '' if w in ('קרוב', 'בא', 'באה', 'קרובה', 'אחרון', 'שלי', 'הבא', 'הקרוב') else w


def _infer_course(topic: str) -> str | None:
    """'רקורסיה' → 'intro_to_cs': the course whose notes the RAG search hits most for the topic."""
    from collections import Counter
    from config.paths import ACTIVE_COURSES
    try:                                            # 26/10: a unit of this semester's exam maps decides first
        from agent import exam_weight
        hit = exam_weight._resolve(topic)
        if hit:
            return hit[0]
    except Exception:
        pass
    try:
        from rag.vault_indexer import search
        keys = [h.get('course_key') for h in (search(topic, k=8) or []) if h.get('course_key')]
    except Exception:
        return None
    # "השפעות חיצוניות" hit micro A's old notes → practice from last semester; a successor / active course wins
    keys = [SUCCESSOR.get(k, (k,))[0] for k in keys]
    keys = [k for k in keys if k in ACTIVE_COURSES] or keys
    if not keys:
        return None
    ck, n = Counter(keys).most_common(1)[0]
    return ck if n * 2 >= len(keys) else None


MOVE_VERB = re.compile(r'^\s*(?:תעביר|תעבירי|תזיז|תזיזי|תדחה|תדחי|תקדים|תקדימי)\s+(?:את\s+)?(.+?)\s*$')


def _split_move(text: str):
    """'תעביר את הטיול לצפון ליום חמישי' → ('הטיול לצפון', date). Splits at the LAST ' ל…' whose rest
    is a day — a non-greedy regex cut "לצפון" off the title."""
    from agent.daytools import parse_day
    m = MOVE_VERB.match(text or '')
    if not m:
        return None
    body = m.group(1)
    for k in sorted((i for i in range(len(body)) if body.startswith(' ל', i) or body.startswith(' אל ', i)), reverse=True):
        day = parse_day(body[k:])
        if day:
            return body[:k].strip(), day
    return None
DELETE_RX = re.compile(r'^\s*(?:תמחק|תמחקי|מחק|תוריד|תורידי|תבטל|תבטלי)\s+(?:מהיומן\s+)?(?:את\s+)?(.+?)(?:\s+מ(?:ה)?יומן)?\s*$')
MSG_TO_PARTNER = re.compile(r'^\s*(?:תשלח|תגיד|תכתוב|תודיע)\s+ל(?:בת הזוג)\s+(?:ש)?(.+)$', re.S)
# 25/10 (The partner's birthday): "תכתוב לה בת הזוג מזל טוב" (voice), "הודעה לבת הזוג מזל טוב", "הודעה לבת הזוג: …" matched nothing →
# the assistant answered "מעביר לבת הזוג עכשיו! 🎉" three times and nothing was sent. Any of these is a message to the partner.
PARTNER_MSG_RX = re.compile(
    r'^\s*(?:(?:תשלח|שלח|תגיד|תכתוב|כתוב|תודיע|תעביר|העבר|תאחל|תברך|ברך)(?:י)?\s+(?:הודעה\s+|ברכה\s+)?'
    r'(?:לבת הזוג|לה\s+בת הזוג|ל-בת הזוג|את\s+בת הזוג)\b|(?:הודעה|ברכה|ברכת\s+\S+)\s+לבת הזוג\b)\s*[:,\-–]?\s*(?:ש)?(.*)$', re.S)
GREETING_RX = re.compile(r'מזל\s*טוב|יום\s*הולדת|יומולדת|ברכה|ברכת|מושקע|יפה|חמה|ארוכה|תנסח|נסח|אוהב')
REDRAFT_RX = re.compile(r'(?:תשקיע|תנסח|נסח|תכתוב)\s+.{0,20}(?:יותר|אחרת|מחדש|מושקע)|(?:יותר|פחות)\s+(?:מושקע|ארוכ|קצר|רומנטי|מצחיק)')


def _draft_for_partner(request: str, previous: str = '', client=None) -> str:
    """"מזל טוב" / "ברכה מושקעת ליום ההולדת" → a warm message in the owner's own voice (one flash call)."""
    import os
    import google.genai as genai
    from google.genai import types as T
    client = client or genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    facts = ''
    try:
        from agent import life
        facts = '; '.join(life.facts(outbox_owner())[-15:])
    except Exception:
        pass
    prompt = (f"נסח הודעת וואטסאפ מ{OWNER_NAME} ל{PARTNER_NAME}, בת הזוג שלו. היום {date.today():%d/%m/%Y}.\n"
              f"מה הוא ביקש: \"{request}\"\n" + (f"הנוסח הקודם (הוא ביקש אחר / מושקע יותר): \"{previous}\"\n" if previous else '')
              + (f"מה ידוע עליו: {facts}\n" if facts else '')
              + "בגוף ראשון, כאילו הוא כותב. חם, אישי וטבעי — לא קלישאות של כרטיס ברכה. עד 5 שורות, 1–2 אימוג'י. "
                "אל תמציא עובדות או אירועים (איחור, פספוס, תוכניות לערב, מתנות) — רק מה שהוא ביקש ומה שידוע. "
                "החזר רק את ההודעה עצמה.")
    resp = client.models.generate_content(
        model='gemini-3.8-flash', contents=prompt,
        config=T.GenerateContentConfig(temperature=0.8, max_output_tokens=800,
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    try:
        from utils.usage_tracker import log_gemini
        log_gemini(resp, 'gemini-3.8-flash', op='agent.partner_draft')
    except Exception:
        pass
    return (resp.text or '').strip().strip('"')


def _partner_message(u: dict, text: str) -> list['Out'] | None:
    """A message for the partner → the exact text + "📤 שלח" (nothing goes out without that tap). A greeting request is drafted."""
    m = PARTNER_MSG_RX.match(text)
    if not m:
        return None
    body = (m.group(1) or '').strip()
    if not body or (GREETING_RX.search(body) and len(body.split()) <= 8):
        try:
            draft = _draft_for_partner(body or 'ברכה')
        except Exception as e:
            print(f'  ⚠️ partner draft failed: {e!r}'[:200])
            draft = ''
        if draft:
            u['partner_draft'] = {'request': body or 'ברכה', 'text': draft, 'ts': datetime.now().isoformat(timespec='seconds')}
            return _offer_message(u, 'partner', draft, OWNER_NAME, redo=True)
    return _offer_message(u, 'partner', body or text, OWNER_NAME)
MSG_TO_OWNER = re.compile(r'^\s*(?:תשלחי|תגידי|תכתבי|תודיעי|תשלח|תגיד|תכתוב|תודיע)\s+למשתמש\s+(?:ש)?(.+)$', re.S)


SITE_RX = re.compile(r'^\s*(?:(?:תשלח|שלח|תן|תביא)\s+(?:לי\s+)?(?:את\s+)?(?:ה)?(?:לינק|קישור)\s+(?:ל|של\s+)?)?'
                     r'(?:ה)?אתר(?:\s+(?:של\s+)?(?:ה)?(?:קורסים|לימודים))?\s*[?!.]*\s*$')


REMOVE_RX = re.compile(r'(?:תסיר|תוציא|הסר|תמחק)\s+(?:את\s+)?(.{1,30}?)\s+מה?בוט')


def _invite(msg: In, text: str) -> list[Out] | None:
    """Owner only, in his own chat: a one-time join code (to forward), or taking someone out again."""
    if msg.role != 'owner' or msg.group or msg.forwarded or msg.kind != 'text':
        return None
    from whatsapp import invites
    m = REMOVE_RX.search(text)
    if m:
        audit(msg.sender, 'invite_remove', {'name': m.group(1)})
        return [Out('text', invites.remove(m.group(1).strip()))]
    name = invites.wants(text)
    if name is None:
        return None
    code = invites.create(name, msg.sender)
    if not code:
        return [Out('text', f'⚠️ יש כבר {invites.MAX_OPEN} קודים פתוחים שעוד לא נוצלו — חכה שינוצלו או שיפוגו (24 שעות).')]
    audit(msg.sender, 'invite_created', {'name': name})
    to_owner, fwd = invites.messages(name, code, invites.bot_number())
    return [Out('text', to_owner), Out('text', fwd)]


def _web_direct(msg: In, u: dict, text: str) -> list[Out] | None:
    """The browser agent: a new request, "עצור", or the answer to the question it asked (owner only)."""
    if msg.role != 'owner' or msg.forwarded:
        return None
    from agent import web_agent
    live = [j for j in web_agent.jobs() if j.get('chat') == msg.sender and j.get('status') in ('queued', 'running', 'ask', 'confirm')]
    if live and web_agent.STOP_RX.search(text):
        for j in live:
            j['status'] = 'stopped'
            web_agent.save(j)
        audit(msg.sender, 'web_stop', {'ids': [j['id'] for j in live]})
        return [Out('text', '⏹️ עצרתי. לא נשלח כלום באתר.')]
    if web_agent.wants(text):
        from agent import secretary
        url = web_agent.url_of(text, (secretary.current(u) or {}).get('url', ''))
        jid, say = web_agent.start(msg.sender, text, url)
        audit(msg.sender, 'web_start', {'id': jid, 'url': url})
        return [Out('text', say)]
    w = web_agent.waiting(msg.sender)
    if w and text and not text.startswith(('קלוד', 'claude')) and len(text) <= 300 and '?' not in text:  # a question = for the bot
        audit(msg.sender, 'web_answer', {'id': w['id']})
        return [Out('text', web_agent.answer(msg.sender, w['id'], 'text', text))]
    return None


def _direct(msg: In, u: dict) -> list[Out] | None:
    """Requests with one obvious meaning, handled without the classifier (it mis-routed all of these)."""
    text = (msg.text or '').strip()
    me = _booking_me(msg, u, text)                                 # "ישראל ישראלי 050-…" for a pending booking
    if me is not None:
        return me
    inv = _invite(msg, text)                                       # 03/11 "תוסיף את יוסי לבוט" → a one-time code to the owner
    if inv is not None:
        return inv
    web = _web_direct(msg, u, text)                                # 03/11 "תרשום אותי לכנס <link>" (agent/web_agent)
    if web is not None:
        return web
    from agent import booking
    if booking.BOOK_RX.search(text) and not booking.DONE_RX.search(text):   # "תזמין לי ולבת הזוג מקום למסעדת a…"
        return _booking(msg, u, text)
    coord = _coord_owner(msg, u, text, {msg.sender: u})          # "תתאם עם יוסי…", "ליוסי: …", "מה המצב עם יוסי?"
    if coord is not None:
        return coord
    evrem = _event_reminder_cmd(text)                              # "תזכיר לי על המסעדה יום לפני"
    if evrem is not None:
        return evrem
    if SITE_RX.search(text):                                       # 29/10: the private course site
        from scripts.build_course_site import URL
        return [Out('text', f'🌐 אתר הקורסים שלך (מתעדכן כל לילה):\n{URL}\nנפתח רק במכשיר שמחובר ל-Tailscale.')]
    if is_exam_time_q(text):
        return _exam_time(text, u=u)
    from integrations import tau_portal as _tp
    if _tp.GRADE_Q.search(text):
        return [Out('text', _tp.grades_answer(text))]
    if BRIEF_OFF.search(text):
        u['brief_off'] = True
        return [Out('text', '🔕 בסדר — בלי הודעת בוקר. "תפעיל הודעת בוקר" כדי להחזיר.')]
    if BRIEF_ON.search(text):
        u['brief_off'] = False
        return [Out('text', '☀️ הודעת הבוקר חזרה — כל יום א׳–ו׳ ב-07:30.')]
    if re.fullmatch(r'\s*(?:מה|תן לי את)?\s*(?:ה)?(?:סיכום|תדריך|הודעת) (?:ה)?בוקר\??\s*', text):
        return [Out('text', morning_brief_text(chat=msg.sender, u=u))]
    from integrations import tau_mail as _tm
    if _tm.MAIL_Q.search(text) and not MAIL_DRAFT.search(text) and not _mind_on(msg):   # "מה כתב המרצה בתוכנה 1?"
        if not _tm.configured():
            return [Out('text', '📧 המייל האוניברסיטאי עוד לא מחובר — צריך אישור חד-פעמי ממך (scripts/tau_mail_auth.py).')]
        try:
            ctx = _last_question(u) if BARE_MAIL.fullmatch(text.strip()) else ''
            return [Out('text', _tm.answer(text, context=ctx))]
        except Exception as e:
            print(f'  ⚠️ mail answer failed: {e}')
            return [Out('text', '⚠️ לא הצלחתי לקרוא את המייל כרגע. נסה שוב עוד מעט.')]
    if TT_OFF.search(text):
        u['timetable_sync'] = False
        return [Out('text', '⏹️ הפסקתי לסנכרן את מערכת השעות. מה שכבר ביומן נשאר.')]
    if TT_ON.search(text):
        return [_timetable_offer(u)]
    if (pm := _partner_message(u, text)) is not None:
        return pm
    if re.search(r'מתי\s+(?:מגישים|להגיש|צריך להגיש|ההגשה|הדדליין)|עד מתי\s+(?:להגיש|מגישים)|מה הדדליין', text):
        if (one := _one_deadline(text)) is not None:   # 26/10: answered from the calendar ("עד 22:00") instead
            return [Out('text', one)]
    d = u.get('partner_draft') or {}
    if d and REDRAFT_RX.search(text) and datetime.now() - datetime.fromisoformat(d['ts']) < timedelta(minutes=30):
        try:                                           # "תשקיע בברכה יותר מושקעת" → a new draft, same ✅
            new = _draft_for_partner(f"{d['request']} — {text}", d['text'])
        except Exception as e:
            print(f'  ⚠️ partner redraft failed: {e!r}'[:200])
            new = ''
        if new:
            u['partner_draft'] = {**d, 'text': new, 'ts': datetime.now().isoformat(timespec='seconds')}
            return _offer_message(u, 'partner', new, OWNER_NAME, redo=True)
    if m := MSG_TO_PARTNER.match(text):
        return _offer_message(u, 'partner', m.group(1), f'{OWNER_NAME}')
    if re.search(r'תזכורת', text):
        return None                                   # reminders: agent/life.py handles cancel/list
    from integrations import gcal
    if not gcal.configured():
        return None
    from agent import calendar_edit
    if calendar_edit.HINT.search(text) and not _split_move(text):   # "החלפנו את המסעדה של אבא בללכת למסעדה-ב"
        try:
            p = calendar_edit.plan(text, calendar_edit.upcoming())
        except Exception as e:
            print(f'  ⚠️ calendar edit failed: {e}')
            p = None
        if p and calendar_edit.describe(p):
            return [_edit_offer(u, p)]
    from agent.daytools import parse_day
    if mv := _split_move(text):
        what, new_day = mv
        hits = gcal.find_events(what)
        if not hits:
            return [Out('text', f'🤔 לא מצאתי ביומן אירוע בשם "{what}".')]
        ev = hits[0]
        pid = secrets.token_hex(3)
        u.setdefault('edits', {})[pid] = {'kind': 'move', 'event': ev, 'day': new_day.isoformat(),
                                          'ts': datetime.now().isoformat(timespec='seconds')}
        return [Out('buttons', f"📅 *{ev.get('summary', '')}*\n{gcal.when_text(ev)}  →  "
                               f"יום {WEEKDAY_HE_[new_day.weekday()]} {new_day:%d/%m}\n\nלהעביר?",
                    buttons=[('✅ להעביר', f'ce:ok:{pid}'), ('❌ לא', f'ce:no:{pid}')])]
    if (m := DELETE_RX.match(text)) and ('יומן' in text or gcal.find_events(m.group(1))):
        hits = gcal.find_events(m.group(1))
        if not hits:
            return [Out('text', f'🤔 לא מצאתי ביומן אירוע בשם "{m.group(1)}".')]
        ev = hits[0]
        pid = secrets.token_hex(3)
        u.setdefault('edits', {})[pid] = {'kind': 'delete', 'event': ev, 'ts': datetime.now().isoformat(timespec='seconds')}
        more = f'\n(יש עוד {len(hits) - 1} דומים — אמחק רק את הזה)' if len(hits) > 1 else ''
        return [Out('buttons', f"🗑️ *{ev.get('summary', '')}* — {gcal.when_text(ev)}{more}\n\nלמחוק מהיומן?",
                    buttons=[('🗑️ למחוק', f'ce:ok:{pid}'), ('❌ לא', f'ce:no:{pid}')])]
    return None


WEEKDAY_HE_ = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']


def _offer_message(u: dict, to_role: str, body: str, from_name: str, redo: bool = False) -> list[Out]:
    from whatsapp import outbox
    to = outbox.owner_chat() if to_role == 'owner' else outbox.partner_chat()
    if not to:
        return [Out('text', 'אין לי למי לשלוח את זה כרגע.')]
    pid = secrets.token_hex(3)
    text = f'💬 {from_name}: {body.strip()}'
    who = OWNER_NAME if to_role == 'owner' else PARTNER_NAME
    u.setdefault('msgs', {})[pid] = {'to': to, 'text': text, 'who': who, 'ts': datetime.now().isoformat(timespec='seconds')}
    buttons = [('📤 שלח', f'msg:send:{pid}'), ('❌ לא', f'msg:no:{pid}')]
    if redo:
        buttons.insert(1, ('✏️ נוסח אחר', f'msg:redo:{pid}'))
    return [Out('buttons', f'לשלוח ל{who}:\n"{text}"', buttons=buttons)]


def _confirm_action(msg: In, u: dict, p: str) -> list[Out]:
    _, op, pid = (p.split(':', 2) + ['', ''])[:3]
    if p.startswith('msg:'):
        item = (u.get('msgs') or {}).pop(pid, None)
        if not item:
            return [Out('text', 'כבר לא בתוקף.')]
        if op == 'redo' and u.get('partner_draft'):
            d = u['partner_draft']
            try:
                new = _draft_for_partner(d['request'], d['text'])
            except Exception as e:
                print(f'  ⚠️ partner redraft failed: {e!r}'[:200])
                new = ''
            if new:
                u['partner_draft'] = {**d, 'text': new, 'ts': datetime.now().isoformat(timespec='seconds')}
                return _offer_message(u, 'partner', new, OWNER_NAME, redo=True)
            return [Out('text', '⚠️ לא הצלחתי לנסח מחדש — אפשר לכתוב את הנוסח בעצמך: "תשלח לבת הזוג ש…".')]
        if op != 'send':
            return [Out('text', '❌ לא נשלח.')]
        from whatsapp import outbox
        ok = outbox.send_text(item['to'], item['text'])
        audit(msg.sender, 'message_sent' if ok else 'message_failed', {'text': item['text'][:200]})
        return [Out('text', '📤 נשלח.' if ok else '⚠️ השליחה נכשלה.')]
    item = (u.get('edits') or {}).pop(pid, None)
    if not item:
        return [Out('text', 'הבקשה כבר לא בתוקף — בקש שוב.')]
    if op != 'ok':
        return [Out('text', '👍 לא שיניתי כלום.')]
    if u.get('paused'):
        return [Out('text', '⏸️ הסוכן עצור — לא משנה את היומן.')]
    from integrations import gcal
    ev = item['event']
    if item['kind'] == 'edit':
        from agent import calendar_edit
        if _already_applied(ev, item['patch']):             # 18/10: one change was written 4 times
            return [Out('text', f"👍 כבר מעודכן ביומן: {ev.get('summary', '')}")]
        new = calendar_edit.apply({'event': ev, **item['patch']})
        for other in item.get('also') or []:                # the same new room for the rest of that lesson's series
            try:
                calendar_edit.apply({'event': other, **item['patch']})
            except Exception as e:
                print(f'  ⚠️ series edit failed: {e}')
        audit(msg.sender, 'calendar_edit', {'id': ev.get('id'), 'from': ev.get('summary'), 'patch': item['patch'],
                                            'also': len(item.get('also') or [])})
        from agent import journal
        journal.record(msg.sender, 'cal_edit', ev.get('summary', ''), confirm=True, inverse={'items': [
            {'old': {k: ev.get(k) for k in ('summary', 'location', 'start', 'end')}, 'new': {**new, '_cal': ev.get('_cal', 'primary')}}]})
        return [Out('text', f"✅ עודכן: {new.get('summary', '')} — {gcal.when_text(new)}"
                            + (f" · 📍 {new['location']}" if new.get('location') else ''))]
    if item['kind'] == 'move':
        moved = gcal.move_event(ev, date.fromisoformat(item['day']))
        audit(msg.sender, 'calendar_move', {'id': ev.get('id'), 'title': ev.get('summary'), 'to': item['day']})
        old_day = str((ev.get('start') or {}).get('dateTime') or (ev.get('start') or {}).get('date') or '')[:10]
        if old_day and isinstance(moved, dict) and moved.get('id'):
            from agent import journal
            journal.record(msg.sender, 'cal_move', ev.get('summary', ''), confirm=True,
                           inverse={'event': {**moved, '_cal': ev.get('_cal', 'primary')}, 'day': old_day})
        return [Out('text', f"✅ הועבר: {ev.get('summary', '')} → {date.fromisoformat(item['day']):%d/%m}")]
    gcal.delete_event(ev['id'], calendar=gcal.cal_of(ev))
    audit(msg.sender, 'calendar_delete', {'id': ev.get('id'), 'title': ev.get('summary')})
    from agent import journal
    journal.record(msg.sender, 'cal_delete', ev.get('summary', ''))
    return [Out('text', f"🗑️ נמחק מהיומן: {ev.get('summary', '')}")]


def _hint(u: dict) -> str:
    """Mid-session only (05/11): a session idle for 2h+ isn't brought up under every reply."""
    t = u.get('tutor') or {}
    if not t.get('active'):
        return ''
    from agent import study_push, tutor
    if not study_push.on() and tutor.idle_hours(t) > 2:
        return ''
    return '\n\n📚 החזרה מחכה — כתוב "נמשיך" כשבא לך.'


def _hint_out(u: dict) -> list[Out]:
    h = _hint(u)
    return [Out('text', h.strip())] if h else []


# 04/11 review: The owner sees the question, not the command, and the model that writes it reads calendar / Moodle titles. Some
# router paths act with no ✅ ("קלוד:", the browser agent, removing someone from the bot, a relay to a contact) — so a "כן"
# may only run a read / study request, by its first word, and nothing with a link or an "X:" relay.
FOLLOW_SAFE = re.compile(r'^\s*(?:תעשה\s+לי\s+(?:חזרה|תוכנית|תרגול|סיכום)|תבנה\s+לי\s+תוכנית|תן\s+לי\s+(?:תרגיל|שאל|סיכום|חזרה)|'
                         r'תתרגל|תסכם|תסביר|תראה\s+לי|מה|מי|מתי|איפה|כמה|איזה|אילו)(?=\s)')


STUDY_FOLLOW = re.compile(r'חזרה|תרגול|תרגיל|לתרגל|נתרגל|תתרגל|שאלות?\s+(?:על|ב|מבחן)|בחן\s+אות|תבחן|חידון|תוכנית\s+(?:לימוד|למידה)')
BARE_MAIL = re.compile(r'(?:תבדוק|בדוק|תחפש|חפש|תסתכל|תראה|תיכנס|לך|כתוב|זה|יש)?\s*(?:לי\s+)?(?:גם\s+)?(?:ב|ל|את\s+ה)?(?:אי)?מייל(?:ים)?\s*[.!?]?')


TASK_DETAIL = re.compile(r'(?<![א-ת])(?:איזה|אילו|כמה|מה|מתי|עד מתי|איפה|למי|לאן|מי)(?![א-ת])')


THIN_REPLY = re.compile(r'לא\s+(?:מופיע|מופיעה|מצוין|מצוינ\S*|רשום|רשומה|כתוב|ידוע)|אין\s+(?:לי|שם|ביומן)\s+(?:את\s+)?\S*\s*'
                        r'(?:שם|מידע|פרטים|נתונים|חדר|מיקום)|לא\s+מצאתי|שווה\s+לבדוק')


def _about_mail_task(msg: In, q: str) -> bool:
    from agent import tasks
    return bool(TASK_DETAIL.search(q)) and any(t.get('src') for t in tasks.about(msg.sender, q))


def _dig(msg: In, d: dict, q: str) -> str | None:
    """The model answered in words but the real source wasn't read. 04/11 "איזה ספרים להחזיר": a task's title instead of
    its mail. 23/10 "איפה ההרצאה": "לא מופיע, שווה לבדוק במודל". An assistant goes and looks before saying that."""
    if d.get('kind') not in ('chat', 'unclear') or msg.role != 'owner' or msg.group or d.get('asks_action'):
        return None
    if _about_mail_task(msg, q):
        found = _mail_lookup(q, msg.sender)
        if found:
            return found
    ck = _course_in_text(q) if THIN_REPLY.search(d.get('reply') or '') else None
    if ck:
        from agent import course_lookup
        return course_lookup.find(q, ck, '')
    return None


def _task_hints(chat: str, text: str) -> list[str]:
    """Gmail queries for the open tasks this question is about: the sender when it's an address, the task's words."""
    from agent import tasks
    out = []
    for t in tasks.about(chat, text):
        src = str(t.get('src') or '')
        out.append(f'from:{src}' if re.fullmatch(r'[\w.@-]+', src) else ' '.join(t.get('text', '').split()[:4]))
    return list(dict.fromkeys(out))[:2]


def _mail_lookup(text: str, chat: str = '') -> str | None:
    """The mailbox's answer, or None (not connected / nothing there) and the caller goes on."""
    from integrations import tau_mail
    if not tau_mail.configured():
        return None
    try:
        found = tau_mail.answer(text, hints=_task_hints(chat, text) if chat else None)
        return None if found.startswith('🔎') else found
    except Exception as e:
        print(f'  ⚠️ mail lookup failed: {e!r}'[:200])
        return None


def _last_question(u: dict) -> str:
    """The owner's last message before this one, within an hour — what a bare "תבדוק במייל" is about."""
    now = datetime.now()
    for h in reversed(u.get('history') or []):
        if h['role'] != 'user' or BARE_MAIL.fullmatch(h['text'].strip()):
            continue
        try:
            return h['text'] if now - datetime.fromisoformat(h.get('ts', '')) < timedelta(hours=1) else ''
        except ValueError:
            return ''
    return ''


def _follow_ok(command: str) -> bool:
    c = command.strip()
    return len(c.split()) >= 2 and bool(FOLLOW_SAFE.match(c)) and not re.search(r'https?://|www\.|[:：]', c)


def _study_follow(follow: dict) -> bool:
    """05/11: the model likes "רוצה תוכנית חזרה עד אז?" after an exam date. Learning content is on request (agent/study_push)."""
    from agent import study_push
    return not study_push.on() and bool(STUDY_FOLLOW.search(f"{follow.get('ask', '')} {follow.get('command', '')}"))


def _follow_is_latest(fu: dict, chat: str) -> bool:
    """The "כן" answers the bot's latest message: a proactive note or a ✅ prompt sent since then means it's about that."""
    try:
        from whatsapp import outbox
        if outbox.options_open(chat):                    # a numbered question is waiting — the "כן" is about it
            return False
        sent = outbox.recent_sent(chat, n=1)
    except Exception:
        return False
    return not sent or fu.get('ask', '') in sent[-1].get('text', '')


def _route(msg: In, u: dict, st: dict | None = None) -> list[Out]:
    text = (msg.text or '').strip()
    if msg.kind in ('text', 'other') and not text and not msg.payload and not msg.media:
        # 15/11 16:06 an edit of "חטפתי את המלון" (a protocol message: no text) got the whole help menu. Nothing to answer
        return []
    if msg.kind == 'contact' and not msg.text:
        return [Out('text', '📇 קיבלתי איש קשר ריק, בלי שם או מספר.')]
    if msg.kind == 'unsupported':
        what = text if text in ('סרטון', 'מיקום', 'סקר') else 'את סוג ההודעה הזה'
        return [Out('text', f'🙈 {what} אני עוד לא יודע לקרוא. אפשר לכתוב לי במילים מה צריך?')]
    # the assistant's one offered next step is for the very next message only — whatever handles that message
    fu = u.pop('follow_up', None)
    if isinstance(fu, dict) and fu.get('command') and msg.kind == 'text' and msg.role == 'owner' and not msg.forwarded \
            and YES_RX.match(text) and (datetime.now() - datetime.fromisoformat(fu['ts'])).total_seconds() < FOLLOW_UP_SECONDS \
            and _follow_is_latest(fu, msg.sender):
        from dataclasses import replace as _replace          # "כן" → that request, shown, through the whole router
        return [Out('text', f'▶️ "{fu["command"]}"')] + _route(_replace(msg, text=fu['command']), u, st)
    if text in STOP_WORDS and not msg.forwarded:
        u['paused'] = True
        audit(msg.sender, 'pause', {})
        return [Out('text', '⏸️ הסוכן עצר — לא אבצע שום פעולה (יומן וכו׳) עד "הפעל סוכן". שאלות לימוד עדיין עונה.')]
    if text in START_WORDS and not msg.forwarded:
        u['paused'] = False
        audit(msg.sender, 'resume', {})
        return [Out('text', '▶️ הסוכן פעיל שוב.')]
    if msg.kind == 'button':
        outs = _button(msg, u, st)
        if msg.text and outs and outs[0].kind == 'text':      # answering an older question → say which
            outs[0] = Out('text', f'(לגבי: {msg.text})\n{outs[0].text}', outs[0].path, outs[0].buttons)
        return outs
    if msg.kind == 'text' and msg.role == 'owner' and not msg.forwarded:          # 10/11 the bot's own alert pasted back
        from agent import known_alerts                                             # → the real steps, not a Claude job
        if how := known_alerts.how_to(text):
            return [Out('text', how)]
    if msg.kind == 'text' and msg.channel == 'whatsapp' and not msg.forwarded:   # 05/11 "מה קלוד עושה", "תעצור את קלוד"…
        from agent import claude_job
        said = claude_job.control(text, bare=not (u.get('quiz') or (u.get('tutor') or {}).get('active')))
        if said:
            if said[0] != 'status':
                audit(msg.sender, f'claude_job_{said[0]}', {})
            return [Out('text', said[1])]
    if msg.kind == 'text' and re.fullmatch(r'\s*\d{1,2}\s*\.?\s*', text) and not u.get('quiz') \
            and not (u.get('tutor') or {}).get('active') and not u.get('cal_draft_text'):   # a number with nothing open
        if _mind_on(msg) and (outs := _mind_turn(msg, u, text)):   # it read the recent messages: it can say which question
            return outs
        return [Out('text', f'🤔 לא בטוח למה "{text.strip()}" מתייחס — השאלה עם האפשרויות כבר לא פעילה. '
                            'תכתוב במילים מה רצית?')]
    if msg.kind == 'text' and ('השב במספר' in text or 'השב באות' in text or text.startswith('↩️ *עוד שאלה פתוחה')):
        # 24/10: the owner pasted the bot's own question back (its "1" had expired) → it was read as a link to summarise
        from whatsapp import outbox
        live = outbox.options_open(msg.sender) or bool(outbox._open_list(
            json.loads(outbox.OPTIONS.read_text(encoding='utf-8')) if outbox.OPTIONS.exists() else {}, msg.sender))
        return [Out('text', '🙂 זו ההודעה שלי. כדי לענות עליה — כתוב רק את המספר (למשל 1).' if live else
                    '⌛ השאלה הזו כבר לא פעילה אצלי. כתוב במילים מה רצית (למשל "תוסיף ליומן הגשת תרגיל 2 ב-3/11") ואעשה.')]
    if msg.kind == 'text' and msg.forwarded:
        return _forwarded(msg, u, text)
    if msg.kind == 'text' and (fa := _forwarded_act(msg, u, text)) is not None:
        return fa
    if msg.kind == 'text':
        from agent import feedback                             # 👎 / "לא נכון" → the next daily review, first
        if feedback.is_feedback(text):
            return [Out('text', feedback.record(u, text))]
        if (fb := feedback.add_comment(u, text)) is not None:
            return [Out('text', fb)]
        if msg.role == 'owner' and not msg.group and feedback.is_never_again(text):   # 16/11 "יופי שלא יקרה שוב"
            said = feedback.never_again(list(u.get('history') or []), text)
            audit(msg.sender, 'never_again', {'text': text[:120]})
            return [Out('text', said)]
        try:                                                   # "לא, התכוונתי…" / the same question again → review
            feedback.implicit(text)
        except Exception as e:
            print(f'  ⚠️ implicit feedback failed: {e}')
        from agent import secretary
        if secretary.URL_RX.search(text):                      # a link → read it (the question is the rest)
            before = (u.get('doc') or {}).get('ts')
            return _keep_file(msg, u, before, _link(u, text))
        tutor_outs = _tutor(u, text, msg.sender)
        if tutor_outs is not None:
            return tutor_outs
        if msg.role == 'owner' and not msg.group and _study_on():   # 19/11 "על מה אני צריך להתמקד?" / "מה המשוב בתוכנה?"
            try:                                               # his grades + the grader's feedback (agent/grades), no model
                from agent import grades
                if (gr := grades.answer(text)) is not None:
                    return [Out('text', gr)]
            except Exception as e:
                print(f'  ⚠️ grades: {e!r}'[:200])
        if msg.role == 'owner' and not msg.group and _study_on():   # 18/11 "על מה לשים את הכסף בהסתברות?": past exams, no model
            try:
                from agent import exam_plan
                if (ep := exam_plan.answer(text)) is not None:
                    return [Out('text', ep)]
            except Exception as e:
                print(f'  ⚠️ exam plan: {e!r}'[:200])
        if CAPABILITIES.search(text) and not CAN_MSG_RX.match(text) and not _mind_on(msg):   # never let the RAG guess
            return [Out('text', menu() + _hint(u))]
        if THANKS.fullmatch(text):
            return [Out('text', 'בכיף 🙂' + _hint(u))]
        if (fp := _fast(msg, u, text)) is not None:
            return fp
        try:
            pl = _plan_and_learn(msg, u, text)
        except Exception as e:
            print(f'  ⚠️ planner failed: {e!r}'[:200])
            pl = None
        if pl is not None:
            return pl
        direct = _direct(msg, u)
        if direct is not None:
            return direct + _hint_out(u)
        life_outs = _life(msg, u, text)
        if life_outs is not None:
            return life_outs
        from agent import exam_weight                          # 18/10: "is this topic worth it for the exam?"
        if (eq := exam_weight.exam_question(text, u)) or (eq := exam_weight.exam_solution(text, u)):
            return [Out('text', eq)]
        if exam_weight.RX.search(text):
            ans = exam_weight.answer(text)
            if ans:
                return [Out('text', ans)]
        if secretary.is_followup(u, text, msg.reply_to) and (secretary.current(u) or {}).get('nlm_source'):
            d = secretary.current(u)
            try:
                from agent import nlm_docs
                ans, conv = nlm_docs.ask(text, [d['nlm_source']], d.get('nlm_conv'))
                d['nlm_conv'] = conv or d.get('nlm_conv')
                return [Out('text', f'📎 {ans}')]
            except Exception as e:
                print(f'  ⚠️ NotebookLM follow-up failed: {e!r}'[:200])
        if (cf := _course_file(msg, text)) is not None:       # 03/11 eval: "יש לך את המצגת של שיעור 3 במאקרו?"
            return cf
        from agent import nlm_docs as _nd, nlm_ask as _na
        from agent import brain as _br                         # the brain notebook: documents + mail + facts (free)
        if not _mind_on(msg) and not secretary.current(u) and (_nd.RECALL_RX.search(text) or _br.ASK_RX.search(text)) \
                and _br.has_sources() and not _na._broken_recently():                  # "מה היה כתוב בסילבוס ששלחתי?" days later
            try:
                ans, _c = _nd.ask(text)
                return [Out('text', f'📎 {ans}')]
            except Exception as e:
                print(f'  ⚠️ NotebookLM recall failed: {e!r}'[:200])
        if secretary.is_followup(u, text, msg.reply_to):      # "ומה כתוב שם על…" about the last link/file
            d = secretary.current(u)
            if d.get('url') and not d.get('path'):
                try:
                    return [Out('text', f"🔗 {secretary.ask_url(d['url'], text)}")]
                except Exception as e:
                    return [Out('text', f'🔗 {e}')]
            return _secretary(u, Path(d['path']), d['name'], text)[:-1]
    from agent import claude_job
    task = claude_job.task_of(text) if msg.channel == 'whatsapp' and not msg.forwarded else None
    if task:                                   # "קלוד: …" → Claude Code on the project (owner only)
        jid, reply = claude_job.start(msg.sender, task)
        audit(msg.sender, 'claude_job_start', {'id': jid, 'task': task[:300]})
        return [Out('text', reply)]
    outs: list[Out] = []
    if msg.kind == 'voice':
        text = transcribe(msg.media) if msg.media else ''
        if not text:
            return [Out('text', '🎤 לא הצלחתי לשמוע את ההקלטה. נסה שוב או כתוב.')]
        outs.append(Out('text', f'🎤 "{text}"'))
        tutor_outs = None if msg.forwarded else _tutor(u, text)
        if tutor_outs is not None:
            return outs + tutor_outs
        # 26/10: a voice note skipped every command (they run for kind 'text' only) and went straight to the free
        # assistant — "תכתוב לה בת הזוג מזל טוב" (25/10) never reached the message-to-The partner path. Same route as typing it.
        if not u.pop('_voice_reroute', False):
            u['_voice_reroute'] = True
            try:
                return outs + _route(In(msg.sender, 'text', text=text.strip().rstrip('.'), channel=msg.channel,
                                        role=msg.role, reply_to=msg.reply_to, group=msg.group, context=msg.context,
                                        forwarded=msg.forwarded), u, st)
            finally:
                u.pop('_voice_reroute', None)
    if msg.kind == 'image':
        before = (u.get('doc') or {}).get('ts')                # a forwarded caption is content, not what the owner asks
        return _keep_file(msg, u, before, _image(msg, '' if msg.forwarded else text, u))
    if msg.kind == 'contact':                                # the owner shared a contact card
        from agent import contacts
        name, phone = contacts.from_vcard(*((msg.text or '') + '\n').split('\n', 1))
        if not name or not phone:
            return [Out('text', '📇 לא מצאתי באיש הקשר מספר וואטסאפ שאפשר לכתוב אליו.' if name else
                                '📇 לא הצלחתי לקרוא את איש הקשר.')]
        if not contacts.add(name, phone):
            return [Out('text', '📇 לא הצלחתי לשמור את איש הקשר.')]
        # 15/11 "תיצור איתם קשר ותבטל את המלון" a minute after a card: "איתם" is this card (_reach)
        u['last_contact'] = {'name': name, 'ts': datetime.now().isoformat(timespec='seconds')}
        wait = u.pop('coord_wait', None)
        if wait and (datetime.now() - datetime.fromisoformat(wait['ts'])).total_seconds() < 1800:
            contacts.add(wait['name'] or name, phone)
            return [Out('text', f'📇 שמרתי את {name}.')] + _coord_draft(msg, u, wait['text'])
        return [Out('text', f'📇 שמרתי את {name}. אפשר לכתוב "תתאם עם {name.split()[0]} …".')]
    if msg.kind == 'document':
        from agent import secretary
        if not msg.media:
            return [Out('text', '📎 לא הצלחתי להוריד את הקובץ. נסה לשלוח שוב.')]
        media = Path(msg.media)
        if media.suffix.lower() == '.zip':
            media = _unzip_chat(media) or media
            if media.suffix.lower() == '.zip':
                return [Out('text', '📎 בקובץ ה-zip אין שיחה שאני יודע לקרוא (ייצוא צ\'אט בלי מדיה עובד הכי טוב).')]
        bad = secretary.file_ok(media)
        if bad:
            return [Out('text', f'📎 {bad}')]
        q = '' if msg.forwarded else text
        if not q and media.suffix.lower() == '.txt':
            try:
                if len(CHAT_LINE.findall(media.read_text(encoding='utf-8', errors='ignore')[:20000])) >= 5:
                    q = CHAT_Q                                   # a WhatsApp chat export → summarise the conversation
            except OSError:
                pass
        before = (u.get('doc') or {}).get('ts')
        return _keep_file(msg, u, before, _secretary(u, media, media.name.split('__', 1)[-1], q))
    if not text:
        return [Out('text', menu())]
    if u.get('quiz') and re.fullmatch(r'[אבגדabcd1-4]', text.lower().strip('.!? ')):
        return outs + _quiz_answer(msg, u, text)
    return outs + _text(msg, u, text) + _hint_out(u)


# ── text → intent → action ──────────────────────────────────────────────────

SUCCESSOR = {'microeconomics': ('microeconomics_2', r"מיקרו\s*(?:כלכלה\s*)?א(?:['׳]|\b)|micro\s*a\b")}


def _this_semester(params: dict, text: str) -> dict:
    """23/10: "מה למדנו בהרצאה האחרונה במיקרו?" was answered from micro A (last semester's lecture 13) — the classifier
    picked 'microeconomics'. A finished course with a current successor means the successor, unless the owner named the
    old one ("מיקרו א׳")."""
    ck = (params.get('course') or '').strip()
    if ck in SUCCESSOR:
        new, old_rx = SUCCESSOR[ck]
        if not re.search(old_rx, text or '', re.I):
            return {**params, 'course': new}
    return params


def _course(params: dict) -> str | None:
    from config.paths import COURSE_NAMES
    c = (params.get('course') or '').strip()
    return c if c in COURSE_NAMES else None


def _history(u: dict) -> str:
    return '\n'.join(f"{h['role']}: {h['text']}" for h in (u.get('history') or [])[-6:])


def _remember(u: dict, role: str, text: str, others: bool = False) -> None:
    """others: the text rests on what the loop read (mail, the web), not on the owner's words alone."""
    row = {'role': role, 'text': text[:500], 'ts': datetime.now().isoformat(timespec='seconds')}
    u['history'] = ((u.get('history') or []) + [{**row, 'others': True} if others else row])[-30:]


def _remember_turn(msg: In, u: dict, outs: list) -> None:
    """05/11: only messages that reached the classifier entered the history, so what the planner, the link reader or the
    tutor answered was missing, and the next "ומה עם…?" had nothing to lean on."""
    text = (msg.text or '').strip()
    if msg.forwarded or msg.kind != 'text' or not text or any(h['text'] == text[:500] for h in (u.get('history') or [])[-4:] if h['role'] == 'user'):
        return                                                 # 05/11 forwarded = someone else's words, never the owner's
    _remember(u, 'user', text)
    for o in outs:
        if o.kind in ('text', 'buttons') and o.text:
            _remember(u, 'bot', o.text)


def _text(msg: In, u: dict, text: str) -> list[Out]:
    draft = u.pop('cal_draft_text', None)
    # 06/10 stress test: after one clarifying question EVERY later message ("תודה!", "מה מזג האוויר?")
    # was glued onto the old calendar draft. Only a short time/date reply within 15 min continues it.
    if isinstance(draft, dict):
        fresh = (datetime.now() - datetime.fromisoformat(draft['ts'])).total_seconds() < 900
        draft = draft['text'] if fresh else None
    from agent.daytools import looks_like_time_answer
    if draft and looks_like_time_answer(text):        # the ✅ step still guards the result
        return run_intent(msg, u, 'calendar_add', {}, f'{draft} — {text}')
    if _mind_on(msg):
        _TURN_START[id(msg)] = time.monotonic()
        outs = _mind_turn(msg, u, text)
        others = u.pop('_read_others', False)
        if outs is not None:
            _remember(u, 'user', text)
            for o in outs:
                if o.kind in ('text', 'buttons'):
                    _remember(u, 'bot', o.text, others)
            _TURN_START.pop(id(msg), None)
            return outs
    from bot.brain import classify
    _prefetch_decide(msg, u, text)
    try:
        intent = classify(text, _history(u))
        action, params = intent.get('action', 'ask'), intent.get('params') or {}
        if action in STUDY_ACTIONS and not _course(params) and (ck := _course_in_text(text)):
            params = {**params, 'course': ck}                 # 03/11 eval: "…במיקרו" came back with no course
        params = _this_semester(params, text)
        _remember(u, 'user', text)
        outs = run_intent(msg, u, action, params, text)
    finally:
        _TURN_START.pop(id(msg), None)
        left = _PREFETCH.pop(id(msg), None)           # not used (a command) → cancelled if it hasn't started
        if left:
            left[2].cancel()
    for o in outs:
        if o.kind in ('text', 'buttons'):
            _remember(u, 'bot', o.text)
    return outs


def _mind_on(msg: 'In') -> bool:
    """The owner himself, typing or speaking in his private chat. Groups, friends, partner, contacts and forwarded text never."""
    if msg.role != 'owner' or msg.group or msg.forwarded or msg.channel != 'whatsapp' or msg.kind != 'text':
        return False
    from agent import mind
    try:
        return mind.on() and mind.available()            # locked or capped: all of the old path, its guards included
    except Exception as e:
        print(f'  ⚠️ mind availability: {e!r}'[:200])
        return False


def _mind_turn(msg: 'In', u: dict, text: str) -> list['Out'] | None:
    """The agent loop's turn (agent/mind.py). None = not its message, or it failed: the classifier takes over."""
    from agent import mind
    quoted = ''
    if msg.reply_to and msg.role == 'owner' and msg.channel == 'whatsapp':
        try:                                              # 15/11 "מה השינוי ?" on a quoted notification
            from whatsapp import outbox
            quoted = outbox.quoted_text(msg.sender, msg.reply_to)
        except Exception as e:
            print(f'  ⚠️ quoted message: {e!r}'[:160])
    if quoted:
        u['_quoted'] = quoted
    try:
        d = mind.think(text, msg.sender, u)
    finally:
        u.pop('_quoted', None)
    if d is None or d['kind'] == 'command':
        return None                                      # the router runs the owner's own words, never the model's
    if (d.get('_turn') or {}).get('tainted'):
        u['_read_others'] = True
    if d.get('promise_fix') and msg.role == 'owner':        # 16/11 a promised change → recorded, and said as it is
        from agent import feedback
        said = feedback.never_again(list(u.get('history') or []) + [{'role': 'user', 'text': text}], text)
        d['reply'] = f"{d['reply']}\n\n{said}".strip() if d.get('reply') else said
    try:
        if d['kind'] == 'study':
            from contextlib import nullcontext
            from config.paths import ACTIVE_COURSES
            from utils.gemini_sub import api_only
            q = (d.get('resolved') or '').strip()
            if not 0 < len(q) <= 300 or (d.get('_turn') or {}).get('tainted'):
                q = text                                 # what the model wrote after reading others' text isn't a question
            with api_only() if q != text else nullcontext():   # and its rewording stays off the subscription agent
                return _study_answer(msg, q, d['course'] if d.get('course') in ACTIVE_COURSES else _course_in_text(q))
        outs = [o for o in (_assistant(msg, u, text, d) or []) if o.text or o.kind != 'text']
    except Exception as e:
        print(f'  ⚠️ acting on the loop failed: {e!r}'[:300])
        return None
    act = d.get('action')                                # from here _assistant has written: the classifier must not run too
    if act:
        try:
            outs += _mind_action(msg, u, act['kind'], act['text'])
        except Exception as e:
            print(f'  ⚠️ the loop\'s action failed: {e!r}'[:300])
            outs.append(Out('text', '⚠️ את הפעולה לא הצלחתי לבצע. נסה שוב.'))
    if d.get('clarify') or (not act and _repeats(u, outs, text)):
        # 15/11: "ביטלתי את המלון" / "אני ביטלתי" / "זה סגור אני ביטלתי" got one refusal, word for word, three times.
        # Asked again in other words → which of his open things it is, a button each; nothing to offer → the old path
        from agent import open_items
        try:
            found = open_items.items(msg.sender)
            c = open_items.clarify(msg.sender, sorted(found, key=lambda i: not i['raised']), text)
        except Exception as e:
            print(f'  ⚠️ clarify: {e!r}'[:200])
            c = None
        if c:
            lead = '\n'.join(o.text for o in outs if o.kind == 'text' and o.text and o.text.strip() != mind.CLARIFY
                             and not _repeats(u, [o], text)).strip()
            return [Out('buttons', f'{lead}\n\n{c[0]}' if lead else c[0], buttons=c[1])]
        if d.get('clarify') and '@' in (msg.sender or ''):
            # 17/11 "כן פאדל" / "פאדל" → "רק לוודא, על מה מדובר?" twice, with nothing to pick: the numbered question his
            # words name is the thing to clarify. It comes back as it was, its options under it
            from whatsapp import outbox
            try:
                q = outbox.pull_question(msg.sender, text)
            except Exception as e:
                print(f'  ⚠️ clarify (open question): {e!r}'[:200])
                q = None
            if q:
                return [Out('buttons', q['text'], buttons=q['buttons'])]
        if not d.get('clarify'):
            return None
    return outs or None


def _repeats(u: dict, outs: list, text: str) -> bool:
    """This answer is, word for word, the bot's last one, and he wrote something else since: he wasn't understood."""
    hist = u.get('history') or []
    last_bot = next((h for h in reversed(hist) if h.get('role') == 'bot'), None)
    last_user = next((h for h in reversed(hist) if h.get('role') == 'user'), None)
    said = '\n'.join(o.text for o in outs if o.kind in ('text', 'buttons') and o.text).strip()
    return bool(said and last_bot and last_user and said[:500].strip() == (last_bot.get('text') or '').strip()
                and (last_user.get('text') or '').strip() != (text or '').strip())


def _mind_action(msg: 'In', u: dict, kind: str, what: str) -> list['Out']:
    """An action the owner asked for (mind._owner_asked checked his own words). Calendar and messages stop at their ✅, as always.
    Each goes straight to its flow, so no wording of the model's can reach another command."""
    from utils.gemini_sub import api_only
    if u.get('paused'):
        return [Out('text', '⏸️ הסוכן עצור — לא מבצע כלום עד "הפעל סוכן".')]
    with api_only():                                     # the text may rest on a mail: not for the subscription agent
        if kind == 'calendar_add':
            return _calendar(msg, u, 'calendar_add', {}, what)
        if kind == 'message_partner':
            return _offer_message(u, 'partner', what, OWNER_NAME)
        if kind == 'reminder':                           # 11/11 no hour → a sensible one + one-tap times, never lost
            from agent import life
            res = life.set_reminder(msg.sender, what, own_words=msg.text or '')
            if res is None:
                return [Out('text', '🤔 על מה להזכיר?')]
            return [Out('buttons', res[1], buttons=res[2])]
    return []


YES_RX = re.compile(r'^\s*(?:כן|כן כן|כן בבקשה|כן תודה|יאללה|בטח|סבבה|אוקיי?|ok|למה לא|קדימה|בכיף|נשמע טוב|תעשה|יאללה תעשה)'
                    r'[\s!.,🙂👍]*$', re.I)
FOLLOW_UP_SECONDS = 30 * 60
STUDY_ACTIONS = {'ask', 'course_summary', 'file_send', 'list_summaries', 'practice_set', 'quiz_chat', 'mistakes',
                 'mastery_map', 'topic_guide', 'coverage_check', 'coverage_map', 'exam_report'}

# 03/11 eval (median 14.8s: classify 4–19s, THEN the assistant 6–14s): for a message that isn't an obvious command the
# assistant's call starts together with the classifier — the answer waits for the slower of the two, not their sum.
# A command (calendar, practice, quiz, deadlines, web…) never starts it, so the common command path adds no call.
COMMANDISH = re.compile(r'תוסיף|תכניס|תקבע|קבע לי|תזכיר|תזכורת|יומן|מחר|היום|השבוע|ביום\s+[א-ת]|תרגילים|סט תרגול|'
                        r'לתרגל|בחן אותי|חידון|מפת|טעויות|מטלות|הגשות|ציונים|מזג|חדשות|תשלח|שלח לי|סיכום של|מדריך|'
                        r'האם למדנו|מבחנים קודמים|אין\s+(?:לי\s+)?(?:לימודים|שיעור)|הודעה ל|תמחק|תזיז|תבטל|https?://|'
                        r'מבחן|בוחן|מועד\s+[אבג]|מצגת|שקפים|סילבוס')
_PREFETCH: dict[int, tuple] = {}
_TURN_START: dict[int, float] = {}                   # id(msg) → when the agent loop took this message
TURN_SECONDS = 90
_POOL = None


def _prefetch_decide(msg: 'In', u: dict, text: str) -> None:
    global _POOL
    if os.environ.get('SSA_ROUTER') != 'agent' or os.environ.get('SSA_PREFETCH', '1') == '0' \
            or msg.role != 'owner' or msg.group or len(text) < 3 or COMMANDISH.search(text):
        return
    from utils.budget import budget_lock_reason
    from utils import llm_health
    if budget_lock_reason() or llm_health.is_open() or llm_health.TURN.get() and llm_health.TURN.get().get('block'):
        return
    from concurrent.futures import ThreadPoolExecutor
    from agent import assistant
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='decide')
    hist = _history({'history': (u.get('history') or []) + [{'role': 'user', 'text': text[:500]}]})
    import contextvars                                 # api_only has to reach the pool's thread
    _PREFETCH[id(msg)] = (text, hist, _POOL.submit(contextvars.copy_context().run, assistant.decide, text, msg.sender, hist))


FILE_KIND = (('מצגת|שקפ|slides', r'מצגת|שקפ|slides|lecture|הרצא'), ('סילבוס|syllabus', r'סילבוס|syllab'),
             ('דף נוסחאות', r'נוסחאות'))
# (no bare "תרגול": "איפה התרגול בתוכנה היום?" asks for a room; no "סיכום"/"קובץ": "תשלח לי סיכום של ההרצאה
#  האחרונה" is our own summary, not a Moodle file)
FILE_ASK = re.compile(r'יש לך|יש את|תשלח|שלח לי|תביא|איפה|אפשר את|תן לי|יש במודל|העלו')


def _course_file(msg: 'In', text: str, index: dict | None = None) -> list['Out'] | None:
    """03/11 eval: "יש לך את המצגת של שיעור 3 במאקרו?" → the NotebookLM brain said a confident "no" while
    data/moodle/macroeconomics/files/186470_מצגת שיעור 3.pdf exists. A question about a course file is answered from
    the Moodle files index (collectors/course_files): found → the file itself; not there → what the course has.
    None = not a file question / no course / no index (the old path)."""
    kind = next(((k, rx) for k, rx in FILE_KIND if re.search(k, text)), None)
    if not kind or not FILE_ASK.search(text) or msg.role != 'owner' or msg.group:
        return None
    ck = _course_in_text(text)
    if not ck:
        return None
    from collectors import course_files
    from config.paths import COURSE_NAMES
    idx = course_files.load_index(ck) if index is None else index
    entries = [e for e in idx.values() if e.get('file')]
    if not entries:
        return None
    want = course_files._nums(text)                  # "שיעור 3" → 3; never a bare digit ("תוכנה 1")
    if not want and re.search(r'\d', re.sub(r'תוכנה\s*\d|\d{1,2}[./]\d{1,2}', '', text)):
        hits_ok = False                              # a number we can't read: list what's there, don't guess a file
    else:
        hits_ok = True
    slides = kind[0].startswith('מצגת')
    hits = [e for e in entries if re.search(kind[1], e['title'], re.I) and not re.search(r'פתרון|solution', e['title'], re.I)
            and not (slides and course_files.SKIP_RX.search(f"{e['title']} {e.get('section', '')}"))
            and (not want or want & course_files._nums(f"{e['title']} {e.get('section', '')}"))]
    hits = sorted((e for e in hits if (ROOT / e['file']).exists()), key=lambda e: e.get('first_seen', ''), reverse=True)
    name = COURSE_NAMES.get(ck, ck)
    if hits and hits_ok:
        seen, out = set(), []
        for e in hits:
            t = re.sub(r'\W+', '', e['title'])
            if t in seen or len(out) >= 2:
                continue
            seen.add(t)
            out.append(Out('document', f"📎 {e['title']} — {name} (ממודל)", path=ROOT / e['file']))
        return out
    have = sorted(entries, key=lambda e: e.get('first_seen', ''))[-5:]
    return [Out('text', f"במודל של {name} אין (עדיין) קובץ כזה. מה שיש שם עכשיו, האחרונים:\n"
                        + '\n'.join(f"• {e['title']}" for e in have))]


def _course_in_text(text: str) -> str | None:
    """'…במיקרו' / 'המצגת של שיעור 3 במאקרו' → the course key (this semester's, unless the old one is named)."""
    # a whole word (with a Hebrew prefix ב/ל/ה/ש/ו/מ/כ), never a substring; only a course the owner takes now, or the
    # old one he explicitly named ("מיקרו א׳") — "מבוא" / "אלגברה" in a sentence must not pull an old course in
    from config.paths import ACTIVE_COURSES
    from integrations import tau_portal
    t = text or ''
    for alias, hint in sorted(tau_portal.ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(r'(?<![א-תa-z])[בלהשומכ]{0,2}' + re.escape(alias) + r'(?![א-תa-z])', t, re.I):
            ck = _this_semester({'course': _course_key_of(hint)}, t).get('course')
            if ck and (ck in ACTIVE_COURSES or ck in SUCCESSOR):
                return ck                                # else: an old alias in passing — keep looking
    return None


def _il_now() -> datetime:
    """Israel wall-clock now, naive (the VM's processes may run in UTC)."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('Asia/Jerusalem')).replace(tzinfo=None)


STUDY_ACTIONS = frozenset({'assignments', 'practice_set', 'mastery_map', 'mistakes', 'quiz_chat', 'course_summary',
                           'file_send', 'list_summaries', 'no_class', 'list_no_class', 'topic_guide', 'exam_report',
                           'coverage_map', 'coverage_check'})


def _study_on() -> bool:
    try:
        from config.features import study_enabled
        return study_enabled()
    except Exception:
        return False


def run_intent(msg: In, u: dict, action: str, params: dict, text: str) -> list[Out]:
    from bot import handlers
    if not _study_on() and action in STUDY_ACTIONS:      # assistant-only install: no courses, Moodle or summaries
        from config.features import STUDY_OFF_TEXT
        return [Out('text', STUDY_OFF_TEXT)]
    ck = _course(params)
    if action == 'greeting':
        if THANKS.search(text):
            return [Out('text', 'בכיף 🙂')]
        if msg.role == 'owner' and len(text.split()) > 2:      # a sentence, not a hello → read it in context
            outs = _assistant(msg, u, text)
            if outs is not None:
                return outs
        return [Out('text', menu())]
    if action == 'web_search' or (action in ('ask', 'greeting', 'chat', 'general', 'unknown', '') and WEB_RX.search(text)
                                   and not re.search(r'מבחן|ציון|סיכום|הרצאה', text)):
        from integrations.websearch import ask_web
        return [Out('text', ask_web(text))]
    if action == 'today':
        # 03/11 ("מה לגבי התרגולים היום ב-14 ו-16, איפה הם?" → "📅 סטטוס יומי: סיכומים חדשים (1)"): the router's
        # 'today' is "what do I have today" — the day itself (lessons with rooms, homework, reminders); the old
        # Telegram digest of new summaries only for "מה חדש"
        if not re.search(r'חדש|התעדכן|הוסיפו|עדכונים', text):
            from agent.daytools import day_overview
            now = _il_now()
            kw = {'after': now} if REMAINING_RX.search(text) else {}   # "מה נשאר לי היום" → from now on
            # a group sees nothing private
            return [Out('text', day_overview(now.date(), msg.sender, hide_private=bool(msg.group), **kw))]
        return [Out('text', handlers.handle_today())]
    if action == 'assignments':
        if (one := _one_deadline(text, ck)) is not None:
            return [Out('text', one)]
        return [Out('text', handlers.handle_deadline())]
    if action in ('calendar_add', 'calendar_list'):
        return _calendar(msg, u, action, params, text)
    if action == 'practice_set':
        topic = (params.get('topic') or '').strip() or None
        return _practice(ck or (_infer_course(topic) if topic else None), topic, params.get('n') or 6, u=u)
    if action == 'mastery_map':
        from config.paths import ACTIVE_COURSES
        from utils.mastery import map_text
        return [Out('text', map_text(c)) for c in ([ck] if ck else ACTIVE_COURSES)]
    if action == 'mistakes':
        return _mistakes(ck)
    if action == 'quiz_chat':
        return _quiz_question(u, ck)
    if action in ('course_summary', 'file_send', 'list_summaries') and ck:
        return _latest_summary(ck, text)
    if action == 'no_class' and not NO_CLASS_RX.search(text) and msg.role == 'owner':
        # 20/10: "בכלכלה אמרו שבמקום השיעור השלישי יכלו הקלטה" was marked "no studies today" — a day off is said
        # in so many words; anything else is read in context
        outs = _assistant(msg, u, text)
        if outs is not None:
            return outs
    if action in ('no_class', 'list_no_class'):
        return [Out('text', no_class(text, params.get('action_type', 'add'), list_only=action == 'list_no_class'))]
    if action == 'topic_guide':
        return _topic_guide((params.get('topic') or '').strip(), ck)
    if action == 'exam_report':
        from config.paths import ACTIVE_COURSES
        from generators.exam_report import build_exam_report
        return [Out('text', build_exam_report(c)) for c in ([ck] if ck else ACTIVE_COURSES)]
    if action == 'coverage_map':
        if not ck:
            return [Out('text', 'לאיזה קורס? למשל: "מפת כיסוי בהסתברות".')]
        from generators.coverage_map import build_coverage_map
        return [Out('text', build_coverage_map(ck))]
    if action == 'coverage_check':
        return [Out('text', handlers.check_coverage((params.get('topic') or text).strip(), ck))]
    if re.search(r'מה התאריך|איזה יום היום|מה היום בתאריך|איזה תאריך', text):
        return [Out('text', today_text())]
    if re.search(r'(ה)?קורסים (שלי|הקרובים|בסמסטר)|מערכת (ה)?שעות|מה אני לומד', text):
        return [Out('text', my_courses_text())]
    # "מתי המבחן הקרוב שלי" / "מה ההגשה הבאה" — the calendar knows, the lecture summaries don't
    if EXAM_WORD.search(text) and EXAM_ASK.search(text):
        from integrations import gcal
        if gcal.configured():
            ans = _exams_text(u, text, ck)
            if ans is not None:
                return [Out('text', ans)]
            # a course named and no date anywhere we have → the assistant (it looks it up), not other courses' exams
    if ck and LAST_LECTURE.search(text):
        return _latest_summary(ck, text)
    if ck and LAST_TAUGHT.search(text):                  # "מה למדו בהרצאה האחרונה במיקרו" → this semester's latest
        lt = _last_taught(ck, text)
        if lt:
            return [Out('text', lt)]
    if msg.role == 'owner' and action == 'ask':
        outs = _assistant(msg, u, text)
        if outs is not None:
            return outs
        text = u.pop('_resolved', text)                # a study follow-up ("ומה עם הוכחה?") in full
        ck = ck or _course_in_text(text)
    if not _study_on():                                # nothing to search in the summaries: say how to use me instead
        return [Out('text', '🤔 לא בטוח שהבנתי. ' + menu())]
    return _study_answer(msg, text, ck)


def _study_answer(msg: In, text: str, ck: str | None) -> list[Out]:
    from bot import handlers
    if ck and msg.role == 'owner':                     # 13/10: NotebookLM first — free, sources only, follow-ups
        from agent import nlm_ask
        if nlm_ask.has_notebook(ck):
            from agent import interim                    # 17/11 the one interim line of the turn (agent/interim)
            interim.say(interim.COURSE)
            ans = nlm_ask.ask(ck, text, msg.sender)
            if ans:
                return [Out('text', ans)]
    return [Out('text', handlers.handle_ask(text, ck))]


# ── study extras shared with the Telegram bot ───────────────────────────────

NO_CLASS = ROOT / 'data' / 'no_class_days.json'
NO_CLASS_RX = re.compile(r'אין (?:לי |לנו )?(?:לימודים|שיעורים|אוניברסיטה)|בלי לימודים|(?:יש|חזרו) (?:לי )?לימודים|'
                         r'ימי? (?:בלי|ללא) לימודים|חופש|חופשה|לא (?:אגיע|מגיע|אהיה|הולך) (?:ל|ב)(?:אוניברסיטה|לימודים|שיעורים)|'
                         r'(?:ה)?ימים (?:ש)?(?:סימנת|בלי)|טעות')


def no_class(text: str, action_type: str = 'add', list_only: bool = False, client=None, path: Path | None = None) -> str:
    """Mark/unmark no-class days (same file the pipeline's semester gate and the primer read)."""
    p = path or NO_CLASS
    try:
        cur = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cur = {}
    dates = set(cur.get('dates', []))
    if list_only:
        upcoming = sorted(d for d in dates if d >= date.today().isoformat())
        return ('🗓️ *ימים בלי לימודים:*\n' + '\n'.join(f'• {d}' for d in upcoming)) if upcoming else 'לא סימנת ימים בלי לימודים.'
    import os
    from google.genai import types as T
    from integrations.gcal import WEEKDAY_HE
    if client is None:
        import google.genai as genai
        client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    today = date.today()
    resp = client.models.generate_content(
        model='gemini-3.8-flash',
        contents=(f'היום {today.isoformat()} (יום {WEEKDAY_HE[today.weekday()]}). חלץ את התאריכים שהמשתמש מתכוון אליהם '
                  f'("מחר", "ראשון ושני", "השבוע", "25/10"). JSON בלבד: {{"dates": ["YYYY-MM-DD"], "remove": false}} '
                  f'(remove=true אם הוא מבטל: "יש לימודים", "טעות"). הטקסט: {text}'),
        config=T.GenerateContentConfig(max_output_tokens=2000, temperature=0.0, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, 'gemini-3.8-flash', op='agent.no_class')
    data = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', (resp.text or '').strip()) or '{}')
    if isinstance(data, list):
        data = {'dates': data}
    found = [d for d in data.get('dates') or [] if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(d))]
    if not found:
        return '🤔 לא הבנתי אילו ימים. למשל: "אין לימודים מחר" או "אין לימודים ראשון 25/10".'
    if data.get('remove') or action_type == 'remove':
        dates -= set(found)
        msg = '↩️ חזרו להיות ימי לימודים: ' + ', '.join(sorted(found))
    else:
        dates |= set(found)
        msg = '🏖️ סימנתי בלי לימודים: ' + ', '.join(sorted(found)) + '\n(באותם ימים לא אשלח הכנה לשיעור.)'
    cur['dates'] = sorted(dates)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding='utf-8')
    return msg


def _topic_guide(topic: str, ck: str | None) -> list[Out]:
    if not topic:
        return [Out('text', 'איזה נושא ללמד מאפס? למשל: "תכין לי מדריך מאפס להתפלגות בינומית".')]
    from generators.topic_guide import build_topic_guide
    from generators.pdf_generator import markdown_to_pdf
    md = build_topic_guide(topic, ck)
    out = ROOT / 'data' / 'topic_guides' / (re.sub(r'[^\w \u0590-\u05FF-]', '', topic)[:40].strip() + '.pdf')
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        markdown_to_pdf(md, str(out), f'מדריך מאפס: {topic}', '')
        return [Out('document', f'📘 מדריך מאפס: {topic}', path=out)]
    except Exception as e:
        print(f'  ⚠️ topic guide pdf failed: {e}')
        return [Out('text', md[:3800])]


# ── small facts the RAG shouldn't guess ─────────────────────────────────────

DAYS_HE = {'sun': 'ראשון', 'mon': 'שני', 'tue': 'שלישי', 'wed': 'רביעי', 'thu': 'חמישי', 'fri': 'שישי'}


def today_text() -> str:
    from integrations.gcal import WEEKDAY_HE
    d = date.today()
    return f"📅 היום יום {WEEKDAY_HE[d.weekday()]}, {d:%d/%m/%Y}."


def my_courses_text() -> str:
    """This semester's courses + the owner's own timetable (config/courses.yaml my_sessions)."""
    from config.features import courses_config, study_enabled, STUDY_OFF_TEXT
    if not study_enabled():
        return STUDY_OFF_TEXT
    from config.paths import ACTIVE_COURSES, COURSE_NAMES
    cfg = courses_config()
    courses = cfg.get('courses', cfg)
    start = ((cfg.get('settings') or {}).get('schedule') or {}).get('semester_start')
    lines = ['🎓 *הקורסים שלך בסמסטר הזה*' + (f' (מתחיל {start})' if start else '')]
    try:
        from integrations import tau_portal
        site_events = tau_portal.schedule(live=False)[0] or []
    except Exception:
        site_events = []
    from agent import timetable as _tt
    from agent.daytools import CODE_DAYS as _CODE_DAYS
    for ck in ACTIVE_COURSES:
        lines.append(f"\n*{COURSE_NAMES.get(ck, ck)}*")
        for s in (courses.get(ck) or {}).get('my_sessions') or []:
            kind = 'הרצאה' if s.get('type') == 'lecture' else 'תרגול'
            # 03/11: the university's room for that slot wins over the yaml's (stale) one
            word = (re.findall(r'[א-ת]{3,}', COURSE_NAMES.get(ck, '')) or [''])[0]
            room = _tt.usual_place(site_events, word, _CODE_DAYS.get(s.get('day'), -1), str(s.get('start') or '')) \
                or s.get('location')
            loc = f" · {room}" if room else ''
            lines.append(f"• {kind}: יום {DAYS_HE.get(s.get('day'), s.get('day'))} {s.get('start')}–{s.get('end')}{loc}")
    try:                                         # 03/11: who teaches each group (the university site)
        from integrations import tau_portal
        from agent import timetable
        st = timetable.staff(tau_portal.schedule(live=False)[0])
        if st:
            lines += ['\n👩‍🏫 *מרצים ומתרגלים*'] + st
    except Exception:
        pass
    return '\n'.join(lines)


# ── study ───────────────────────────────────────────────────────────────────

def _practice(ck: str | None, topic: str | None, n, homework: dict | None = None, u: dict | None = None) -> list[Out]:
    if not ck:
        return [Out('text', 'באיזה קורס לתרגל? למשל: "תן לי תרגילים בהסתברות".')]
    from generators.practice_set import practice_pdf, aimed_at_homework
    from generators.practice_grader import FOLLOW_UP
    from utils.open_homework import next_assignment
    if homework is None and not topic:
        hw = next_assignment(ck)
        homework = hw if hw and hw.get('pdf') else None
    try:
        n = max(3, min(int(n), 12))
    except (TypeError, ValueError):
        n = 6
    try:
        pdf = practice_pdf(ck, topic, None, n, homework)
    except ValueError as e:
        # 26/10: "תן לי תרגילים על השפעות חיצוניות" before it was taught → "אין סיכומים". The past exams have it:
        # a real exam question on the unit (with "פתרון" after it), when the course has an exam map.
        if topic:
            try:
                from agent import exam_weight
                q = exam_weight.exam_question(f'תביא לי שאלה על {topic}', u if u is not None else {}, ck)
                if q and not q.startswith('אין לי'):
                    return [Out('text', '📭 עוד אין סיכומי שיעור על זה, אז הנה תרגול משאלת מבחן אמיתית:\n\n' + q)]
            except Exception as ex:
                print(f'  ⚠️ practice → exam question: {ex!r}'[:200])
        return [Out('text', f'🤷 {e}')]
    aimed = aimed_at_homework(pdf.with_suffix('.md').read_text(encoding='utf-8'))
    cap = ('🏋️ סט תרגול' + (' — מכוון למטלה הקרובה' if aimed else '') + '. הפתרונות בחלק ב׳.\n' + FOLLOW_UP)
    return [Out('document', cap, path=pdf, register_practice=(str(pdf.with_suffix('.md')), ck))]


def _mistakes(ck: str | None) -> list[Out]:
    if not ck:
        return [Out('text', 'של איזה קורס? למשל: "מחברת טעויות בהסתברות".')]
    from utils.mistakes import notebook_md
    md = notebook_md(ck)
    if not md:
        return [Out('text', '📕 מחברת הטעויות בקורס הזה עוד ריקה.')]
    from config.paths import COURSE_NAMES
    from generators.pdf_generator import markdown_to_pdf
    out = ROOT / 'data' / 'mistakes_pdf' / f'מחברת טעויות - {ck}.pdf'
    out.parent.mkdir(parents=True, exist_ok=True)
    markdown_to_pdf(md, str(out), 'מחברת טעויות', COURSE_NAMES.get(ck, ck))
    return [Out('document', '📕 מחברת הטעויות — רק מה שעוד לא תוקן', path=out)]


def _one_deadline(text: str, ck: str | None = None, items: list[dict] | None = None, now: datetime | None = None) -> str | None:
    """26/10: "מתי מגישים את תרגיל 2 בהסתברות?" got the 7-day list without exercise 2 (due 3/11). A named exercise
    number → that one, from every deadline the bot knows (Moodle + lecturer sites), with its submitted state."""
    m = re.search(r'(?:תרגיל|מטלה|ממ"ן|עבודה|hw|quiz|בוחן)\s*(?:מס(?:פר|\')?\s*)?(\d{1,2})', text or '', re.I)
    if not m:
        return None
    from utils import deadlines as dl
    from config.paths import COURSE_NAMES
    now = now or datetime.now()
    items = items if items is not None else dl.load_items()
    hits = [a for a in items if dl._ex_num(a) == m.group(1) and (not ck or a.get('course_key') == ck)]
    if not ck:                                          # no course from the classifier → the one named in the text
        named = [a for a in hits if any(w in (text or '') for w in
                                        re.findall(r'[א-ת]{4,}', COURSE_NAMES.get(a.get('course_key'), ''))[:1])]
        hits = named or hits
    if not hits:
        return None
    st = dl.load_state()
    lines = []
    for a in sorted(hits, key=lambda a: dl._due(a) or datetime.max)[:3]:
        due = dl._due(a)
        left = (due.date() - now.date()).days if due else None
        when = f"{due:%d/%m} ב-{due:%H:%M}" + (' (היום)' if left == 0 else ' (מחר)' if left == 1 else
                                              f' (בעוד {left} ימים)' if left and left > 0 else ' (עבר)') if due else 'בלי תאריך'
        done = ' · ✅ הוגש' if dl.is_done(a, st) else ''
        lines.append(f"📝 *{COURSE_NAMES.get(a.get('course_key'), '')} — {a.get('title', '')}*: {when}{done}")
    return '\n'.join(lines)


def _latest_summary(ck: str, text: str = '') -> list[Out]:
    from config.paths import COURSE_NAMES
    # 26/10: "הסיכום של ההרצאה האחרונה בתוכנה 1" sent the recitation (the newest of any kind); replaced/ stand-ins
    # never count
    sub = 'tutorials' if re.search(r'תרגול|תרגיל(?!\s*בית)|recitation', text or '', re.I) else \
        'lectures' if re.search(r'הרצאה|שיעור|lecture', text or '', re.I) else '*'
    pdfs = sorted((p for p in (ROOT / 'data' / 'summaries' / ck).glob(f'{sub}/*.pdf') if p.parent.name != 'replaced'),
                  key=lambda p: p.name)
    if not pdfs:
        from config.paths import ACTIVE_COURSES
        soon = ' — הסמסטר מתחיל ב-18.10, ואחרי כל הרצאה אשלח סיכום לבד.' if ck in ACTIVE_COURSES else '.'
        return [Out('text', f'עוד לא היו הרצאות ב{COURSE_NAMES.get(ck, ck)}{soon}')]
    return [Out('document', f'📚 {pdfs[-1].stem}', path=pdfs[-1])]


def _quiz_question(u: dict, ck: str | None) -> list[Out]:
    if not ck:
        return [Out('text', 'באיזה קורס? (הסתברות / תוכנה 1 / מיקרו / מאקרו)')]
    import os
    import random
    from config.paths import COURSE_NAMES
    from rag.vault_indexer import search
    hits = search(COURSE_NAMES.get(ck, ''), k=20, course_filter=ck)
    if not hits:
        return [Out('text', 'אין לי עדיין חומר בקורס הזה לחידון.')]
    ctx = random.choice(hits[:10])
    import google.genai as genai
    from google.genai import types as T
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    resp = client.models.generate_content(
        model='gemini-3.8-flash',
        contents=(f'בנה שאלה אמריקאית קצרה בעברית על החומר הבא (מתוך {ctx.get("stem", "")}):\n'
                  f'{ctx.get("text", "")[:2500]}\n\nJSON בלבד: {{"question": "...", "options": {{"a": "...", '
                  f'"b": "...", "c": "...", "d": "..."}}, "correct": "a|b|c|d", "explanation": "...", '
                  f'"topic": "נושא, 2-5 מילים"}}'),
        config=T.GenerateContentConfig(max_output_tokens=1500, temperature=0.5,
                                       response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, 'gemini-3.8-flash', op='agent.quiz')
    q = json.loads(resp.text or '{}')
    u['quiz'] = {**q, 'course': ck}
    o = q.get('options') or {}
    return [Out('text', f"🧠 *{q.get('question', '')}*\n\nא) {o.get('a', '')}\nב) {o.get('b', '')}\n"
                        f"ג) {o.get('c', '')}\nד) {o.get('d', '')}\n\nענה: א/ב/ג/ד")]


def _quiz_answer(msg: In, u: dict, text: str) -> list[Out]:
    q = u.pop('quiz')
    pick = {'א': 'a', 'ב': 'b', 'ג': 'c', 'ד': 'd', '1': 'a', '2': 'b', '3': 'c', '4': 'd'}.get(text.strip(' .!?'),
                                                                                                text.strip(' .!?').lower())
    correct = str(q.get('correct', 'a')).lower()
    from utils.mastery import record as mastery
    mastery(q.get('course', ''), q.get('topic') or q.get('question', '')[:120], 1.0 if pick == correct else 0.0, 'quiz')
    o = q.get('options') or {}
    if pick == correct:
        return [Out('text', f"✅ נכון! {q.get('explanation', '')}\nעוד שאלה? כתוב \"בחן אותי\".")]
    from utils.mistakes import record as mistake
    mistake(q.get('course', ''), 'quiz', topic=q.get('topic', ''), question=q.get('question', ''),
            wrong=o.get(pick, pick), correct=f"{o.get(correct, correct)} — {q.get('explanation', '')}")
    return [Out('text', f"❌ התשובה: {o.get(correct, correct.upper())}\n{q.get('explanation', '')}\n"
                        f"📕 נוסף למחברת הטעויות.")]


# ── calendar (✅ before any write) ──────────────────────────────────────────

def _calendar(msg: In, u: dict, action: str, params: dict, text: str) -> list[Out]:
    from integrations import gcal
    if not gcal.configured():
        return [Out('text', '🗓️ היומן עוד לא מחובר (צריך אישור חד-פעמי מגוגל — ראה SETUP.md).')]
    if action == 'calendar_list':
        when = (params.get('date') or '').strip()
        if re.search(r'מבחן|בוחן|מועד|הגשה', text):
            ans = _exams_text(u, text, _course_in_text(text))
            if ans is not None:
                return [Out('text', ans)]
            outs = _assistant(msg, u, text) if msg.role == 'owner' else None    # a course with no date → look it up
            return outs or [Out('text', f'📚 לא מצאתי מועד מבחן ב"{exam_query(text)}" — לא ביומן ולא באתר האוניברסיטה.')]
        # the classifier doesn't know today's date and may invent one — trust it only if plausible
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', when) and \
                date.today() - timedelta(days=7) <= date.fromisoformat(when) <= date.today() + timedelta(days=365) \
                and not re.search(r'השבוע|שבוע', text) and not REMAINING_RX.search(text):
            return [Out('text', gcal.day_text(date.fromisoformat(when)))]
        from agent.daytools import parse_day, day_overview
        day = parse_day(text)
        if day and not re.search(r'השבוע|שבוע הבא', text):
            kw = {'hide_private': True} if msg.group else {}
            now = _il_now()
            if day in (date.today(), now.date()) and REMAINING_RX.search(text):     # "מה נשאר לי היום" → from now on
                kw['after'] = now
            return [Out('text', day_overview(day, msg.sender, **kw))]
        # "מתי יש לי משהו עם אבא?" — a person / thing named → search the calendar for it (08/10: showed the week)
        if m := re.search(r'(?:עם|של|לגבי|על|ה?פגישה|ה?ארוחה|ה?יום הולדת)\s+(?:ה)?([א-ת][א-ת"\']{1,})', text):
            hits = [e for e in gcal.find_events(m.group(1), days_back=0, days_ahead=180)][:6]
            if hits:
                return [Out('text', f'🔎 *ביומן — "{m.group(1)}":*\n' +
                            '\n'.join(f"• {gcal.when_text(e)} — {e.get('summary', '')}" for e in hits))]
        return [Out('text', gcal.week_text(date.today()))]           # no day named → the coming week
    if u.get('paused'):
        return [Out('text', '⏸️ הסוכן עצור — לא מוסיף ליומן. "הפעל סוכן" כדי לחזור.')]
    events, question = gcal.parse_events(text)
    if question or not events:
        u['cal_draft_text'] = {'text': text, 'ts': datetime.now().isoformat(timespec='seconds')}
        return [Out('text', f"🤔 {question or 'מה בדיוק להוסיף ליומן, ומתי?'}")]
    pid = secrets.token_hex(4)
    extras = event_extras(text)
    u.setdefault('pending', {})[pid] = {'events': events, 'ts': datetime.now().isoformat(timespec='seconds'), 'extras': extras}
    body = '\n\n'.join(gcal.describe(ev) for ev in events)
    if extras:
        body += ('\n\n' + ('⚠️ אזכיר לך לפני מועד הביטול\n' if extras.get('cancel_hours') else '')
                 + (f'💬 וגם {PARTNER_NAME} תקבל תזכורות' if extras.get('partner') else '')).rstrip()
    ask = 'להוסיף ליומן?' if len(events) == 1 else f'להוסיף את כל {len(events)} האירועים ליומן?'
    ok_label = '✅ הוסף' if len(events) == 1 else '✅ הוסף הכל'
    return [Out('buttons', f'{body}\n\n{ask}', buttons=[(ok_label, f'cal:ok:{pid}'), ('❌ בטל', f'cal:no:{pid}')])]


# ── buttons ─────────────────────────────────────────────────────────────────

def _button(msg: In, u: dict, st: dict | None = None) -> list[Out]:
    p = msg.payload or ''
    if p.startswith('oq:'):                                  # 19/11 a bare answer that isn't any one question's (outbox)
        return _which_question(msg, p)

    if p.startswith('jund:'):                                # 19/11 the ✅ for an undo (agent/journal)
        from agent import journal
        return [Out('text', journal.button(msg.sender, p))]
    if p.startswith('lf:'):                                  # 🧠 the evening "save what I learned?"
        from agent import learn
        return [Out('text', learn.button(u, msg.sender, p))]
    if p.startswith('cal:') and not p.startswith('cal:undo:'):
        _, op, pid = (p.split(':', 2) + ['', ''])[:3]
        item = (u.get('pending') or {}).pop(pid, None)
        if not item:
            return [Out('text', 'הבקשה הזו כבר לא בתוקף — בקש שוב.')]
        if op != 'ok':
            _tell_proposer(item, f"❌ {OWNER_NAME} לא אישר: " + ', '.join(e['title'] for e in item.get('events') or []))
            return [Out('text', '❌ בוטל, לא נוסף ליומן.')]
        if datetime.now() - datetime.fromisoformat(item['ts']) > PENDING_TTL and not _all_future(item):
            # 23/10: "1" to a mail offer 4.5h later was refused — the dates are absolute; only a past one is stale
            return [Out('text', '⌛ האירוע כבר עבר — לא הוספתי.')]
        if u.get('paused'):
            return [Out('text', '⏸️ הסוכן עצור — לא מוסיף ליומן.')]
        events = item.get('events') or ([item['event']] if item.get('event') else [])
        ids, added, skipped = _add_events(msg.sender, events)
        _tell_proposer(item, f"✅ {OWNER_NAME} אישר ונוסף ליומן: " + ', '.join(added))
        text = (f"✅ נוסף ליומן: {added[0]}" if len(added) == 1 else
                f"✅ נוספו {len(added)} אירועים ליומן:\n" + '\n'.join(f'• {t}' for t in added)) if added else ''
        if skipped:
            text += ('\n' if text else '') + '↩️ כבר היו ביומן (לא שכפלתי): ' + ', '.join(skipped)
        if item.get('extras') and ids:
            return [Out('text', text + '\n' + _apply_extras(msg, item['extras'], ids, events))]
        return [Out('text', text)] + _reminder_offer(u, ids, events)
    if p.startswith('cal:undo:'):
        undo = (u.get('undo') or {}).pop(p.split(':', 2)[2], None)
        if not undo:
            return [Out('text', 'כבר לא אפשרי לבטל את זה מכאן — אפשר למחוק ביומן.')]
        from integrations import gcal
        for eid in undo['ids']:
            try:
                gcal.delete_event(eid)
            except Exception as e:
                print(f'  ⚠️ undo failed for {eid}: {e}')
        audit(msg.sender, 'calendar_undo', undo)
        _tell_proposer(undo, f"↩️ {OWNER_NAME} ביטל: " + ', '.join(undo['titles']))
        return [Out('text', '↩️ בוטל והוסר מהיומן: ' + ', '.join(undo['titles']))]
    if p.startswith('rm:'):
        return _set_reminders(msg, u, p)
    if p.startswith('oi:'):                                  # 15/11 "על מה מדובר? [המלון בטבריה] [משהו אחר]"
        from agent import open_items
        return [Out('text', open_items.button(msg.sender, p))]
    if p.startswith('vi:'):                                  # 16/11 a line he wrote in Obsidian ("✍️ שלי"): his ✅ makes it his words
        from vault_sync import mine
        reply, then = mine.button(msg.sender, p)
        if then and not u.get('paused'):
            from dataclasses import replace as _replace
            return [Out('text', f'{reply}\n▶️ "{then}"')] + _route(_replace(msg, kind='text', text=then, payload=''), u, st)
        return [Out('text', reply)]
    if p.startswith('rmd:'):
        from agent import life
        _, op, rid = (p.split(':', 2) + ['', ''])[:3]
        return [Out('text', life.reminder_action(msg.sender, op, rid))]
    if p.startswith('esc:'):                                 # 11/11 the follow-up of something he marked important
        from agent import escalate
        _, op, eid = (p.split(':', 2) + ['', ''])[:3]
        return [Out('text', escalate.button(msg.sender, op, eid))]
    if p.startswith('nz:') and msg.role == 'owner':          # 16/11 mute / undo a kind of proactive message (agent/noise)
        from agent import noise
        _, op, kind = (p.split(':', 2) + ['', ''])[:3]
        audit(msg.sender, f'noise_{op}', {'kind': kind})
        return [Out('text', noise.answer(op, kind))]
    if p.startswith('sec:'):                                 # 11/11 a secretary note's one-tap answer (agent/proactive)
        from agent import proactive
        _, op, oid = (p.split(':', 2) + ['', ''])[:3]
        reply, then = proactive.answer(msg.sender, op, oid)
        audit(msg.sender, f'secretary_{op}', {'offer': oid})
        if then and not u.get('paused'):
            from dataclasses import replace as _replace      # "✍️ תנסח לי ברכה" → the usual drafting flow (✅ to send)
            return [Out('text', f'▶️ "{then}"')] + _route(_replace(msg, kind='text', text=then, payload=''), u, st)
        return [Out('text', reply or '👍')]
    if p.startswith('wa:'):
        from agent import web_agent
        _, op, jid = (p.split(':', 2) + ['', ''])[:3]
        audit(msg.sender, f'web_{op}', {'id': jid})
        return [Out('text', web_agent.answer(msg.sender, jid, op))]
    if p.startswith('task:'):
        from agent import tasks
        _, op, tid = (p.split(':', 2) + ['', ''])[:3]
        return [Out('text', tasks.action(msg.sender, op, tid))]
    if p.startswith('ce:') or p.startswith('msg:'):
        return _confirm_action(msg, u, p)
    if p.startswith('ex:'):
        return _exam_answer_button(msg, u, p)
    if p.startswith('ft:'):
        return _feature_button(msg, u, p)
    if p.startswith('co:'):
        return _coord_button(msg, u, p)
    if p.startswith('bk:'):
        return _booking_button(msg, u, p)
    if p.startswith('em:'):
        from agent import life
        _, op, did = (p.split(':', 2) + ['', ''])[:3]
        dr = life.take_draft(did, msg.sender)
        if not dr:
            return [Out('text', 'הטיוטה כבר לא בתוקף.')]
        if op != 'send':
            return [Out('text', '❌ בוטל, לא נשלח.')]
        if not dr.get('to'):
            return [Out('text', 'חסרה כתובת — בקש שוב עם כתובת.')]
        from generators.gmail_sender import GmailSender
        html = "<div dir='rtl' style='font-family:Arial'>" + (dr.get('body') or '').replace('\n', '<br>') + '</div>'
        ok = GmailSender().send_email(to=dr['to'], subject=dr.get('subject', ''), body_html=html)
        audit(msg.sender, 'email_sent' if ok else 'email_failed', {'to': dr['to'], 'subject': dr.get('subject')})
        return [Out('text', f"📤 נשלח ל-{dr['to']}." if ok else '⚠️ השליחה נכשלה.')]
    if p.startswith('cj:'):
        from agent import claude_job
        _, op, jid = (p.split(':', 2) + ['', ''])[:3]
        audit(msg.sender, f'claude_job_{op}', {'id': jid})
        unclear = claude_job.digit_guard(msg.sender, jid, op) if op in ('deploy', 'discard') else None
        if unclear:
            return [Out('text', unclear)]
        if op == 'deploy':
            return [Out('text', f'🚀 פורס את משימה {jid} דרך הבדיקות האוטומטיות… (כדקה)'), Out('text', claude_job.deploy(jid))]
        if op == 'diff':
            return [Out('text', claude_job.diff(jid))]
        return [Out('text', claude_job.discard(jid))]
    if p.startswith('noop'):
        return [Out('text', '👍')]
    if p.startswith('hw:'):
        _, op, key = (p.split(':', 2) + ['', ''])[:3]
        from utils import deadlines as dl
        if op == 'done':                                  # 20/10: checked in Moodle before it's closed
            item = dl.find(key)
            audit(msg.sender, 'homework_done', {'key': key})
            if item:
                from agent import loops
                u['submit_last'] = key
                return [Out('text', loops.verify(item))]
            dl.mark_done(key)
            return [Out('text', '✅ סומן כהוגש — לא אזכיר יותר על המטלה הזו.')]
        if op == 'prac':
            item = dl.find(key)
            if not item:
                return [Out('text', '⚠️ לא מצאתי את המטלה.')]
            from utils.open_homework import next_assignment
            hw = next_assignment(item.get('course_key', ''), horizon_days=30)
            hw = hw if hw and dl.key_of(hw) == key and hw.get('pdf') else None
            return _practice(item.get('course_key'), None, 6, homework=hw)
    if p.startswith('menu:'):
        return _text(msg, u, p.split(':', 1)[1])
    return [Out('text', menu())]


# ── calendar writes + reminders (owner and partner) ──────────────────────────

def _add_events(sender: str, events: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Insert, skipping anything already on the calendar. → (event ids, added titles, skipped titles)."""
    from integrations import gcal
    ids, added, skipped = [], [], []
    for ev in events:
        try:
            if gcal.exists_similar(ev):
                skipped.append(ev['title'])
                continue
        except Exception:
            pass
        created = gcal.insert_event(ev)
        audit(sender, 'calendar_add', {'event': ev, 'id': created.get('id'), 'link': created.get('htmlLink')})
        ids.append(created.get('id', ''))
        added.append(ev['title'])
    if [i for i in ids if i]:                           # 19/11 undoable after a ✅ — the undo asks ✅ too (agent/journal)
        from agent import journal
        journal.record(sender, 'cal_add', ', '.join(added), {'ids': [i for i in ids if i]}, confirm=True)
    return [i for i in ids if i], added, skipped


def _reminder_offer(u: dict, ids: list[str], events: list[dict]) -> list[Out]:
    """After adding: offer a Google Calendar reminder (pops up on the phone even without the bot)."""
    if not ids:
        return []
    from integrations import gcal
    key = secrets.token_hex(3)
    u.setdefault('rem', {})[key] = {'ids': ids, 'ts': datetime.now().isoformat(timespec='seconds')}
    opts = gcal.REMINDERS_ALLDAY if all(e.get('all_day') for e in events) else gcal.REMINDERS_TIMED
    note = '' if all(e.get('all_day') for e in events) else '\n(בכל מקרה אזכיר כאן בצ\'אט שעה לפני)'
    # 17/11 "כן פאדל" / "פאדל" to this question got "רק לוודא, על מה מדובר?" twice: it didn't say what it was about
    what = events[0].get('title', '') if len(events) == 1 else ''
    many = '\n(אפשר גם כמה, למשל "1 2")'
    return [Out('buttons', f'🔔 תזכורת נוספת{f" ל{what}" if what else ""}? אזכיר כאן בצ\'אט וגם תקפוץ מהיומן.{note}{many}',
                buttons=[(lbl, f'rm:{m}:{key}') for lbl, m in opts] + [('בלי תזכורת', f'rm:none:{key}')])]


def _set_reminders(msg: In, u: dict, p: str) -> list[Out]:
    _, minutes, key = (p.split(':', 2) + ['', ''])[:3]
    rem = (u.get('rem') or {}).pop(key, None)
    if not rem:
        return [Out('text', 'כבר לא בתוקף — אפשר להוסיף תזכורת ישירות ביומן.')]
    from agent import event_reminders as er
    if minutes == 'none':
        for eid in rem['ids']:
            er.set_override(eid, [])
        return [Out('text', '👍 בלי תזכורת.')]
    from integrations import gcal
    many = [int(m) for m in minutes.split('+') if m.isdigit()]      # 17/11 "1 2": several picks of one question
    if not many:
        return [Out('text', 'כבר לא בתוקף — אפשר להוסיף תזכורת ישירות ביומן.')]
    for eid in rem['ids']:
        gcal.set_reminder(eid, many if len(many) > 1 else many[0])
        # the chat reminder too: the chosen times, plus the usual hour-before when a choice is a day or more ahead
        er.set_override(eid, many + ([er._load()['settings'].get('lead', er.DEFAULT_LEAD)]
                                     if any(1440 <= m < 6 * 1440 for m in many) else []))
    audit(msg.sender, 'calendar_reminder', {'ids': rem['ids'], 'minutes': many if len(many) > 1 else many[0]})
    if len(many) > 1:
        return [Out('text', f'🔔 נקבעו {len(many)} תזכורות ({", ".join(_lead_words(m) for m in sorted(many, reverse=True))}) '
                            f'— אזכיר כאן בצ\'אט, וגם יקפצו בטלפון מיומן גוגל.')]
    return [Out('text', '🔔 נקבעה תזכורת — אזכיר כאן בצ\'אט, וגם תקפוץ בטלפון מיומן גוגל.')]


def _which_question(msg: In, p: str) -> list[Out]:
    """19/11 the owner answered "2" with two numbered questions open and the bot couldn't know which. Nothing is guessed now:
    a quote of a question he already answered says so, and a digit two open questions could mean asks which one, naming
    both. Each option is that question's own answer: a ✅ there is still his explicit pick."""
    from whatsapp import outbox
    if p == outbox.GONE:
        return [Out('text', 'על השאלה הזאת כבר ענית (או שהיא הוחלפה). אם משהו השתנה, תכתוב לי במילים.')]
    _, _, picks, style = (p.split(':') + ['', '', '', ''])[:4]
    try:
        q = outbox.ask_which(msg.sender, picks, style)
    except Exception as e:
        print(f'  ⚠️ which question: {e!r}'[:200])
        q = None
    if q:
        return [Out('buttons', q[0], buttons=q[1])]
    return [Out('text', '🤔 לא בטוח לאיזו שאלה התכוונת. תכתוב במילים?')]


def _lead_words(m: int) -> str:
    """1440 → "יום לפני", 120 → "שעתיים לפני": the button's own words."""
    from integrations import gcal
    for lbl, v in gcal.REMINDERS_TIMED + gcal.REMINDERS_ALLDAY:
        if v == m:
            return lbl.replace('🔔', '').strip()
    return f'{m} דקות לפני'


# ── partner (The partner): adds to the owner's calendar after her own ✅, with a conflict check ──

import os as _os
OWNER_NAME = _os.environ.get('WA_OWNER_NAME', 'המשתמש')
PARTNER_NAME = _os.environ.get('WA_PARTNER_NAME', 'בת הזוג')
PARTNER_MENU = (f'היי {PARTNER_NAME} 👋 אני העוזר של {OWNER_NAME}. אפשר:\n'
                f'• "תזכיר ל{OWNER_NAME} שקבענו מסעדה בחמישי ב-20:00" — אבדוק את היומן שלו, ואחרי שתאשרי זה נכנס\n'
                f'• "מה יש ל{OWNER_NAME} השבוע?" · "הוא פנוי ביום שישי?"\n'
                f'• "תוסיפי חלב לרשימת הקניות" · "מה ברשימת הקניות?" (רשימות משותפות עם {OWNER_NAME})\n'
                f'• "תזכיר לי מחר ב-9 …" (תזכורות שלך)\n'
                f'{OWNER_NAME} מקבל עדכון על כל מה שנוסף (ויכול לבטל).')


def _tell_proposer(item: dict, text: str) -> None:
    """After the owner decides on a partner's proposal, let her know (WhatsApp bridge)."""
    who = item.get('proposed_by')
    if not who:
        return
    try:
        from whatsapp import outbox
        outbox.send_text(who, text)
    except Exception as e:
        print(f'  ⚠️ partner notify failed: {e}')


def _notify_owner_added(st: dict, partner: str, events: list[dict], ids: list[str], titles: list[str],
                        conflicts: list[str]) -> None:
    """FYI to the owner after the partner added something, with a one-tap undo."""
    from integrations import gcal
    from whatsapp import outbox
    owner = outbox.owner_chat()
    if not owner or st is None or not ids:
        return
    key = secrets.token_hex(3)
    st.setdefault(owner, {}).setdefault('undo', {})[key] = {'ids': ids, 'titles': titles, 'proposed_by': partner}
    body = '\n\n'.join(gcal.describe(e) for e in events if e['title'] in titles)
    warn = ('\n\n⚠️ שים לב:\n' + '\n'.join(conflicts)) if conflicts else ''
    outbox.send_options(owner, f'💌 {PARTNER_NAME} הוסיפה ליומן שלך:\n\n{body}{warn}',
                        [('👍 סבבה', f'noop:{key}'), ('↩️ לבטל', f'cal:undo:{key}')], busy='hold')


def _forward_to_owner(st: dict, partner: str, item: dict) -> bool:
    from integrations import gcal
    from whatsapp import outbox
    owner = outbox.owner_chat()
    if not owner or st is None:
        return False
    pid = secrets.token_hex(4)
    st.setdefault(owner, {}).setdefault('pending', {})[pid] = {
        'events': item['events'], 'ts': datetime.now().isoformat(timespec='seconds'), 'proposed_by': partner}
    body = '\n\n'.join(gcal.describe(e) for e in item['events'])
    warn = ('\n\n⚠️ שים לב:\n' + '\n'.join(item['conflicts'])) if item.get('conflicts') else ''
    audit(partner, 'partner_proposal', {'events': item['events'], 'conflicts': item.get('conflicts')})
    return outbox.send_options(owner, f'💌 {PARTNER_NAME} מציעה להוסיף ליומן שלך:\n\n{body}{warn}',
                               [('✅ להוסיף', f'cal:ok:{pid}'), ('❌ לא', f'cal:no:{pid}')], busy='hold')


def _partner_recent(u: dict, minutes: int = 15) -> str:
    """The partner's last few turns: "החתונה ב19:30" leans on "תוסיף שאני ב22.11 בחתונה" of a minute ago."""
    rows = []
    for h in (u.get('history') or [])[-6:]:
        try:
            if datetime.now() - datetime.fromisoformat(h.get('ts', '')) <= timedelta(minutes=minutes):
                rows.append(f"{PARTNER_NAME if h.get('role') == 'user' else 'הבוט'}: {h.get('text', '')[:300]}")
        except ValueError:
            continue
    return '\n'.join(rows)


CAL_CLAIM_RX = re.compile(r'(?:הוסיפה|הוספתי|הוסיף|נוסף|נוספה|נוספו|רשמה|קבעה)\s+(?:\S+\s+){0,3}?(?:ליומן|ביומן)')
CAL_ASK_RX = re.compile(r'תזכיר|קבענו|תוסיף|תוסיפי|תכניס|תרשום|ליומן|ביומן')
DATE_OR_TIME_RX = re.compile(r'(?<!\d)\d{1,2}[./:]\d{1,2}(?!\d)|ב-?\d{1,2}(?!\d)|בשעה|'
                             r'(?:^|\s)(?:ב|ו?ב)?(?:ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת|מחר|מחרתיים)(?:\s|$)')


def _partner_cal_followup(text: str, recent: str) -> bool:
    """A short date/time note right after the partner asked for something in the calendar ("22.11 בשעה 19:30") completes that
    request — it isn't a message for the owner."""
    if not recent or len(text.split()) > 8 or not DATE_OR_TIME_RX.search(text):
        return False
    hers = [ln for ln in recent.splitlines() if ln.startswith(f'{PARTNER_NAME}:')]
    return any(CAL_ASK_RX.search(ln) for ln in hers)


def _partner_calendar(u: dict, text: str, recent: str) -> list[Out]:
    """The partner's calendar request → events parsed (with her last messages for a missing date / time) → her ✅."""
    from integrations import gcal
    ctx = (f'(ההודעות הקודמות בשיחה, רק כדי להשלים פרט שחסר בהודעה החדשה, כמו תאריך או שעה. לא ליצור מהן אירוע נוסף:\n'
           f'{recent})\nההודעה החדשה: ') if recent else ''
    events, question = gcal.parse_events(
        f'(בקשה של {PARTNER_NAME}, בת הזוג של {OWNER_NAME}, ליומן שלו) {ctx}{text}')
    if question or not events:
        return [Out('text', f"🤔 {question or 'מתי בדיוק?'}")]
    for e in events:
        e['notes'] = (e.get('notes', '') + f' · הוצע ע"י {PARTNER_NAME}').strip(' ·')
    found = gcal.conflicts(events, hide_private=True)
    item = {'events': events, 'conflicts': found}
    body = '\n\n'.join(gcal.describe(e) for e in events)
    pid = secrets.token_hex(4)
    u.setdefault('pending', {})[pid] = item
    if found:
        return [Out('buttons', f'{body}\n\n🤔 רגע — אצל {OWNER_NAME} סביב התאריך הזה:\n' + '\n'.join(found) +
                    '\n\nבטוחה? להוסיף בכל זאת?',
                    buttons=[('✅ כן, תוסיף', f'pp:ok:{pid}'), ('❌ לא', f'pp:no:{pid}')])]
    return [Out('buttons', f'{body}\n\n✅ אין התנגשויות ביומן של {OWNER_NAME}. להוסיף?',
                buttons=[('✅ תוסיף', f'pp:ok:{pid}'), ('❌ לא', f'pp:no:{pid}')])]


@api_only()                                             # The partner's words go to the API, like a group's and a friend's
def _partner(msg: In, u: dict, st: dict) -> list[Out]:
    from integrations import gcal
    text = (msg.text or '').strip()
    if msg.kind == 'button':
        if (msg.payload or '').startswith('oq:'):             # 19/11 which of her two open questions (outbox)
            return _which_question(msg, msg.payload)
        if (msg.payload or '').startswith('rm:'):
            return _set_reminders(msg, u, msg.payload)
        if (msg.payload or '').startswith('rmd:'):
            from agent import life
            _, op, rid = (msg.payload.split(':', 2) + ['', ''])[:3]
            return [Out('text', life.reminder_action(msg.sender, op, rid))]
        if (msg.payload or '').startswith('msg:'):
            return _confirm_action(msg, u, msg.payload)
        _, op, pid = ((msg.payload or '').split(':', 2) + ['', ''])[:3]
        item = (u.get('pending') or {}).pop(pid, None) if msg.payload.startswith('pp:') else None
        if not item:
            return [Out('text', 'הבקשה כבר לא בתוקף — שלחי שוב.')]
        if op != 'ok':
            return [Out('text', '👍 בוטל, לא הוספתי.')]
        from whatsapp import outbox
        if (st or {}).get(outbox.owner_chat(), {}).get('paused'):
            return [Out('text', f'⏸️ {OWNER_NAME} עצר כרגע הוספות ליומן.')]
        ids, added, skipped = _add_events(msg.sender, item['events'])
        _notify_owner_added(st, msg.sender, item['events'], ids, added, item.get('conflicts') or [])
        text = (f'✅ נוסף ליומן של {OWNER_NAME}: ' + ', '.join(added) + f'. הוא קיבל עדכון.') if added else ''
        if skipped:
            text += ('\n' if text else '') + '↩️ כבר היה ביומן: ' + ', '.join(skipped)
        return [Out('text', text)] + _reminder_offer(u, ids, item['events'])
    if msg.kind in ('image', 'voice') and msg.media and not u.get('_media_reroute'):
        # 03/11 (The owner: "בתמונות של בת הזוג שיבין מה יש בהן"): a photo / voice note got the menu. Now it's understood and
        # goes on as if she had typed it: an invitation → a calendar proposal (The owner still approves), a list → her
        # lists, anything else → an answer about it.
        said = transcribe(Path(msg.media)) if msg.kind == 'voice' else _image_as_text(Path(msg.media), text)
        if not said:
            return [Out('text', '🙈 לא הצלחתי לקרוא את זה — אפשר לנסות שוב או לכתוב במילים?')]
        u['_media_reroute'] = True
        try:
            return _partner(In(msg.sender, 'text', text=said, channel=msg.channel, role=msg.role,
                               reply_to=msg.reply_to, context=msg.context, forwarded=msg.forwarded), u, st)
        finally:
            u.pop('_media_reroute', None)
    if msg.kind != 'text' or not text:
        return [Out('text', PARTNER_MENU)]
    from agent import feedback
    if feedback.is_feedback(text):
        return [Out('text', feedback.record(u, text, role='partner'))]
    if (fb := feedback.add_comment(u, text)) is not None:
        return [Out('text', fb)]
    if THANKS.fullmatch(text):
        return [Out('text', 'בכיף 🙂')]
    if m := MSG_TO_OWNER.match(text):
        return _offer_message(u, 'owner', m.group(1), PARTNER_NAME)
    life_outs = _life(msg, u, text)                 # shared lists, her own reminders and memory
    if life_outs is not None:
        return life_outs
    from agent.daytools import parse_day, day_overview
    if re.search(r'פנוי|עושה|יש לו|מה יש ל|תוכניות|עסוק', text) and not re.search(r'תזכיר|קבענו|תוסיף|תכניס|תרשום', text):
        day = parse_day(text)
        if day and not re.search(r'השבוע', text):
            return [Out('text', day_overview(day, hide_private=True).replace('אין כלום ביומן', f'אין ל{OWNER_NAME} כלום ביומן'))]
    if is_exam_time_q(text):
        return _exam_time(text, partner=True)
    if EXAM_WORD.search(text) and EXAM_ASK.search(text) and gcal.configured():      # 07/10 review: got the menu
        return [Out('text', gcal.upcoming_text(hide_private=True, query=exam_query(text)))]
    if WEB_RX.search(text):
        from integrations.websearch import ask_web
        return [Out('text', ask_web(text, PARTNER_NAME))]
    if not gcal.configured():
        return [Out('text', f'היומן של {OWNER_NAME} עוד לא מחובר.')]
    from bot.brain import classify
    recent = _partner_recent(u)
    action = (classify(text, recent).get('action') or '')
    if action == 'calendar_list' or re.search(r'פנוי|יש לו|מה יש ל|השבוע|ביומן', text) and \
            not re.search(r'תזכיר|קבענו|תוסיף|תכניס|תרשום', text):
        day = None
        if re.search(r'מחר', text):
            day = date.today() + timedelta(days=1)
        elif re.search(r'היום', text):
            day = date.today()
        return [Out('text', gcal.day_text(day, hide_private=True) if day
                    else gcal.week_text(date.today(), hide_private=True))]
    if action in ('calendar_add', 'reminder') or re.search(r'תזכיר|קבענו|תוסיף|תכניס|תרשום', text) \
            or _partner_cal_followup(text, recent):
        return _partner_calendar(u, text, recent)
    if len(text.split()) > 2:
        from agent import assistant
        d = assistant.decide_partner(text, msg.sender, _history(u))
        if d.get('to_owner') and CAL_CLAIM_RX.search(d['to_owner']):
            # 09/11: "22.11 בשעה 19:30" became a message to the owner saying "בת הזוג הוסיפה אירוע ליומן: חתונה…" — nothing was
            # added. A note to the owner that says something went into the calendar is a calendar proposal instead.
            print(f"  ⚠️ partner note claimed a calendar add: {d['to_owner'][:120]!r}")
            return _partner_calendar(u, text, recent)
        if d.get('to_owner'):
            return _offer_message(u, 'owner', d['to_owner'], PARTNER_NAME)
        if d.get('reply') and CLAIM_RX.search(d['reply']):        # 26/10: never claim an action from here either
            print(f"  ⚠️ partner assistant claimed an action: {d['reply'][:120]!r}")
            return [Out('text', f'⚠️ את זה עוד לא עשיתי. אפשר לכתוב למשל "תזכירי ל{OWNER_NAME} ש…" או '
                                f'"תגידי ל{OWNER_NAME} ש…" — ואני אעביר.')]
        if d.get('reply'):
            return [Out('text', d['reply'])]
    return [Out('text', PARTNER_MENU)]


# ── media ───────────────────────────────────────────────────────────────────

def _dated_offer(u: dict, dated: str, intro: str = '📅 מצאתי שם תאריכים:') -> list[Out]:
    """Dates found in a document → ONE '✅ להוסיף ליומן?' (the usual pending-calendar flow)."""
    if not dated or u.get('paused') or re.fullmatch(r'\s*אין\s*\.?\s*', dated):
        return []
    from integrations import gcal
    try:
        events, _q = gcal.parse_events(dated) if gcal.configured() else ([], None)
    except Exception:
        events = []
    if not events:
        return []
    pid = secrets.token_hex(4)
    u.setdefault('pending', {})[pid] = {'events': events, 'ts': datetime.now().isoformat(timespec='seconds')}
    body = '\n\n'.join(gcal.describe(ev) for ev in events)
    ask = 'להוסיף ליומן?' if len(events) == 1 else f'להוסיף את כל {len(events)} ליומן?'
    return [Out('buttons', f'{intro}\n\n{body}\n\n{ask}', buttons=[('✅ הוסף', f'cal:ok:{pid}'), ('❌ לא צריך', f'cal:no:{pid}')])]


def _nlm_doc(u: dict, ref: str, kind: str, name: str, question: str = '') -> list[Out] | None:
    """14/10: a link / PDF / Word file → a source in NotebookLM (the connected Google account — no API cost), and it's
    remembered for later. None → use the old Gemini path (NotebookLM down / unsupported)."""
    from agent import nlm_ask, nlm_docs, secretary
    if nlm_ask._broken_recently():
        return None
    try:
        sid = nlm_docs.add(ref, kind, name)
        answer, conv = nlm_docs.ask(question or nlm_docs.SUMMARY_Q, [sid])
        dated = '' if question else nlm_docs.ask(nlm_docs.DATED_Q, [sid])[0]
    except Exception as e:
        print(f'  ⚠️ NotebookLM document failed: {e!r}'[:300])
        nlm_ask._mark(True)
        return None
    nlm_ask._mark(False)
    secretary.remember(u, Path(ref) if kind == 'file' else None, name, url=ref if kind == 'url' else '',
                       answer='' if question else answer)
    u['doc'].update(nlm_source=sid, nlm_conv=conv)
    try:                                                       # the Obsidian brain keeps a readable copy
        from agent import brain
        brain.note_doc(name, kind, ref, answer)
    except Exception as e:
        print(f'  ⚠️ brain note failed: {e!r}'[:200])
    return ([Out('text', f'📎 *{name}*\n\n{answer}')] + _dated_offer(u, dated)
            + _kept_hint(u))


# 15/11 "יש לך מספר הזמנה" five days after the booking PDF: the read lasted 30 minutes ("(חצי שעה)"). Now it's kept.
KEPT_HINT = '💬 שמרתי את זה אצלי — אפשר לשאול עליו גם בעוד שבוע ("מה היה כתוב ב…")'
HINT_DAYS = 7


def _kept_hint(u: dict) -> list['Out']:
    """17/11 the same "💬 שמרתי את זה אצלי… ("מה מספר ההזמנה")" came as its own message under every link, a LinkedIn post
    too. Said once a week: what he already knows is noise."""
    last = u.get('kept_hint_ts') or ''
    try:
        if last and datetime.now() - datetime.fromisoformat(last) < timedelta(days=HINT_DAYS):
            return []
    except ValueError:
        pass
    u['kept_hint_ts'] = datetime.now().isoformat(timespec='seconds')
    return [Out('text', KEPT_HINT)]


def _keep_file(msg: 'In', u: dict, before, outs: list['Out']) -> list['Out']:
    """What the owner himself sent and the bot just read (u['doc'] is new and has its summary) → agent/my_files, for good."""
    d = u.get('doc') or {}
    if msg.role == 'owner' and not msg.group and d and d.get('ts') != before and (d.get('answer') or '').strip():
        try:
            from agent import my_files
            my_files.add(msg.sender, d.get('name', ''), d['answer'], d.get('keys', ''), path=d.get('path') or None,
                         url=d.get('url', ''))
        except Exception as e:
            print(f'  ⚠️ my_files add: {e!r}'[:200])
    return outs


CHAT_Q = ('זו שיחה מקבוצת וואטסאפ. סכם בעברית לוואטסאפ: *על מה דיברו* (עד 5 נקודות), *החלטות*, '
          '*מה מבקשים ממני / משימות*, ו-*תאריכים ודדליינים*. רק ממה שכתוב בשיחה.')
CHAT_LINE = re.compile(r'(?m)^\[?\d{1,2}[./]\d{1,2}[./]\d{2,4},? \d{1,2}:\d{2}')
FWD_ACT = re.compile(r'^\s*(?:תסכם|סכם|תסכמי|מה (?:מבקשים|רוצים) ממני|מה החלטנו|מה הדדליינים|מה חשוב|תוסיף ליומן את)')


def _forwarded(msg: 'In', u: dict, text: str) -> list['Out']:
    """14/10: forwarded group messages are collected — never run as commands ("תזכיר לי…" from someone else in a group
    must not set the owner's reminder). One acknowledgement per burst; "תסכם" then summarises them."""
    now = datetime.now()
    buf = u.setdefault('fwd', {'items': [], 'ack': ''})
    if buf['items'] and now - datetime.fromisoformat(buf['items'][-1]['ts']) > timedelta(minutes=30):
        buf['items'] = []
    buf['items'] = (buf['items'] + [{'t': text[:3000], 'ts': now.isoformat(timespec='seconds')}])[-300:]
    if buf.get('ack') and now - datetime.fromisoformat(buf['ack']) < timedelta(seconds=90):
        return []
    buf['ack'] = now.isoformat(timespec='seconds')
    return [Out('text', '📥 קיבלתי הודעות מועברות. מה לעשות איתן? למשל "תסכם" · "מה מבקשים ממני?" · '
                        '"תוסיף ליומן את התאריכים" (תוך חצי שעה).')]


def _forwarded_act(msg: 'In', u: dict, text: str) -> list['Out'] | None:
    buf = u.get('fwd') or {}
    items = buf.get('items') or []
    if not items or not FWD_ACT.match(text) or \
            datetime.now() - datetime.fromisoformat(items[-1]['ts']) > timedelta(minutes=30):
        return None
    from agent import secretary
    d = secretary.DOCS
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"forwarded_{datetime.now():%Y%m%d_%H%M%S}.txt"
    path.write_text('\n\n'.join(i['t'] for i in items), encoding='utf-8')
    u['fwd'] = {'items': [], 'ack': ''}
    q = CHAT_Q if re.match(r'^\s*(?:תסכם|סכם|תסכמי)\s*(?:את זה|לי|אותן)?\s*[.!?]*\s*$', text) else text
    return _secretary(u, path, f'{len(items)} הודעות מועברות', q)


def _unzip_chat(path: Path) -> Path | None:
    """A WhatsApp chat export (.zip) → its chat .txt."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            txt = next((n for n in z.namelist() if n.lower().endswith('.txt')), None)
            if not txt or z.getinfo(txt).file_size > 15 * 1024 * 1024:
                return None
            out = path.with_suffix('.txt')
            out.write_bytes(z.read(txt))
            return out
    except (zipfile.BadZipFile, OSError):
        return None


def _secretary(u: dict, path: Path, name: str, question: str = '', what: str = '') -> list[Out]:
    """Read a link/file/photo → answer (only from its content) + offer its dates for the calendar (✅ flow)."""
    from agent import secretary, nlm_docs
    if nlm_docs.supported(path) and (via := _nlm_doc(u, str(path), 'file', name, question)) is not None:
        return via
    try:
        r = secretary.ask(path, question, what)
    except Exception as e:
        print(f'  ⚠️ secretary failed: {e}')
        return [Out('text', '⚠️ לא הצלחתי לקרוא את זה עכשיו. נסה שוב עוד רגע.')]
    secretary.remember(u, path, name, answer='' if question else r['answer'], keys=r.get('keys', ''))
    outs = [Out('text', f'📎 *{name}*\n\n{r["answer"]}')] + _dated_offer(u, r['dated'])
    outs += _kept_hint(u)
    return outs


# words written to other people (a post he shares), not to the bot: plural asks, "היי כולם"
SHARE_RX = re.compile(r'(?<![א-ת])(?:תיכנסו|תכנסו|תעשו\s+לייק|תגיבו|תשתפו|תפרגנו|תצביעו|הצביעו|היי\s+כולם|'
                      r'אשמח\s+(?:מאוד\s+|מאד\s+)?אם\s+ת[א-ת]+ו)(?![א-ת])')


def _link(u: dict, text: str) -> list[Out]:
    from agent import secretary
    url = secretary.URL_RX.search(text).group(0).rstrip('.,)!?')
    question = secretary.URL_RX.sub('', text).strip()
    if SHARE_RX.search(question) and '?' not in question:
        # 17/11 12:44 his LinkedIn post with "אשמח מאוד אם תיכנסו, תעשו לייק" (words for his friends) was read as a
        # request to the bot: a rewrite of his message and an offer of more versions. It's the link he shared: read it
        question = ''
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ''
    if host and secretary._safe_host(host) and (via := _nlm_doc(u, url, 'url', host, question)) is not None:
        return via
    try:
        path, title = secretary.save_link(url)
    except secretary.Unreadable as e:              # 403 / JavaScript page → let Google's side read it
        try:
            answer = secretary.ask_url(url, question)
        except Exception as e2:
            return [Out('text', f'🔗 {e2 if isinstance(e2, ValueError) else e}')]
        secretary.remember(u, None, url, url=url, answer='' if question else answer)
        return [Out('text', f'🔗 {answer}')] + _kept_hint(u)
    except ValueError as e:
        return [Out('text', f'🔗 {e}')]
    except Exception as e:
        print(f'  ⚠️ link fetch failed: {e}')
        return [Out('text', '🔗 לא הצלחתי לפתוח את הקישור (האתר לא ענה). נסה שוב, או שלח צילום מסך / PDF.')]
    return _secretary(u, path, title, question, what=f'קישור ({url})')


def _image(msg: In, caption: str, u: dict | None = None) -> list[Out]:
    from generators.practice_grader import resolve_set, grade
    m = re.search(r'תרגיל\s*(\d+)', caption or '')
    found = resolve_set(msg.sender, msg.reply_to, None)
    if found and (m or found.get('via_reply')):
        r = grade(found['md'], found['course'], [msg.media], int(m.group(1)) if m else None)
        return [Out('text', r['text'])]
    if u is not None and msg.media:                    # a photo of a letter / invitation / screenshot / exercise
        return _secretary(u, Path(msg.media), 'התמונה', caption or '')
    import os
    import google.genai as genai
    from google.genai import types as T
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    mime = 'image/png' if str(msg.media).lower().endswith('.png') else 'image/jpeg'
    resp = client.models.generate_content(
        model='gemini-3.8-flash',
        contents=[T.Part.from_bytes(data=Path(msg.media).read_bytes(), mime_type=mime),
                  caption or 'נתח את התמונה. אם זה תרגיל — הסבר איך ניגשים אליו צעד-צעד. '
                             'אם זה לוח/שקף — חלץ והסבר. ענה בעברית, בכתיב מתמטי פשוט (בלי LaTeX).'],
        config=T.GenerateContentConfig(max_output_tokens=3000, temperature=0.2,
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, 'gemini-3.8-flash', op='agent.image')
    return [Out('text', (resp.text or '').strip() or '⚠️ לא הצלחתי לנתח את התמונה.')]


IMAGE_AS_TEXT = """{who} שלחה את התמונה הזו לעוזר בוואטסאפ{cap}. כתוב בעברית, בגוף ראשון כאילו היא כותבת, מה היא מבקשת או
מספרת דרך התמונה — הודעה אחת שאפשר לפעול לפיה. הזמנה / כרטיס / צילום מסך של אירוע → "תוסיף ליומן של המשתמש: <מה>,
<תאריך>, <שעה>, <מקום>" עם כל הפרטים שבתמונה. רשימה (קניות / משימות) → "תוסיף לרשימה: …". קבלה / מסמך / מכתב →
מה כתוב בו בקצרה ומה צריך לעשות. סתם תמונה → מה רואים בה ושאלה או תגובה טבעית. בלי להמציא פרטים שלא בתמונה."""


def _image_as_text(path: Path, caption: str = '', who: str | None = None) -> str:
    """A photo → what the sender is saying with it, as one message the text path can act on. '' on failure."""
    try:
        from utils.budget import budget_lock_reason
        if budget_lock_reason():
            return ''
        import os
        import google.genai as genai
        from google.genai import types as T
        client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
        mime = 'image/png' if str(path).lower().endswith('.png') else 'image/jpeg'
        prompt = IMAGE_AS_TEXT.format(who=who or PARTNER_NAME, cap=f' עם הכיתוב "{caption}"' if caption else '')
        resp = client.models.generate_content(
            model='gemini-3.8-flash', contents=[T.Part.from_bytes(data=path.read_bytes(), mime_type=mime), prompt],
            config=T.GenerateContentConfig(max_output_tokens=800, temperature=0.2,
                                           thinking_config=T.ThinkingConfig(thinking_level='low')))
        from utils.usage_tracker import log_gemini
        log_gemini(resp, 'gemini-3.8-flash', op='agent.image_as_text')
        return (resp.text or '').strip()
    except Exception as e:
        print(f'  ⚠️ image → text failed: {e!r}'[:200])
        return ''


def transcribe(path: Path) -> str:
    """Hebrew voice note → text (OpenAI, same model as lecture transcription). '' on failure."""
    try:
        from utils.budget import budget_lock_reason
        if budget_lock_reason():
            return ''
        import os
        from openai import OpenAI
        client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
        with open(path, 'rb') as f:
            try:
                r = client.audio.transcriptions.create(model='gpt-4o-transcribe', file=f, language='he')
            except Exception:
                f.seek(0)
                r = client.audio.transcriptions.create(model='whisper-1', file=f, language='he')
        try:
            from utils.usage_tracker import log_whisper
            log_whisper(max(1.0, Path(path).stat().st_size / 2000), op='agent.voice')   # ~16 kbps opus
        except Exception:
            pass
        return (getattr(r, 'text', '') or '').strip()
    except Exception as e:
        print(f'  ⚠️ transcription failed: {e}')
        return ''
