"""
agent/group_helper.py — the bot answers the owner's friends in a group, about the public project only (17/11).

The owner gave the project (SSA_STARTER_REPO) to his friends; they ask him setup questions in their group. He wants the
bot to answer instead, as an experiment, and to answer them very well. Opt-in per group and per friend, all by the owner:
  "@בוט תענה לחברים כאן"        this group: answer the friends I mark (also turns the group on)
  "@בוט תענה ל<שם>"             on that friend's message (a reply) or tagging him: his sender id joins the list
  "@בוט תפסיק לענות ל<שם>"      removes him · "@בוט תפסיק לענות לחברים" / "@בוט שקט": off for the group
  "מה קרה בקבוצה?"              (in private, or tagging the bot there) what they asked and what it answered
Who a friend is comes from the sender id the bridge reads (never a name, never the words). The bridge passes a
friend's text only when the group is on, this mode is on and the sender is on the list (whatsapp/bridge.group_event).

Safety, the same shape as the debates (agent/group_mode.py): this path has no tools and writes no memory. It imports
nothing that holds the owner's data: the only knowledge is the public repo (agent/starter_help: its own NotebookLM notebook,
or the repo's chunks + one API call through gemini_sub.api_only). The friend's words go in a random-delimiter block.
Before any model: anything about the owner (his number, calendar, files, mail, keys, memory, where he is) or an attempt to
change the rules gets a fixed polite line and is logged. After the model: guard(mode='helper') — no phone, mail, key
value, tag-all, insult, impersonation or rule text, and a link only if the project's own guides carry it. Caps: per
friend and per group a day, a cooldown per friend (a message inside it waits and is answered with the next one), the
input cut at MAX_IN. Not sure → it says so and asks the owner in the group (plain text: his number is never written), and
The owner gets one private line without the friend's words. Every exchange goes to data/group_helper_log.jsonl with the
friend's id hashed, for "מה קרה בקבוצה?" and the weekly report.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from agent.group_mode import _read, _write, clean, guard, visible
from utils.gemini_sub import api_only

ROOT = Path(__file__).resolve().parent.parent
FRIENDS = ROOT / 'data' / 'group_friends.json'        # {gid: {on, allow: [sender ids], names: {id: label}}}
USAGE = ROOT / 'data' / 'group_helper_usage.json'     # counts and times only
PENDING = ROOT / 'data' / 'group_helper_pending.json'  # a message held by the cooldown; gone once answered
LOG = ROOT / 'data' / 'group_helper_log.jsonl'
FRIEND_DAY = 15
GROUP_DAY = 60
COOLDOWN = 20
MAX_IN = 600
MAX_PENDING = 2
NOTE_EVERY = 1800                                      # the private "a question waits for you" line, at most this often
LOG_KEEP = 3000
LAST_BLOCKED = ''

REFUSE = 'זה לא משהו שאני עונה עליו כאן. אני עונה רק על שאלות על הפרויקט 🙂'
NOPE = '🙂 את זה אני לא עושה. אני עונה כאן רק על שאלות על הפרויקט.'
INTRO = '🤖 כאן העוזר של המשתמש, בוט ולא המשתמש עצמו.'
ASK_OWNER_ID = '@המשתמש תוכל לעזור כאן?'
SMALL = '😄 אני העוזר של המשתמש, כאן רק בשביל שאלות על הפרויקט: הקמה, מפתחות, וואטסאפ, עלויות, תקלות.'
CAP = 'הגעתי למכסה היומית שלי כאן. המשתמש יענה 🙂'

# a speed bump in front of the model, never the control: what protects the owner is that nothing of his is on this path
OVERRIDE_RX = re.compile(
    r'ignore\s+(?:all|any|the|your|previous|prior|above)|disregard\s+(?:all|the|your|previous)|forget\s+(?:all|your|previous)|'
    r'system\s*prompt|developer\s+mode|jailbreak|you\s+are\s+now|new\s+instructions|reveal\s+your|'
    r'</?\s*(?:system|assistant|instructions?)\b|\[\s*(?:system|inst)\b|end\s+data|'
    r'התעלם|תתעלם|תשכח\s+(?:את\s+)?(?:כל\s+)?ה(?:הוראות|כללים)|שכח\s+(?:את\s+)?(?:כל\s+)?ה(?:הוראות|כללים)|'
    r'הוראות\s+(?:קודמות|חדשות|המערכת|מערכת)|ההוראות\s+שלך|הכללים\s+שלך|הפרומפט\s+שלך|מעכשיו\s+אתה|מהיום\s+אתה|'
    r'תתחזה|תעמיד\s+פנים|אני\s+המשתמש\b|כאן\s+המשתמש\b|'
    r'המשתמש\s+(?:אמר|ביקש|אישר|מאשר|הורה|מבקש|רוצה)\s+(?:ש)?(?:תשלח|תיתן|תתן|תראה|תעביר|תגלה|תתעלם|מותר|לך\s+ל|ממך\s+ל)', re.I)
_OWNER_WORDS = [w for w in re.split(r'\s+', os.environ.get('WA_OWNER_NAME', '').strip()) if len(w) > 1]   # his name, from .env
_ME = '(?:' + '|'.join(['(?:ה)?משתמש', 'הבעלים', 'owner'] + [re.escape(w) for w in _OWNER_WORDS]) + ')'
PERSONAL_RX = re.compile(
    rf'(?:מספר|טלפון|נייד|כתובת|מייל|אימייל|יומן|לו"?ז|לוח\s*זמנים|סיסמ|קבצ|הודעות|זיכרון|ציונ|מערכת\s*(?:ה)?שעות|'
    rf'תמונות|אנשי\s*(?:ה)?קשר|פגישות|משימות|תזכורות|חשבון)\S*\s+(?:ה?פרטי\S*\s+)?(?:של\s+|ל)?{_ME}\b|'
    rf'{_ME}\s+(?:איפה|נמצא|עושה|עסוק|פנוי|בבית|ישן|חזר|יוצא|עובד\s+היום|בצבא|בבסיס)|איפה\s+{_ME}|מה\s+{_ME}\s+עושה|'
    rf'מתי\s+{_ME}\s+(?:פנוי|חוזר|יוצא|בבית)|עם\s+מי\s+{_ME}|(?:יש|מה)\s+ל{_ME}|'
    rf'ל{_ME}\s+(?:יש\s+)?ב?(?:יומן|לו"?ז|מחר|היום|השבוע)|'
    r'(?:תשלח|שלח|תדביק|תראה|תן|תביא|תעביר|תקריא|send|show|paste|give|print|cat)\S*\s+(?:לי\s+|לנו\s+|me\s+|us\s+)?(?:את\s+)?'
    r'(?:ה-?|the\s+)?(?:\.env|מפתח|key|token|טוקן|סיסמ|password|master_token|storage_state|קובץ\s+ה?סודות)|'
    r'(?:\.env|מפתח|ה?key|token|טוקן|סיסמ|password)\S*\s+(?:שלך|של\s+המשתמש|שלכם|של\s+הבוט|שלו|שבשרת|ששמור|שיש\s+לך|of\s+owner|you\s+use)|'
    rf'(?:phone|number|calendar|e-?mail|address|files|password|schedule)\s+of\s+{_ME}|{_ME}\'?s\s+(?:phone|number|calendar|e-?mail|files|password|schedule)',
    re.I)
TOPIC_RX = re.compile(
    r'התקנ|להתקין|מתקינ|הגדר|\.env|מפתח|\bkey\b|api|gemini|ג.?מיני|וואטסאפ|ווטסאפ|whatsapp|סים|esim|sim\b|צימוד|pair|'
    r'יומן|calendar|oauth|שרת|server|oracle|אורקל|windows|ווינדוס|וינדוס|לינוקס|linux|mac\b|python|פייתון|pip\b|venv|'
    r'requirements|setup|שכב|layer|SSA_|WA_|TAU_|GEMINI|OPENAI|משתנ|smart.?study|מודל\b|moodle|panopto|פנופטו|notebooklm|תמלול|סיכומ|עולה|עלות|כסף|'
    r'תקציב|budget|דולר|\$|בדיק|tests?\b|pytest|ריפו|repo|github|גיטהאב|גיט|קוד|הבוט|העוזר|בוט|באג|bug|שגיאה|error|'
    r'traceback|לא\s+עובד|לא\s+עונה|נתקע|נופל|crash|תקלה|דשבורד|dashboard|טלגרם|telegram|cron|systemd|תזכורת|תזכורות|'
    r'אבטחה|בטוח|סיסמ|הרשא|גישה|פרטיות|privacy|security|deploy|פריסה|clone|שיבוט|הפרויקט|project|claude|קלוד|'
    r'מנוי|subscription|פרטנר|בת\s*זוג|בן\s*זוג|partner|הרשמה|לבטל|token|טוקן|מייל|gmail|courses\.yaml|קורס', re.I)
QUESTION_RX = re.compile(r'\?|^\s*(?:איך|מה|למה|איפה|מאיפה|אפשר|יש|צריך|כמה|מי|האם|מתי|איזה|איזו|זה|הוא|how|what|why|where|'
                         r'can|does|do|is|should|which)\b|לא\s+עובד|לא\s+עונה|שגיאה|error|נתקע|נכשל|failed|בעיה', re.I)
SMALLTALK_RX = re.compile(r'^\W*(?:ח{2,}|לול|lol|haha|תודה(?:\s+רבה)?|תנקס|thanks?|thx|אחלה|יא\s*גבר|גבר|אחי|סבבה|'
                          r'וואו|מלך|כפרה|🙏|😂|👍|❤️|🔥)\W*$', re.I)
THANKS_RX = re.compile(r'תודה|תנקס|thanks|thx|🙏', re.I)


def _h(s: str) -> str:
    return hashlib.sha256((s or '').encode()).hexdigest()[:10]


def _fid(m: dict) -> str:
    ids = sorted(str(x).split('@')[0].split(':')[0] for x in (m.get('sender') or []) if x)
    return ids[0] if ids else ''


# ── who is on (The owner's controls) ─────────────────────────────────────────────────────────────────────

def cfg() -> dict:
    d = _read(FRIENDS, {})
    return d if isinstance(d, dict) else {}


def _save_cfg(d: dict) -> None:
    _write(FRIENDS, d)


def gate_view() -> dict:
    """What the bridge may see: per group, on or not and the friends' sender ids. No names, no words."""
    return {gid: {'on': bool(g.get('on')), 'allow': sorted(g.get('allow') or [])} for gid, g in cfg().items()
            if g.get('allow') or g.get('on')}


