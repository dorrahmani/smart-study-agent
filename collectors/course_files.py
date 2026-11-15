"""
collectors/course_files.py — every course's Moodle files (slides, lecture notes) → the summary of the matching lecture.

The owner 19/10: "במודל העלו בתוכנה 1 מצגות — אחרי שמסכמים את ההקלטה תשתמש גם במצגת, וככה בכל קורס". The file collector
(moodle_collector.collect_all) only ran from the old CLI — the server pipeline never downloaded a single slide this
semester. Here: each active course's Moodle pages (all its group ids) → resources with their section ("שיעור 1 - מבוא",
"מצגות - שיעורים") → new files downloaded once into data/moodle/<course>/files/ (pptx/docx text extracted to .md,
no extra dependencies) → for_lecture() picks the 1–3 that belong to a given lecture (its number in the file / section
name, else what appeared that week), and the summarizer merges them as their own block. No LLM here.
A .ppt has no readable XML: convert_pending() makes a <name>.pdf beside it with LibreOffice when the machine has it.
10/11: quiz activities (Software 1's "Quick-check quizzes", mod/quiz) were skipped — only resource/folder were read.
A quiz the owner already finished has a review page (GET only) → <cmid>_quiz_<name>.md, module 'quiz', so it reaches the
course's NotebookLM notebook. A quiz never attempted shows no questions; it is NOT started here (that would count as
an attempt) — it is just left out of the index and looked at again on the next run.
"""
from __future__ import annotations

import html as _html
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from utils.sandbox import bwrap_available, run_sandboxed

ROOT = Path(__file__).resolve().parent.parent
MOODLE = 'https://moodle.tau.ac.il'
SLIDE_RX = re.compile(r'מצגת|שקפ|slides?|lecture|הרצא|שיעור|סיכום שיעור|lec\b|class', re.I)
SKIP_RX = re.compile(r'פתרון|מבחן|בוחן|solution|exam|דף נוסחאות|סילבוס|syllabus|תרגיל בית|מטלה|homework|hw\b', re.I)
MAX_BYTES = 40 * 1024 * 1024
OFFICE_EXT = ('.ppt', '.pptx')
CONVERT_TIMEOUT = 180
CONVERT_MAX_FILE = 300 * 1024 * 1024
CONVERT_MAX_MEMORY = 4 * 1024 * 1024 * 1024
SEMESTER_START = date(2026, 10, 18)


def _dir(course_key: str) -> Path:
    return ROOT / 'data' / 'moodle' / course_key / 'files'


def _index_path(course_key: str) -> Path:
    return _dir(course_key) / 'index.json'


