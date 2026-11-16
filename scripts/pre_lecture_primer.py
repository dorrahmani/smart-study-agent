# -*- coding: utf-8 -*-
"""scripts/pre_lecture_primer.py — a short WhatsApp message about an hour before each class.

The owner 05/11: he asked NotebookLM about macro and the answer was good, so the daily reminders move there. For every class
in his own timetable (his groups, lectures and tutorials): what happened last time, what's expected this time, and
1–2 things worth having in mind. From the course's notebook; if it fails, the same format from Gemini; no questions
and no homework. A topic nobody wrote down is not said.

The notebook gets this class's slides and syllabus just before the question (the nightly sync adds the rest).
Each class is sent once (data/primer_sent.json). A budget lock stops everything. Three notebook failures in a row
bring one alert a day. The old version with recall questions: scripts/archive/pre_lecture_primer_v1_questions.py.

Cron: */30 4-15 * * 0-4 (UTC). Start times are Israel wall-clock and il_now() reads them that way, so a class at 08:00
gets its message at 07:00 in summer and in winter.
Cost: NotebookLM is on the subscription; the Gemini fallback ~$0.001 a message.
"""
from __future__ import annotations
import json
import os
import re
import sys
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass
if os.environ.get('GEMINI_API_KEY'):
    os.environ.pop('GOOGLE_API_KEY', None)

from config.paths import PANOPTO_DIR, SUMMARIES_DIR, COURSE_NAMES
from utils.budget import budget_lock_reason

STATE_PATH = ROOT / 'data' / 'primer_sent.json'
NLM_STATE = ROOT / 'data' / 'primer_nlm.json'
LOCK_PATH = ROOT / 'data' / 'primer.lock'
NO_CLASS_PATH = ROOT / 'data' / 'no_class_days.json'
BUDGET_LOCK = ROOT / 'data' / 'budget_lock.json'

GEMINI_MODEL = 'gemini-2.5-flash'
NLM_SECONDS = 75
SYNC_SECONDS = 90
GEMINI_SECONDS = 40
ALERT_AFTER = 3
MAX_CHARS = 1500
RECAP_CHARS = 450
EXPECTED_CHARS = 280
KEEP_CHARS = 170

TUTORIAL_KW = ('תרגול', 'tut', 'tirgul', 'recitation', 'תרגיל')
TUT_FILE_RX = re.compile(r'תרגול|recitation|tirgul|tutorial', re.I)
LEC_FILE_RX = re.compile(r'שיעור|הרצא|lecture|מצגת', re.I)
SYLLABUS_RX = re.compile(r'סילבוס|syllab', re.I)
HOMEWORK_RX = re.compile(r'שיעורי? בית|תרגיל(?:י)? בית|מטל[הת]|להגיש|הגשה|homework|assignment', re.I)
LABEL_RX = re.compile(r'^[\s*•#_]*(סיכום|צפוי|לזכור)[\s*_]*:[\s*_]*', re.M)
SOURCE_RX = re.compile(r'\[\s*מקור\s*:\s*([^\]\n]+?)\s*\]\s*\.?')
UNKNOWN_RX = re.compile(r'לא ידוע|לא צוין|לא מצוין|אין מידע|ככל הנראה|כנראה|סביר ש')
REFUSED_RX = re.compile(r'אין (?:במקורות|מידע)|לא נמצא במקורות|המקורות (?:אינם|לא)')
ORDINALS = {'ראשון': 1, 'שני': 2, 'שלישי': 3, 'רביעי': 4, 'חמישי': 5, 'שישי': 6, 'שביעי': 7, 'שמיני': 8, 'תשיעי': 9,
            'עשירי': 10}

BRIEF = """כתוב למשתמש הודעת וואטסאפ קצרה לפני {kind} בקורס "{course}" ({when}).

עובדות מהמערכת:
{facts}

כתוב בעברית, רק ממה שכתוב במקורות ובעובדות למעלה, בדיוק שלושה חלקים ובלי שום דבר אחר:
סיכום: 2–4 שורות קצרות, מה נלמד בשיעור הקודם ({last}) ולא בשיעורים אחרים.
צפוי: משפט או שניים מה צפוי בשיעור הזה, רק אם מקור אומר את זה (סדר הסילבוס, מצגת או סיכום שעלו, הודעה במודל), ובסוף [מקור: שם המקור]. אם אף מקור לא אומר, כתוב רק: לא ידוע
לזכור: 1–2 דברים קצרים שכדאי שיהיו בראש לקראת השיעור, כל אחד בשורה משלו שמתחילה ב-•

בלי שאלות, בלי שיעורי בית ומטלות, בלי הקדמה, בלי מספרי הפניות."""


