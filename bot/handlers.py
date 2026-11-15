"""
bot/handlers.py — transport-agnostic command handlers.

Each handler takes a `args: str` (raw text after the command) plus optional
context (course filter), and returns a markdown-formatted Hebrew string
ready for posting to Telegram, WhatsApp, or any other surface.

Handlers DO NOT touch the transport — they're pure functions over the vault
and existing pipelines. The Telegram bot wraps them with parse_mode='Markdown'.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from config.paths import COURSE_NAMES, COURSE_ORDER


# ─── /ask ──────────────────────────────────────────────────────────────────

_ASSIGNMENT_KEYWORDS = ('מטלה', 'מטלות',
                        'הגשה', 'הגשת', 'להגיש', 'דדליין', 'דדליינים',
                        'מועד הגשה', 'תאריך הגשה')

def _looks_like_assignment_query(q: str) -> bool:
    ql = q.strip()
    return any(kw in ql for kw in _ASSIGNMENT_KEYWORDS)

LAST_ASK_SOURCES: list = []  # cited sources of the most recent handle_ask


def handle_ask(question: str, course_filter: str | None = None) -> str:
    """RAG question over vault summaries.
    Smart routing: assignment queries → structured assignments data, not RAG."""
    if not question.strip():
        return ('שלח שאלה אחרי `/ask`. דוגמה:\n'
                '`/ask מה זה גרף קשירוּת?`')

    # Intent routing — assignment questions go to structured data (no hallucination)
    if _looks_like_assignment_query(question):
        try:
            grades = handle_grades()
            deadlines = handle_deadline()
            return (f'{grades}\n\n━━━━━━━━━━━\n{deadlines}')
        except Exception as e:
            pass  # fall through to RAG
    try:
        from rag.chat import chat
        result = chat(question, course_filter=course_filter)
    except Exception as e:
        return f'⚠️ שגיאה במנוע RAG: {e}'

    import re as _re
    answer = (result.get('answer') or '').strip() or '_(אין תשובה)_'
    sources = result.get('sources') or []

    # Extract STRICT citations: only [[...]] wikilinks the LLM explicitly used
    cited_stems = set(m.group(1).strip() for m in _re.finditer(r'\[\[([^\]]+)\]\]', answer))

    # Strip [[stem]] from the answer text — replace with just the stem (cleaner)
    def _clean_link(m):
        s = m.group(1).strip()
        # if it's a date-prefixed lecture stem, keep the title part
        return s
    answer_clean = _re.sub(r'\[\[([^\]]+)\]\]', _clean_link, answer)
    out = answer_clean

    # Filter sources: only those whose stem is in cited_stems
    relevant: list[dict] = []
    if cited_stems:
        for s in sources:
            stem = (s.get('stem') or '').strip()
            if stem and stem in cited_stems:
                relevant.append(s)
    # if LLM didn't cite anyone explicitly — don't show sources at all (avoid noise)

    # Map course keys to Hebrew names
    COURSE_HE = {
        'intro_to_cs': 'מבוא מורחב',
        'calculus_2': 'חדו״א 2',
        'discrete_math_2': 'בדידה 2',
        'linear_algebra_2': 'לינארית 2',
        'microeconomics': 'מיקרו א\'',
    }
    # Replace course_keys in answer body with Hebrew names
    for k, v in COURSE_HE.items():
        out = out.replace(k, v)

    if relevant:
        out += '\n\n┄┄┄┄┄┄┄┄┄┄┄\n📚 מקורות:'
        seen = set()
        for s in relevant[:3]:
            title = (s.get('title') or s.get('stem') or '?').strip()
            if title in seen: continue
            seen.add(title)
            ck = s.get('course_key', '')
            ck_he = COURSE_HE.get(ck, ck)
            date = s.get('date', '')
            # Format: "📖 שם — קורס · תאריך"
            line = f'\n  📖 {title}'
            if ck_he:
                line += f' — {ck_he}'
            if date:
                line += f'  ({date})'
            out += line
    global LAST_ASK_SOURCES
    LAST_ASK_SOURCES = relevant if relevant else (sources[:1] if sources else [])
    return out


# ─── /today ────────────────────────────────────────────────────────────────

def handle_today(args: str = '') -> str:
    """Rich daily snapshot: new summaries, today's lectures, upcoming deadlines.
    Mirrors the morning briefing but on-demand."""
    import json as _json
    from pathlib import Path as _Path
    try:
        from dashboard import vault_reader
    except Exception as e:
        vault_reader = None

    today = datetime.now().date()
    since = today - timedelta(days=2)  # last ~2 days
    lines: list[str] = [f'📅 *סטטוס יומי — {today.strftime("%d/%m/%Y")}*\n']

    # ── 1. New summaries (last 2 days) ──────────────────────────────────
    new_summaries = []
    if vault_reader is not None:
        try:
            for s in vault_reader.list_summaries():
                d = s.get('date')
                if d and d >= since.isoformat():
                    new_summaries.append(s)
        except Exception: pass

    if new_summaries:
        lines.append(f'📚 *סיכומים חדשים ({len(new_summaries)}):*')
        for s in new_summaries[:8]:
            ck = s.get('course_key') or '?'
            cname = COURSE_NAMES.get(ck, ck)
            lines.append(f'  🎓 _{cname}_ — {s.get("title") or s.get("stem")}')
        if len(new_summaries) > 8:
            lines.append(f'  ועוד {len(new_summaries)-8}...')
        lines.append('')

    # ── 2. Today's lectures (from Panopto SRTs) ─────────────────────────
    try:
        from config.paths import PANOPTO_DIR, COURSE_NAMES as _CN
        today_str = today.strftime('%Y-%m-%d')
        today_lectures = []
        for srt in _Path(PANOPTO_DIR).glob('*.srt'):
            if srt.name.endswith('.bad') or srt.name.endswith('.bak'): continue
            stem = srt.stem
            for ck in sorted(_CN, key=len, reverse=True):          # 23/10: microeconomics ⊂ microeconomics_2
                if stem.startswith(ck + '_'):
                    rest = stem[len(ck)+1:]
                    if len(rest) >= 10 and rest[:10] == today_str:
                        today_lectures.append((ck, rest[11:]))
                    break
        if today_lectures:
            lines.append('🎓 *הרצאות שעלו היום:*')
            for ck, title in today_lectures:
                lines.append(f'  • {_CN.get(ck, ck)}: {title}')
            lines.append('')
    except Exception: pass

    # ── 3. Upcoming deadlines (next 7 days) ─────────────────────────────
    try:
        from config.paths import ROOT_DIR as _RD
    except ImportError:
        _RD = _Path(__file__).resolve().parent.parent
    if True:
        try:
            # not-yet-submitted only (utils/deadlines: Moodle status + "✅ הגשתי")
            from utils.deadlines import pending as _pending
            upcoming = [(a['due'], a) for a in _pending(horizon_days=7)]
            if upcoming:
                lines.append('⏰ *דדליינים השבוע:*')
                for dt, a in upcoming[:6]:
                    days_left = (dt.date() - today).days
                    label = 'היום!' if days_left == 0 else (
                            'מחר' if days_left == 1 else f'בעוד {days_left} ימים')
                    ck = a.get('course_key', '?')
                    title = a.get('title', '?')
                    lines.append(f'  • [{label}] {COURSE_NAMES.get(ck, ck)}: {title}')
                lines.append('')
        except Exception: pass

    if len(lines) <= 2:
        lines.append('☕ שום דבר חדש. יום שקט!')

    return '\n'.join(lines)


# ─── /grades ───────────────────────────────────────────────────────────────

def _is_real_assignment(a: dict) -> bool:
    """A 'real' assignment = has a due_date. Filters out Wikidot pages classified as assignments."""
    fm = a.get('fm') or {}
    return bool(fm.get('due_date'))

def handle_grades(args: str = '') -> str:
    """Pending assignments + grades by course (real assignments only — has due_date)."""
    try:
        from dashboard import vault_reader
    except Exception as e:
        return f'⚠️ vault_reader unavailable: {e}'

    # only this semester's courses — last semester's homework (some deliberately skipped) is noise
    try:
        from config.paths import ACTIVE_COURSES as _active
    except ImportError:
        _active = None
    by_course: dict[str, dict[str, int]] = {}
    for a in vault_reader.list_assignments(course=None):
        if not _is_real_assignment(a):
            continue  # skip wikidot pages, recitations, etc.
        ck = a.get('course_key') or 'unknown'
        if _active and ck not in _active:
            continue
        s  = a.get('status') or 'unknown'
        by_course.setdefault(ck, {})[s] = by_course.get(ck, {}).get(s, 0) + 1

    if not by_course:
        return '_אין עדיין מטלות בקורסים של הסמסטר הזה._'

    lines = ['*📊 סטטוס מטלות לפי קורס:*', '']
    for ck in COURSE_ORDER:
        if ck not in by_course:
            continue
        cname = COURSE_NAMES.get(ck, ck)
        bits = []
        for status, n in by_course[ck].items():
            emoji = {'pending': '⏳', 'submitted': '✅',
                     'graded': '💯', 'late': '⚠️'}.get(status, '◽')
            bits.append(f'{emoji}{n}')
        lines.append(f'• *{cname}*: ' + '  '.join(bits))
    return '\n'.join(lines)


# ─── /deadline ─────────────────────────────────────────────────────────────

def handle_deadline(args: str = '') -> str:
    """Next 7 days of due-dates — only what is not submitted yet."""
    try:
        # 03/11 eval: an empty pending list ("all handed in") was treated as a failure → the vault fallback listed
        # everything due, submitted or not. Only a broken / empty Moodle cache falls back now.
        from utils.deadlines import load_items, summary_line
        if load_items():
            return summary_line()
    except Exception:
        pass                                   # fall back to the vault listing below
    try:
        from dashboard import vault_reader
    except Exception as e:
        return f'⚠️ vault_reader unavailable: {e}'

    today = datetime.now().date()
    horizon = today + timedelta(days=7)
    upcoming: list[tuple[str, str, str]] = []
    for a in vault_reader.list_assignments(course=None):
        due = (a.get('fm') or {}).get('due_date')
        if not due:
            continue
        try:
            d = datetime.strptime(str(due)[:10], '%Y-%m-%d').date()
        except Exception:
            continue
        if today <= d <= horizon:
            ck = a.get('course_key') or '?'
            upcoming.append((str(due), COURSE_NAMES.get(ck, ck),
                             a.get('title', '?')))
    upcoming.sort()
    if not upcoming:
        return '_אין מטלות להגשה בשבוע הקרוב._'

    lines = [f'*⏰ מועדי הגשה ({today} → {horizon}):*', '']
    for due, cname, title in upcoming:
        lines.append(f'• *{due}*  _{cname}_ — {title}')
    return '\n'.join(lines)


# ─── /sync ─────────────────────────────────────────────────────────────────

def handle_sync(args: str = '', preview: bool = False) -> str:
    """Trigger sync_all in conservative mode (cheap pull only)."""
    try:
        from pipelines.sync_all import sync_all
    except Exception as e:
        return f'⚠️ sync_all unavailable: {e}'

    res = sync_all(
        budget_usd=0.5,
        include_whisper=False,
        include_summaries=False,
        include_notebooklm=False,
        preview=preview,
    )
    return res.get('summary_text') or '_(אין סיכום)_'


# ─── /help ─────────────────────────────────────────────────────────────────

HELP = (
    '*🤖 Smart Study Agent — פקודות:*\n'
    '\n'
    '`/ask <שאלה>` — שאלה חופשית על החומר\n'
    '`/today` — מה חדש מאתמול\n'
    '`/grades` — מצב מטלות לפי קורס\n'
    '`/deadline` — מה צריך להגיש בשבוע הקרוב\n'
    '`/sync` — הרצת סנכרון מהיר (בלי Whisper/סיכומים)\n'
    '`/preview` — תצוגה מקדימה של מה היה מסונכרן\n'
    '`/help` — הצגה זו'
)


def handle_help(args: str = '') -> str:
    return HELP

def grade_teachback(concept: str, course: str | None, explanation: str) -> str:
    """Feynman mode: grade the user's own explanation of a concept against
    the course material (RAG context). Returns Hebrew feedback text."""
    from pathlib import Path as _P
    import os as _os
    if (_P(__file__).resolve().parent.parent / 'data' / 'budget_lock.json').exists():
        return '⛔ התקציב נעול כרגע — לא יכול לבדוק הסברים.'
    if len((explanation or '').strip()) < 15:
        return 'ההסבר קצר מדי 🙂 נסה להסביר במשפטים מלאים — מה הרעיון, למה זה נכון, ודוגמה.'
    ctx = ''
    try:
        from rag.vault_indexer import search as _search
        chunks = _search(concept, k=4, course_filter=course or None)
        ctx = '\n\n'.join((c.get('text') or '')[:1200] for c in chunks[:4])
    except Exception:
        pass
    key = _os.environ.get('GEMINI_API_KEY') or _os.environ.get('GOOGLE_API_KEY')
    if not key:
        return '⚠️ אין מפתח Gemini זמין.'
    try:
        from google import genai
        from google.genai import types as _gt
        client = genai.Client(api_key=key)
        prompt = (
            'אתה בוחן בשיטת פיינמן. סטודנט מסביר לך מושג במילים שלו. '
            'השווה את ההסבר לחומר הקורס וענה בעברית, קצר וממוקד, במבנה הבא בדיוק:\n'
            '🎯 ציון: X/10\n'
            '✅ מה היה מדויק: (שורה-שתיים)\n'
            '⚠️ מה חסר או שגוי: (הנקודה הכי חשובה, ואם יש טעות מהותית — הדגש אותה)\n'
            '❓ שאלת המשך: (שאלה אחת שתבדוק את החור הכי גדול בהבנה)\n\n'
            'אם ההסבר לא קשור למושג בכלל — כתוב זאת בעדינות.\n\n'
            f'המושג: {concept}\n\n'
            f'חומר הקורס (מקור אמת):\n{ctx[:5000] if ctx else "(אין הקשר — שפוט לפי ידע כללי בקורס אקדמי)"}\n\n'
            f'ההסבר של הסטודנט:\n{explanation[:3000]}'
        )
        resp = client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=_gt.GenerateContentConfig(max_output_tokens=800, temperature=0.2,
                                          thinking_config=_gt.ThinkingConfig(thinking_budget=0)))
        out = (resp.text or '').strip()
        if out:
            return '🧑\u200d🏫 ' + out + '\n\n(רוצה לנסות שוב? כתוב "אני אסביר לך את ..." מחדש)'
    except Exception as e:
        return f'⚠️ הבדיקה נכשלה: {e}'
    return '⚠️ לא הצלחתי לבדוק את ההסבר.'

def check_coverage(topic: str, course: str | None) -> str:
    """Did the course ACTUALLY cover `topic` this semester? War-cut aware:
    material may have been dropped, so old exam questions can be irrelevant.
    Grounds the verdict only in the real lecture/tutorial summaries."""
    import os as _os
    topic = (topic or '').strip()
    if not topic:
        return 'איזה נושא לבדוק? למשל: "האם למדנו משפט קיילי-המילטון?"'
    try:
        from rag.vault_indexer import search as _search
        chunks = _search(topic, k=10, course_filter=course or None)
    except Exception as e:
        return f'⚠️ שגיאה בחיפוש: {e}'
    if not chunks:
        return (f'❌ לא מצאתי שום אזכור של "{topic}" בסיכומים'
                f'{" של הקורס" if course else ""}.\n'
                f'סביר שהנושא *לא נלמד* הסמסטר (אולי ירד בגלל המלחמה) — '
                f'אם יש עליו שאלה במבחן ישן, כדאי לוודא מול המרצה שהיא עדיין רלוונטית.')
    ctx_parts, seen = [], set()
    for c in chunks[:10]:
        src = (c.get('stem') or c.get('title') or '?')
        if src in seen:
            continue
        seen.add(src)
        ctx_parts.append(f"[מקור: {src}]\n{(c.get('text') or '')[:900]}")
    ctx = '\n\n'.join(ctx_parts)
    key = _os.environ.get('GEMINI_API_KEY') or _os.environ.get('GOOGLE_API_KEY')
    if not key:
        return '⚠️ אין מפתח Gemini זמין.'
    prompt = (
        'אתה בודק אם נושא אכן *נלמד בפועל* בקורס, על סמך הסיכומים של ההרצאות והתרגולים שהתקיימו.\n'
        'רקע קריטי: הסמסטר ירד חומר בגלל המלחמה, אז נושאים שמופיעים בסילבוס או במבחנים ישנים '
        'עלולים *לא להילמד* השנה. לכן חשוב לתת תשובה מדויקת ולא לנחש.\n\n'
        f'הנושא לבדיקה: "{topic}"\n\n'
        f'הסיכומים (מקור אמת יחיד — מה שבאמת נלמד):\n{ctx[:9000]}\n\n'
        'ענה בעברית, קצר, במבנה הזה בדיוק:\n'
        '📊 כיסוי: [✅ נלמד / ⚠️ נלמד חלקית / ❌ לא נמצא בחומר שנלמד]\n'
        '📍 איפה: (אם נלמד — באיזו הרצאה/תרגול ותאריך לפי שמות המקורות; אם לא — "לא מופיע בסיכומים")\n'
        '📝 היקף: (משפט — האם לעומק או רק הוזכר בחטף)\n'
        '💡 למבחן: (אם ❌ — כתוב שסביר שהנושא ירד ואולי לא רלוונטי, אך יש לוודא מול המרצה; '
        'אם ⚠️ — מה כן כוסה ומה אולי לא)\n\n'
        'כלל ברזל: אל תכתוב "נלמד" בלי עדות ממשית בסיכומים. אזכור חולף אינו כיסוי.'
    )
    try:
        from google import genai
        from google.genai import types as _gt
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=_gt.GenerateContentConfig(max_output_tokens=700, temperature=0.1,
                                          thinking_config=_gt.ThinkingConfig(thinking_budget=0)))
        try:
            from utils.usage_tracker import log_gemini
            log_gemini(resp, 'gemini-2.5-flash', op='coverage_check')
        except Exception:
            pass
        out = (resp.text or '').strip()
        return ('🔎 *בדיקת כיסוי: ' + topic + '*\n\n' + out) if out else '⚠️ לא הצלחתי להכריע.'
    except Exception as e:
        return f'⚠️ שגיאת Gemini: {e}'

