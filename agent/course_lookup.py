"""
agent/course_lookup.py — a course fact the bot doesn't have → it looks in the course's own sources, like an assistant.

The owner 03/11: "לא רוצה שינחש, אני רוצה שיחפש וידע — שיחפש קודם במודל ובסילבוסים". Also: "שיזכור איפה מצא פעם קודמת…
הסתברות ישר ללכת לאתר ולמודל, השאר הרוב במודל".

Sources per course, all already on the server:
  • the Moodle files (data/moodle/<course>/files, collectors/course_files.py), syllabus / admin first;
  • the lecturer's site (collectors/lecturer_site.SITES): the home page plus his summaries;
  • the Moodle announcements (the vault);
  • our own course site (data/site/<course>/…/search.json, built by scripts/build_course_site.py).
One Gemini call reads the most likely documents and answers only from them: found → the answer plus where.
It remembers per course and topic which document answered (data/course_lookup.json), so the next time that
document goes first. It also keeps the answer for a week. Not found → None, and the caller searches the web.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import time
from pathlib import Path

from utils.gemini_sub import api_only

ROOT = Path(__file__).resolve().parent.parent
MEMORY = ROOT / 'data' / 'course_lookup.json'
MODEL = 'gemini-3.8-flash'
MAX_CHARS = 150_000
ANSWER_DAYS = 7
TIME_WORDS = re.compile(r'השבוע|שבוע הבא|היום|מחר|הבא|הקרוב|עכשיו|נשאר')    # the answer changes with the date: not kept
SITE_SECONDS = 10                                        # a slow lecturer site can't hold up a WhatsApp answer
SITE_MAX_BYTES = 2_000_000
SITE_BACKOFF = 3600
INFO_RX = re.compile(r'סילבוס|syllab|נהלי|admin|מידע|דרישות|מנהל|הודעה|ציון|מבנה', re.I)


def _mem(path: Path | None = None) -> dict:
    try:
        return json.loads((path or MEMORY).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save(m: dict, path: Path | None = None) -> None:
    p = path or MEMORY
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding='utf-8')


def _norm(q: str) -> str:
    return ' '.join(re.findall(r'[\w֐-׿]+', (q or '').lower()))


SITE_MAX_PAGES = 15


def site_links(page: str, base: str) -> list[str]:
    """The site's own pages linked from `page`: same host, under the site's folder (`base`), html (or no extension),
    no files / anchors / mail. In page order, unique."""
    from urllib.parse import urljoin, urldefrag, urlparse
    root = urlparse(base)
    out = []
    for href in re.findall(r'(?i)href\s*=\s*["\']([^"\']+)["\']', page or ''):
        if href.startswith(('mailto:', 'javascript:', '#', 'tel:')):
            continue
        url = urldefrag(urljoin(base, _html.unescape(href.strip())))[0]
        p = urlparse(url)
        ext = p.path.rsplit('.', 1)[-1].lower() if '.' in p.path.rsplit('/', 1)[-1] else ''
        if p.netloc != root.netloc or not p.path.startswith(root.path) or ext not in ('', 'html', 'htm', 'php', 'asp', 'aspx'):
            continue
        if url not in out and url.rstrip('/') != base.rstrip('/'):
            out.append(url)
    return out


def _page_text(page: str) -> str:
    text = _html.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'(?is)<(script|style).*?</\1>', '', page)))
    return re.sub(r'\s+', ' ', text).strip()


def _site_pages(ck: str, get=None, cache_dir: Path | None = None, now: float | None = None) -> list[tuple[str, str, str]]:
    """The lecturer's site — the home page and every page of the site it links to (one level, ≤ SITE_MAX_PAGES):
    03/11 eval — grade rules (main/content.html), office hours (main/meetings.html) and the TA's contact
    (main/ta.html) were never read, so the lookup went to the web (40–52s, unverified extras). The weekly log and
    the summaries pages always (The owner 03/11: "המרצה מעלה הכל לפה"). Each page and the link list cached for a day."""
    from collectors import lecturer_site
    site = lecturer_site.SITES.get(ck)
    if not site:
        return []
    get = get or lecturer_site._get
    now = now or time.time()
    d = cache_dir or (ROOT / 'data' / 'moodle' / ck)
    base = site['weeks'].split('/lec/')[0] + '/'

    deadline = time.monotonic() + SITE_SECONDS
    down = d / 'site_down'                               # after a failure: an hour on the saved copies

    def fetch(name: str, url: str) -> str:
        cache = d / f'site_{name}.txt'
        stale = not cache.exists() or now - cache.stat().st_mtime > 86400
        resting = down.exists() and now - down.stat().st_mtime < SITE_BACKOFF
        if (stale or (name == 'home' and not (d / 'site_links.json').exists())) and not resting and time.monotonic() < deadline:
            try:
                raw = get(url)[:SITE_MAX_BYTES]
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(_page_text(raw), encoding='utf-8')
                if name == 'home':
                    (d / 'site_links.json').write_text(json.dumps(site_links(raw, base)), encoding='utf-8')
            except Exception:
                d.mkdir(parents=True, exist_ok=True)
                down.touch()
                if not cache.exists():
                    raise
        return cache.read_text(encoding='utf-8')             # an old copy beats no copy
    out = []
    try:
        out.append(('site:home', f'האתר של המרצה — דף הבית ({base})', fetch('home', base)))
    except Exception as e:
        print(f'  ⚠️ lecturer site home ({ck}): {e!r}'[:160])
    try:
        links = json.loads((d / 'site_links.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        links = []
    pages = [('weeks', 'מה נלמד בכל שבוע', site['weeks']), ('summaries', 'סיכומים והקלטות', site['summaries'])]
    known = {u for _, _, u in pages}
    for url in links:
        if url in known or len(pages) >= SITE_MAX_PAGES:
            continue
        slug = re.sub(r'[^\w]+', '_', url[len(base):].rsplit('.', 1)[0]).strip('_')[:60] or 'page'
        pages.append((f'p_{slug}', url[len(base):], url))
        known.add(url)
    for name, label, url in pages:
        try:
            out.append((f'site:{name}', f'האתר של המרצה — {label} ({url})', fetch(name, url)))
        except Exception as e:
            print(f'  ⚠️ lecturer site {name} ({ck}): {e!r}'[:160])
    return out


def unread_pdfs(ck: str) -> list[str]:
    """Info PDFs nobody transcribed yet, so no search sees them."""
    base = ROOT / 'data' / 'moodle' / ck
    return [p.name for sub in ('files', 'lecturer') for p in sorted((base / sub).glob('*.pdf'))
            if INFO_RX.search(p.name) and not p.with_suffix('.md').exists()]


def documents(ck: str, read_pdfs: bool = True) -> list[tuple[str, str, str]]:
    """[(id, title, text)] — every source of the course that has text."""
    base = ROOT / 'data' / 'moodle' / ck
    docs = _site_pages(ck)
    for sub, label in (('files', 'מודל'), ('lecturer', 'אתר המרצה')):
        d = base / sub
        if not d.is_dir():
            continue
        for pdf in sorted(d.glob('*.pdf')):                # a syllabus / admin PDF is read once (a few cents)
            if read_pdfs and INFO_RX.search(pdf.name) and not pdf.with_suffix('.md').exists():
                try:
                    from collectors.lecturer_site import transcribe
                    transcribe(pdf, 'העתק את כל תוכן המסמך לטקסט עברי תקין (כיוון קריאה רגיל). בלי הערות משלך.')
                except Exception as e:
                    print(f'  ⚠️ read {pdf.name}: {e!r}'[:160])
        for md in sorted(d.glob('*.md')):
            try:
                docs.append((f'{sub}:{md.name}', f"{label}: {re.sub(r'^[0-9:_]+', '', md.stem)}",
                             md.read_text(encoding='utf-8')))
            except OSError:
                pass
    for f in sorted(base.parent.parent / 'site' / ck / t / 'search.json' for t in ('topics', 'guide', 'lectures', 'tutorials')):
        try:                                               # our own course site (scripts/build_course_site.py)
            for e in json.loads(f.read_text(encoding='utf-8')):
                docs.append((f"ours:{f.parent.name}/{e['u']}", f"האתר שלנו ({f.parent.name}): {e['t']}", e.get('x') or ''))
        except (OSError, ValueError, KeyError):
            pass
    try:
        from dashboard import vault_reader
        for n in vault_reader.list_announcements(ck)[:40]:
            docs.append((f"ann:{n['filename']}", f"הודעה במודל: {n['title']} ({n.get('date', '')})", n.get('body') or ''))
    except Exception as e:
        print(f'  ⚠️ announcements ({ck}): {e!r}'[:160])
    return [d for d in docs if d[2].strip()]


def _variants(question: str) -> list[str]:
    """The question's words, with a Hebrew prefix letter off: "בסילבוס" should find "סילבוס"."""
    out = []
    for w in _norm(question).split():
        if len(w) < 2:
            continue
        out.append(w)
        if len(w) > 3 and w[0] in 'בהולמשכ':
            out.append(w[1:])
    return list(dict.fromkeys(out))