def _send(text: str) -> None:
    # raise: a failed send must NOT stay recorded as sent
    from utils.notify import deliver                    # WhatsApp only (10/11: Telegram deleted)
    if not deliver(text, markdown=False):
        raise RuntimeError('primer: send failed on every channel')


_DAY_TO_PYWD = {'sun': 6, 'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5}


def il_now(now: datetime | None = None) -> datetime:
    """03/11 (The owner: "אתה שולח לי הודעות שעה אחרי שהתרגול מתחיל"): the cron line has no TZ, so datetime.now() was UTC
    and lesson times are Israel wall-clock — every warm-up came 2h (summer) / 1h (winter) late, after the lesson began.
    Israel time whatever the process clock is (winter/summer from the tz database)."""
    from datetime import timezone
    from zoneinfo import ZoneInfo
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone()                          # a naive time = this process's local clock
    return now.astimezone(ZoneInfo('Asia/Jerusalem'))


def starting_soon(items: list[dict], now_min: int, lead: int) -> list[dict]:
    """The sessions starting within `lead` minutes of `now_min` (Israel minutes since midnight)."""
    picked = []
    for it in items:
        try:
            hh, mm = it['start'].split(':')[:2]
            delta = (int(hh) * 60 + int(mm)) - now_min
        except (ValueError, AttributeError):
            continue
        if 0 <= delta <= lead:
            picked.append(it)
    return picked


def sessions_today(today: date | None = None) -> list[dict]:
    """Today's classes from the user's OWN timetable (`my_sessions` in
    courses.yaml, taken from his Google Calendar) — lectures *and* tutorials,
    sorted by start time.

    This replaces guessing from historical SRT weekdays, which needed >=2 past
    lectures on the same weekday and therefore stayed silent for the first
    weeks of a brand-new course. Returns [] on no-class days.
    """
    import yaml
    try:
        with open(ROOT / 'config' / 'courses.yaml', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        print(f'cannot read courses.yaml: {e}')
        return []

    today = today or il_now().date()
    from utils.semester import semester_active
    active, why = semester_active(cfg, today, respect_no_class_dates=False)
    if not active:
        print(why)
        return []
    sched = (cfg.get('settings') or {}).get('schedule') or {}
    if str(today) in {str(d) for d in (sched.get('no_class_dates') or [])}:
        print('no-class day (courses.yaml)')
        return []

    out = []
    for ck, course in (cfg.get('courses') or {}).items():
        for s in (course.get('my_sessions') or []):
            if _DAY_TO_PYWD.get(str(s.get('day', '')).lower()[:3]) == today.weekday():
                out.append({
                    'course': ck,
                    'type': s.get('type', 'lecture'),
                    'start': str(s.get('start', '')),
                    'group': str(s.get('group', '')),
                    'location': str(s.get('location', '')),
                    'weekly': sum(x.get('type', 'lecture') == s.get('type', 'lecture') for x in course['my_sessions']),
                })
    out.sort(key=lambda x: x['start'])
    return _site_rooms(_drop_cancelled(out, today), today)


def _drop_cancelled(items: list[dict], today: date, events: list[dict] | None = None) -> list[dict]:
    """20/10: tutorials marked "❌ בוטל — …" in the studies calendar still got a primer. A session whose calendar
    lesson at the same start time is cancelled is dropped (best-effort: no calendar → unchanged)."""
    if events is None:
        try:
            from integrations import gcal
            events = gcal.events_on(today) if gcal.configured() else []
        except Exception as e:
            print(f'cancelled-lesson check failed: {e!r}'[:200])
            events = []
    gone = {((e.get('start') or {}).get('dateTime') or '')[11:16] for e in events
            if (e.get('summary') or '').lstrip().startswith('❌')}
    return [s for s in items if s['start'][:5].zfill(5) not in gone]


def _site_rooms(items: list[dict], today: date) -> list[dict]:
    """18/10: the primer said "בניין לדוגמה 436" (the yaml's room) on the day the lecture was in בניין לדוגמה 514 —
    the university site's room for that day and hour wins."""
    try:
        from collectors.announcement_scraper import course_numbers
        from integrations import tau_portal
        events = tau_portal.schedule(live=False)[0] or []
        num_to_key = course_numbers()
    except Exception:
        return items
    for it in items:
        e = next((e for e in events if e.get('date') == today.isoformat() and e.get('time') == it['start'][:5]
                  and num_to_key.get((e.get('course_number') or '').lstrip('0')) == it['course']), None)
        if e and (e.get('building') or e.get('room')):
            it['location'] = ' '.join(x for x in (e.get('building', ''), e.get('room', '')) if x)
    return items


def courses_with_lecture_today() -> list[str]:
    """LEGACY, no longer called: it guessed today's courses from past recordings, so a day whose classes were all
    cancelled could come back through it. The timetable (`my_sessions`) is the only source now."""
    today_wd = il_now().weekday()
    per_course_dates: dict[str, set] = {}
    for srt in PANOPTO_DIR.glob('*.srt'):
        stem = srt.stem
        for ck in COURSE_NAMES:
            if not stem.startswith(ck + '_'):
                continue
            rest = stem[len(ck) + 1:]
            m = re.match(r'(\d{4}-\d{2}-\d{2})_(.*)$', rest)
            if not m:
                break
            d_str, title = m.group(1), m.group(2).lower()
            if any(k in title for k in TUTORIAL_KW):
                break
            try:
                d = datetime.strptime(d_str, '%Y-%m-%d').date()
            except ValueError:
                break
            if d.weekday() == today_wd:
                per_course_dates.setdefault(ck, set()).add(d_str)
            break
    return [ck for ck, ds in per_course_dates.items() if len(ds) >= 2]


def latest_summary(ck: str, sub: str = 'lectures') -> Path | None:
    """Newest summary for a course; sub = 'lectures' or 'tutorials'."""
    d = SUMMARIES_DIR / ck / sub
    if not d.exists():
        return None
    files = [p for p in d.glob('*.md') if re.match(r'\d{4}-\d{2}-\d{2}', p.stem)]
    return max(files, key=lambda p: p.stem[:10]) if files else None


def latest_lecture_summary(ck: str) -> Path | None:   # back-compat
    return latest_summary(ck, 'lectures')


def extract_tldr(md_path: Path) -> str:
    try:
        for line in md_path.read_text(encoding='utf-8', errors='replace').splitlines():
            if line.strip().startswith('>') and 'TL;DR' in line:
                return re.sub(r'^>\s*📝?\s*\*?\*?TL;DR:?\*?\*?\s*', '', line.strip())[:400]
    except Exception:
        pass
    return ''


# ── the last class, what's next ─────────────────────────────────────────────

def week_start(d: date) -> date:
    """The Sunday of d's week."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _summary_day(p: Path) -> date:
    return date.fromisoformat(p.stem[:10])


def summaries_for(ck: str, sub: str, today: date, weekly: int = 1) -> tuple[Path | None, Path | None]:
    """(the summary of the owner's last class of this type, a parallel group's summary of the class he is about to have).

    Recordings often come from another group: macro's lecture is recorded on Wednesday and the owner's group meets on
    Thursday, so the newest summary can be today's class itself. With one meeting a week, a summary from an earlier
    week is the past, and one from this week (before today) is the parallel group ahead of him."""
    d = SUMMARIES_DIR / ck / sub
    if not d.exists():
        return None, None
    files = [p for p in d.glob('*.md') if re.match(r'\d{4}-\d{2}-\d{2}', p.stem)]
    edge = today if weekly > 1 else week_start(today)
    past = [p for p in files if p.stem[:10] < edge.isoformat()]
    ahead = [p for p in files if edge.isoformat() <= p.stem[:10] < today.isoformat()]
    newest = lambda ps: max(ps, key=lambda p: p.stem[:10]) if ps else None
    return newest(past), newest(ahead)


def _title_of(p: Path) -> str:
    return re.sub(r'^\d{4}-\d{2}-\d{2}\s*-\s*', '', p.stem).strip()


def class_number(text: str) -> int | None:
    """The class number in a title: "שיעור 3", "Recitation 2", "תרגול שלישי". None when there is none."""
    from collectors.course_files import _nums
    nums = _nums(text) | set(re.findall(r'recitation\s*(\d{1,2})\b', text or '', re.I))
    nums |= {str(ORDINALS[w]) for w in re.findall(r'תרגול\s+([א-ת]+)', text or '') if w in ORDINALS}
    return max(map(int, nums)) if nums else None


def next_number(last: Path | None, today: date) -> int | None:
    """This class's number: the last summary's + 1, only when that summary is from last week. An older one means a class
    has no summary yet (a recording that hasn't come in), and +1 would point at the class the owner already had."""
    if not last or (week_start(today) - week_start(_summary_day(last))).days != 7:
        return None
    n = class_number(_title_of(last))
    return n + 1 if n else None


def _is_tutorial_file(e: dict) -> bool:
    if TUT_FILE_RX.search(e.get('title', '')):
        return True
    return not LEC_FILE_RX.search(e.get('title', '')) and bool(TUT_FILE_RX.search(e.get('section', '')))


def posted_files(ck: str, is_tut: bool, number: int | None, today: date, index: dict | None = None) -> tuple[list[dict], bool]:
    """(Moodle files, True when they carry this class's own number). Only files that exist on disk.
    Otherwise what went up since the last class and isn't numbered as an earlier one — False: not known to be for today."""
    from collectors import course_files
    from generators.notebooklm_agent import worthy_moodle_entries
    idx = course_files.load_index(ck) if index is None else index
    files = [e for e in worthy_moodle_entries(idx) if not SYLLABUS_RX.search(e['title'])
             and _is_tutorial_file(e) == is_tut and (ROOT / e['file']).exists()]
    nums = {e['id']: class_number(f"{e['title']} {e.get('section', '')}") for e in files}
    if number and (mine := [e for e in files if nums[e['id']] == number]):
        return mine[:3], True
    cutoff = (today - timedelta(days=6)).isoformat()
    return [e for e in files if e.get('first_seen', '') >= cutoff
            and not (number and nums[e['id']] and nums[e['id']] < number)][:3], False


def syllabus_entries(ck: str, index: dict | None = None) -> list[dict]:
    from collectors import course_files
    from generators.notebooklm_agent import worthy_moodle_entries
    idx = course_files.load_index(ck) if index is None else index
    return [e for e in worthy_moodle_entries(idx) if SYLLABUS_RX.search(e['title'])][:1]


def _for_my_group(ck: str, note: dict, body: str) -> bool:
    """A Moodle note about another group (Sunday's tutorials merged, Wednesday's recitation cancelled) → not his."""
    try:
        from integrations import univ_relevance as ur
        return ur.classify(ck, note.get('title', ''), body, ur.ref_date(note.get('date')))['rel'] != 'no'
    except Exception as e:
        print(f'  relevance failed ({ck}): {e!r}'[:200])
        return True


def recent_announcements(ck: str, today: date) -> list[str]:
    """The Moodle notes of the past 10 days. Macro's weekly mail says what the next lecture covers."""
    try:
        from dashboard import vault_reader
        notes = vault_reader.list_announcements(ck)
    except Exception as e:
        print(f'  announcements failed ({ck}): {e!r}'[:200])
        return []
    cutoff = (today - timedelta(days=10)).isoformat()
    out = []
    for n in notes[:3]:
        if str(n.get('date', '')) >= cutoff:
            body = re.sub(r'^.*?\n---\s*\n', '', n.get('body') or '', count=1, flags=re.S).strip()
            if not _for_my_group(ck, n, body):                 # 15/11 another group's note isn't quoted to him
                continue
            out.append(f"הודעה במודל מ-{n.get('date')}, \"{n.get('title', '')}\": {body[:1500]}")
    return out


def class_facts(last: Path | None, ahead: Path | None, posted: list[dict], for_today: bool, notes: list[str],
                number: int | None = None) -> str:
    rows = []
    if last:
        rows.append(f'השיעור הקודם ({_summary_day(last):%d/%m}): "{_title_of(last)}".')
        if number:
            rows.append(f'לפי המספור, השיעור הזה הוא מספר {number}.')
    if ahead:
        rows.append(f'קבוצה מקבילה כבר למדה את השיעור הזה ב-{_summary_day(ahead):%d/%m}, "{_title_of(ahead)}": '
                    f'{extract_tldr(ahead)}')
    names = ', '.join(f"\"{e['title']}\" (ביחידה \"{e.get('section', '')}\")" for e in posted)
    if posted:
        rows.append(f'החומר של השיעור הזה כבר במודל: {names}.' if for_today
                    else f'עלה למודל בשבוע האחרון, לא בטוח שזה לשיעור הזה: {names}.')
    else:
        rows.append('לא ידוע על מצגת שעלתה לשיעור הזה.')
    return '\n'.join(f'- {r}' for r in rows + notes)


# ── the three short parts ───────────────────────────────────────────────────

def _bounded(fn, seconds: float):
    """fn() in a daemon thread. `with ThreadPoolExecutor` isn't enough: leaving the block, and the end of the
    process, both wait for a hung worker. Here a hang is left behind and the script goes on and exits."""
    box: dict = {}

    def run():
        try:
            box['value'] = fn()
        except BaseException as e:                      # handed to the caller
            box['error'] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f'no answer within {seconds}s')
    if 'error' in box:
        raise box['error']
    return box.get('value')


def _plain(text: str, limit: int) -> str:
    """Sentences up to `limit` characters, without questions and without homework (The owner has no time for them)."""
    lines: list[list[str]] = []
    size = 0
    for line in (text or '').splitlines():
        kept: list[str] = []
        lines.append(kept)
        for s in re.split(r'(?<=[.!])\s+', line.strip().lstrip('•*- ').strip()):
            s = s.strip().strip('*').strip()
            if not s or '?' in s or HOMEWORK_RX.search(s):
                continue
            if size + len(s) > limit:
                done = '\n'.join(' '.join(k) for k in lines if k)
                return done or s[:limit - 1].rstrip() + '…'
            kept.append(s)
            size += len(s) + 1
    return '\n'.join(' '.join(k) for k in lines if k)


def parse_brief(text: str) -> dict | None:
    """The answer → {recap, expected, source, keep}; None when there is no usable recap. `expected` stays only with a
    named source: a topic nobody wrote down is not said."""
    marks = list(LABEL_RX.finditer(text or ''))
    parts: dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        parts.setdefault(m.group(1), text[m.end():end].strip())
    recap = _plain(parts.get('סיכום', ''), RECAP_CHARS)
    if not recap or REFUSED_RX.search(recap):
        return None
    raw = parts.get('צפוי', '')
    tag = SOURCE_RX.search(raw)
    expected = '' if UNKNOWN_RX.search(raw) or not tag else _plain(SOURCE_RX.sub('', raw), EXPECTED_CHARS)
    keep = [k for k in (_plain(x, KEEP_CHARS) for x in parts.get('לזכור', '').splitlines()) if k][:2]
    return {'recap': recap, 'expected': expected, 'source': tag.group(1).strip() if expected and tag else '', 'keep': keep}


def _note_notebook(ok: bool, today: date, path: Path | None = None) -> None:
    """Three failures in a row → one WhatsApp alert a day. A single failure stays silent: the fallback covers it."""
    p = path or NLM_STATE
    try:
        st = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        st = {}
    st['fails'] = 0 if ok else int(st.get('fails') or 0) + 1
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(st), encoding='utf-8')
    tmp.replace(p)
    if st['fails'] >= ALERT_AFTER:
        from utils.notify import notify
        notify(f"⚠️ NotebookLM לא עונה לתזכורות לפני שיעור ({st['fails']} ברצף). הן ממשיכות לצאת דרך Gemini. "
               'כנראה צריך לחדש את ההתחברות.', key=f'primer-nlm:{today.isoformat()}')


def notebook_brief(ck: str, question: str, today: date, dry: bool = False, agent=None) -> dict | None:
    from agent import nlm_ask
    resting = nlm_ask._broken_recently()                # the 6h rest after a failure, shared with the bot
    if not resting and not nlm_ask.has_notebook(ck):
        return None                                     # no notebook for this course: not a failure
    brief = None
    if not resting:
        try:
            if agent is None:
                from generators.notebooklm_agent import NotebookLMAgent
                agent = NotebookLMAgent()
            result = _bounded(lambda: agent.ask(ck, 'all', question), NLM_SECONDS)
            if not dry:
                nlm_ask._mark(False)
            brief = parse_brief(nlm_ask.whatsapp_text(result.get('answer') or ''))
            if brief is None:
                print(f'  notebook answer unusable ({ck})')
        except Exception as e:
            print(f'  NotebookLM failed ({ck}): {e!r}'[:300])
            if not dry:
                nlm_ask._mark(True)
    if not dry:
        _note_notebook(brief is not None, today)
    return brief


def _read(p: Path, limit: int) -> str:
    try:
        return p.read_text(encoding='utf-8', errors='replace')[:limit]
    except OSError:
        return ''


def _gemini(prompt: str) -> str:
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    os.environ.pop('GOOGLE_API_KEY', None)
    if not key:
        return ''
    from google import genai
    from google.genai import types as gt
    # 10/11: `genai.Client(...).models.generate_content` left the Client unreferenced; it was collected and closed its
    # HTTP client mid-call — "Cannot send a request, as the client has been closed." on 3 of 5 primers that day.
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=gt.GenerateContentConfig(max_output_tokens=500, temperature=0.2,
                                        thinking_config=gt.ThinkingConfig(thinking_budget=0)))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, GEMINI_MODEL, op='primer')
    return (resp.text or '').strip()


