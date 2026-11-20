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


# ── "שלא יקרה שוב" (16/11) ─────────────────────────────────────────────────────────────────────────────────────────
# 16/11 20:19 the bot admitted the cancelled tutorial ("טעות שלי"), the owner: "יופי שלא יקרה שוב" → "לגמרי, לקחתי לתשומת ליבי
# ואוודא שזה לא יחזור על עצמו." Nothing had been taken anywhere: the bot has no memory that changes its behaviour by
# itself. Now: the mistake goes into the improvement backlog with the turns it happened in, what can be set right away
# is set (and said exactly), and nothing is promised that wasn't done.
NEVER_AGAIN_RX = re.compile(r'(?:^|\s)(?:ש|ו?ש)?(?:זה\s+)?לא\s+(?:י|ת)(?:קרה|חזור)\s+(?:על\s+עצמ[וה]\s+)?(?:שוב|עוד\s+פעם|יותר)|'
                            r'תלמד\s+(?:מזה|מהטעות|מהטעויות|לקח)|אל\s+ת(?:עשה|חזור\s+על)\s+(?:את\s+)?זה\s+(?:שוב|יותר)|'
                            r'שלא\s+(?:תחזור|תעשה)\s+(?:על\s+)?(?:זה|את\s+זה)\s+(?:שוב|יותר)|תיזהר\s+בפעם\s+הבאה|'
                            r'(?:שלא|לא)\s+יחזור\s+על\s+עצמו')
# what an answer may not say unless it was done: "אוודא שזה לא יחזור", "לקחתי לתשומת ליבי", "זה לא יקרה שוב"
EMPTY_PROMISE_RX = re.compile(r'אוודא\s+ש(?:זה\s+|הטעות\s+|זה\s+כבר\s+)?לא|אדאג\s+ש(?:זה\s+)?לא|לקחתי\s+(?:את\s+זה\s+)?לתשומת\s+ליב|לקחתי\s+לקח|'
                              r'(?:זה\s+)?לא\s+(?:י|ת)(?:קרה|חזור)\s+(?:על\s+עצמ[וה]\s+)?(?:שוב|יותר)|לא\s+יחזור\s+על\s+עצמו|'
                              r'אשתדל\s+ש(?:זה\s+)?לא|אהיה\s+(?:יותר\s+)?(?:זהיר|קשוב)|מעכשיו\s+אקפיד|אקפיד\s+(?:מעכשיו\s+)?ש|'
                              r'רשמתי\s+לעצמי|אשים\s+לב\s+להבא|להבא\s+(?:אשים|אקפיד|אזהר|אבדוק)')
RECORDED = 'רשמתי את זה ברשימת התיקונים של הבוט, עם השיחה הזו. עד שהתיקון נכנס, זה עדיין יכול לקרות.'


def is_never_again(text: str) -> bool:
    t = (text or '').strip()
    return 0 < len(t) <= 90 and bool(NEVER_AGAIN_RX.search(t))


def _context(history: list[dict], n: int = 4) -> str:
    rows = [h for h in history or [] if h.get('text')][-n:]
    return ' ⏎ '.join(f"{'המשתמש' if h.get('role') == 'user' else 'בוט'}: {str(h['text'])[:240]}" for h in rows)


def _applied_now(history: list[dict], today=None) -> list[str]:
    """What is already true now about the mistake in these turns: a schedule change it was about (agent/schedule),
    a kind of message he turned off (agent/noise). Only facts read from the stores — never a guess."""
    from datetime import date as _date
    said = ' '.join(str(h.get('text') or '') for h in (history or [])[-4:])
    out = []
    try:
        from agent import schedule
        from agent.lesson_sync import _words
        from config.paths import COURSE_NAMES
        from integrations.gcal import WEEKDAY_HE
        today = today or _date.today()
        for o in schedule.load():
            if o.get('date', '') < today.isoformat():
                continue
            name = COURSE_NAMES.get(o.get('ck'), o.get('ck', ''))
            if _words(name) & _words(said) or (o.get('ck') and o['ck'] in said):
                d = _date.fromisoformat(o['date'])
                out.append(f"הלו״ז שלך מסמן עכשיו: {WEEKDAY_HE[d.weekday()]} {d:%d/%m} {o['start']} "
                           f"{schedule.KIND_HE.get(o['kind'], '')} {name}{schedule.suffix({**o, 'note': ''})}")
    except Exception as e:
        print(f'  ⚠️ never-again schedule check: {e!r}'[:200])
    return out[:3]


