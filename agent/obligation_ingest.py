"""
agent/obligation_ingest.py — an assignment / quiz that a mail or a Moodle post NAMES, tracked at once (19/11).

19/11 21:11 the Software 1 lecturer posted "Assignment #2 is now available on Moodle. It is due on 06.12.2026 at 23:59";
the tracker (Moodle light check: Sun–Thu 06:40 / 10:40 / 14:40, the nightly scrape 22:00) saw it hours later, after the owner
had to ask. The university mail check runs every 15 minutes and the Moodle announcements watcher 3× a day: this module
reads what they already fetched (no new request, no LLM), and when a text says "<assignment / quiz / exercise N> ... due
<date>" it
    1  writes the item to data/assignments/announced.json (utils/deadlines.load_items and agent/obligations.collect read
       it like the lecturer-site list; a Moodle item with the same course + kind + number replaces it, inheriting its state)
    2  refreshes the obligations store right away (offline) → agent/proactive's next tick tells him
    3  starts the Moodle light check in the background (at most once / 30 min) so the real Moodle item arrives within a minute

Safety: the text is somebody else's words — only its number, date and a how-to-submit hint are read; the title is rebuilt
here ("Assignment 2"), never copied. A mail that isn't verified as the university's (DKIM) is tracked but flagged
`unverified` (shown as "לפי מייל — לא אומת"). Never a guess: no clear course, no due phrase + date → nothing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'assignments' / 'announced.json'
KICK_STAMP = ROOT / 'data' / 'obligation_ingest_kick.json'
KICK_EVERY = timedelta(minutes=30)

QUIZ_RX = re.compile(r'\bquiz(?:zes)?\b|בוחן|בחנים|קוויז', re.I)
ASSIGN_RX = re.compile(r'\bassignments?\b|\bhomework\b|\bhw\b|\bexercises?\b|\bproject\b|\bproblem set\b|מטלה|מטלות|תרגיל|שיעורי בית|'
                       r'עבודה להגשה|פרויקט|פרוייקט', re.I)
NUM_RX = re.compile(r'(?:assignment|quiz|homework|hw|exercise|project|problem set|מטלה|תרגיל(?: בית)?|בוחן|שיעורי בית|פרויקט)'
                    r'\s*(?:no\.?|number|מספר|#)?\s*[#]?\s*(\d{1,2})(?!\d|[./:]\d)', re.I)
CUE_RX = re.compile(r'\bdue\b|deadline|\bby\b|until|submit|closes?\b|\bcut-?off\b|עד\s|להגשה|הגשה|מועד הגשה|תאריך הגשה|נסגר|'
                    r'תאריך אחרון|הדדליין', re.I)
PAST_RX = re.compile(r'was due|were due|has passed|passed|הוגשה|עבר המועד|היה להגשה', re.I)
EN_MONTHS = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
HE_MONTHS = {'ינואר': 1, 'פברואר': 2, 'מרץ': 3, 'מרס': 3, 'אפריל': 4, 'מאי': 5, 'יוני': 6, 'יולי': 7, 'אוגוסט': 8,
             'ספטמבר': 9, 'אוקטובר': 10, 'נובמבר': 11, 'דצמבר': 12}
DATE_NUM_RX = re.compile(r'(?<![\d.:/])(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d:])')
DATE_EN_RX = re.compile(r'(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(' + '|'.join(m for m in
                        ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']) +
                        r')[a-z]*\.?,?(?:\s+(\d{4}))?', re.I)
DATE_EN2_RX = re.compile(r'(' + '|'.join(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']) +
                         r')[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?(?!\d|:)', re.I)
DATE_HE_RX = re.compile(r'(\d{1,2})\s+ב?(' + '|'.join(HE_MONTHS) + r')(?:\s+(\d{4}))?')
TIME_RX = re.compile(r'(\d{1,2}):(\d{2})(?:\s*(AM|PM))?', re.I)

HOW = [(re.compile(r'(?:by|via|through|to)\s+e-?mail|e-?mail (?:it |them |your \w+ )?to|במייל|בדוא"?ל|שלחו[^.\n]{0,30}ל[- ]?\S+@|send[^.\n]{0,40}@', re.I), 'במייל'),
       (re.compile(r'in class|in person|hard ?copy|on paper|printed|בכיתה|בתחילת השיעור|בנייר|מודפס|פיזית', re.I), 'בכיתה / על נייר'),
       (re.compile(r'google ?form|\bform\b|טופס', re.I), 'בטופס'),
       (re.compile(r'moodle|מודל', re.I), 'במודל')]
EMAIL_RX = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')


def _load(path: Path | None = None) -> list[dict]:
    try:
        d = json.loads((path or STORE).read_text(encoding='utf-8'))
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save(items: list[dict], path: Path | None = None) -> None:
    p = path or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def _date_in(s: str, ref: datetime) -> tuple[datetime | None, int]:
    """First date in the sentence → (datetime at its time or 23:59, end offset). A missing year = the next such day."""
    hit = None
    for rx, kind in ((DATE_NUM_RX, 'n'), (DATE_EN_RX, 'e'), (DATE_EN2_RX, 'e2'), (DATE_HE_RX, 'h')):
        for m in rx.finditer(s):
            if kind == 'n':
                d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
                if not y and not CUE_RX.search(s[max(0, m.start() - 30):m.start()]):
                    continue                                  # "1.5 points" is not a day
            elif kind == 'e':
                d, mo, y = int(m.group(1)), EN_MONTHS[m.group(2)[:3].lower()], m.group(3)
            elif kind == 'e2':
                mo, d, y = EN_MONTHS[m.group(1)[:3].lower()], int(m.group(2)), m.group(3)
            else:
                d, mo, y = int(m.group(1)), HE_MONTHS[m.group(2)], m.group(3)
            if y and len(str(y)) == 2:
                y = 2000 + int(y)
            if not 1 <= mo <= 12 or not 1 <= d <= 31:
                continue
            if hit is None or m.start() < hit[0]:
                hit = (m.start(), m.end(), d, mo, int(y) if y else None)
    if not hit:
        return None, 0
    _s, end, d, mo, y = hit
    try:
        year = y or ref.year
        t = TIME_RX.search(s[end:end + 40])
        hh, mm = (int(t.group(1)), int(t.group(2))) if t else (23, 59)
        if t and t.group(3):
            hh = hh % 12 + (12 if t.group(3).upper() == 'PM' else 0)
        dt = datetime(year, mo, d, hh if hh < 24 else 23, mm if mm < 60 else 59)
        if not y and dt < ref - timedelta(days=30):
            dt = dt.replace(year=year + 1)
        return dt, end
    except ValueError:
        return None, 0


def _how(text: str) -> str:
    for rx, label in HOW:
        if rx.search(text or ''):
            if label == 'במייל':
                m = EMAIL_RX.search(text or '')
                return 'במייל' + (f' ל-{m.group(0)}' if m else '')
            return label
    return ''


def detect(subject: str, body: str, ref: datetime) -> list[dict]:
    """[{kind, num, due, how}] — every "<kind> [N] ... due <date>" the text says. Pure."""
    text = f'{subject or ""}. {body or ""}'
    ctx_kind = 'quiz' if QUIZ_RX.search(subject or '') and not ASSIGN_RX.search(subject or '') else \
        'assign' if ASSIGN_RX.search(subject or '') else ''
    m0 = NUM_RX.search(subject or '')
    ctx_num = m0.group(1) if m0 else ''
    out = []
    for sent in re.split(r'(?<=[.!?])\s+(?=[A-Zא-ת\d#])|\n+', text):
        sent = sent.strip()
        if not sent:
            continue
        kind = 'quiz' if QUIZ_RX.search(sent) else 'assign' if ASSIGN_RX.search(sent) else ctx_kind
        mn = NUM_RX.search(sent)
        num = mn.group(1) if mn else (ctx_num if kind == ctx_kind else '')
        if not kind or not CUE_RX.search(sent) or PAST_RX.search(sent):
            continue
        due, _ = _date_in(sent, ref)
        if not due or due < ref - timedelta(days=1):
            continue
        if any(o['kind'] == kind and o['num'] == num and o['due'] == due for o in out):
            continue
        out.append({'kind': kind, 'num': num, 'due': due, 'how': _how(sent) or _how(text)})
    return out


def _title(kind: str, num: str, sample: str) -> str:
    he = bool(re.search(r'[א-ת]', sample or ''))
    base = ('בוחן' if kind == 'quiz' else 'מטלה') if he else ('Quiz' if kind == 'quiz' else 'Assignment')
    return f'{base} {num}'.strip()


def _course_of_mail(m: dict) -> str | None:
    """The one active course a university mail is about: its course number (8 digits, or Moodle's 10), else its name."""
    from agent import obligations as ob
    codes = ob._course_codes()
    active = ob._active()
    hay = f"{m.get('subject', '')} {m.get('from', '')} {(m.get('text') or '')[:600]}"
    cks = {codes[c[:8]] for c in re.findall(r'(?<!\d)(\d{8}|\d{10})(?!\d)', hay) if c[:8] in codes}
    if not cks:
        try:
            from integrations import tau_mail
            cks = set(tau_mail._course_words(hay))
        except Exception:
            cks = set()
    cks = {c for c in cks if not active or c in active}
    return next(iter(cks)) if len(cks) == 1 else None


def _for_me(ck: str, text: str) -> bool:
    try:
        from integrations import univ_relevance
        return univ_relevance.for_me(ck, text)
    except Exception:
        return True


def merge(found: list[dict], ck: str, via: str, sent: datetime, now: datetime, sample: str, verified: bool,
          path: Path | None = None, names: dict | None = None) -> list[dict]:
    """Candidates of one text → announced.json. → the items that are new or whose due date moved."""
    items = _load(path)
    changed = []
    try:
        from config.paths import COURSE_NAMES
    except Exception:
        COURSE_NAMES = {}
    for f in found:
        key_num = f['num']
        for it in items:
            same = it.get('course_key') == ck and (it.get('kind') or 'assign') == f['kind']
            if same and ((key_num and re.findall(r'\d+', it.get('title', ''))[:1] == [key_num])
                         or (not key_num and it.get('due_date', '')[:10] == f['due'].isoformat()[:10])):
                if it.get('due_date') != f['due'].isoformat(timespec='seconds'):
                    it['due_date'] = f['due'].isoformat(timespec='seconds')
                    it['moved_at'] = now.isoformat(timespec='minutes')
                    changed.append(it)
                if f['how'] and not it.get('how'):
                    it['how'] = f['how']
                if verified and it.get('unverified'):
                    it['unverified'] = False
                break
        else:
            slug = key_num or f['due'].strftime('%Y%m%d')
            it = {'course_key': ck, 'course_name': COURSE_NAMES.get(ck, ck), 'title': _title(f['kind'], key_num, sample),
                  'assign_id': f"ann-{f['kind']}-{slug}", 'due_date': f['due'].isoformat(timespec='seconds'),
                  'start_date': sent.isoformat(timespec='minutes'), 'url': '', 'kind': f['kind'], 'source': 'announced',
                  'how': f['how'], 'via': via, 'unverified': not verified, 'submitted': False,
                  'first_seen': now.isoformat(timespec='minutes')}
            items.append(it)
            changed.append(it)
    if changed:
        _save(items, path)
    return changed


def _sent(raw, now: datetime) -> datetime:
    try:
        d = parsedate_to_datetime(str(raw))
        return d.replace(tzinfo=None) if d else now
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(raw)[:19])
        except ValueError:
            return now