def gemini_brief(question: str, last: Path, posted: list[dict], syllabus: list[dict], ask=None) -> dict | None:
    """The fallback: the same three parts from our own copies of the sources (~$0.001)."""
    blocks = [f'=== סיכום השיעור הקודם ===\n{_read(last, 3000)}']
    for e in syllabus + posted[:2]:
        md = (ROOT / e['file']).with_suffix('.md')
        if md.exists():
            blocks.append(f"=== {e['title']} ===\n{_read(md, 2500)}")
    try:
        return parse_brief(_bounded(lambda: (ask or _gemini)(question + '\n\nהמקורות:\n' + '\n\n'.join(blocks)), GEMINI_SECONDS))
    except Exception as e:
        print(f'  Gemini failed: {e!r}'[:300])
        return None


def sync_for_class(ck: str, last: Path | None, entries: list[dict], agent=None) -> None:
    """This class's sources in the notebook before the question. The nightly sync adds everything; slides that went up
    during the day get here. Best effort, within SYNC_SECONDS: the question goes out either way."""
    from agent import nlm_ask
    from generators import notebooklm_agent as nlm
    if not nlm_ask.has_notebook(ck):
        return
    have = set(((nlm._load_notebooks_index().get(ck) or {}).get('all') or {}).get('sources_synced') or [])
    summary = [str(p) for p in ([last.with_suffix('.pdf')] if last else []) if p.exists()]
    pdfs, texts = nlm.moodle_source_paths(ck, entries)
    missing = [p for p in summary + pdfs + texts if os.path.basename(p) not in have]
    if not missing:
        return
    try:
        agent = agent or nlm.NotebookLMAgent()
        r = _bounded(lambda: agent.sync_sources(ck, 'all', summary_pdfs=summary, moodle_pdfs=pdfs, text_files=texts),
                     SYNC_SECONDS)
        print(f"  notebook +{len(r.get('added') or [])} sources ({ck})")
    except Exception as e:
        print(f'  notebook sync failed ({ck}): {e!r}'[:300])