def load_index(course_key: str) -> dict:
    try:
        return json.loads(_index_path(course_key).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def parse_course_page(page: str) -> list[dict]:
    """[{id, title, section}] — every resource with the name of the section it's in."""
    secs = [(m.start(), _html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip())
            for m in re.finditer(r'class="[^"]*sectionname[^"]*"[^>]*>(.*?)</h3>', page, re.S)]
    out, seen = [], set()
    for m in re.finditer(r'href="https://moodle\.tau\.ac\.il/mod/resource/view\.php\?id=(\d+)"[^>]*>(.*?)</a>', page, re.S):
        rid = m.group(1)
        if rid in seen:
            continue
        seen.add(rid)
        title = re.sub(r'\s+', ' ', _html.unescape(re.sub(r'<[^>]+>', ' ', m.group(2))))
        title = re.sub(r'\s+(קובץ|File)$', '', title).strip()
        sec = [n for p, n in secs if p < m.start()]
        out.append({'id': rid, 'title': title or f'file {rid}', 'section': sec[-1] if sec else ''})
    return out


def _text_of_office(path: Path) -> str:
    """pptx / docx → plain text (their XML), so the summary can use slides that aren't PDFs."""
    try:
        with zipfile.ZipFile(path) as z:
            names = sorted((n for n in z.namelist() if re.match(r'ppt/slides/slide\d+\.xml$', n)),
                           key=lambda n: int(re.search(r'(\d+)\.xml$', n).group(1)))
            if names:
                parts = []
                for i, n in enumerate(names, 1):
                    t = ' '.join(re.findall(r'<a:t>([^<]*)</a:t>', z.read(n).decode('utf-8', 'replace')))
                    parts.append(f'### שקף {i}\n{_html.unescape(t)}')
                return '\n\n'.join(parts)
            if 'word/document.xml' in z.namelist():
                x = z.read('word/document.xml').decode('utf-8', 'replace')
                paras = [' '.join(re.findall(r'<w:t[^>]*>([^<]*)</w:t>', p)) for p in re.findall(r'<w:p[ >].*?</w:p>', x, re.S)]
                return _html.unescape('\n'.join(p for p in paras if p.strip()))
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    return ''


def office_binary() -> str | None:
    """No sandbox, no LibreOffice: a file off Moodle isn't opened outside one, it waits ('waiting')."""
    return (shutil.which('soffice') or shutil.which('libreoffice')) if bwrap_available() else None


def to_pdf(path: Path, binary: str, run=run_sandboxed) -> Path | None:
    """slides.ppt → slides.pdf beside it, or None. Made in a temp folder (with its own LibreOffice profile, so two runs
    don't collide) and moved in only when it is a real PDF, so a half-made file never reaches a notebook.
    Audit A8: a deck is someone else's file and LibreOffice is a big parser, so it runs in bwrap (utils/sandbox.py): no
    /home, no network, a scrubbed environment, CPU, file and memory limits. It sees a copy under a fixed name and that's all."""
    target = path.with_suffix('.pdf')
    with tempfile.TemporaryDirectory(prefix='soffice-') as tmp:
        src, made = Path(tmp) / f'deck{path.suffix.lower()}', Path(tmp) / 'deck.pdf'
        try:
            shutil.copyfile(path, src)
            run([binary, '--headless', '--norestore', f'-env:UserInstallation={(Path(tmp) / "profile").as_uri()}',
                 '--convert-to', 'pdf', '--outdir', tmp, str(src)], Path(tmp), timeout=CONVERT_TIMEOUT,
                extra_env={'LC_ALL': 'C.UTF-8'}, file_bytes=CONVERT_MAX_FILE, memory_bytes=CONVERT_MAX_MEMORY)
            with made.open('rb') as f:
                if f.read(5) != b'%PDF-':
                    return None
        except (OSError, subprocess.SubprocessError):
            return None
        part = target.with_name(target.name + '.part')
        shutil.copyfile(made, part)
        part.replace(target)
    return target


def convert_pending(course_key: str, idx: dict | None = None, binary: str | None = None, run=run_sandboxed) -> dict:
    """06/11: micro's .ppt decks (3–8, 10) have no PDF and no text, so they never reached the notebook. Every .ppt / .pptx
    with neither gets a PDF. A .pptx whose text was read has its .md and needs none. No LibreOffice → nothing is touched,
    the files wait ('waiting'). A file that fails is tried again the next day, not on every run.
    idx: the index being worked on (the caller saves it); None = this course's, saved here."""
    index = load_index(course_key) if idx is None else idx
    res: dict = {'converted': [], 'failed': [], 'waiting': []}
    todo = []
    for e in index.values():
        p = ROOT / e['file'] if e.get('file') else None
        if p is None or p.suffix.lower() not in OFFICE_EXT or not p.exists() or p.with_suffix('.pdf').exists():
            continue
        md = p.with_suffix('.md')
        if not (md.exists() and md.stat().st_size > 200) and e.get('convert_failed') != date.today().isoformat():
            todo.append((e, p))
    binary = binary or office_binary()
    if not binary:
        res['waiting'] = [p.name for _, p in todo]
        return res
    for e, p in todo:
        if to_pdf(p, binary, run):
            e.pop('convert_failed', None)
            res['converted'].append(p.name)
        else:
            e['convert_failed'] = date.today().isoformat()
            res['failed'].append(p.name)
    if idx is None and todo:
        _index_path(course_key).write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding='utf-8')
    return res