def never_again(history: list[dict], text: str, now: datetime | None = None, path: Path | None = None) -> str:
    """"שלא יקרה שוב" about the last exchange → recorded in the improvement backlog (with the turns), and the reply says
    only what is true: what is already set, and that the fix itself is recorded."""
    from agent import improve
    now = now or datetime.now()
    ctx = _context(history)
    last_bot = next((str(h.get('text') or '') for h in reversed(history or []) if h.get('role') == 'bot'), '')
    last_his = next((str(h.get('text') or '') for h in reversed(history or []) if h.get('role') == 'user'
                     and not is_never_again(h.get('text', ''))), '')
    title = f'המשתמש ביקש שלא יחזור: {last_his[:70] or last_bot[:70]}'
    try:
        improve.add_backlog([{'title': title, 'evidence': f'{ctx} ⏎ המשתמש: {text.strip()[:120]}',
                              'change': 'לתקן כך שהטעות בשיחה הזו לא תחזור (ההקשר בראיה). בקשה ישירה של המשתמש.',
                              'files': [], 'why': 'נאמר בשיחה — "שלא יקרה שוב"'}], now, path)
    except Exception as e:
        print(f'  ⚠️ never-again backlog: {e!r}'[:200])
        return 'לא הצלחתי לרשום את זה כרגע. תכתוב "קלוד: …" עם מה שצריך לתקן ואפתח לו משימה.'
    done = _applied_now(history, now.date())
    if done:
        return ('רשמתי את זה ברשימת התיקונים של הבוט, עם השיחה הזו. מה שכבר מסודר עכשיו:\n'
                + '\n'.join(f'• {x}' for x in done))
    return RECORDED


def strip_promises(reply: str) -> tuple[str, bool]:
    """An answer's promise of a change nobody made ("אוודא שזה לא יחזור על עצמו") comes off; → (reply, was there one)."""
    keep, hit = [], False
    for ln in (reply or '').splitlines():
        if EMPTY_PROMISE_RX.search(ln):
            hit = True
            parts = [s for s in re.split(r'(?<=[.!?])\s+', ln) if s.strip() and not EMPTY_PROMISE_RX.search(s)]
            ln = ' '.join(parts).strip()
            if re.fullmatch(r'[\W_]*(?:לגמרי|בהחלט|כמובן|סבבה|צודק|אתה צודק|מסכים)?[\W_]*', ln or ''):
                continue
        keep.append(ln)
    return '\n'.join(keep).strip(), hit


def since(when: datetime, path: Path | None = None) -> list[dict]:
    try:
        rows = [json.loads(l) for l in (path or STORE).read_text(encoding='utf-8').splitlines() if l.strip()]
    except (OSError, ValueError):
        return []
    return [r for r in rows if r.get('ts', '') >= when.isoformat()]