def mode(gid: str) -> dict:
    return cfg().get(gid) or {}


def set_mode(gid: str, on: bool) -> dict:
    d = cfg()
    g = d.setdefault(gid, {'allow': [], 'names': {}})
    g['on'] = on
    _save_cfg(d)
    return g


def add(gid: str, ids: list[str], label: str = '') -> list[str]:
    """→ the labels of who was added."""
    d = cfg()
    g = d.setdefault(gid, {'on': True, 'allow': [], 'names': {}})
    out = []
    for i in ids:
        i = str(i).split('@')[0].split(':')[0]
        if not i:
            continue
        if i not in g['allow']:
            g['allow'].append(i)
        n = clean(label)[:20] if label and len(ids) == 1 else ''
        g.setdefault('names', {})[i] = n or g['names'].get(i) or f'חבר {len(g["names"]) + 1}'
        out.append(g['names'][i])
    _save_cfg(d)
    return out


def remove(gid: str, ids: list[str]) -> list[str]:
    d = cfg()
    g = d.get(gid) or {}
    out = []
    for i in ids:
        i = str(i).split('@')[0].split(':')[0]
        if i in (g.get('allow') or []):
            g['allow'].remove(i)
            out.append((g.get('names') or {}).get(i, 'החבר'))
    if g:
        _save_cfg(d)
    return out


