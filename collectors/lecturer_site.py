"""
collectors/lecturer_site.py — a lecturer's own course site → his summaries for exactly what was taught that week.

The owner 18/10: "בהסתברות כל הרצאה שנגמרת הוא מעלה כאן סיכומים — אני ארצה סיכום אחוד". Naftali Zevuloni's site has
  • a weekly log (lec/lec27.html): week N → what was taught (topics, or the numbers of his recordings: 0050, 0100…)
  • a summaries table (sum/sum.html): topic → summary PDF (√) + recordings
For a lecture on date D: the week's row → the matching topics → their PDFs (downloaded once, cached) → handed to the
summarizer FIRST as the written material, so the lecture summary is expanded from his own summaries
("📖 השלמה מהחומר") — one unified summary. No LLM here. The site's firewall rejects non-browser requests.
"""
from __future__ import annotations

import html as _html
import re
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                         'Chrome/124.0 Safari/537.36',
           'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
           'Accept-Language': 'he-IL,he;q=0.9,en;q=0.8'}
SITES = {
    'probability': {'weeks': 'https://lecturer-site.example.com/duhugi/lec/lec27.html',
                    'summaries': 'https://lecturer-site.example.com/duhugi/sum/sum.html',
                    'start': date(2026, 10, 18)},          # week 1 = the semester's first week (Sunday)
}
STOP = {'על', 'של', 'את', 'עם', 'גם', 'נלמד', 'למדנו', 'נושא', 'חלק', 'סוגיות', 'נוספות', 'הוכחת', 'למעונינים',
        'בחזרה', 'ההקלטות', 'הסיכומים', 'והקלטות'}


def _text(cell: str) -> str:
    return re.sub(r'\s+', ' ', _html.unescape(re.sub(r'<[^>]+>', ' ', cell))).strip()


def _cells(row: str) -> list[str]:
    """<td> contents — split on the opening tags: the weekly page leaves a cell without its closing </td>."""
    return [re.sub(r'</t[dh]>\s*$', '', c.strip(), flags=re.I) for c in re.split(r'<t[dh](?:\s[^>]*)?>', row, flags=re.I)[1:]]


def _get(url: str) -> str:
    import requests
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.content.decode('utf-8', 'replace')


def parse_summaries(page: str, base: str) -> list[dict]:
    """[{topic, pdf (absolute url or ''), recordings: ['0050', …]}] from the summaries table."""
    out = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S | re.I):
        cells = _cells(row)
        if len(cells) < 2:
            continue
        topic = _text(cells[0])
        pdf = next((h for h in re.findall(r'href="([^"]+\.pdf)"', row, re.I)), '')
        recs = re.findall(r'>\s*(\d{4})\s*</a>', row)
        if topic and (pdf or recs):
            out.append({'topic': topic, 'pdf': (base.rsplit('/', 1)[0] + '/' + pdf) if pdf and '://' not in pdf else pdf,
                        'recordings': recs})
    return out


def parse_weeks(page: str) -> dict[int, str]:
    """{week number: what was taught} from the weekly log (empty weeks left out)."""
    out = {}
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S | re.I):
        cells = [_text(c) for c in _cells(row)]
        if len(cells) >= 2 and cells[0].isdigit() and cells[1].strip(' \xa0'):
            out[int(cells[0])] = cells[1]
    return out


def _words(s: str) -> set[str]:
    return {w.strip('׳\'",.()') for w in re.findall(r'[֐-׿\w׳\'"]{3,}', s)} - STOP


def topics_for(week_text: str, rows: list[dict]) -> list[dict]:
    """The summary rows a week covers: its recordings named explicitly, or (when it's prose) ≥2 shared topic words."""
    named = set(re.findall(r'\b\d{4}\b', week_text))
    hits = [r for r in rows if named & set(r['recordings'])]
    if not hits:
        ww = _words(week_text)
        hits = [r for r in rows if len(_words(r['topic']) & ww) >= 2]
    seen, out = set(), []
    for r in hits:
        if r['pdf'] and r['pdf'] not in seen:
            seen.add(r['pdf'])
            out.append(r)
    return out


def week_of(course_key: str, day: date) -> int:
    start = SITES[course_key]['start']
    return (day - start).days // 7 + 1


