"""
agent/chat_sub.py — plain conversation on the owner's Gemini Pro subscription instead of the paid API (19/11).

The owner: "לא להשתמש ב-API אלא בג'מיני, סתם שיחה". Measured on the server 19/11, ten short prompts through agy: 10/10 answered,
4.6–11.0 s (median ~6 s). An owner turn that needs no tool and no data — a greeting, thanks, a feeling, "תנסח / תתרגם /
תכתוב לי הודעה", a general "מה זה X" outside his courses — goes there. Everything else stays on agent/mind's API loop.

Safety, all in code:
  • eligible() is a strict allow-list: a short message of one of those shapes, no action word (שלח, יומן, תזכיר, בת הזוג,
    קלוד…), nothing about his own days, mail, exams, courses or files, no open numbered question, no quoted message.
  • agy gets ONLY this message and his own recent turns + the bot's replies to them (no row marked `others`), no calendar,
    mail, memory or anyone else's text: agy is an agent, and nothing third-party reaches it (utils/gemini_sub.api_only).
  • The reply must pass check(): no "I did / I will" (mind.claims), no study offer, no time/date/link that isn't already
    in his words, not the NEED marker, not too long. Anything off, a timeout or a resting subscription → None, and the
    API loop answers as before. The subscription never costs a reply.
  • At most DAY_CAP turns a day (data/mind_log.jsonl rows kind='sub'), inside gemini_sub's own DAILY_CAP.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAY_CAP = 150
TIMEOUT = 15
NEED = 'NEED_TOOLS'
MAX_CHARS = 1200

_SMALL = re.compile(r'^\s*(?:היי+|הי|שלום|אהלן|הלו|יו|בוקר טוב|צהריים טובים|ערב טוב|לילה טוב|שבוע טוב|שבת שלום|'
                    r'תודה(?: רבה)?(?: לך)?|תודה(?: רבה)? אחי|אחלה|מעולה|מגניב|יופי|וואו|חחח+|לול|מה נשמע|מה שלומך|'
                    r'מה העניינים|מה איתך|איך אתה|אני (?:עייף|עייפה|לחוץ|לחוצה|שמח|שמחה|עצוב|גמור|מבואס|בלחץ)[^?]{0,60}|'
                    r'סתם|כלום|אוהב אותך|אתה (?:תותח|אלוף|מלך|גדול))[\s!.,❤️🙏👍😊😂🤣]*$')
_REWRITE = re.compile(r'^\s*(?:תנסח|תנסחי|נסח|תשכתב|שכתב|תתרגם|תרגם|תקצר|תשפר את הניסוח|תכתוב לי|תכתבי לי|כתוב לי)\s+\S')
_EXPLAIN = re.compile(r'^\s*(?:מה זה|מה זאת|מה ההבדל בין|תסביר לי מה זה|תסביר מה זה|תסביר לי|תסביר)\s+\S')
_ACT = re.compile(r'שלח|תשלח|לשלוח|יומן|ביומן|תזכיר|תזכורת|תוסיף|תזכור|תמחק|תבטל|קלוד|בת הזוג|מייל|אימייל|וואטסאפ|הודעה ל|'
                  r'תתקשר|תקבע|תרשום|תירשם|הזמנה|מלון|קובץ|תמונה|לינק|קישור|הבוט|באג|תתקן')
_MINE = re.compile(r'(?<![א-ת])(?:לי|שלי|אצלי|אותי|שלנו|יש לי|מחר|היום|השבוע|מתי|איפה|באיזו|באיזה|כמה|מבחן|בוחן|הגשה|'
                   r'מטלה|תרגיל|ציון|קורס|שיעור|הרצאה|תרגול|מרצה|מתרגל|סילבוס|מודל|ספרייה|תשלום|מזכירות)(?![א-ת])')
_REWRITE_MINE = re.compile(r'(?<![א-ת])(?:מחר|היום|השבוע|מתי|איפה|כמה|מבחן|הגשה|ציון)(?![א-ת])\s*\?')

PROMPT = """אתה העוזר האישי של המשתמש בוואטסאפ: חבר חכם, עברית מדוברת, קצר וחם (1–4 שורות; בניסוח או תרגום — רק הטקסט המבוקש,
גרסה אחת או שתיים). אימוג'י אחד לכל היותר, *מודגש* בכוכבית אחת, בלי כותרות.
אין לך כאן גישה ליומן, למייל, לקורסים, לקבצים או לזיכרון שלו. אם כדי לענות צריך מידע אישי עליו, נתון עדכני, חומר של קורס,
או לבצע משהו (לשלוח, להוסיף, להזכיר, לזכור) — ענה בדיוק במילה {need} ותו לא.
אסור: לכתוב שעשית או שתעשה משהו, להציע תרגול/חזרה/שאלות, להמציא עובדות עליו.