# ── 👍/👎 as a WhatsApp reaction (17/11, the owner's improvement 5) ─────────────────────────────────────────────────────────
# A long answer gets one small line under it, a few times a day: react 👍 or 👎 on the message. whatsapp/bridge passes a
# reaction as {'type': 'reaction', 'text': emoji, 'reply_to': id of the message reacted to}; whatsapp/worker hands it to
# on_reaction() (no model, no core turn). Every rating → data/feedback_ratings.jsonl with the question/answer ids; a 👎
# also → data/feedback.jsonl (the daily review's "fix first") and the improvement backlog (agent/improve). A 👍 is
# silent. Any reaction of his also counts as "he answered" for agent/rhythm (data/wa_reactions.jsonl).
# Not numbered buttons on purpose: a "1️⃣ 👍" would take the bare digit from a real open question (outbox.take_option
# answers the latest) and hold back the next proactive question for 3h (outbox.options_open).
RATINGS = ROOT / 'data' / 'feedback_ratings.jsonl'
OFFERS = ROOT / 'data' / 'feedback_offers.json'
REACTIONS = ROOT / 'data' / 'wa_reactions.jsonl'
FOOTER = '\n\n_👍 / 👎 כריאקשן על ההודעה — ככה אני לומד מה עובד_'
OFFER_MIN_CHARS = 500                     # a substantive answer, not "✅ נוסף ליומן"
OFFERS_PER_DAY = 3
OFFER_GAP = timedelta(hours=2)
UP = {'👍', '👍🏻', '👍🏼', '👍🏽', '👍🏾', '👍🏿', '❤️', '🙏', '💯', '🔥', '👌'}
DOWN = {'👎', '👎🏻', '👎🏼', '👎🏽', '👎🏾', '👎🏿'}
NO_OFFER_RX = re.compile(r'^\s*(?:⚠️|🤔|📝|⌛|🙂 זו ההודעה שלי|🛠️ קלוד התחיל)|משהו נכשל אצלי|לא זמין כרגע')


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def offer(msg, outs: list, msg_id: str = '', now: datetime | None = None, path: Path | None = None) -> list:
    """The worker, before sending the owner's answer: maybe add FOOTER to it. Only his own private text/voice question, an
    answer of plain text ≥500 chars, ≤3 a day, ≥2h apart, outside quiet hours. The offer is kept (question id + text,
    answer text) so the reaction finds what it rated."""
    now = now or datetime.now()
    if getattr(msg, 'role', '') != 'owner' or getattr(msg, 'group', False) or getattr(msg, 'forwarded', False) \
            or getattr(msg, 'kind', '') not in ('text', 'voice') or not outs or any(o.kind != 'text' for o in outs):
        return outs
    answer = '\n'.join(o.text or '' for o in outs)
    if len(answer) < OFFER_MIN_CHARS or NO_OFFER_RX.search(answer[:200]) or is_feedback(getattr(msg, 'text', '')):
        return outs
    try:
        from utils.notify import in_quiet_hours
        if in_quiet_hours(now):
            return outs
    except Exception:
        pass
    p = path or OFFERS
    st = _read_json(p, {})
    day = now.date().isoformat()
    if st.get('day') != day:
        st['day'], st['n'] = day, 0
    last = st.get('last')
    if st.get('n', 0) >= OFFERS_PER_DAY or (last and now - datetime.fromisoformat(last) < OFFER_GAP):
        return outs
    st['n'] = st.get('n', 0) + 1
    st['last'] = now.isoformat(timespec='seconds')
    cut = (now - timedelta(days=14)).isoformat()
    st['offers'] = [o for o in st.get('offers') or [] if o.get('ts', '') >= cut][-40:] + [
        {'ts': now.isoformat(timespec='seconds'), 'chat': getattr(msg, 'sender', ''), 'q_id': msg_id,
         'q': (getattr(msg, 'text', '') or '')[:500], 'a': answer[:1500]}]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
    last_out = outs[-1]
    return outs[:-1] + [type(last_out)('text', (last_out.text or '') + FOOTER)]


def _key(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '').replace(FOOTER.strip(), '')).strip()[:160]


def _exchange_for(answer: str, chat: str, offers_path: Path | None = None, convo: Path | None = None) -> dict:
    """The offer (or else the logged exchange) whose answer is the message he reacted to."""
    k = _key(answer)
    if not k:
        return {}
    for o in reversed(_read_json(offers_path or OFFERS, {}).get('offers') or []):
        a = _key(o.get('a', ''))
        if o.get('chat') in ('', chat) and a and (k[:120] in a or a[:120] in k):
            return {'q_id': o.get('q_id', ''), 'in': o.get('q', ''), 'out': o.get('a', ''), 'at': o.get('ts', ''),
                    'offered': True}
    try:
        lines = (convo or CONVO).read_text(encoding='utf-8').splitlines()[-400:]
    except OSError:
        lines = []
    for raw in reversed(lines):
        try:
            t = json.loads(raw)
        except ValueError:
            continue
        out = ' ⏎ '.join(x for _, x in t.get('out') or [] if isinstance(x, str))
        if t.get('role') == 'owner' and k[:80] in _key(out):
            return {'in': t.get('in', ''), 'out': out[:1500], 'at': t.get('ts', ''), 'offered': False}
    return {'out': answer[:1500], 'offered': False}