def passages(question: str, docs: list, limit: int = 6, width: int = 1000) -> list[tuple[str, str, str]]:
    """[(doc id, title, the part of the text around the words)], best first, no LLM: rarer words count more, a word in the
    title counts too, at most two parts from one document."""
    import math
    words = _variants(question)
    if not words or not docs:
        return []
    low = [(d, d[2].lower(), d[1].lower()) for d in docs]
    df = {w: sum(w in t or w in title for _, t, title in low) for w in words}
    weight = {w: math.log(1 + len(docs) / df[w]) for w in words if df[w]}
    found = []
    for d, text, title in low:
        head = sum(v for w, v in weight.items() if w in title)
        spots: dict[int, set] = {}
        for w in weight:
            at, n = text.find(w), 0
            while at >= 0 and n < 40:
                spots.setdefault(at // width, set()).add(w)
                at, n = text.find(w, at + 1), n + 1
        if head and not spots:                           # the document is named after it: its opening is the answer
            found.append((0.5 * head, d[0], d[1], d[2][:width + 300].strip()))
        ranked = sorted(spots.items(), key=lambda kv: -sum(weight[w] for w in kv[1]))[:2]
        for block, hit in ranked:
            start = max(0, block * width - 150)
            found.append((sum(weight[w] for w in hit) + 0.5 * head, d[0], d[1], d[2][start:start + width + 300].strip()))
    found.sort(key=lambda r: -r[0])
    return [(i, t, x) for _, i, t, x in found[:limit]]


def file_names(ck: str, question: str, limit: int = 8) -> list[str]:
    """The course's Moodle files whose name has one of the question's words."""
    d = ROOT / 'data' / 'moodle' / ck / 'files'
    words = _variants(question)
    try:
        names = sorted(p.name for p in d.iterdir() if p.suffix.lower() in ('.pdf', '.pptx', '.ppt', '.docx', '.xlsx', '.zip'))
    except OSError:
        return []
    return [re.sub(r'^[0-9:_]+', '', n) for n in names if any(w in n.lower() for w in words)][:limit]


def rank(docs: list, question: str, remembered: list[str]) -> list:
    """Remembered sources first (this topic, then this course), then syllabus / admin / the site / announcements,
    then by how many of the question's words appear."""
    words = {w for w in _norm(question).split() if len(w) > 2}

    def score(d):
        i, title, text = d
        mem = remembered.index(i) if i in remembered else None
        info = bool(INFO_RX.search(title)) or i.startswith(('site:', 'ann:'))
        hits = sum(w in text for w in words)
        return (mem is None, mem or 0, not info, -hits)
    return sorted(docs, key=score)


PROMPT = """אתה העוזר של המשתמש, סטודנט באוניברסיטת ת"א. היום {today} (שבוע {week} בסמסטר). השאלה שלו על הקורס "{course}": {question}
לפניך מסמכים של הקורס: סילבוס, קבצים ממודל, הודעות במודל ואתר המרצה. ענה רק ממה שכתוב בהם במפורש.
התאם לתפקיד ולקבוצה שנשאלו: מתרגל ≠ מרצה, והקבוצה של המשתמש היא {groups}.
החזר JSON בלבד: {{"found": true/false, "answer": "תשובה קצרה בעברית לוואטסאפ (1–4 שורות)", "source": "ה-id של המסמך"}}
אם התשובה לא כתובה במסמכים — found=false. אל תנחש.

{docs}"""


@api_only()                                             # pages from the web
def _ask(prompt: str) -> dict:
    from utils.budget import budget_lock_reason
    if budget_lock_reason():
        return {}
    import google.genai as genai
    from google.genai import types as T
    from utils.usage_tracker import log_gemini
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    resp = client.models.generate_content(model=MODEL, contents=prompt, config=T.GenerateContentConfig(
        temperature=0.1, max_output_tokens=1500, response_mime_type='application/json',
        thinking_config=T.ThinkingConfig(thinking_level='low')))
    log_gemini(resp, MODEL, op='agent.course_lookup')
    from utils.gemini_sub import json_of
    return json.loads(json_of(resp.text) or '{}')


PATHS_LOG = ROOT / 'data' / 'course_answer_paths.jsonl'
SAVED_PER_LOOKUP = 0.05     # USD a Gemini lookup costs: ~30k tokens of course documents in + the answer, gemini-3.8-flash


def log_path(ck: str, path: str, sec: float, saved: float = 0.0, log: Path | None = None, ledger: bool = True) -> None:
    """Which path answered a course question: notebook (free) / gemini / cache / none. A notebook answer also goes in
    the cost ledger (data/usage.jsonl) at $0, with what the Gemini lookup would have cost as extra.saved_usd."""
    if log is None:
        return
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'course': ck, 'path': path, 'sec': round(sec, 1),
                                'saved_usd': saved}) + '\n')
    except OSError:
        pass
    if saved and ledger:
        try:
            from utils.usage_tracker import log_usage
            log_usage(service='notebooklm', model='notebooklm', op='agent.course_lookup.notebook',
                      extra={'saved_usd': saved, 'course': ck})
        except Exception:
            pass
    print(f'  📚 course question ({ck}) answered by: {path} in {sec:.1f}s' + (f' (saved ~${saved})' if saved else ''))


