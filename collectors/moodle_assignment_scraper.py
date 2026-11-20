"""
collectors/moodle_assignment_scraper.py
========================================
Auto-discover ALL assignments across all configured courses.
Replaces the hardcoded ASSIGNMENTS_DATA in fetch_assignments_now.py.

Strategy:
  1. Log into Moodle (reuse moodle_collector._login_and_get_session)
  2. For each course in config/courses.yaml:
     - GET /course/view.php?id=<course_id>
     - Find all <a href="/mod/assign/view.php?id=N">
     - For each assignment URL: fetch the assign page
       - Extract title from <h1> / breadcrumb
       - Extract due_date from "מסתיים" / "Due date" text
       - Extract submission status from "מצב ההגשה"
  3. Write to data/assignments/_cache.json

Idempotent. Run anytime to refresh.
"""
from __future__ import annotations
import json, os, re, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=ROOT / '.env', override=True)
except ImportError:
    pass

import yaml
import requests

CACHE_PATH = ROOT / 'data' / 'assignments' / '_cache.json'
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

MOODLE_BASE = 'https://moodle.tau.ac.il'

# Hebrew month names for date parsing
HE_MONTHS = {
    'ינואר': 1, 'פברואר': 2, 'מרץ': 3, 'מרס': 3, 'אפריל': 4,
    'מאי': 5, 'יוני': 6, 'יולי': 7, 'אוגוסט': 8,
    'ספטמבר': 9, 'אוקטובר': 10, 'נובמבר': 11, 'דצמבר': 12,
}

DATE_PATTERNS = [
    # "יום ראשון, 17 מאי 2026, 23:59"
    re.compile(r'(\d{1,2})\s+(' + '|'.join(HE_MONTHS) + r')\s+(\d{4}),?\s*(\d{1,2}):(\d{2})'),
    # "17/05/2026, 23:59"
    re.compile(r'(\d{1,2})/(\d{1,2})/(\d{4}),?\s*(\d{1,2}):(\d{2})'),
]


def parse_he_date(text: str) -> str | None:
    """Try to parse a Hebrew or numeric date string from Moodle into ISO."""
    if not text:
        return None
    for i, pat in enumerate(DATE_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        if i == 0:  # Hebrew month name
            day, month_he, year, hour, minute = m.groups()
            month = HE_MONTHS.get(month_he)
            if not month:
                continue
        else:  # dd/mm/yyyy
            day, month, year, hour, minute = m.groups()
        try:
            dt = datetime(int(year), int(month), int(day), int(hour), int(minute))
            return dt.isoformat()
        except Exception:
            continue
    # 19/10: Software 1's Moodle is in English — "Closes: Monday, 26 October 2026, 11:59 PM"
    m = EN_DATE.search(text)
    if m:
        day, mon, year, hour, minute, ampm = m.groups()
        h = int(hour) % 12 + (12 if (ampm or '').upper() == 'PM' else 0) if ampm else int(hour)
        try:
            return datetime(int(year), EN_MONTHS[mon[:3].lower()], int(day), h, int(minute)).isoformat()
        except (KeyError, ValueError):
            return None
    return None


EN_MONTHS = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
EN_DATE = re.compile(r'(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4}),?\s*(\d{1,2}):(\d{2})\s*(AM|PM)?', re.I)


def login_and_get_session() -> requests.Session:
    """Reuse the playwright login flow from moodle_collector to obtain cookies."""
    from collectors.moodle_collector import MoodleCollector
    mc = MoodleCollector()
    return mc._login_and_get_session()


_TITLE_RE = re.compile(r'תרגיל|מטלה|HW|homework|הגשה', re.IGNORECASE)

def find_assignment_links_on_course_page(session: requests.Session, course_id: str) -> list[dict]:
    """Returns list of {url, title, kind} for ALL assignment-like links on course page.
    Catches /mod/assign/ (real assignments) AND /mod/resource/ with assignment-like titles
    (microeconomics-style — assignments uploaded as PDFs)."""
    r = session.get(f'{MOODLE_BASE}/course/view.php?id={course_id}', timeout=20)
    if r.status_code != 200:
        print(f'  ❌ course {course_id}: HTTP {r.status_code}')
        return []
    html = r.text
    out = []
    seen_ids: set[str] = set()

    # 1. Real assignments (/mod/assign/)
    for m in re.finditer(
        r'href="(https?://moodle\.tau\.ac\.il/mod/assign/view\.php\?id=(\d+))[^"]*"[^>]*>(.*?)</a>',
        html, re.DOTALL
    ):
        href, aid, raw_text = m.groups()
        if aid in seen_ids: continue
        seen_ids.add(aid)
        text = re.sub(r'<[^>]+>', ' ', raw_text)
        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'\s+(מטלה|Assignment)$', '', text, flags=re.I)
        out.append({'url': href, 'assign_id': aid, 'title': text, 'kind': 'assign'})

    # 2. Resource files with assignment-like titles (תרגיל / מטלה / HW)
    for m in re.finditer(
        r'href="(https?://moodle\.tau\.ac\.il/mod/resource/view\.php\?id=(\d+))[^"]*"[^>]*>(.*?)</a>',
        html, re.DOTALL
    ):
        href, rid, raw_text = m.groups()
        if rid in seen_ids: continue
        text = re.sub(r'<[^>]+>', ' ', raw_text)
        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'\s+(קובץ|File|PDF)$', '', text, flags=re.I)
        if not _TITLE_RE.search(text):
            continue   # skip lecture notes, syllabus, etc.
        seen_ids.add(rid)
        out.append({'url': href, 'assign_id': rid, 'title': text, 'kind': 'resource'})

    return out