# ── the message ─────────────────────────────────────────────────────────────

def render(item: dict, last: Path | None, brief: dict | None, posted: list[dict], for_today: bool,
           stale: bool = False) -> str:
    name = COURSE_NAMES.get(item['course'], item['course'])
    kind = 'תרגול' if item['type'] == 'tutorial' else 'הרצאה'
    when = f" ב-{item['start']}" if item['start'] else ''
    grp = f" (קב׳ {item['group']})" if item['group'] else ''
    where = f" · {item['location']}" if item['location'] else ''
    blocks = [f'⏰ עוד מעט: {name} — {kind}{when}{grp}{where}']
    recap = (brief or {}).get('recap') or (extract_tldr(last) if last else '')
    if last and recap:
        label = 'הסיכום האחרון שיש' if stale else 'בפעם הקודמת'
        blocks.append(f'{label} ({_summary_day(last):%d/%m}):\n{recap}')
    today_lines = []
    if (brief or {}).get('expected'):
        today_lines.append(f"{brief['expected']} (לפי {brief['source']})")
    if posted:
        names = ', '.join(e['title'] for e in posted)
        today_lines.append(f'📎 כבר במודל לשיעור הזה: {names}' if for_today else f'📎 עלה למודל השבוע: {names}')
    if today_lines:
        blocks.append('היום:\n' + '\n'.join(today_lines))
    elif brief:
        blocks.append('מה יהיה היום: אין עדיין מקור שאומר.')
    if (brief or {}).get('keep'):
        blocks.append('שווה שיהיה בראש:\n' + '\n'.join(f'• {k}' for k in brief['keep']))
    return '\n\n'.join(blocks)