השיחה האחרונה (רק ההקשר):
{history}

ההודעה של המשתמש:
{text}"""


_ANSWERS = re.compile(r'^\s*(?:אחלה|מעולה|יופי|סבבה|מגניב|סתם|כלום)(?![א-ת])')


def _yes(text: str) -> bool:
    try:
        from agent.core import YES_RX
        return bool(YES_RX.match(text))
    except Exception:
        return True


def _course_word(text: str) -> bool:
    try:
        from agent.core import _course_in_text
        return bool(_course_in_text(text))
    except Exception:
        return True                                        # can't tell → not here


def eligible(text: str, u: dict, chat: str = '', open_qs: list | None = None) -> bool:
    t = (text or '').strip()
    if not t or len(t) > 400 or u.get('_quoted') or re.search(r'https?://|www\.|@', t):
        return False
    if re.fullmatch(r'\d{1,3}[.)]?', t):
        return False
    if open_qs is None:
        try:
            from agent import assistant
            open_qs = assistant._open_qs(chat)
        except Exception:
            return False
    # 19/11 on the server he had 4 open questions at any time: only a word that could answer one ("אחלה", "סבבה", "כן")
    # waits for the loop, which knows them. A greeting, a thanks, a rewrite or "מה זה" answers nothing.
    if open_qs and (_ANSWERS.match(t) or _yes(t)):
        return False
    if _SMALL.match(t):
        return True
    if _ACT.search(t):
        return False
    if _REWRITE.match(t):
        return not _REWRITE_MINE.search(t)
    if _EXPLAIN.match(t) and len(t) <= 120:
        return not _MINE.search(t) and not _course_word(t)
    return False


def _history(u: dict, n: int = 6) -> str:
    rows = [h for h in (u.get('history') or []) if not h.get('others')][-n:]
    return '\n'.join(f"{'המשתמש' if h.get('role') == 'user' else 'אתה'}: {str(h.get('text', ''))[:300]}" for h in rows) or '(אין)'


def check(reply: str | None, text: str, u: dict) -> str | None:
    from agent import mind
    r = (reply or '').replace('**', '*').strip()
    if not r or NEED in r or len(r) > MAX_CHARS or mind.claims(r) or mind.STUDY_OFFER.search(r):
        return None
    known = mind.specifics(text + '\n' + _history(u, 12))
    if mind.specifics(r) - known:                          # a time, date or link he didn't give: not from here
        return None
    return r


def used_today(path: Path | None = None) -> int:
    today, n = date.today().isoformat(), 0
    try:
        with open(path or ROOT / 'data' / 'mind_log.jsonl', 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 262144))
            for ln in f.read().decode('utf-8', 'ignore').splitlines():
                if ln.startswith('{"ts": "' + today) and '"kind": "sub"' in ln:
                    n += 1
    except OSError:
        pass
    return n


def reply(text: str, u: dict, ask=None) -> str | None:
    """The subscription's checked answer, or None (the API loop answers)."""
    from utils import gemini_sub
    if used_today() >= DAY_CAP:
        return None
    try:
        out = (ask or gemini_sub.ask)(PROMPT.format(need=NEED, history=_history(u), text=text.strip()), timeout=TIMEOUT)
    except Exception as e:
        print(f'  ⚠️ chat on the subscription: {e!r}'[:200])
        return None
    r = check(out, text, u)
    if out and not r:
        print('  ⚠️ chat on the subscription: answer not used, the API loop answers')
    if r:
        try:
            from utils.usage_tracker import log_usage
            log_usage(service='gemini-subscription', model=f'agy-{gemini_sub.MODEL}', op='agent.chat_sub',
                      extra={'chars_in': len(text), 'chars_out': len(r)})
        except Exception:
            pass
    return r