def kick_light_check(now: datetime | None = None, stamp: Path | None = None, popen=subprocess.Popen) -> bool:
    """Start scripts/moodle_light_check.py in the background (the VM only; at most once per 30 minutes). → started."""
    now = now or datetime.now()
    py = ROOT / 'venv' / 'bin' / 'python3'
    if sys.platform == 'win32' or not py.exists():
        return False
    sp = stamp or KICK_STAMP
    try:
        last = datetime.fromisoformat(json.loads(sp.read_text(encoding='utf-8')).get('at', ''))
        if now - last < KICK_EVERY:
            return False
    except (OSError, ValueError, TypeError):
        pass
    try:
        log = open(ROOT / 'logs' / 'moodle_light_check.log', 'a', encoding='utf-8')
        popen([str(py), str(ROOT / 'scripts' / 'moodle_light_check.py')], cwd=str(ROOT), stdout=log, stderr=log,
              start_new_session=True, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        sp.write_text(json.dumps({'at': now.isoformat(timespec='seconds')}), encoding='utf-8')
        return True
    except Exception as e:
        print(f'  ⚠️ light-check kick failed: {e!r}'[:200])
        return False


def after(new: list[dict], now: datetime, refresh=None, kick=None) -> None:
    """Something new was named: the store now (offline), the Moodle check in the background. Never raises."""
    if not new:
        return
    try:
        (refresh or __import__('agent.obligations', fromlist=['refresh']).refresh)(now)
    except Exception as e:
        print(f'  ⚠️ obligations refresh after ingest: {e!r}'[:200])
    try:
        (kick or kick_light_check)(now)
    except Exception as e:
        print(f'  ⚠️ light-check kick: {e!r}'[:200])


def from_mails(mails: list[dict], now: datetime | None = None, path: Path | None = None, refresh=None, kick=None,
               course_of=None) -> list[dict]:
    """University mails (integrations/tau_mail.parse shape) → announced.json. → new / moved items."""
    now = now or datetime.now()
    new = []
    for m in mails or []:
        try:
            ck = (course_of or _course_of_mail)(m)
            if not ck:
                continue
            sent = _sent(m.get('date'), now)
            found = detect(m.get('subject', ''), m.get('text') or m.get('snippet', ''), sent)
            if not found or not _for_me(ck, f"{m.get('subject', '')} {(m.get('text') or '')[:300]}"):
                continue
            new += merge(found, ck, 'mail', sent, now, f"{m.get('subject', '')} {m.get('text', '')[:200]}",
                         bool(m.get('verified')), path)
        except Exception as e:
            print(f'  ⚠️ obligation ingest (mail): {e!r}'[:200])
    after(new, now, refresh, kick)
    return new


def from_announcements(items: list[dict], now: datetime | None = None, path: Path | None = None, refresh=None,
                       kick=None) -> list[dict]:
    """New Moodle posts (collectors/announcement_scraper shape: course_key, title, body, date) → announced.json."""
    now = now or datetime.now()
    new = []
    for it in items or []:
        try:
            ck = it.get('course_key')
            if not ck:
                continue
            sent = _sent(it.get('date'), now)
            found = detect(it.get('title', ''), it.get('body', ''), sent)
            if not found or not _for_me(ck, f"{it.get('title', '')} {(it.get('body') or '')[:300]}"):
                continue
            new += merge(found, ck, 'moodle_post', sent, now, f"{it.get('title', '')} {it.get('body', '')[:200]}", True, path)
        except Exception as e:
            print(f'  ⚠️ obligation ingest (post): {e!r}'[:200])
    after(new, now, refresh, kick)
    return new