def build_message(item: dict, today: date, dry: bool = False) -> str:
    ck = item['course']
    is_tut = item['type'] == 'tutorial'
    last, ahead = summaries_for(ck, 'tutorials' if is_tut else 'lectures', today, item.get('weekly', 1))
    number = next_number(last, today)
    posted, for_today = posted_files(ck, is_tut, number, today)
    if not last:                                        # a first class: nothing to recap, nothing to ask about
        return render(item, None, None, posted, for_today)
    syllabus = syllabus_entries(ck)
    question = BRIEF.format(kind='תרגול' if is_tut else 'הרצאה', course=COURSE_NAMES.get(ck, ck),
                            when=f"{today:%d/%m} {item['start']}", last=f'{_summary_day(last):%d/%m}',
                            facts=class_facts(last, ahead, posted, for_today, recent_announcements(ck, today), number))
    if not dry:
        sync_for_class(ck, last, posted + syllabus)
    brief = notebook_brief(ck, question, today, dry) or gemini_brief(question, last, posted, syllabus)
    return render(item, last, brief, posted, for_today, stale=(week_start(today) - week_start(_summary_day(last))).days > 7)


def _key(today: date, item: dict) -> str:
    return f"{today.isoformat()}:{item['course']}:{item['type']}:{item['start']}"