def _folder_files(s, cm: dict) -> list[tuple[str, str, str]]:
    """A Moodle folder → [(file id, "<folder> — <file name>", direct url)] for each file inside."""
    try:
        page = s.get(f"{MOODLE}/mod/folder/view.php?id={cm['id']}", timeout=30).text
    except Exception:
        return []
    out, seen = [], set()
    for url in re.findall(r'href="(https?://moodle\.tau\.ac\.il/pluginfile\.php/[^"]+/mod_folder/content/[^"]+)"', page):
        url = _html.unescape(url).split('?')[0]
        if url in seen:
            continue
        seen.add(url)
        from urllib.parse import unquote
        name = unquote(url.rsplit('/', 1)[-1])
        fid = f"{cm['id']}:{name}"
        out.append((fid, f"{cm['name']} — {Path(name).stem}", url))
    return out


REVIEW_RX = re.compile(r'mod/quiz/review\.php\?attempt=(\d+)')


def _html_text(fragment: str) -> str:
    """A bit of Moodle HTML → readable text: <pre> code kept as fenced blocks, <code> as `x`, block tags → lines."""
    blocks: list[str] = []

    def _pre(m):
        code = _html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).replace('\xa0', ' ').strip('\n')
        blocks.append(f'\n```java\n{code}\n```\n')
        return f'\x00{len(blocks) - 1}\x00'
    t = re.sub(r'(?s)<pre[^>]*>(.*?)</pre>', _pre, fragment)
    t = re.sub(r'(?s)<code[^>]*>(.*?)</code>', lambda m: '`' + re.sub(r'<[^>]+>', '', m.group(1)) + '`', t)
    t = re.sub(r'(?i)<br\s*/?>|</(p|div|li|tr|h\d)>', '\n', t)
    t = _html.unescape(re.sub(r'<[^>]+>', '', t)).replace('\xa0', ' ')
    t = re.sub(r'\x00(\d+)\x00', lambda m: blocks[int(m.group(1))], t)
    t = re.sub(r'[ \t]+\n', '\n', t)
    return re.sub(r'\n{3,}', '\n\n', t).strip()


def _first(rx: str, s: str) -> str:
    m = re.search(rx, s, re.S)
    return m.group(1) if m else ''