def label_of(gid: str, fid: str) -> str:
    return ((mode(gid).get('names') or {}).get(fid)) or 'חבר'


# ── counts ─────────────────────────────────────────────────────────────────────────────────────────

def _usage() -> dict:
    u = _read(USAGE, {}) or {}
    today = date.today().isoformat()
    if u.get('day') != today:
        u = {'day': today, 'groups': {}, 'friends': {}, 'last': u.get('last') or {}, 'said': {}, 'note': u.get('note', 0)}
    return u


def _said_once(u: dict, key: str) -> bool:
    """True the first time `key` comes up today (an intro, a small-talk line, the cap line)."""
    if u.setdefault('said', {}).get(key):
        return False
    u['said'][key] = 1
    return True


def log(event: str, gid: str, fid: str, q: str = '', a: str = '', **kw) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {'ts': datetime.now().isoformat(timespec='seconds'), 'event': event, 'group': _h(gid), 'friend': _h(fid),
               'name': label_of(gid, fid) if fid else '', 'q': clean(q)[:300], 'a': (a or '')[:600], **kw}
        new = not LOG.exists()
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        if new:
            os.chmod(LOG, 0o600)
        if LOG.stat().st_size > LOG_KEEP * 1200:                   # keep the last LOG_KEEP rows
            rows = LOG.read_text(encoding='utf-8').splitlines()[-LOG_KEEP:]
            LOG.write_text('\n'.join(rows) + '\n', encoding='utf-8')
    except OSError:
        pass