def fetch_assignment_details(session: requests.Session, assign_url: str) -> dict:
    """Fetch the assignment view page and extract title, due_date, submission status."""
    r = session.get(assign_url, timeout=20)
    if r.status_code != 200:
        return {}
    html = r.text

    # Title (from h1 or page header)
    title = None
    m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
    if m:
        title = re.sub(r'<[^>]+>', '', m.group(1)).strip()

    # Due date — look for "מסתיים" or "Due date" followed by a date
    # The structure on Moodle is usually:
    #   <td>מסתיים</td><td>יום ראשון, 17 מאי 2026, 23:59</td>
    #   OR text "מסתיים: יום ראשון, 17 מאי 2026, 23:59"
    # 14/11 the real Software 1 page: "<strong>Due:</strong> Thursday, 19 November 2026, 23:59" — no "Due date" there,
    # so Assignment 1 had no due date at all (no reminder could ever fire). "Opened:" likewise for the start.
    due_date = _date_after(html, ['מסתיים', 'Due date', 'Due:', 'תאריך הגשה', 'Cut-off date', 'ייסגר', 'נסגר', 'Closes',
                                  'closes'])   # + quizzes
    start_date = _date_after(html, ['נפתחת', 'נפתח', 'Open from', 'Allow submissions from', 'Opened:', 'Opens:'])
    cutoff_date = _date_after(html, ['Cut-off date', 'תאריך סגירה', 'מועד סגירה'])   # late submission allowed until

    # Submitted? Look for "הוגש להערכה" / "Submitted for grading"
    submitted = False
    for marker in ['הוגש להערכה', 'Submitted for grading', 'submissionstatussubmitted', 'הניסיון הוגש', '>Finished<']:
        if marker in html:
            submitted = True
            break

    # Linked files (introattachment) — the first is what they download as the assignment PDF
    files = intro_files(html)
    quiz_state, grade, graded = activity_state(html, '/mod/quiz/' in assign_url)
    if quiz_state == 'finished':
        submitted = True
    feedback = feedback_of(html) if '/mod/assign/' in assign_url else {}
    if feedback.get('grade') and not grade:
        grade = feedback['grade']

    return {
        'title': title,
        'due_date': due_date,
        'start_date': start_date,
        'cutoff_date': cutoff_date if cutoff_date and cutoff_date != due_date else None,
        'quiz_state': quiz_state,
        'grade': grade,
        'graded': graded or bool(feedback),
        'feedback': feedback,
        'submitted': submitted,
        'assignment_file_url': files[0] if files else None,
        'attachment_urls': files,
        'intro': assignment_intro(html),
    }