# 17/11 measured on the server: a NotebookLM answer takes 25–52s (median ~38s), the Gemini lookup 3–5s. The owner chose
# (17/11) free answers with sources over speed: the notebook gets 45s; a slower one is left behind (not "down") for the
# Gemini read, so the worst case is ~50s, inside the 90s turn.
NOTEBOOK_SECONDS = 45


def _notebook(question: str, ck: str) -> dict | None:
    from agent import nlm_ask
    return nlm_ask.ask_grounded(ck, question, timeout=NOTEBOOK_SECONDS)


def find(question: str, ck: str, topic: str = '', ask=None, docs: list | None = None, path: Path | None = None,
         now: float | None = None, notebook=None, log: Path | None = None) -> str | None:
    """→ the answer with its source, or None (not in the course's sources).
    17/11: the course's NotebookLM notebook first (free; it holds every lecture's transcript and summary): a grounded
    answer is used with its sources. Not grounded / too slow / down → the paid Gemini read below.
    notebook=False skips it; a test that hands in `ask` / `docs` (and no notebook) never reaches the real one."""
    m = _mem(path)
    now = now or time.time()
    key = f'{ck}|{_norm(question)}'
    timely = bool(TIME_WORDS.search(question or ''))
    injected = ask is not None or docs is not None             # a test: nothing real is called or written
    logp = log if log is not None else (None if injected else PATHS_LOG)
    ledger = log is None and not injected
    old = (m.get('answers') or {}).get(key)
    if old and not timely and now - old['at'] < ANSWER_DAYS * 86400:
        log_path(ck, 'cache', 0.0, log=logp, ledger=ledger)
        return old['text']
    nb = notebook if notebook is not None else (None if injected else _notebook)
    t0 = time.monotonic()
    if nb:
        if nb is _notebook:                                     # 17/11 up to 45s: one short line first (agent/interim)
            try:
                from agent import interim, nlm_ask
                if nlm_ask.has_notebook(ck):
                    interim.say(interim.COURSE)
            except Exception as e:
                print(f'  ⚠️ interim: {e!r}'[:160])
        try:
            got = nb(question, ck)
        except Exception as e:
            print(f'  ⚠️ notebook first ({ck}): {e!r}'[:200])
            got = None
        if got and (got.get('text') or '').strip():
            log_path(ck, 'notebook', time.monotonic() - t0, SAVED_PER_LOOKUP, log=logp, ledger=ledger)
            return got['text']
    text = _gemini_find(question, ck, topic, ask, docs, path, now, m, key, timely)
    log_path(ck, 'gemini' if text else 'none', time.monotonic() - t0, log=logp, ledger=ledger)
    return text