def quiz_review_to_md(page: str, title: str = '') -> str:
    """A Moodle quiz review page (review.php?…&showall=1) → markdown: each question, its options, what was chosen,
    the right answer when the review shows it (else: chosen + "Correct" = right), and any feedback. No LLM."""
    title = title or _html.unescape(_first(r'<title>(.*?)(?::| \|)', page)).strip() or 'Quiz'
    out = [f'# {title} — סקירת ניסיון (Moodle)', '']
    summary = _first(r'(?s)<table[^>]*quizreviewsummary[^>]*>(.*?)</table>', page)
    for th, td in re.findall(r'(?s)<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>', summary):
        out.append(f'- {_html_text(th)}: {_html_text(td)}')
    starts = [m.start() for m in re.finditer(r'<div id="question-[\d-]+" class="que ', page)]
    for n, (a, b) in enumerate(zip(starts, starts[1:] + [len(page)]), 1):
        q = page[a:b]
        qtype = _first(r'class="que (\w+)', q)
        num = _html_text(_first(r'<span class="qno">(.*?)</span>', q)) or str(n)
        state = _html_text(_first(r'<div class="state">(.*?)</div>', q))
        qtext = _html_text(_first(r'(?s)<div class="qtext">(.*?)</div>\s*(?:</div>\s*)?<(?:fieldset|div class="ablock)', q)
                           or _first(r'(?s)<div class="qtext">(.*?)<fieldset', q))
        out += ['', f'## שאלה {num}' + (f' ({qtype})' if qtype else ''), '', qtext, '']
        for cls, inner in re.findall(r'(?s)<div class="(r\d+[^"]*)">(.*?)</div>', q):
            label = _html_text(_first(r'(?s)<label[^>]*>(.*?)</label>', inner) or
                               _first(r'(?s)<div class="flex-fill[^"]*">(.*?)</div>', inner)) or _html_text(inner)
            chosen = 'checked="checked"' in inner
            out.append(f"- [{'x' if chosen else ' '}] {label}" + (' ✅' if chosen and 'correct' in cls.split() else '')
                       + (' ❌' if chosen and 'incorrect' in cls.split() else ''))
        right = _html_text(_first(r'(?s)<div class="rightanswer">(.*?)</div>', q))
        if right:
            out.append(f'\n**{right}**')
        elif state.lower() == 'correct':
            picked = [_html_text(_first(r'(?s)<label[^>]*>(.*?)</label>', i)) for c, i in
                      re.findall(r'(?s)<div class="(r\d+[^"]*)">(.*?)</div>', q) if 'checked="checked"' in i]
            if picked:
                out.append(f"\n**התשובה הנכונה: {', '.join(picked)}**")
        for fb in ('specificfeedback', 'generalfeedback', 'feedback'):
            t = _html_text(_first(rf'(?s)<div class="{fb}">(.*?)</div>\s*</div>', q))
            if right and t.startswith(right):                  # Moodle's .feedback wraps the .rightanswer
                t = t[len(right):].strip()
            if t:
                out.append(f'\n> {t}')
        if state:
            out.append(f'\nמצב: {state}')
    return '\n'.join(out).strip() + '\n'


def _quiz_review(s, cm: dict) -> tuple[str, str] | None:
    """(attempt id, review page HTML) of the latest FINISHED attempt, or None. GET only: view.php lists the attempts
    with their review links; nothing here starts, continues or submits an attempt (no startattempt.php, no POST)."""
    try:
        view = s.get(f"{MOODLE}/mod/quiz/view.php?id={cm['id']}", timeout=30).text
    except Exception:
        return None
    attempts = sorted({int(a) for a in REVIEW_RX.findall(_html.unescape(view))})
    if not attempts:
        return None
    att = str(attempts[-1])
    r = s.get(f"{MOODLE}/mod/quiz/review.php?attempt={att}&cmid={cm['id']}&showall=1", timeout=60)
    if r.status_code != 200 or 'class="que ' not in r.text:
        return None
    return att, r.text


def _download(s, rid: str, dest_stem: Path, direct: str | None = None) -> Path | None:
    if direct:
        url = direct
    else:
        page = s.get(f'{MOODLE}/mod/resource/view.php?id={rid}', timeout=30, allow_redirects=True)
        url = page.url if 'pluginfile.php' in page.url else next(iter(re.findall(r'href="(https?://[^"]*pluginfile\.php[^"]+)"', page.text)), '')
    if not url:
        return None
    r = s.get(url, timeout=120, allow_redirects=True, stream=True)
    size = int(r.headers.get('Content-Length') or 0)
    if r.status_code != 200 or size > MAX_BYTES:
        return None
    ext = Path(url.split('?')[0]).suffix.lower() or '.pdf'
    p = Path(str(dest_stem).replace(':', '_') + ext)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(r.content)
    if ext in ('.pptx', '.docx'):
        t = _text_of_office(p)
        if t:
            p.with_suffix('.md').write_text(t, encoding='utf-8')
    return p