def _load_sent() -> dict:
    """A missing file is an empty state. A broken one stops the run (the cron alert): sending from nothing could repeat
    every class of the last two weeks' keys."""
    try:
        return json.loads(STATE_PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def _save_sent(sent: dict, today: date) -> None:
    cutoff = (today - timedelta(days=14)).isoformat()   # keys older than two weeks go, so the file stays small
    keep = {k: v for k, v in sent.items() if str(k)[:10] >= cutoff}
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(STATE_PATH)


@contextmanager
def _one_at_a_time():
    """A slow run must not overlap the next cron tick: both would send."""
    try:
        import fcntl
    except ImportError:                                 # Windows: tests only
        yield True
        return
    with open(LOCK_PATH, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def _run_classes(now: datetime, dry: bool) -> None:
    today = now.date()
    sent = _load_sent()
    todays = sessions_today(today)                      # his own timetable, his groups only
    if not todays:
        print('no classes today'); return
    LEAD_MIN = int(os.environ.get('PRIMER_LEAD_MIN', '75'))
    if '--all' not in sys.argv:                         # --all: ignore the window (manual runs)
        todays = starting_soon(todays, now.hour * 60 + now.minute, LEAD_MIN)   # Israel time, in any season
        if not todays:
            print(f'no class starting within {LEAD_MIN} min'); return
    todays = [it for it in todays if not sent.get(_key(today, it))]
    if not todays:
        print('already sent for these classes'); return

    failed = []
    for item in todays:
        key = _key(today, item)
        try:
            msg = build_message(item, today, dry)[:MAX_CHARS]
            if dry:
                print('--- DRY RUN (not sent) ---\n' + msg)
                continue
            sent[key] = True                            # written first: a crash after the send can't send again
            _save_sent(sent, today)
            _send(msg)
            print(f'primer sent: {key}')
        except Exception as e:
            print(f'primer failed for {key}: {e!r}'[:300])
            if sent.pop(key, None):                     # not delivered: the next tick tries again
                _save_sent(sent, today)
            failed.append(key)
    if failed:
        raise RuntimeError(f'primer: {len(failed)} class(es) not sent')


def main(now: datetime | None = None) -> None:
    now = now or il_now()
    today = now.date()
    if today.weekday() in (4, 5):  # Fri/Sat — no classes in Israel
        print('weekend, skip'); return
    reason = budget_lock_reason(BUDGET_LOCK)
    if reason:
        print(f'budget locked, skip: {reason}'); return
    try:
        no_class = json.loads(NO_CLASS_PATH.read_text(encoding='utf-8')).get('dates', [])
    except Exception:
        no_class = []
    if today.isoformat() in no_class:
        print('no-class day, skip'); return
    with _one_at_a_time() as mine:
        if not mine:
            print('another run is still working, skip'); return
        _run_classes(now, '--dry-run' in sys.argv)


if __name__ == '__main__':
    main()