# ── a friend's message ─────────────────────────────────────────────────────────────────────────────

def screen(raw: str) -> str:
    """Before any model: 'override' (changing the rules), 'personal' (The owner's things), or ''."""
    t = visible(raw)
    if OVERRIDE_RX.search(t):
        return 'override'
    if PERSONAL_RX.search(t):
        return 'personal'
    return ''


def wanted(m: dict, raw: str) -> str:
    """Does this message ask the bot anything? 'ask' | 'small' (addressed small talk) | '' (not for the bot)."""
    addressed = bool(m.get('tagged') or m.get('reply_to_bot'))
    if m.get('type', 'text') != 'text' or not raw.strip():
        return ''
    if addressed:
        return 'small' if SMALLTALK_RX.match(raw) or (len(raw.split()) <= 2 and not TOPIC_RX.search(raw) and '?' not in raw) else 'ask'
    if m.get('tags_other') or m.get('reply_to_other'):          # a friend asking another friend
        return ''
    return 'ask' if TOPIC_RX.search(raw) and QUESTION_RX.search(raw) else ''


@api_only()
def reply_for(question: str, path: str = '') -> tuple[str, str, str]:
    """The answer path itself, without caps or logs (scripts/repo_qa_eval.py runs this): → (status, text, why).
    status: ok | none | offtopic | refused | blocked | error. text is what would be sent ('' = nothing)."""
    from agent import starter_help
    why = screen(question)
    if why:
        return 'refused', (NOPE if why == 'override' else REFUSE), why
    q = clean(question)[:MAX_IN]
    st, text, used = starter_help.answer(q, path)
    if st == 'offtopic':
        return st, '', used
    if st == 'error':
        return st, f'לא הצלחתי לבדוק בפרויקט כרגע. {ASK_OWNER_ID}', used
    text = visible(text).strip()
    hit = guard(text, starter_help.allowed_links(), mode='helper')
    if hit:
        global LAST_BLOCKED
        LAST_BLOCKED = text                                        # for scripts/repo_qa_eval.py -v; never sent
        return 'blocked', f'לא שלחתי את התשובה: יצא בה משהו שאסור לי לכתוב כאן. {ASK_OWNER_ID}', hit
    if st == 'none':
        return st, f'🛠️ {text}\n{ASK_OWNER_ID}', used
    return st, f'🛠️ {text}', used


def _note_owner(u: dict, gid: str, fid: str) -> list:
    """One private line to the owner: a question waits for him. Never the friend's words (they would reach his agent)."""
    now = time.time()
    if now - u.get('note', 0) < NOTE_EVERY:
        return []
    u['note'] = now
    from whatsapp import outbox
    chat = outbox.owner_chat()
    if chat:
        outbox.send_text(chat, f'❓ בקבוצה של החברים {label_of(gid, fid)} שאל משהו שלא ידעתי לענות עליו. '
                               'תציץ שם, או כתוב לי "מה קרה בקבוצה?".')
    return []


def _answer(gid: str, fid: str, q: str, u: dict, addressed: bool = True) -> list[str]:
    st, text, why = reply_for(q)
    u.setdefault('groups', {})[gid] = u['groups'].get(gid, 0) + 1
    u.setdefault('friends', {})[fid] = u['friends'].get(fid, 0) + 1
    log(st, gid, fid, q, text, why=why)
    if st in ('none', 'error', 'blocked'):
        _note_owner(u, gid, fid)
    if st == 'offtopic':
        return [SMALL] if addressed and _said_once(u, f'small:{fid}') else []
    if not text:
        return []
    if st in ('ok', 'none') and _said_once(u, f'intro:{fid}'):
        text = f'{INTRO}\n{text}'
    return [text]