def _date_after(html: str, labels: list[str]) -> str | None:
    """The first label (in order) followed by a date Moodle wrote → ISO, else None."""
    for label in labels:
        pat = re.compile(re.escape(label) + r'[:\s<>/td"\']*(.+?)(?:</td>|<br|<\s*/p|<\s*/div|\.\s|$)', re.DOTALL)
        m = pat.search(html or '')
        if m:
            iso = parse_he_date(re.sub(r'<[^>]+>', ' ', m.group(1))[:200])
            if iso:
                return iso
    return None


QUIZ_STATES = [('finished', r'Finished|הסתיים|הושלם'), ('in_progress', r'In progress|בתהליך|בביצוע'),
               ('not_finished', r'Never submitted|Overdue|לא הוגש|באיחור')]
GRADE_RX = re.compile(r'(?:Highest grade|Your final grade for this quiz is|Grade|ציון(?: סופי| גבוה ביותר)?)\s*:?\s*'
                      r'(\d+(?:\.\d+)?)\s*(?:/|out of|מתוך)\s*(\d+(?:\.\d+)?)', re.I)


def activity_state(html: str, quiz: bool) -> tuple[str | None, str | None, bool]:
    """14/11 (the obligations layer, agent/obligations.py) → (quiz_state, grade, graded).
    quiz_state, from the quiz's own attempts table ("Status Finished" on the real page): 'finished' | 'in_progress' |
    'not_finished' | 'not_attempted' (no attempt at all); None for an assignment. grade 'x/y' when the page shows it
    (Software 1's quizzes hide marks). graded: an assignment whose grading status says it was graded. Reads the view page
    only — the same GET as before; nothing here starts an attempt."""
    text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html or ''))
    state = None
    if quiz:
        statuses = re.findall(r'(?:Status|מצב)\s*:?\s*(' + '|'.join(rx for _s, rx in QUIZ_STATES) + ')', text)
        state = next((s for s, rx in QUIZ_STATES if any(re.fullmatch(rx, x) for x in statuses)), None)
        if state is None:
            state = 'finished' if '>Finished<' in (html or '') else 'not_attempted'
    m = GRADE_RX.search(text)
    grade = f'{m.group(1)}/{m.group(2)}' if m else None
    graded = bool(re.search(r'Grading status\s*:?\s*Graded|מצב מתן (?:ה)?ציון\s*:?\s*ניתן ציון', text)) or \
        ('ניתן ציון' in text and 'לא ניתן ציון' not in text)
    return state, grade, graded


FEEDBACK_FILE_RX = re.compile(r'href="(https?://moodle\.tau\.ac\.il/pluginfile\.php/[^"]+/assignfeedback_file/[^"]+)"')


def feedback_of(html: str) -> dict:
    """14/11 the grader's feedback on a submitted assignment: Moodle's <div class="feedback"> block (grade, comments)
    + its feedback files. {} when there is none (the real pages of 14/11: nothing graded yet — "לא ניתן ציון")."""
    from collectors.course_files import _html_text
    import html as _h
    m = re.search(r'(?s)<div[^>]*class="[^"]*\bfeedback\b[^"]*"[^>]*>(.*?)(?=<div[^>]*class="[^"]*(?:submissionstatustable|'
                  r'activity-navigation)|</section>|$)', html or '')
    files = []
    for u in FEEDBACK_FILE_RX.findall(html or ''):
        u = _h.unescape(u)
        if u not in files:
            files.append(u)
    rows = feedback_table(m.group(1)) if m else {}
    if rows:                                   # 19/11 the real block is a table; the old dump ran on into the page's scripts
        text = '\n'.join(f'{k}: {v}' for k, v in rows.items() if v)[:INTRO_MAX]
    else:
        text = _html_text(re.sub(r'(?s)<form\b.*?</form>|<script\b.*?</script>', ' ', m.group(1)))[:INTRO_MAX].strip() if m else ''
    if not text and not files:
        return {}
    g = GRADE_RX.search(re.sub(r'\s+', ' ', text))
    out = {'text': text, 'files': files, 'grade': f'{g.group(1)}/{g.group(2)}' if g else None}
    for key, rx in FEEDBACK_FIELDS:
        v = next((val for k, val in rows.items() if re.search(rx, k, re.I)), '')
        if v:
            out[key] = v
    return out