def _sync_quiz(s, course_key: str, cm: dict, idx: dict) -> dict | None:
    """A finished quiz → its review as .md + an index entry (module 'quiz'). Not finished / not reviewable → None and
    nothing indexed, so the next run looks again.
    15/11: a review was saved once, so a re-attempt (or answers Moodle reveals only after the quiz closes) never reached
    the file. Now the latest finished attempt is read every run (the same two GETs); when the attempt or its review text
    changed, the previous file moves to files/replaced/<stem>.attempt<N>[.<date>].md and the new one takes its place
    (the NotebookLM coursework sync replaces the source by its content hash). Still GET only — never starts an attempt."""
    fid = f"{cm['id']}:quiz"
    prev = idx.get(fid)
    try:
        got = _quiz_review(s, cm)
    except Exception as e:
        print(f"  ⚠️ quiz {cm.get('name', '')}: {e}"[:200])
        return None
    if not got:
        return None
    att, page = got
    md = quiz_review_to_md(page, cm['name'])
    if prev and prev.get('file'):
        p = ROOT / prev['file']
        old = p.read_text(encoding='utf-8') if p.exists() else ''
        if old == md and str(prev.get('attempt')) == att:
            return None                                  # nothing new: no write
        if old:
            aside = p.parent / 'replaced' / f"{p.stem}.attempt{prev.get('attempt') or 'x'}" \
                f"{'' if str(prev.get('attempt')) != att else '.' + date.today().isoformat()}.md"
            aside.parent.mkdir(parents=True, exist_ok=True)
            aside.write_text(old, encoding='utf-8')
    else:
        safe = re.sub(r'[^\w֐-׿\- ]', '', cm['name'])[:60].strip() or cm['id']
        p = _dir(course_key) / f"{cm['id']}_quiz_{safe}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(md, encoding='utf-8')
    idx[fid] = {**(prev or {}), 'id': fid, 'title': cm['name'], 'section': cm.get('section', '') or (prev or {}).get('section', ''),
                'module': 'quiz', 'file': str(p.relative_to(ROOT)), 'attempt': att,
                'first_seen': (prev or {}).get('first_seen') or date.today().isoformat()}
    if prev:
        idx[fid]['updated'] = date.today().isoformat()
    return idx[fid]


def sync(course_key: str, session=None, ids: list[str] | None = None) -> list[dict]:
    """New resources of this course → downloaded + indexed. → the new entries."""
    from collectors.announcement_scraper import moodle_ids
    if session is None:
        from collectors.moodle_collector import MoodleCollector
        session = MoodleCollector()._login_and_get_session()
    from collectors.moodle_api import course_state, sesskey
    idx = load_index(course_key)
    new = []
    key = sesskey(session)
    for cid in ids if ids is not None else moodle_ids(course_key, None):
        cms = course_state(session, cid, key)           # every activity, incl. lazily-rendered sections (19/10)
        if not cms:
            try:
                cms = [{'id': r['id'], 'module': 'resource', 'name': r['title'], 'section': r['section'], 'url': ''}
                       for r in parse_course_page(session.get(f'{MOODLE}/course/view.php?id={cid}', timeout=30).text)]
            except Exception as e:
                print(f'  ⚠️ {course_key} {cid}: {e}')
                continue
        for cm in cms:
            if cm['module'] == 'resource':
                items = [(cm['id'], cm['name'], None)]
            elif cm['module'] == 'folder':             # Software 1's "קבצי הרצאה 1" — the files inside
                items = _folder_files(session, cm)
            elif cm['module'] == 'quiz':               # 10/11: Software 1's quick-check quizzes — the review, once done
                if (e := _sync_quiz(session, course_key, cm, idx)):
                    new.append(e)
                continue
            else:
                continue
            for fid, title, url in items:
                if fid in idx:
                    continue
                safe = re.sub(r'[^\w֐-׿\- ]', '', title)[:60].strip() or fid
                try:
                    p = _download(session, cm['id'], _dir(course_key) / f'{fid}_{safe}', direct=url)
                except Exception as e:
                    print(f'  ⚠️ {title}: {e}')
                    p = None
                idx[fid] = {'id': fid, 'title': title, 'section': cm['section'], 'module': cm['module'],
                            'file': str(p.relative_to(ROOT)) if p else '', 'first_seen': date.today().isoformat()}
                if p:
                    new.append(idx[fid])
    try:
        conv = convert_pending(course_key, idx)
        if conv['converted'] or conv['failed']:
            print(f"  📂 {course_key}: slides → PDF: {len(conv['converted'])} made, {len(conv['failed'])} failed")
        if conv['waiting'] and new:
            print(f"  ⚠️ {course_key}: {len(conv['waiting'])} slide files wait for LibreOffice (no soffice here)")
    except Exception as e:                              # never costs the index of what was just downloaded
        print(f'  ⚠️ {course_key} slides → PDF: {e!r}'[:200])
    _index_path(course_key).parent.mkdir(parents=True, exist_ok=True)
    _index_path(course_key).write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding='utf-8')
    if new:
        print(f'  📂 {course_key}: {len(new)} new Moodle files ({", ".join(n["title"][:25] for n in new[:5])}…)')
    return new