def handle(m: dict, now: float | None = None) -> list[str]:
    """A friend's message the bridge passed → the replies (each quotes the message: whatsapp/worker)."""
    now = now or time.time()
    gid, fid = m.get('chat', ''), _fid(m)
    g = mode(gid)
    sender = {str(x).split('@')[0].split(':')[0] for x in (m.get('sender') or [])}
    if not g.get('on') or not fid or not sender & set(g.get('allow') or []) or m.get('forwarded') or m.get('edited'):
        return []
    raw = visible(m.get('text') or '')
    kind = wanted(m, raw)
    why = screen(raw)
    if not kind and not why:
        return []
    u = _usage()
    try:
        if why:                                                    # said once per friend a day; logged every time
            log('refused', gid, fid, raw, why=why)
            _count_blocked(gid, why)
            return [NOPE if why == 'override' else REFUSE] if _said_once(u, f'refuse:{fid}:{why}') else []
        if kind == 'small':
            line = 'בכיף 🙂' if THANKS_RX.search(raw) else SMALL
            return [line] if _said_once(u, f'small:{fid}:{line[:4]}') else []
        if u['friends'].get(fid, 0) >= FRIEND_DAY or u['groups'].get(gid, 0) >= GROUP_DAY:
            log('capped', gid, fid, raw)
            return [CAP] if _said_once(u, f'cap:{fid}') else []
        q = clean(raw)[:MAX_IN]
        if now - (u.get('last') or {}).get(fid, 0) < COOLDOWN:      # waits for the next turn of the clock
            p = _read(PENDING, {}) or {}
            k = f'{gid}|{fid}'
            held = p.get(k) or {'q': [], 'quote': {}, 'addressed': False}
            if len(held['q']) < MAX_PENDING:
                held['q'].append(q)
                held['addressed'] = held.get('addressed') or bool(m.get('tagged') or m.get('reply_to_bot'))
                held['quote'] = {'id': m.get('id', ''), 'participant': m.get('participant', ''), 'text': q[:200]}
                p[k] = held
                _write(PENDING, p)
            return []
        u.setdefault('last', {})[fid] = now
        _write(USAGE, u)                                           # before the call: a crash can't repeat it
        return _answer(gid, fid, q, u, bool(m.get('tagged') or m.get('reply_to_bot')))
    finally:
        _write(USAGE, u)


def _count_blocked(gid: str, why: str) -> None:
    try:
        from agent import group_mode
        group_mode.blocked(gid, f'helper_{why}')
    except Exception:
        pass


def tick(now: float | None = None) -> list[tuple[str, str, dict]]:
    """Messages held by the cooldown get their answer: → (group, text, the message to quote)."""
    now = now or time.time()
    p = _read(PENDING, {}) or {}
    if not p:
        return []
    out = []
    u = _usage()
    for k in list(p):
        gid, _, fid = k.partition('|')
        if now - (u.get('last') or {}).get(fid, 0) < COOLDOWN:
            continue
        held = p.pop(k)
        _write(PENDING, p)
        g = mode(gid)
        if not g.get('on') or fid not in (g.get('allow') or []):
            continue
        if u['friends'].get(fid, 0) >= FRIEND_DAY or u['groups'].get(gid, 0) >= GROUP_DAY:
            continue
        u.setdefault('last', {})[fid] = now
        _write(USAGE, u)
        out += [(gid, t, held.get('quote') or {}) for t in _answer(gid, fid, '\n'.join(held['q'])[:MAX_IN], u, bool(held.get('addressed')))]
    _write(USAGE, u)
    return out


def clear_pending() -> None:
    _write(PENDING, {})


# ── what the owner sees ──────────────────────────────────────────────────────────────────────────────────

REPORT_RX = re.compile(r'\s*(?:מה\s+(?:קרה|קורה|היה|הלך)\s+(?:ב|עם\s+ה)קבוצה(?:\s+של\s+החברים)?|מה\s+החברים\s+שאלו|'
                       r'מה\s+שאלו\s+(?:אותך\s+)?(?:ב)?(?:קבוצה|החברים)|דוח\s+(?:ה)?קבוצה)\s*[?.!]*\s*', re.I)