# 19/11 the rows of Moodle's feedback table (th → td), Hebrew and English pages alike
FEEDBACK_FIELDS = [('grade_text', r'^(?:ציון|ציונים|Grade)$'), ('graded_at', r'נבדק ב|Graded on'),
                   ('grader', r'נבדק על|Graded by'), ('comments', r'הערות|Feedback comments')]


def feedback_table(block: str) -> dict[str, str]:
    """{row title: its text} of the feedback table ("ציונים" → "עובר", "הערות למשוב" → "מצוין"). Images (emoticons, the
    grader's avatar initials) are dropped; the grader's name is the cell's text. {} when there is no table."""
    import html as _h
    out = {}
    for th, td in re.findall(r'(?s)<tr[^>]*>\s*<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>\s*</tr>', block or ''):
        td = re.sub(r'(?s)<span[^>]*userinitials[^>]*>.*?</span>|<img[^>]*>', ' ', td)
        td = re.sub(r'(?i)<br\s*/?>|</p>|</li>|</div>', '\n', td)
        k = re.sub(r'\s+', ' ', _h.unescape(re.sub(r'<[^>]+>', ' ', th))).strip()
        v = '\n'.join(re.sub(r'[ \t\xa0]+', ' ', x).strip() for x in _h.unescape(re.sub(r'<[^>]+>', ' ', td)).splitlines())
        v = re.sub(r'\n{2,}', '\n', v).strip()
        if k:
            out[k] = v
    return out


def save_feedback(entry: dict, dest_dir: Path, session=None) -> list[Path]:
    """The feedback → data/moodle/<course>/files/<id>_feedback_<title>.md (+ its files beside it), rewritten only when it
    changed. GET only (the feedback files are pluginfile downloads of the owner's own graded work)."""
    from urllib.parse import unquote
    fb = entry.get('feedback') or {}
    if not fb:
        return []
    title = re.sub(r'[<>:"/\\|?*]', '', entry.get('title') or 'untitled').strip()[:60]
    stem = f"{entry.get('assign_id') or 'x'}_feedback_{title}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    md = dest_dir / f'{stem}.md'
    body = (f"# משוב — {entry.get('course_name', '')}: {entry.get('title', '')}\n\n"
            + (f"ציון: {fb['grade']}\n\n" if fb.get('grade') else '') + (fb.get('text') or '') + '\n')
    made = []
    if not md.exists() or md.read_text(encoding='utf-8') != body:
        md.write_text(body, encoding='utf-8')
        made.append(md)
    for n, url in enumerate(fb.get('files') or [], 1):
        name = re.sub(r'[<>:"/\\|?*]', '', unquote(url.split('?')[0].rsplit('/', 1)[-1]))[:60] or f'file{n}'
        p = dest_dir / f'{stem}_{n}_{name}'
        if session is None or (p.exists() and p.stat().st_size > 0):
            continue
        try:
            r = session.get(url, timeout=60, allow_redirects=True)
            if r.status_code == 200 and r.content:
                p.write_bytes(r.content)
                made.append(p)
        except Exception as e:
            print(f'  ⚠️ feedback file {title}: {e}'[:200])
    return made


# 11/11: an assignment (mod/assign) is a page, not a file: the exercise is its description and/or its attached files.
# Only the first attachment was kept, and the description never — so an exercise written in the page reached nothing.
# GET only: the view page; nothing here opens the submission editor or posts anything.
INTRO_FILE_RX = re.compile(r'href="(https?://moodle\.tau\.ac\.il/pluginfile\.php/[^"]+/mod_assign/introattachment/[^"]+)"')
INTRO_MAX = 6000


def intro_files(html: str) -> list[str]:
    """Every attached file of the assignment's description, once each, in page order."""
    import html as _h
    out = []
    for u in INTRO_FILE_RX.findall(html or ''):
        u = _h.unescape(u)
        if u.split('?')[0] not in [x.split('?')[0] for x in out]:
            out.append(u)
    return out