def _nums(text: str) -> set[str]:
    return set(re.findall(r"(?:שיעור|הרצאה|מצגת|תרגול|lecture|lec|slides|class|week|שבוע)\s*(?:מספר|מס['׳]?\.?)?\s*[:#-]?\s*(\d{1,2})\b",
                          text or '', re.I))


def for_lecture(course_key: str, day: date, title: str = '', max_files: int = 3, read_pdfs: bool = True) -> list[str]:
    """The Moodle files of THIS lecture: slides/notes whose title or section carries its number (from the recording's
    title, else its week), else slide-like files that first appeared in that week. Paths to read (.md for pptx/docx)."""
    idx = load_index(course_key)
    files = [e for e in idx.values() if e.get('file') and e.get('module') != 'quiz'   # a quiz review isn't slides
             and not SKIP_RX.search(f"{e['title']} {e['section']}")]
    week = (day - SEMESTER_START).days // 7 + 1
    want = _nums(title) or {str(week)}
    hits = [e for e in files if want & _nums(f"{e['title']} {e['section']}")]
    if not hits:
        lo, hi = (day - timedelta(days=6)).isoformat(), (day + timedelta(days=3)).isoformat()
        hits = [e for e in files if lo <= e.get('first_seen', '') <= hi and SLIDE_RX.search(f"{e['title']} {e['section']}")]
    out, seen = [], set()
    for e in hits:                                      # 20/10: micro's two group pages list every deck twice
        t = re.sub(r'\W+', '', e['title'])
        if t in seen:
            continue
        seen.add(t)
        if len(out) >= max_files:
            break
        p = ROOT / e['file']
        if p.suffix.lower() in OFFICE_EXT and p.with_suffix('.pdf').exists():
            p = p.with_suffix('.pdf')                   # the PDF convert_pending made: this one can be read
        md = p.with_suffix('.md')
        if not md.exists() and p.suffix.lower() == '.pdf' and read_pdfs:
            md = _read_slides(p) or md
        out.append(str(md if md.exists() else p))
    return out


SLIDES_Q = ('אלה שקפי הרצאה / קובץ של קורס באוניברסיטה. העתק את כל התוכן לטקסט תקין (עברית בכיוון קריאה רגיל, '
            'אנגלית וקוד כמו שהם), כל נוסחה ב-LaTeX בין $...$, כל שקף מתחיל בשורה "### שקף N" עם הכותרת שלו. '
            'טבלה → רשימה. תרשים → משפט שמתאר מה הוא מראה. בלי הערות משלך.')


def _read_slides(pdf: Path) -> Path | None:
    """23/10: PDF slides were read with pdfplumber only (pymupdf isn't installed) — image slides and reversed Hebrew
    came out empty and were skipped silently (macro's deck, Software 1's lecture files). Gemini reads the PDF once
    → <name>.md next to it (a few cents, once); the summarizer prefers that .md for Moodle files."""
    try:
        from collectors.lecturer_site import transcribe
        md = transcribe(pdf, SLIDES_Q)
        return md if md.exists() and md.stat().st_size > 200 else None
    except Exception as e:
        print(f'  ⚠️ slides read failed ({pdf.name}): {e!r}'[:200])
        return None