def on_reaction(m: dict, now: datetime | None = None, quoted=None, paths: dict | None = None) -> str:
    """A reaction from the bridge → '' (nothing to say) or a short ack for a 👎. The owner's only; the partner's / a contact's are
    ignored. Never raises out of a file problem."""
    now = now or datetime.now()
    paths = paths or {}
    chat, emoji, ref = m.get('chat', ''), (m.get('text') or '').strip(), m.get('reply_to') or ''
    if m.get('role') != 'owner' or m.get('group') or not emoji:
        return ''
    stamp = now.isoformat(timespec='seconds')
    try:
        p = paths.get('reactions') or REACTIONS
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': stamp, 'chat': chat, 'emoji': emoji, 'reply_to': ref}, ensure_ascii=False) + '\n')
    except OSError:
        pass
    value = 'up' if emoji in UP else 'down' if emoji in DOWN else ''
    if not value:
        return ''
    try:
        if quoted is None:
            from whatsapp import outbox
            quoted = outbox.quoted_text
        answer = quoted(chat, ref) or ''
    except Exception as e:
        print(f'  ⚠️ reaction quoted: {e!r}'[:200])
        answer = ''
    ex = _exchange_for(answer, chat, paths.get('offers'), paths.get('convo'))
    rec = {'ts': stamp, 'value': value, 'emoji': emoji, 'answer_id': ref, 'question_id': ex.get('q_id', ''),
           'q': ex.get('in', '')[:500], 'a': (ex.get('out') or answer)[:1500], 'q_ts': ex.get('at', ''),
           'offered': bool(ex.get('offered')), 'via': 'reaction'}
    try:
        p = paths.get('ratings') or RATINGS
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        return ''
    if value == 'up':
        return ''
    try:                                                   # the daily review's "fix first" list
        p = paths.get('store') or STORE
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': stamp, 'role': 'owner', 'in': rec['q'], 'out': rec['a'], 'at': rec['q_ts'],
                                'comment': '', 'via': 'reaction', 'answer_id': ref,
                                'question_id': rec['question_id']}, ensure_ascii=False) + '\n')
    except OSError:
        pass
    try:                                                   # and the improvement backlog (agent/improve)
        from agent import improve
        what = re.sub(r'\s+', ' ', rec['q'] or rec['a'])
        improve.add_backlog([{'title': f'👎 מהמשתמש על תשובה: {what[:70]}',
                              'evidence': f"המשתמש: {rec['q'][:300]} ⏎ בוט: {rec['a'][:600]}",
                              'change': 'לבדוק למה התשובה לא טובה ולתקן (המשתמש סימן 👎 כריאקשן).',
                              'files': [], 'why': '👎 של המשתמש'}], now, paths.get('backlog'))
    except Exception as e:
        print(f'  ⚠️ 👎 backlog: {e!r}'[:200])
    return '📝 תודה, סימנתי 👎 — זה נכנס לרשימת התיקונים.'


def ratings_since(when: datetime, path: Path | None = None) -> list[dict]:
    try:
        rows = [json.loads(l) for l in (path or RATINGS).read_text(encoding='utf-8').splitlines() if l.strip()]
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get('ts', '') >= when.isoformat()]


def weekly_line(since: datetime, path: Path | None = None) -> str:
    """For scripts/weekly_bot_report.py ('' when nothing was rated this week)."""
    rows = ratings_since(since, path)
    if not rows:
        return ''
    up = sum(r.get('value') == 'up' for r in rows)
    return f'👍 {up} · 👎 {len(rows) - up} על תשובות השבוע' + (' (ה-👎 ברשימת התיקונים)' if len(rows) > up else '')