def _rows(days: int = 7) -> list[dict]:
    since = datetime.now() - timedelta(days=days)
    out = []
    try:
        for ln in LOG.read_text(encoding='utf-8').splitlines():
            try:
                r = json.loads(ln)
                if datetime.fromisoformat(r['ts']) >= since:
                    out.append(r)
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return out


def _snip(s: str, n: int = 70) -> str:
    s = re.sub(r'\s+', ' ', clean(s or '')).strip()
    if screen(s):
        return '(ניסיון עקיפה, נחסם)'
    return s if len(s) <= n else s[:n - 1] + '…'


def report(days: int = 7) -> str:
    """"מה קרה בקבוצה?" → counts per friend, the last exchanges, what it didn't know. The friends' words are cut short
    and an attempt at the rules is shown only as such: this text reaches the owner's own chat."""
    rows = _rows(days)
    if not rows:
        on = [g for g in cfg().values() if g.get('on')]
        return ('👥 עוד לא שאלו אותי כלום בקבוצה של החברים.' if on else
                '👥 מצב "תענה לחברים" לא פעיל באף קבוצה. כדי להפעיל: בקבוצה, תתייג אותי ותכתוב "תענה לחברים כאן".')
    asked = [r for r in rows if r['event'] in ('ok', 'none', 'error', 'blocked', 'offtopic')]
    ok = sum(r['event'] == 'ok' for r in asked)
    unsure = [r for r in asked if r['event'] in ('none', 'error', 'blocked')]
    refused = [r for r in rows if r['event'] == 'refused']
    capped = sum(r['event'] == 'capped' for r in rows)
    lines = [f'👥 *הקבוצה של החברים, {days} ימים אחרונים*',
             f'{len(asked)} שאלות · {ok} נענו · {len(unsure)} לא ידעתי · {len(refused)} סירבתי' + (f' · {capped} מעבר למכסה' if capped else '')]
    per: dict[str, list] = {}
    for r in asked:
        per.setdefault(r.get('name') or 'חבר', []).append(r)
    if per:
        lines.append('מי שאל: ' + ' · '.join(f'{n} {len(v)}' for n, v in sorted(per.items(), key=lambda x: -len(x[1]))))
    if unsure:
        lines += ['', '❓ *לא ידעתי לענות:*'] + [f"• {r.get('name') or 'חבר'}: {_snip(r.get('q'))}" for r in unsure[-5:]]
    last = [r for r in asked if r['event'] == 'ok'][-5:]
    if last:
        lines += ['', '*אחרונות:*'] + [f"• {r.get('name') or 'חבר'}: {_snip(r.get('q'), 60)} → {_snip((r.get('a') or '').replace('🛠️', '').replace(INTRO, ''), 60)}"
                                       for r in last]
    if refused:
        kinds = {'personal': 'על דברים שלך', 'override': 'לשנות לי את הכללים'}
        lines += ['', '🛡️ ' + ' · '.join(f"{sum(r.get('why') == k for r in refused)} {v}" for k, v in kinds.items()
                                       if any(r.get('why') == k for r in refused))]
    return '\n'.join(lines)


def weekly_line(since: datetime | None = None) -> str:
    """One line for scripts/weekly_bot_report.py ('' when nothing happened)."""
    rows = _rows(7)
    if since is not None:
        rows = [r for r in rows if datetime.fromisoformat(r['ts']) >= since.replace(tzinfo=None)]
    asked = [r for r in rows if r['event'] in ('ok', 'none', 'error', 'blocked', 'offtopic')]
    if not asked and not any(r['event'] == 'refused' for r in rows):
        return ''
    ok = sum(r['event'] == 'ok' for r in asked)
    unsure = sum(r['event'] in ('none', 'error', 'blocked') for r in asked)
    refused = sum(r['event'] == 'refused' for r in rows)
    return f'👥 חברים בקבוצה: {len(asked)} שאלות, {ok} נענו, {unsure} לא ידעתי' + (f', {refused} סירובים' if refused else '') + \
           ' ("מה קרה בקבוצה?")'