def week_pdfs(course_key: str, day: date, cache: Path | None = None) -> list[str]:
    """The lecturer's summary PDFs for the week of `day` (downloaded once). [] if the course has no site, the week
    isn't filled in yet, or the site is down — the summary then goes on as before."""
    site = SITES.get(course_key)
    if not site or day < site['start']:
        return []
    try:
        weeks = parse_weeks(_get(site['weeks']))
        text = weeks.get(week_of(course_key, day), '')
        if not text:
            print(f'  ℹ️ lecturer site: week {week_of(course_key, day)} not filled in yet')
            return []
        rows = topics_for(text, parse_summaries(_get(site['summaries']), site['summaries']))
    except Exception as e:
        print(f'  ⚠️ lecturer site ({course_key}): {e}')
        return []
    import requests
    d = cache or (ROOT / 'data' / 'moodle' / course_key / 'lecturer')     # a subfolder: not globbed twice
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for r in rows:
        p = d / Path(r['pdf']).name
        if not p.exists():
            try:
                resp = requests.get(r['pdf'], headers=HEADERS, timeout=60)
                if resp.status_code != 200 or not resp.content.startswith(b'%PDF'):
                    continue
                p.write_bytes(resp.content)
            except Exception as e:
                print(f"  ⚠️ {r['pdf']}: {e}")
                continue
        out.append(str(p))
    for pdf in out:
        try:
            transcribe(Path(pdf))
        except Exception as e:
            print(f'  ⚠️ transcribing {Path(pdf).name}: {e}')
    if out:
        print(f"  📘 lecturer summaries for week {week_of(course_key, day)}: {', '.join(Path(x).name for x in out)}")
    return out


YT_RX = re.compile(r'href="(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]{6,}[^"]*)"', re.I)
CHUNK_S = 1800                                  # 30-minute pieces — a 3h lecture's transcript doesn't fit one answer
MAX_S = 4 * 3600
YT_Q = ('תמלל את קטע ההקלטה של השיעור בעברית, מילה במילה ככל האפשר, עם חותמות זמן מוחלטות [HH:MM:SS] כל 20–40 שניות '
        '(מתחילת הסרטון). רק התמלול, בלי סיכום. אם אין בקטע דיבור — החזר ריק.')


def week_recordings(page: str) -> dict[int, list[str]]:
    """{week: YouTube links} — 18/10: Naftali records his lectures to YouTube (Panopto has only a placeholder);
    the weekly log links "הקלטת השיעור מהכיתה נמצאת כאן"."""
    out = {}
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S | re.I):
        cells = _cells(row)
        if len(cells) >= 2 and _text(cells[0]).isdigit():
            links = YT_RX.findall(row)
            if links:
                out[int(_text(cells[0]))] = links
    return out


def _stamp(sec: int) -> str:
    return f'{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}'


def to_srt(text: str) -> str:
    """'[HH:MM:SS] words …' (Gemini's transcript) → SRT cues, each ending where the next begins."""
    parts = re.findall(r'\[(\d{1,2}):(\d{2}):(\d{2})\]\s*([^\[]+)', text or '')
    cues = []
    for i, (h, m, s, words) in enumerate(parts):
        t0 = int(h) * 3600 + int(m) * 60 + int(s)
        nxt = parts[i + 1] if i + 1 < len(parts) else None
        t1 = int(nxt[0]) * 3600 + int(nxt[1]) * 60 + int(nxt[2]) if nxt else t0 + 30
        if words.strip():
            cues.append(f'{len(cues) + 1}\n{_stamp(t0)},000 --> {_stamp(max(t1, t0 + 1))},000\n{words.strip()}\n')
    return '\n'.join(cues)


def youtube_transcript(url: str, max_s: int = MAX_S, client=None) -> str:
    """Gemini reads the YouTube video itself (Google fetches it — the VM is blocked by YouTube), 30 minutes at a time,
    audio + 1 frame/10s at low resolution: ~$0.85 for a 3-hour lecture. Stops at the end of the video."""
    import os
    import google.genai as genai
    from google.genai import types as T
    from utils.usage_tracker import log_gemini
    client = client or genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    pieces = []
    for start in range(0, max_s, CHUNK_S):
        part = T.Part(file_data=T.FileData(file_uri=url),
                      video_metadata=T.VideoMetadata(start_offset=f'{start}s', end_offset=f'{start + CHUNK_S}s', fps=0.1))
        try:
            r = client.models.generate_content(
                model='gemini-3.8-flash', contents=[part, YT_Q],
                config=T.GenerateContentConfig(max_output_tokens=16000, temperature=0.2, media_resolution='MEDIA_RESOLUTION_LOW',
                                               thinking_config=T.ThinkingConfig(thinking_level='low')))
        except Exception as e:                      # past the end of the video → the API refuses the offset
            print(f'  ℹ️ youtube {start // 60}min: {str(e)[:120]}')
            break
        log_gemini(r, 'gemini-3.8-flash', op='lecturer_site.youtube')
        text = (r.text or '').strip()
        if len(text) < 200:
            break
        pieces.append(text)
    return '\n'.join(pieces)