def assignment_intro(html: str) -> str:
    """The assignment's description as text ('' if none). Moodle 4: <div class="activity-description" id="intro">;
    older themes: <div id="intro" class="box …">. The list of attached files inside it is left out."""
    from collectors.course_files import _html_text
    m = re.search(r'(?s)<div[^>]*\bid="intro"[^>]*>(.*?)(?=<div[^>]*(?:class="[^"]*(?:submissionstatustable|'
                  r'activity-dates|completion-info|gradingsummary)|id="region-main-settings)|</section>|$)', html or '')
    if not m:
        return ''
    body = re.sub(r'(?s)<(ul|div)[^>]*(?:introattachment|fileuploadsubmission)[^>]*>.*?</\1>', ' ', m.group(1))
    body = re.sub(r'(?s)<a[^>]*mod_assign/introattachment[^>]*>.*?</a>', ' ', body)
    # 13/11 the first real page: "Edit submission / Remove submission" (the submission buttons) came out as the
    # description's last lines. A description never holds a form or a button.
    body = re.sub(r'(?s)<form\b.*?</form>|<button\b.*?</button>|<a\b[^>]*class="[^"]*\bbtn\b[^"]*"[^>]*>.*?</a>', ' ', body)
    return _html_text(body)[:INTRO_MAX].strip()


def save_assignment_extras(entry: dict, dest_dir: Path, session=None) -> list[Path]:
    """What the assignment PDF alone misses → beside it in data/assignments/<course>/:
    the description as <title>_assignment.md, and attachments 2..n as <title>_assignment_<n>_<name>. The first attachment
    stays scripts/download_assignments.py's <title>_assignment.pdf. Written once; without a session only the text.
    → the files written now."""
    from urllib.parse import unquote
    title = re.sub(r'[<>:"/\\|?*]', '', entry.get('title') or 'untitled').strip()[:80]
    dest_dir.mkdir(parents=True, exist_ok=True)
    made = []
    intro = (entry.get('intro') or '').strip()
    md = dest_dir / f'{title}_assignment.md'
    if intro and not md.exists():
        head = f"# {entry.get('title') or title}\n\n" + (f"מועד הגשה: {entry['due_date']}\n\n" if entry.get('due_date') else '')
        md.write_text(head + intro + '\n', encoding='utf-8')
        made.append(md)
    if session is None:
        return made
    for n, url in enumerate((entry.get('attachment_urls') or [])[1:], 2):
        name = re.sub(r'[<>:"/\\|?*]', '', unquote(url.split('?')[0].rsplit('/', 1)[-1]))[:60] or f'file{n}'
        p = dest_dir / f'{title}_assignment_{n}_{name}'
        if p.exists() and p.stat().st_size > 0:
            continue
        try:
            r = session.get(url, timeout=60, allow_redirects=True)
        except Exception as e:
            print(f'  ⚠️ {title} attachment {n}: {e}'[:200])
            continue
        if r.status_code == 200 and r.content:
            p.write_bytes(r.content)
            made.append(p)
    return made


def extras_missing(entry: dict, dest_dir: Path) -> bool:
    """True when an attachment beyond the first isn't on disk yet (the description needs no login)."""
    title = re.sub(r'[<>:"/\\|?*]', '', entry.get('title') or 'untitled').strip()[:80]
    return any(not list(dest_dir.glob(f'{glob_escape(title)}_assignment_{n}_*'))
               for n in range(2, len(entry.get('attachment_urls') or []) + 1))


def glob_escape(s: str) -> str:
    return re.sub(r'([\[\]*?])', r'[\1]', s)


def make_entry(ck: str, course_name: str, link: dict, details: dict, now: datetime | None = None) -> dict:
    """One _cache.json entry (the nightly scrape and scripts/moodle_light_check write the same shape)."""
    now = now or datetime.now()
    entry = {
        'course_key': ck,
        'course_name': course_name,
        'title': details.get('title') or link['title'],
        'url': link['url'],
        'assign_id': link['assign_id'],
        'due_date': details.get('due_date'),
        'start_date': details.get('start_date'),
        'cutoff_date': details.get('cutoff_date'),
        'quiz_state': details.get('quiz_state'),
        'grade': details.get('grade'),
        'graded': details.get('graded', False),
        'feedback': details.get('feedback') or {},
        'section': link.get('section', ''),
        'submitted': details.get('submitted', False),
        'assignment_file_url': details.get('assignment_file_url'),
        'attachment_urls': details.get('attachment_urls') or [],
        'intro': details.get('intro') or '',
        'source': 'moodle_auto',
        'kind': link.get('kind', 'assign'),       # 'resource' = a PDF; Moodle can't show its submission
        '_scraped_at': now.isoformat(),
    }
    if link.get('completion') is not None:       # 15/11 Moodle's completion mark ("Done" / "Mark as done")
        entry['completion'] = link['completion']
    if entry['due_date']:
        try:
            entry['days_left'] = (datetime.fromisoformat(entry['due_date']) - now).days
        except Exception:
            pass
    return entry