def _gemini_find(question, ck, topic, ask, docs, path, now, m, key, timely) -> str | None:
    from config.paths import COURSE_NAMES
    where = m.get('where') or {}
    remembered = list(dict.fromkeys(((where.get(ck) or {}).get(topic) or [])
                                    + [s for t in (where.get(ck) or {}).values() for s in t]))
    picked, size = [], 0
    for d in rank(docs if docs is not None else documents(ck), question, remembered):
        if size + len(d[2]) > MAX_CHARS:
            continue
        picked.append(d)
        size += len(d[2])
    if not picked:
        return None
    groups = 'לא ידוע'
    try:
        from integrations import tau_portal
        from agent import timetable
        name = COURSE_NAMES.get(ck, ck)
        groups = ', '.join(x for x in timetable.staff(tau_portal.schedule(live=False)[0] or [])
                           if any(w in x for w in name.split()[:1])) or groups
    except Exception:
        pass
    body = '\n\n'.join(f'=== id: {i} | {t} ===\n{x}' for i, t, x in picked)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from collectors.course_files import SEMESTER_START
    day = datetime.fromtimestamp(now, ZoneInfo('Asia/Jerusalem')).date()
    try:
        r = (ask or _ask)(PROMPT.format(course=COURSE_NAMES.get(ck, ck), question=question, groups=groups, docs=body,
                                        today=f'{day:%d/%m/%Y}', week=(day - SEMESTER_START).days // 7 + 1))
    except Exception as e:
        print(f'  ⚠️ course lookup failed: {e!r}'[:200])
        return None
    if not r.get('found') or not (r.get('answer') or '').strip():
        return None
    titles = {i: t for i, t, _ in picked}
    src = r.get('source') if r.get('source') in titles else ''
    text = r['answer'].strip() + (f'\n\n📚 מקור: {titles[src]}' if src else '')
    if src:                                               # next time: straight to where it was found
        lst = m.setdefault('where', {}).setdefault(ck, {}).setdefault(topic or 'כללי', [])
        if src in lst:
            lst.remove(src)
        lst.insert(0, src)
        del lst[5:]
    if not timely:
        m.setdefault('answers', {})[key] = {'text': text, 'at': now, 'source': src}
    m['answers'] = dict(sorted((m.get('answers') or {}).items(), key=lambda kv: kv[1]['at'])[-200:])
    _save(m, path)
    return text