def fetch_class_recordings(today: date | None = None, panopto_dir: Path | None = None, transcriber=None) -> list[str]:
    """Each week's class recording (YouTube) that has no transcript yet → data/panopto/<course>_<date>_הרצאה N - <site>.srt,
    which the nightly summaries pick up like any Panopto SRT. One lecture per run; the budget lock applies."""
    from utils.budget import budget_lock_reason
    today = today or date.today()
    pdir = panopto_dir or ROOT / 'data' / 'panopto'
    made = []
    for ck, site in SITES.items():
        try:
            recs = week_recordings(_get(site['weeks']))
        except Exception as e:
            print(f'  ⚠️ lecturer site ({ck}): {e}')
            continue
        for week, links in sorted(recs.items()):
            from datetime import timedelta
            day = site['start'] + timedelta(days=7 * (week - 1))
            if day > today or list(pdir.glob(f'{ck}_{day.isoformat()}_*.srt')):
                continue
            if budget_lock_reason():
                print('  ⏸️ budget lock — not transcribing the class recording')
                return made
            text = (transcriber or youtube_transcript)(links[0])
            srt = to_srt(text)
            if srt.count('-->') < 20:
                print(f'  ⚠️ {ck} week {week}: the recording gave no usable transcript')
                continue
            pdir.mkdir(parents=True, exist_ok=True)
            p = pdir / f'{ck}_{day.isoformat()}_הרצאה {week} - נפתלי.srt'
            p.write_text(srt, encoding='utf-8')
            made.append(str(p))
            print(f'  🎥 {ck} week {week}: transcribed the class recording → {p.name}')
            return made                             # one per run (~$0.85)
    return made


HOMEWORK = {'probability': 'https://lecturer-site.example.com/duhugi/homework/homework.html'}
HW_ITEMS = ROOT / 'data' / 'assignments' / 'lecturer.json'
HW_Q = ('זה תרגיל בית בהסתברות. העתק אותו במלואו לעברית תקינה, נוסחאות ב-LaTeX. שמור במדויק כל תאריך / מועד הגשה '
        'ואופן הגשה.')
DUE_RX = re.compile(r'עד\s+(?:ה?יום\s+\S+\s+)?(?:ה-?)?(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?:\s*(?:בשעה|ב-?)?\s*(\d{1,2}):(\d{2}))?'
                    r'(?:[^\n]{0,20}?(בחצות))?')


def parse_due(text: str) -> str | None:
    """"להגשה עד יום ג' 27.10.26 בחצות" → '2026-10-27T23:59' (midnight = end of that day)."""
    m = DUE_RX.search(text or '')
    if not m:
        return None
    d, mo, y, hh, mm, _midnight = m.groups()
    y = int(y) + (2000 if int(y) < 100 else 0)
    try:
        return datetime(y, int(mo), int(d), int(hh or 23), int(mm or 59)).isoformat(timespec='minutes')
    except ValueError:
        return None


def current_year(page: str) -> str:
    """Only this year's table — 19/10: the homework page lists past years' exercises below ("התרגילים משנת 2021/22"),
    and all 13 of 2021's were taken as deadlines. The first "התרגילים משנת …" section is the current one."""
    first = re.search(r'href="(\w+)/[^"]+\.pdf"', page, re.I)        # the current year's folder ("27/") comes first
    if not first:
        return page
    year = first.group(1)
    return re.sub(r'<tr[^>]*>(?:(?!</tr>).)*href="(?!' + re.escape(year) + r'/)\w+/[^"]+\.pdf".*?</tr>', '', page,
                  flags=re.S | re.I)