def scrape_all() -> dict:
    """Main entrypoint: scrape every course, return cache dict."""
    print('🌐 Moodle assignment auto-discover starting...', flush=True)
    cfg = yaml.safe_load((ROOT / 'config' / 'courses.yaml').read_text(encoding='utf-8'))
    courses = cfg.get('courses', cfg)

    print('🔐 מתחבר ל-Moodle...', flush=True)
    session = login_and_get_session()
    if not session:
        print('❌ לא הצלחתי להתחבר')
        return {'_ts': datetime.now().timestamp(), 'assignments': []}
    print('✅ מחובר', flush=True)

    # Only this semester's courses: past-semester Moodle pages 404 once archived,
    # and that nightly noise trains you to ignore the health alert.
    try:
        from config.paths import ACTIVE_COURSES
        courses = {k: v for k, v in courses.items() if k in ACTIVE_COURSES} or courses
    except ImportError:
        pass

    all_assigns: list[dict] = []
    for ck, ccfg in courses.items():
        moodle_cfg = (ccfg.get('sources') or {}).get('moodle') or {}
        if not moodle_cfg.get('enabled'):
            continue
        course_id = str(moodle_cfg.get('course_id') or '')
        from collectors.announcement_scraper import moodle_ids
        ids = moodle_ids(ck, course_id)                 # yaml id, else the ids discovered from the owner's /my/ page
        if not ids:
            if course_id:  # a placeholder like TO_BE_FILLED — not a real Moodle id
                from utils.pipeline_health import pipeline_warn
                pipeline_warn(f'{ck}: course_id של מודל עדיין לא מולא ({course_id}) — מטלות לא נאספות')
            continue
        course_name = ccfg.get('name', ck)
        print(f'\n📚 {course_name} ({ck})...', flush=True)
        links, seen_links = [], set()
        from collectors.moodle_api import course_state, sesskey
        key = sesskey(session)
        for cid in ids:                                 # lecture + tutorial groups: homework may sit in either
            try:
                found = find_assignment_links_on_course_page(session, cid)
                # 19/10: lazily-rendered sections ("הגשת מטלות") never reach the page HTML — the course state API has
                # every assignment and quiz (The owner's submitted probability exercise 1 was invisible)
                for cm in course_state(session, cid, key):
                    if cm['module'] in ('assign', 'quiz') and cm['url']:
                        found.append({'url': cm['url'], 'assign_id': cm['id'], 'title': cm['name'],
                                      'kind': cm['module'], 'section': cm['section'],
                                      'completion': cm.get('completion')})
                for link in found:
                    u = link['url'].split('&')[0]
                    if u not in seen_links:
                        seen_links.add(u)
                        links.append(link)
            except Exception as e:
                print(f'  ❌ שגיאה ({cid}): {e}')
        print(f'  🔍 נמצאו {len(links)} קישורים למטלות', flush=True)
        for link in links:
            try:
                details = fetch_assignment_details(session, link['url'])
            except Exception as e:
                print(f'  ⚠️  {link["title"]}: {e}')
                continue
            entry = make_entry(ck, course_name, link, details)
            if entry['feedback']:                         # 14/11 his graded feedback → beside the course's files
                try:
                    save_feedback(entry, ROOT / 'data' / 'moodle' / ck / 'files', session)
                except Exception as e:
                    print(f'  ⚠️ feedback save {entry["title"][:30]}: {e!r}'[:200])
            all_assigns.append(entry)
            status = '✅' if entry['submitted'] else '⏳'
            due_str = entry['due_date'] or 'אין תאריך'
            print(f'    {status} {entry["title"][:40]:40} | {due_str}', flush=True)

    cache = {'_ts': datetime.now().timestamp(), 'assignments': all_assigns}
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n💾 נשמר: {CACHE_PATH} ({len(all_assigns)} מטלות)')
    return cache


if __name__ == '__main__':
    scrape_all()