def homework_sync(course_key: str = 'probability', path: Path | None = None) -> list[dict]:
    """19/10 (The owner: "כבר העלו מטלות בהסתברות — למה לא מסומן"): the exercises are posted on the lecturer's site (Moodle
    only takes the submission, and its assignment wasn't open) → each new exercise PDF read once → its due date →
    data/assignments/lecturer.json, which utils/deadlines merges with Moodle's. → the items (new ones flagged 'new')."""
    import json
    import requests
    url = HOMEWORK.get(course_key)
    if not url:
        return []
    p = path or HW_ITEMS
    try:
        items = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        items = []
    have = {i['assign_id'] for i in items}
    page = current_year(_get(url))
    base = url.rsplit('/', 1)[0] + '/'
    from config.paths import COURSE_NAMES
    out_new = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S | re.I):
        cells = _cells(row)
        if not cells or not _text(cells[0]).isdigit():
            continue
        n = int(_text(cells[0]))
        pdf = next(iter(re.findall(r'href="([^"]+\.pdf)"', cells[1] if len(cells) > 1 else '', re.I)), '')
        aid = f'site-{course_key}-ex{n}'
        if not pdf or aid in have:
            continue
        d = ROOT / 'data' / 'moodle' / course_key / 'homework'
        d.mkdir(parents=True, exist_ok=True)
        local = d / pdf.replace('/', '_')                      # "27/ex_01.pdf" → "27_ex_01.pdf": years don't collide
        r = requests.get(base + pdf, headers=HEADERS, timeout=60)
        if r.status_code != 200 or not r.content.startswith(b'%PDF'):
            continue
        local.write_bytes(r.content)
        due = parse_due(transcribe(local, HW_Q).read_text(encoding='utf-8'))
        item = {'course_key': course_key, 'course_name': COURSE_NAMES.get(course_key, course_key), 'title': f'תרגיל {n}',
                'assign_id': aid, 'due_date': due, 'url': base + pdf, 'assignment_file_url': base + pdf,
                'submitted': False, 'source': 'lecturer_site', 'kind': 'assign', 'pdf': str(local)}
        items.append(item)
        out_new.append({**item, 'new': True})
        print(f'  📝 {course_key}: exercise {n} — due {due}')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding='utf-8')
    return out_new


USED = ROOT / 'data' / 'lecturer_used.json'
REBUILD_DAYS = 21


def _load_used(path: Path | None = None) -> dict:
    import json
    try:
        return json.loads((path or USED).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def record(course_key: str, md_path: str, srt_path: str, title: str, day: str, used: list[str],
           path: Path | None = None) -> None:
    """Which of the lecturer's summaries went into this lecture summary — so a later update to his site rebuilds it."""
    import json
    if course_key not in SITES:
        return
    d = _load_used(path)
    d[str(md_path)] = {'course': course_key, 'srt': str(srt_path), 'title': title, 'date': str(day)[:10],
                       'used': sorted(Path(u).name for u in used)}
    (path or USED).parent.mkdir(parents=True, exist_ok=True)
    (path or USED).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')


def stale(today: date | None = None, path: Path | None = None, finder=None) -> list[tuple[str, dict, list[str]]]:
    """Summaries (≤3 weeks old) whose week now has lecturer summaries they didn't include —
    18/10 the owner: "בנייה מחדש אוטומטית כשהמרצה מעדכן". → [(md path, record, the week's PDFs now)]."""
    today = today or date.today()
    finder = finder or week_pdfs
    out = []
    for md, rec in _load_used(path).items():
        try:
            day = date.fromisoformat(rec['date'])
        except (KeyError, ValueError):
            continue
        if not 0 <= (today - day).days <= REBUILD_DAYS or not Path(rec.get('srt', '')).exists():
            continue
        now = finder(rec['course'], day)
        if {Path(p).name for p in now} - set(rec.get('used') or []):
            out.append((md, rec, now))
    return out


SITE_PAGES = {'probability': ['https://lecturer-site.example.com/duhugi/index.html', 'https://lecturer-site.example.com/duhugi/main/content.html',
                               'https://lecturer-site.example.com/duhugi/homework/homework.html',
                               'https://lecturer-site.example.com/duhugi/lec/lec27.html', 'https://lecturer-site.example.com/duhugi/sum/sum.html']}
NB_STATE = ROOT / 'data' / 'lecturer_notebook.json'


def notebook_sources(course_key: str) -> dict[str, str]:
    """{source title: text} — the course site's pages, all his summaries, all his exams + solutions, the weight map.
    Few big sources (NotebookLM caps the number of sources per notebook)."""
    from config.paths import VAULT_DIR
    out = {}
    pages = []
    for u in SITE_PAGES.get(course_key, []):
        try:
            pages.append(f'===== {u} =====\n' + _text(re.sub(r'(?is)<(script|style).*?</\1>', ' ', _get(u))))
        except Exception as e:
            print(f'  ⚠️ {u}: {e}')
    if pages:
        out['🌐 אתר הקורס של נפתלי — דפי הקורס, מטלות, מה למדנו בכל שבוע'] = '\n\n'.join(pages)
    lec = sorted((ROOT / 'data' / 'moodle' / course_key / 'lecturer').glob('*.md'))
    if lec:
        out['📘 סיכומי המרצה'] = '\n\n'.join(f'===== {p.stem} =====\n{p.read_text(encoding="utf-8")}' for p in lec)
    exams_dir = VAULT_DIR / '🧾 מבחנים' / 'הסתברות דו-חוגי'
    ex = sorted(exams_dir.glob('20*.md')) if exams_dir.exists() else []
    if ex:
        out['🧾 מבחנים של נפתלי עם פתרונות'] = '\n\n'.join(f'===== {p.stem} =====\n{p.read_text(encoding="utf-8")}' for p in ex)
    w = exams_dir / '📊 משקל נושאים במבחנים.md'
    if w.exists():
        out['📊 משקל הנושאים במבחנים'] = w.read_text(encoding='utf-8')
    return out


def sync_notebook(course_key: str = 'probability') -> str:
    """The course's NotebookLM notebook ('all' scope — the one course questions go to) gets the site's sources;
    a source is replaced only when its text changed."""
    import hashlib
    import json
    from agent import nlm_docs
    from generators import notebooklm_agent as nlm
    srcs = notebook_sources(course_key)
    if not srcs:
        return 'nothing to sync'
    nb = nlm.NotebookLMAgent().get_or_create_notebook(course_key, 'all')
    try:
        st = json.loads(NB_STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        st = {}
    mine = st.setdefault(course_key, {})
    todo = {t: x for t, x in srcs.items() if (mine.get(t) or {}).get('hash') != hashlib.sha1(x.encode()).hexdigest()}
    if not todo:
        return 'up to date'

    async def go():
        async with await nlm_docs._client() as c:
            done = {}
            for title, text in todo.items():
                old = (mine.get(title) or {}).get('sid')
                if old:
                    try:
                        await c.sources.delete(nb, old)
                    except Exception:
                        pass
                s = await c.sources.add_text(nb, title, text[:900000], wait=True, wait_timeout=300)
                done[title] = {'sid': getattr(s, 'source_id', None) or getattr(s, 'id', None),
                               'hash': hashlib.sha1(text.encode()).hexdigest()}
            return done
    mine.update(nlm_docs._run(go()))
    NB_STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
    idx = nlm._load_notebooks_index()                    # so has_notebook() sends course questions there
    rec = idx.setdefault(course_key, {}).setdefault('all', {'notebook_id': nb})
    rec['sources_synced'] = sorted(set(rec.get('sources_synced') or []) | {f'site:{t}' for t in srcs})
    nlm._save_notebooks_index(idx)
    return f'synced {len(todo)} sources'


TRANSCRIBE_Q = ('זה סיכום של מרצה בקורס הסתברות וסטטיסטיקה. העתק אותו במלואו לטקסט עברי תקין (כיוון קריאה רגיל), '
                'כל נוסחה ב-LaTeX בין $...$, שמור על כותרות, הגדרות, משפטים ודוגמאות כפי שהם. בלי הערות משלך.')


def transcribe(pdf: Path, prompt: str | None = None) -> Path:
    """The lecturer's PDF → clean Hebrew markdown next to it (<name>.md), once. 18/10: pdfplumber gave reversed Hebrew
    ("ה לאש" for "שאלה") and broken math glyphs, and 3 of the 7 PDFs were scans with no text at all — Gemini reads
    the PDF itself. A few cents per PDF, once; the summaries reuse the .md."""
    md = pdf.with_suffix('.md')
    if md.exists() and md.stat().st_size > 200:
        return md
    import os
    import time
    import google.genai as genai
    from google.genai import types as T
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    src = pdf
    if not pdf.name.isascii():                     # 20/10: a Hebrew file name breaks the upload's HTTP header
        import shutil
        import tempfile
        src = Path(tempfile.mkdtemp()) / 'doc.pdf'
        shutil.copy2(pdf, src)
    f = client.files.upload(file=str(src))
    for _ in range(30):
        if getattr(getattr(f, 'state', None), 'name', 'ACTIVE') == 'ACTIVE':
            break
        time.sleep(3)
        f = client.files.get(name=f.name)
    try:
        resp = client.models.generate_content(
            model='gemini-3.8-flash', contents=[f, prompt or TRANSCRIBE_Q],
            config=T.GenerateContentConfig(max_output_tokens=20000, temperature=0.1,
                                           thinking_config=T.ThinkingConfig(thinking_level='low')))
    finally:
        try:
            client.files.delete(name=f.name)
        except Exception:
            pass
    from utils.usage_tracker import log_gemini
    log_gemini(resp, 'gemini-3.8-flash', op='lecturer_site.transcribe')
    text = (resp.text or '').strip()
    if len(text) > 200:
        md.write_text(text, encoding='utf-8')
    return md
