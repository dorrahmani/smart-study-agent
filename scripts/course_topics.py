"""
scripts/course_topics.py — the course as a path of topics, not of lectures (05/11).

The owner: "שזה יהיה תהליך אמיתי של ללמוד נושא. לא בהכרח לפי הסדר שהמרצה לימד ... קצר יותר, ממוקד. אם אני לא מגיע להרצאה אני
בדרך כלל לא עושה כלום חוץ מהסיכום." So the main route of a course is no longer a lecture: it is a topic that takes 10-15
minutes and replaces the lecture.

    the map     data/site_topics/<ck>/map.json. Gemini sorts every section of every summary into topics, says what
                depends on what and which exam topics each one is. Later builds send only the sections it hasn't seen:
                a new lecture joins existing topics or opens a new one, and nothing else is touched. Topic ids stay.
    the order   dependencies first, then what the exams ask most (order()); never the date.
    a topic     data/site_topics/<ck>/topics/<id>.json, one file with the hash of its sources. Written from scratch from
                those sources only, then read twice by a separate reading: a fact check (fact_check → apply_check) that
                drops what the sources don't carry, and build_course_site.checked_quiz for the quiz.
    failure     a topic that fails keeps its previous text; one that never had any is a page of links to its sources.
                What was written stays written, so a build that dies goes on from there. No LLM or no map → the old
                guide is the main route again, as if this file weren't here.

LLM runs only with an `ask` (the nightly build, or `build_course_site.py --topics`), at most NIGHT_CAP topics per course
per night. A rebuild after a homework is marked reads the cache and costs nothing.
Progress is the site's own: a finished topic is a key in ssa-done, so the rings and the "continue" of the hubs work on it.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
TOPICS_DIR = ROOT / 'data' / 'site_topics'
MAP_VERSION = '1'
TOPIC_VERSION = '1'
TOPIC_CHARS = 15000                 # a topic's sources for the model; the rest of the pack is capped below
NIGHT_CAP = 8                       # new topics per course per night; the first full run is by hand (--topics)
MAX_TRIES = 3                       # a topic the model can't get right stops costing money until its sources change
MIN_QUIZ = 3
MAX_TEXT = 10000                    # "קצר יותר": a page that long is a summary again
TEXT_BLOCKS = ('idea', 'why', 'intuition', 'explain', 'trap')

MAP_PROMPT = """אתה מתכנן קורס לימוד עצמי ל"{course}", לסטודנט שלא מגיע להרצאות ולומד רק מהאתר.
לפניך קטעי החומר שנלמדו עד עכשיו, מכל ההרצאות והתרגולים. בנה מהם מפת נושאים. נושא הוא לא הרצאה: נושא הוא רעיון אחד שלומדים
ב-10-15 דקות, וקטעים מהרצאות ומתרגולים שונים על אותו רעיון נכנסים לאותו נושא.
כללים:
- כל נושא: שם קצר וברור (2-6 מילים), one_line של משפט אחד שאומר מה לומדים בו, ובדרך כלל 1-6 קטעים. חומר גדול מדי מתפצל לשני נושאים.
- after: הנושאים שחייבים לדעת לפני הנושא הזה (ref שלהם). רק תלות אמיתית, לא "נלמד קודם".
- exam_topics: מהרשימה למטה בלבד, אילו נושאי מבחן שייכים לנושא. importance: 3 ליבה של הקורס, 2 חשוב, 1 שולי.
- סדר הנושאים הוא סדר הלמידה הכי טוב: קודם מה שהשאר נשענים עליו, ובין נושאים שווים, מה שחשוב יותר למבחן. לא לפי תאריך.
- skip: קטעי מנהלות בלבד (ציונים, דרישות הקורס, היכרות, הודעות).
- out_of_scope: רק אם המקורות אומרים במפורש שמשהו לא למבחן או ירד מהחומר: שם הנושא, למה, וציטוט מדויק מהקטע (quote). אחרת רשימה ריקה.
- כל מספר קטע מופיע פעם אחת בלבד, בנושא או ב-skip.
{existing}
קטעי החומר (מספר · סוג · שיעור · כותרת: התחלת הקטע):
{items}

מבנה הקורס לפי הסילבוס והמצגות:
{plan}

נושאי המבחן ואחוז המבחנים הקודמים שבהם הופיעו:
{exam}

החזר JSON בלבד:
{{"topics": [{{"ref": "N1", "title": "...", "one_line": "...", "items": [1, 4], "after": [], "exam_topics": [], "importance": 2}}],
 "skip": [3], "out_of_scope": [{{"title": "...", "why": "...", "quote": "..."}}]}}"""
MAP_EXISTING = """
כבר יש מפה, והקטעים למטה הם רק החדשים. נושא קיים לא משנים ולא מוחקים:
{rows}
- קטע ששייך לנושא קיים: החזר את הנושא עם אותו ref ועם המספרים של הקטעים החדשים בלבד.
- רעיון חדש באמת: נושא חדש עם ref מהצורה N1, N2. אפשר after לנושאים קיימים.
"""
TOPIC_PROMPT = """אתה המורה הכי טוב שיש ל"{course}". הסטודנט לא היה בהרצאה, והדף הזה מחליף אותה. נושא הדף: "{title}" ({one_line}).
{before}
כתוב את הדף מאפס, כמו שמסבירים לחבר חכם שרואה את הנושא בפעם הראשונה ורוצה להבין ולהצליח במבחן. עברית פשוטה ומדוברת, משפטים
קצרים, פונה אליו ישירות. מונח מקצועי מוסבר במילים פשוטות בפעם הראשונה שהוא מופיע, ומילים גבוהות (כמו "אורתוגונלי") לא נכנסות
כשיש מילה רגילה. אל תעתיק את הסיכום ואל תלך לפי סדר ההרצאה: סדר הכול כך שיהיה הכי קל להבין. קצר וממוקד: רק מה שצריך, בלי
הקדמות ובלי חזרות. אל תחזור על הגדרות מנושאים שהוא כבר למד, רק הפנה אליהן במשפט.

דיוק: כל הגדרה, משפט, נוסחה, תנאי ועובדה חייבים לבוא מהמקורות למטה. אל תוסיף מהידע הכללי שלך. מותר לך להמציא אנלוגיה, ומותר
להמציא מספרים לדוגמה אם החישוב נכון ומשתמש בכלי מהמקורות. המקורות לפניך הם קטעים, וקטע שנגמר באמצע משפט הוא לא תקלה: אל תציין
שמשהו נקטע או חסר, פשוט למד מה שיש.

החזר JSON בלבד:
{{"idea": "הרעיון האחד, במשפט או שניים. אם יש רק דבר אחד לזכור, זה הוא",
 "why": "משפט או שניים: למה זה חשוב, מה זה נותן, ואיך זה נראה במבחן אם המקורות אומרים. בלי אחוזים ובלי ספירת מבחנים",
 "intuition": "אינטואיציה או אנלוגיה מהחיים, עד 4 משפטים",
 "explain": "ההסבר עצמו, 200-450 מילים, Markdown, בנוי כך שאפשר ללמוד ממנו בלי ההרצאה: פותח ממקרה קטן ומוחשי, ורק אחריו הכלל. הגדרה או משפט בפורמט > **הגדרה (שם):** ... או > **משפט (שם):** ... ומיד אחריהם משפט 'במילים פשוטות'. שיטה בצעדים ממוספרים שאפשר לעקוב אחריהם. אם המקורות מספקים קוד, קטע קוד קצר בתוך ``` עם שם השפה",
 "example": {{"problem": "תרגיל אחד אופייני. אם יש במקורות שאלת מבחן על הנושא, בסס את התרגיל עליה", "solution": "פתרון מלא שלב אחר שלב, עד 200 מילים, שבכל שלב אומר גם למה עושים אותו, עם התשובה הסופית בסוף"}},
 "trap": "הטעות הנפוצה בנושא: מה טועים, למה זה נראה נכון, ואיך לזהות ולהימנע. 2-3 משפטים",
 "card": [{{"name": "שם הנוסחה או הכלי", "text": "הנוסחה ומתי משתמשים בה"}}],
 "skipped": "משפט אחד: איזה חלק של הנושא הזה עצמו השארת בחוץ בכוונה (למשל הוכחה ארוכה), כדי שידע שקיים. בלי להזכיר חומר של נושאים אחרים, ובלי להזכיר תמלול, מקורות או שאלות שקיבלת. ריק אם כלום",
 "quiz": [{{"q": "שאלה", "options": ["א", "ב", "ג", "ד"], "answer": 0, "why": "למה זו התשובה, ומה הטעות בשאר"}}]}}
כללים:
- card: עד 6 שורות, רק נוסחאות או תחביר ששווה לזכור, מהנושא הזה בלבד. ריק אם אין. כל נוסחה בין $...$ וכל קוד בין `...` גם כאן.
- quiz: בדיוק 5 שאלות שבודקות הבנה ולא שינון, מהקל לקשה. answer הוא האינדקס (0-3) והמיקום שלו משתנה בין השאלות.
- אל תזכיר הרצאה, מרצה, מתרגל, שיעור או הקלטה. אל תכתוב "כפי שראינו". הערה שהמקורות אומרים שחשובה למבחן מותר להשאיר.
- בלי אימוג'י ובלי כותרות # בתוך הטקסט.
- נוסחאות ב-LaTeX בין $...$, וב-JSON כל backslash כפול (\\\\frac). קוד של שורה בתוך `...`, וקוד של כמה שורות תמיד בתוך ``` עם שם השפה, גם בתרגיל ובשאלות הבדיקה. הזחה ברווחים.
{notes}
המקורות:
{pack}"""
FACT_PROMPT = """אתה בודק עובדות קשוח. לפניך מקורות של קורס ודף לימוד שנכתב מהם. מצא כל טענה בדף שהמקורות סותרים, וכל טענה
עובדתית (הגדרה, משפט, נוסחה, תנאי, תוצאת חישוב) שאין לה תמיכה במקורות. חשב בעצמך כל שלב בדוגמה הפתורה.
אל תדווח על: ניסוח אחר, אנלוגיה, מספרים שהומצאו לדוגמה כשהחישוב נכון ולפי כלי מהמקורות.
החזר JSON בלבד: {{"issues": [{{"quote": "ציטוט מדויק מהדף, 4-15 מילים", "kind": "contradicts או unsupported", "why": "קצר"}}]}}
אם הכול תקין: {{"issues": []}}

מקורות:
{pack}

הדף:
{page}"""


def _site():
    from scripts import build_course_site as s        # the pages' HTML lives there; it imports this file lazily too
    return s


def _plain(s: str) -> str:
    return re.sub(r'[\s*`_]+', ' ', str(s or '')).strip()


def _sha(*parts) -> str:
    return hashlib.sha1('|'.join(map(str, parts)).encode('utf-8')).hexdigest()


def topic_id(ck: str, title: str) -> str:
    return 't-' + _sha(ck, title)[:8]


# ── the sections of the course ─────────────────────────────────────────────────────────────────────────────

def sections_of(ls: list[dict], raw: list[list]) -> list[dict]:
    """Every "topic" section of every summary. The key doesn't hold its position, so a section added to a summary moves
    nothing. `legacy` is that section's key in the old guide: a page read there still counts (keys_rows)."""
    s, out = _site(), []
    for li, (r, row) in enumerate(zip(ls, raw)):
        seen: dict[str, int] = {}
        ours = [x for x in row if x[2] == 'topic']
        stamps = [(re.search(r'<!--ts:([\d:]+)-->', md) or [None, ''])[1] for _, md, _, _ in ours]
        for i, (t, md, _, ptrs) in enumerate(ours):
            seen[t] = seen.get(t, 0) + 1
            out.append({'key': f'{r["md"].name}#{t}' + (f'#{seen[t]}' if seen[t] > 1 else ''), 'title': t, 'md': md,
                        'lesson': r, 'ptrs': ptrs, 'ts': stamps[i], 'ts_end': stamps[i + 1] if i + 1 < len(ours) else '',
                        'legacy': hashlib.sha1(f'{r["md"].name}|{s.nice_title(t)}'.encode('utf-8')).hexdigest()[:10]})
    return out


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _save(p: Path, data) -> None:
    """Whole or not at all: an interrupted build never leaves half a file."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f'{p.name}.{threading.get_ident()}.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(p)


def load_map(ck: str, cache: Path | None = None) -> dict:
    m = _load((cache or TOPICS_DIR) / ck / 'map.json')
    return m if isinstance(m, dict) and m.get('v') == MAP_VERSION and m.get('topics') else {}


# ── the map ────────────────────────────────────────────────────────────────────────────────────────────────

def _gist(md: str) -> str:
    t = _site().clean_md(md)
    t = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', t)
    return re.sub(r'\s+', ' ', re.sub(r'[#>*|`]', ' ', t)).strip()[:110]


def _new_topic(ck: str, title: str, one_line: str = '', importance: int = 2) -> dict:
    return {'id': topic_id(ck, title), 'title': title, 'one_line': one_line, 'keys': [], 'requires': [],
            'exam_topics': [], 'importance': importance if importance in (1, 2, 3) else 2}


def _home_of(sec: dict, topics: list[dict]) -> dict | None:
    """Where a section the model left out belongs: the topic that shares most of its words, 2 at least."""
    hits = _site()._hits
    best = max(topics, key=lambda t: hits(t['title'], sec['title']), default=None)
    return best if best and hits(best['title'], sec['title']) >= 2 else None


def _merge(m: dict, plan: dict, new: list[dict], ck: str, stats: list[dict]) -> dict:
    """The model's answer on top of the map. Raises when it lost too much: the map stays as it was."""
    m = copy.deepcopy(m)
    names = {r['topic'] for r in stats}
    refs = {f'T{i}': t for i, t in enumerate(m['topics'], 1)}
    items = dict(enumerate(new, 1))
    placed: set[int] = set()
    rows = [r for r in plan.get('topics') or [] if isinstance(r, dict)]
    for row in rows:
        ref = str(row.get('ref') or '').strip()
        nums = [n for n in row.get('items') or [] if isinstance(n, int) and n in items and n not in placed]
        t = refs.get(ref)
        if t is None:
            title = re.sub(r'\s+', ' ', str(row.get('title') or '')).strip()[:70]
            if not nums or not title:
                continue
            t = _new_topic(ck, title, str(row.get('one_line') or '').strip()[:200], row.get('importance'))
            while any(x['id'] == t['id'] for x in m['topics']):
                t['id'] += 'x'
            m['topics'].append(t)
            refs[ref or t['id']] = t
        placed.update(nums)
        t['keys'] += [items[n]['key'] for n in nums]
        t['exam_topics'] = list(dict.fromkeys(t['exam_topics'] + [x for x in row.get('exam_topics') or [] if x in names]))
    for row in rows:
        t = refs.get(str(row.get('ref') or '').strip())
        for a in row.get('after') or []:
            dep = refs.get(str(a).strip())
            if t and dep and dep is not t and dep['id'] not in t['requires']:
                t['requires'].append(dep['id'])
    skip = [n for n in plan.get('skip') or [] if isinstance(n, int) and n in items and n not in placed]
    if len(placed) + len(skip) < 0.8 * len(items) or len(skip) > 0.35 * len(items) or not placed and not skip:
        raise ValueError(f'map answer covers {len(placed)}+{len(skip)} of {len(items)}')
    m['skip'] = m.get('skip', []) + [items[n]['key'] for n in skip]
    for n, sec in items.items():                 # what it forgot isn't lost and doesn't cost another call tomorrow
        if n not in placed and n not in skip:
            t = _home_of(sec, m['topics'])
            if t is None:
                t = _new_topic(ck, _site().nice_title(sec['title'])[:70])
                m['topics'].append(t)
            t['keys'].append(sec['key'])
    text = ' '.join(_plain(s['md']) for s in new)
    for o in plan.get('out_of_scope') or []:     # only what really stands in the sources, word for word
        if isinstance(o, dict) and len(_plain(o.get('quote'))) > 8 and _plain(o.get('quote')) in text:
            m.setdefault('out', []).append({'title': str(o.get('title') or '')[:80], 'why': str(o.get('why') or '')[:200]})
    return m


def update_map(ck: str, name: str, secs: list[dict], ctx: dict, ask=None, cache: Path | None = None) -> dict:
    """The saved map, with the sections it hasn't seen placed by one model call. Without `ask` it only drops what's gone."""
    d = (cache or TOPICS_DIR) / ck
    m = load_map(ck, cache) or {'v': MAP_VERSION, 'topics': [], 'skip': [], 'out': []}
    now = {s['key'] for s in secs}
    had = {k for t in m['topics'] for k in t['keys']}
    if had and len(had & now) < 0.5 * len(had):  # the summaries didn't vanish, they're just not here (a half-synced disk)
        print(f'  ⚠️ site topics map {ck}: {len(had & now)}/{len(had)} sections found, the map stays as it is')
        return m
    changed = False
    for t in m['topics']:
        keep = [k for k in t['keys'] if k in now]
        changed |= keep != t['keys']
        t['keys'] = keep
    m['skip'] = [k for k in m['skip'] if k in now]
    known = {k for t in m['topics'] for k in t['keys']} | set(m['skip'])
    new = [s for s in secs if s['key'] not in known]
    if new and ask:
        rows = '\n'.join(f'T{i} · {t["title"]}: {t["one_line"]}' for i, t in enumerate(m['topics'], 1))
        items = '\n'.join(f'{i} · {s["lesson"]["kind"]} · {s["lesson"]["title"][:40]} · {s["title"]}: {_gist(s["md"])}'
                          for i, s in enumerate(new, 1))
        exam = '\n'.join(f'{r["topic"]} ({r["exam_pct"]}%)' for r in ctx.get('stats') or []) or '(אין עדיין מאגר מבחנים לקורס)'
        try:
            raw = ask(MAP_PROMPT.format(course=name, existing=MAP_EXISTING.format(rows=rows) if rows else '', items=items,
                                        plan=ctx.get('plan') or '(אין)', exam=exam))
            m = _merge(m, _site()._stage_json(raw), new, ck, ctx.get('stats') or [])
            changed = True
        except Exception as e:
            print(f'  ⚠️ site topics map {ck}: {e!r}'[:200])
    gone = [t for t in m['topics'] if not t['keys']]
    if gone:                                     # the summary was replaced and nothing took its place
        m['retired'] = (m.get('retired') or []) + [t['id'] for t in gone]
        m['topics'] = [t for t in m['topics'] if t['keys']]
    if (changed or gone) and m['topics']:
        _save(d / 'map.json', m)
    return m


def weight(t: dict, stats: list[dict]) -> int | None:
    pcts = [r['exam_pct'] for r in stats if r['topic'] in t.get('exam_topics', [])]
    return max(pcts) if pcts else None


def order(topics: list[dict], stats: list[dict]) -> list[dict]:
    """Dependencies first, then what the exams ask, then the model's order. A foundation is as important as the most
    important topic resting on it. A cycle (the model's mistake) can't stop the path: it breaks at the first topic."""
    ids = {t['id']: t for t in topics}
    band = {}
    for t in topics:
        w = weight(t, stats)
        band[t['id']] = (2 if w >= 60 else 1 if w >= 30 else 0) if w is not None else t.get('importance', 2) - 1
    for _ in topics:
        for t in topics:
            for r in t['requires']:
                if r in band:
                    band[r] = max(band[r], band[t['id']])
    left, out = list(topics), []
    while left:
        ready = [t for t in left if not any(r in ids and ids[r] in left for r in t['requires'])] or left[:1]
        pick = min(ready, key=lambda t: (-band[t['id']], topics.index(t)))
        out.append(pick)
        left.remove(pick)
    return out


# ── what a topic is written from ───────────────────────────────────────────────────────────────────────────

def _seconds(ts: str) -> int:
    n = 0
    for p in ts.split(':'):
        n = n * 60 + int(p or 0)
    return n


def transcript_slice(sec: dict, ck: str, max_chars: int, panopto: Path | None = None) -> str:
    """What was said during this section, by the timestamps of the summary; nothing when there's no good transcript."""
    r = sec['lesson']
    p = (panopto or ROOT / 'data' / 'panopto') / f'{ck}_{r["date"]}_{r["title"]}.srt'
    if not sec.get('ts') or not p.exists():
        return ''
    try:
        from utils.srt_quality import srt_is_good_hebrew
        if not srt_is_good_hebrew(p):            # Panopto's Cyrillic bug
            return ''
        raw = p.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return ''
    a, b = _seconds(sec['ts']), _seconds(sec['ts_end']) if sec.get('ts_end') else 10 ** 7
    out, prev = [], None
    for m in re.finditer(r'(\d\d):(\d\d):(\d\d)[,.]\d+\s*-->[^\n]*\n(.+?)(?:\n\s*\n|\Z)', raw, re.S):
        at = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        line = ' '.join(m.group(4).split())
        if a <= at < b and line != prev:
            out.append(line)
            prev = line
    return ' '.join(out)[:max_chars]


def _related(text: str, title: str, limit: int) -> str:
    """The lines of a lesson's tips / cheat sheet that talk about this topic."""
    s = _site()
    out, n = [], 0
    for ln in text.split('\n'):
        if len(ln.strip()) > 20 and s._hits(title, ln):
            out.append(ln.strip())
            n += len(ln)
            if n >= limit:
                break
    return '\n'.join(out)[:limit]


def exam_samples(t: dict, ctx: dict, n: int = 2) -> str:
    """Up to n real exam questions of this topic with their solutions, newest first."""
    s, out, seen = _site(), [], set()
    vdir = ctx.get('vdir')
    for name in t.get('exam_topics') or []:
        for ref in (ctx.get('refs') or {}).get(name, []):
            if len(out) >= n or not vdir or (ref['slug'], ref['n']) in seen:
                continue
            seen.add((ref['slug'], ref['n']))
            day = ref['slug'][5:15]
            try:
                qs = (vdir / f'{day} שאלות.md').read_text(encoding='utf-8')
            except OSError:
                continue
            try:
                sol = (vdir / f'{day} פתרון.md').read_text(encoding='utf-8')
            except OSError:
                sol = ''
            for no, q, a in s.exam_pairs(qs, sol):
                if no == ref['n']:
                    out.append(f'### שאלת מבחן {ref["label"]}\n{q[:900]}\nפתרון:\n{a[:900]}')
    return '\n\n'.join(out)


def fair_shares(sizes: list[int], total: int) -> list[int]:
    """How much of each section fits in `total`: short ones whole, what they leave goes to the long ones. A flat share cut
    a four-part section in the middle, and the page said part of the subject was "missing from the sources"."""
    out, left = list(sizes), total
    for done, i in enumerate(sorted(range(len(sizes)), key=lambda k: sizes[k])):
        out[i] = min(sizes[i], left // (len(sizes) - done))
        left -= out[i]
    return out


def source_pack(t: dict, by_key: dict, ctx: dict, ck: str = '') -> tuple[str, str]:
    """(the topic's sources, their hash). The hash is over what the topic is ABOUT: the sections and the slides they point
    at. A new past exam, or a change in the tips of a lesson, doesn't rewrite a topic."""
    s = _site()
    secs = [by_key[k] for k in t['keys'] if k in by_key]
    bodies = [s.clean_md(x['md']) for x in secs]
    core, seen = [], set()
    for x, body, share in zip(secs, bodies, fair_shares([len(b) for b in bodies], TOPIC_CHARS)):
        r = x['lesson']
        core.append(f'### {r["kind"]}: {s.nice_title(x["title"])}\n{body[:share]}')
        for f, a, b in x['ptrs'][:2]:
            pdf = (ctx.get('files') or {}).get(f)
            if pdf and (f, a) not in seen:
                seen.add((f, a))
                pages = s.file_pages(pdf, a, b)[:1500]
                if pages:
                    core.append(f'### שקפים / חומר המרצה: {f}\n{pages}')
    text = '\n\n'.join(core)[:TOPIC_CHARS + 3000]
    h = _sha(TOPIC_VERSION, t['title'], re.sub(r'\s+', ' ', text))[:16]
    more = []
    for r in {id(x['lesson']): x['lesson'] for x in secs}.values():
        tips = _related(r.get('extra') or '', t['title'], 700)
        if tips:
            more.append(f'### טיפים ודף עזר\n{tips}')
    said = ' '.join(filter(None, (transcript_slice(x, ck, 700, ctx.get('panopto')) for x in secs)))[:1800]
    if said:
        more.append(f'### מתוך התמלול\n{said}')
    ex = exam_samples(t, ctx)
    if ex:
        more.append(ex)
    return '\n\n'.join([text] + more), h


# ── writing a topic, and reading it twice ──────────────────────────────────────────────────────────────────

_MATH = re.compile(r'\$\$.+?\$\$|\$[^$\n]+\$|`[^`\n]+`', re.S)


def _sentences(line: str) -> list[str]:
    """A line → its sentences; a dot inside a formula or code doesn't end one."""
    mask = _MATH.sub(lambda m: 'x' * len(m.group(0)), line)
    cuts = [m.end() for m in re.finditer(r'[.!?](?=\s)', mask)]
    return [p for p in (line[a:b] for a, b in zip([0] + cuts, cuts + [len(line)])) if p.strip()]


def drop_sentence(text: str, quote_: str) -> str | None:
    """text without the sentence that holds the quote (the whole line when the quote crosses sentences); None: not there."""
    q = _plain(quote_)
    lines = text.split('\n')
    for i, ln in enumerate(lines):
        if q not in _plain(ln):
            continue
        ss = _sentences(ln)
        keep = [x for x in ss if q not in _plain(x)]
        lines[i] = ''.join(keep).strip() if len(keep) < len(ss) else ''
        return '\n'.join(lines).strip()
    return None


_LOCKED = re.compile(r'```.*?```|`[^`\n]+`|\$\$.+?\$\$|\$[^$\n]+\$', re.S)
_BARE_TEX = re.compile(r'[^\u0590-\u05ff\x00\n]*\\[a-zA-Z]{2,}[^\u0590-\u05ff\x00\n]*')


def _tidy(x):
    """What the model gets wrong in its JSON, fixed in every string: "\\\\binom" written once too often, a formula left
    without $, and a <T> outside code, which the browser would swallow as a tag."""
    if isinstance(x, dict):
        return {k: _tidy(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_tidy(v) for v in x]
    if not isinstance(x, str):
        return x
    keep: list[str] = []

    def lock(m):
        keep.append(m.group(0))
        return f'\x00{len(keep) - 1}\x00'

    def wrap(m):
        g = m.group(0)
        core = g.strip(' ,.;:')
        a = g.index(core)
        return f'{g[:a]}${core}${g[a + len(core):]}'
    t = _LOCKED.sub(lock, re.sub(r'\\\\(?=[a-zA-Z]{2,})', r'\\', x))
    t = re.sub(r'<(?=[a-zA-Z/])', '&lt;', _BARE_TEX.sub(wrap, t))
    return re.sub(r'\x00(\d+)\x00', lambda m: keep[int(m.group(1))], t)


def _bad_math(s: str) -> bool:
    body = re.sub(r'\$\$.+?\$\$', '', s, flags=re.S)
    return (s.count('$$') % 2 == 1 or len(re.findall(r'(?<![\\$])\$(?!\$)', body)) % 2 == 1 or s.count('```') % 2 == 1)


def page_text(d: dict) -> str:
    """What the reader reads, minus the quiz."""
    ex = d.get('example') or {}
    return '\n\n'.join([str(d.get(b) or '') for b in TEXT_BLOCKS[:4]] + [str(ex.get('problem') or ''), str(ex.get('solution') or ''),
                        str(d.get('trap') or '')] + [f'{c.get("name")}: {c.get("text")}' for c in d.get('card') or [] if isinstance(c, dict)])


def topic_ok(d: dict) -> bool:
    s = _site()
    ex = d.get('example') if isinstance(d.get('example'), dict) else {}
    if not all(str(d.get(k) or '').strip() for k in TEXT_BLOCKS) or not (str(ex.get('problem') or '').strip() and str(ex.get('solution') or '').strip()):
        return False
    if not isinstance(d.get('card', []), list) or len(page_text(d)) > MAX_TEXT:
        return False
    qs = d.get('quiz') or []
    if len(qs) < MIN_QUIZ:
        return False
    for q in qs:
        opts = q.get('options') or []
        a = q.get('answer')
        if not (str(q.get('q') or '').strip() and len(opts) >= 3 and isinstance(a, int) and 0 <= a < len(opts)):
            return False
    all_text = [page_text(d)] + [' '.join([str(q['q']), *map(str, q['options']), str(q.get('why') or '')]) for q in qs]
    return not any(s._BAD_ESC.search(t) or _bad_math(t) for t in all_text)


def apply_check(d: dict, issues: list) -> tuple[dict, dict, bool]:
    """The fact check's findings on the page → (page, report, must be rewritten). A false sentence goes, with its row or
    question. A solved example can't lose a sentence and still be solved: a wrong one sends the page back, one only
    unsupported is shown as such. So is a claim the check couldn't quote; a contradiction it can't quote sends it back."""
    d = copy.deepcopy(d)
    rep: dict = {'dropped': [], 'flags': []}
    severe = False
    for it in issues[:20]:
        if not isinstance(it, dict) or not _plain(it.get('quote')):
            continue
        q, contra = str(it['quote']), it.get('kind') == 'contradicts'
        note = {'quote': q[:160], 'why': str(it.get('why') or '')[:160]}
        done = False
        for b in TEXT_BLOCKS:
            new = drop_sentence(str(d.get(b) or ''), q)
            if new is not None:
                d[b] = new
                done = True
                break
        if not done:
            for k, c in enumerate(d.get('card') or []):
                if _plain(q) in _plain(f'{c.get("name")} {c.get("text")}'):
                    d['card'].pop(k)
                    done = True
                    break
        if not done:
            for k, x in enumerate(d.get('quiz') or []):
                if _plain(q) in _plain(' '.join([str(x.get('q')), *map(str, x.get('options') or []), str(x.get('why'))])):
                    d['quiz'].pop(k)
                    done = True
                    break
        if done:
            rep['dropped'].append(note)
        else:
            severe = severe or contra
            rep['flags'].append(note)
    return d, rep, severe or len(rep['dropped']) > 4


def write_topic(t: dict, name: str, pack: str, before: str, ask) -> dict:
    """Write → fact check → quiz by a second reading. Two attempts; the second one hears what was wrong. Raises if neither
    passes, so nothing half-checked is ever saved."""
    s = _site()
    notes, why = '', 'rejected'
    for _ in range(2):
        try:
            d = _tidy(s._stage_json(ask(TOPIC_PROMPT.format(course=name, title=t['title'], one_line=t.get('one_line') or '',
                                                            before=before, notes=notes, pack=pack))))
        except ValueError:
            notes, why = '\nהתשובה הקודמת לא הייתה JSON תקין. החזר JSON תקין ושלם.\n', 'bad json'
            continue
        if not topic_ok(d):
            notes, why = '\nהתשובה הקודמת נפסלה: חסר בה חלק, או נוסחה לא סגורה, או שהיא ארוכה מדי.\n', 'failed the shape check'
            continue
        found = s._stage_json(ask(FACT_PROMPT.format(pack=pack, page=page_text(d) + '\n\n' + json.dumps(
            [{'q': q['q'], 'options': q['options'], 'why': q.get('why')} for q in d['quiz']], ensure_ascii=False)))).get('issues') or []
        d, rep, again = apply_check(d, found)
        if again or not topic_ok(d):
            bad = '; '.join(f'{x["quote"]}: {x["why"]}' for x in rep['dropped'] + rep['flags'])[:1200]
            notes, why = f'\nבדיקת עובדות פסלה את הגרסה הקודמת. אל תחזור על אלה: {bad}\n', 'failed the fact check'
            continue
        d['quiz'] = s.checked_quiz(d['quiz'], pack + '\n\n' + page_text(d), ask)
        if not topic_ok(d):
            notes, why = '\nחלק משאלות הבדיקה נפסלו בקריאה שנייה: כתוב שאלות שאפשר להכריע מהמקורות.\n', 'quiz failed the second reading'
            continue
        d['check'] = rep
        return d
    raise ValueError(why)


def _store(f: Path, rec: dict) -> None:
    """A new version never deletes the old one: it moves aside to old/."""
    old = _load(f)
    if old:
        (f.parent.parent / 'old').mkdir(parents=True, exist_ok=True)
        f.replace(f.parent.parent / 'old' / f'{f.stem}.{old.get("hash", "x")}.json')
    _save(f, rec)


def _exclusive(d: Path):
    """The nightly build and a hand run writing the same topics at once would pay twice. None: someone else is writing."""
    try:
        import fcntl
    except ImportError:
        return open(d / '.lock', 'w')
    fh = open(d / '.lock', 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def refresh(ck: str, name: str, m: dict, by_key: dict, ctx: dict, ask, cache: Path | None = None, cap: int | None = NIGHT_CAP,
            retry: bool = False) -> dict:
    """Write what's missing or out of date, the path's first topics first. Each one is saved as it's done."""
    d = (cache or TOPICS_DIR) / ck
    stats = ctx.get('stats') or []
    path = order(m['topics'], stats)
    fails = _load(d / 'failures.json') or {}
    todo = []
    for i, t in enumerate(path):
        pack, h = source_pack(t, by_key, ctx, ck)
        cur = _load(d / 'topics' / f'{t["id"]}.json')
        if cur and cur.get('hash') == h:
            continue
        f = fails.get(t['id']) or {}
        if f.get('hash') == h and f.get('n', 0) >= MAX_TRIES and not retry:
            continue
        done = ' · '.join(x['title'] for x in path[:i])
        todo.append((t, pack, h, f'הסטודנט כבר למד: {done}.' if done else 'זה הנושא הראשון במסלול.'))
    res = {'written': 0, 'failed': 0, 'waiting': max(0, len(todo) - (cap if cap is not None else len(todo)))}
    todo = todo[:cap] if cap is not None else todo
    if not todo or not ask:
        return res
    d.mkdir(parents=True, exist_ok=True)
    lock = _exclusive(d)
    if lock is None:
        return res
    stop, mu = threading.Event(), threading.Lock()

    def one(job):
        t, pack, h, before = job
        if stop.is_set():
            return
        try:
            data = write_topic(t, name, pack, before, ask)
        except Exception as e:
            if 'budget lock' in repr(e):         # not the topic's fault, and the next one would fail the same way
                stop.set()
                return
            with mu:
                res['failed'] += 1
                n = (fails.get(t['id']) or {}).get('n', 0) if (fails.get(t['id']) or {}).get('hash') == h else 0
                fails[t['id']] = {'hash': h, 'n': n + 1, 'why': repr(e)[:160], 'at': int(time.time())}
            print(f'  ⚠️ site topic {ck} "{t["title"]}": {e!r}'[:200])
            return
        _store(d / 'topics' / f'{t["id"]}.json', {'hash': h, 'at': int(time.time()), 'title': t['title'], 'data': data})
        with mu:
            res['written'] += 1
            fails.pop(t['id'], None)
    try:
        with ThreadPoolExecutor(3) as ex:
            list(ex.map(one, todo))
    finally:
        _save(d / 'failures.json', fails)
        lock.close()
    return res


# ── the pages ──────────────────────────────────────────────────────────────────────────────────────────────

def minutes(d: dict | None) -> int:
    """How long it really takes: reading Hebrew with formulas, trying the example, the check."""
    if not d:
        return 12
    return max(6, min(20, round(len(page_text(d)) / 800 + 3 + len(d.get('quiz') or []))))


def _rec(ck: str, t: dict, cache: Path | None) -> dict | None:
    rec = _load((cache or TOPICS_DIR) / ck / 'topics' / f'{t["id"]}.json')
    return rec if rec and isinstance(rec.get('data'), dict) and topic_ok(rec['data']) else None


def _sources_html(t: dict, by_key: dict, files: dict, used: set) -> str:
    """Every topic says where it came from: the summaries, the moment in the recording, the lecturer's pages."""
    s = _site()
    rows = []
    for k in t['keys']:
        x = by_key.get(k)
        if not x:
            continue
        r = x['lesson']
        video = re.match(r'\s*\[▶️[^\]]*\]\((https?://[^)\s]+)\)', x['md'])
        bits = [f'<a href="../{r["slug"]}">{html.escape(r["kind"])} {r["date"][8:10]}/{r["date"][5:7]}: {html.escape(s.nice_title(x["title"]))}</a>']
        if video:
            bits.append(f'<a href="{html.escape(video.group(1))}">הקלטה</a>')
        for f, a, b in x['ptrs'][:2]:
            pdf = next((p for p in files.values() if p.stem == f), None)
            if pdf:
                used.add(pdf)
                bits.append(f'<a href="../files/{quote(pdf.name)}#page={a}">{html.escape(f)}, עמוד {a}</a>')
        rows.append(f'<li>{" · ".join(bits)}</li>')
    return '<ul>' + ''.join(rows) + '</ul>' if rows else ''


def _exam_chips(t: dict, refs: dict, n: int = 6) -> str:
    seen, out = set(), []
    for name in t.get('exam_topics') or []:
        for r in refs.get(name, []):
            if (r['slug'], r['n']) not in seen and len(out) < n:
                seen.add((r['slug'], r['n']))
                out.append(f'<a class="chip" href="../{r["slug"]}#q{r["n"]}">{html.escape(r["label"])}</a>')
    return ''.join(out)


def topic_body(t: dict, d: dict, stamp: dict) -> str:
    s = _site()
    ex = d['example']
    w3 = lambda md: s.w3_html(s.md_html(str(md)))
    out = [f'<p class="lead">{s._inline(d["idea"])}</p>', f'<h2>למה זה חשוב</h2>{s.md_html(d["why"])}',
           f'<h2>האינטואיציה</h2>{s.md_html(d["intuition"])}', f'<h2>איך זה עובד</h2>{w3(d["explain"])}',
           f'<h2>דוגמה פתורה</h2><div class="w3-example">{s.md_html(ex["problem"])}<p class="mute">נסה כמה דקות לבד, ורק אז פתח.</p>'
           f'<details class="ans"><summary>הצג פתרון</summary>{s.md_html(ex["solution"])}</details></div>',
           f'<h2>המלכודת</h2><blockquote class="exam">{s.md_html(d["trap"])}</blockquote>']
    if d.get('card'):
        rows = ''.join(f'<div><b>{s._inline(c.get("name") or "")}</b><span>{s._inline(c.get("text") or "")}</span></div>'
                       for c in d['card'] if isinstance(c, dict))
        out.append(f'<h2>כרטיס לזכור</h2><div class="tcard">{rows}</div>')
    out.append(f'<h2>בדיקה</h2>{s.quiz_html({"quiz": d["quiz"]})}')
    if stamp.get('exams'):
        out.append(f'<h3>עכשיו שאלה אמיתית ממבחן</h3><div class="chips">{stamp["exams"]}</div>')
    tail = []
    if str(d.get('skipped') or '').strip():
        tail.append(f'<p><strong>מה נשאר בחוץ:</strong> {s._inline(d["skipped"])}</p>')
    rep = d.get('check') or {}
    if rep.get('flags'):
        tail.append('<p><strong>לא אומת במקורות, להתייחס בזהירות:</strong></p><ul>'
                    + ''.join(f'<li>{html.escape(x["quote"])}</li>' for x in rep['flags']) + '</ul>')
    n = len(rep.get('dropped') or [])
    tail.append(f'<p class="mute">כתוב מחדש ממקורות הקורס ונבדק מולם' + (f'. הוסרו {n} טענות שלא נתמכו בהם.' if n > 1 else
                '. הוסרה טענה אחת שלא נתמכה בהם.' if n == 1 else '.') + '</p>')
    if stamp.get('sources'):
        tail.append(f'<details><summary>המקורות של הנושא</summary>{stamp["sources"]}</details>')
    return ''.join(out) + '<hr>' + ''.join(tail)


def pending_body(t: dict, stamp: dict) -> str:
    """Not written yet, or dropped by the checks: not a broken page, the way to learn it meanwhile."""
    line = f'<p class="lead">{html.escape(t.get("one_line") or "")}</p>' if t.get('one_line') else ''
    return (f'{line}<blockquote><p>הדף של הנושא הזה עוד בכתיבה. בינתיים, החומר עצמו נמצא במקורות שלמטה.</p></blockquote>'
            f'<h2>המקורות של הנושא</h2>{stamp.get("sources") or ""}'
            + (f'<h3>שאלות ממבחנים</h3><div class="chips">{stamp["exams"]}</div>' if stamp.get('exams') else ''))


def _key(t: dict) -> str:
    return f'tp-{t["id"]}'


def _side(path: list[dict], cur: str | None) -> str:
    s = _site()
    rows = ''.join(f'<a href="{t["id"]}.html" data-k="{_key(t)}"{" class=on" if t["id"] == cur else ""}>{html.escape(t["title"])}</a>' for t in path)
    return (f'<nav class="side" aria-label="מסלול הלימוד"><div class="prog" data-unit="נושאים"><span></span><b><i></i></b></div>'
            f'<a class="home{" on" if cur is None else ""}" href="./">{s.icon("book")}מסלול הלימוד</a><div class="stops">{rows}</div></nav>')


def keys_rows(path: list[dict], by_key: dict, recs: dict, stats: list[dict]) -> list[list]:
    """[key, href, the old guide's keys, title, minutes, written?, weight, written at] — the whole path for the browser."""
    rows = []
    for t in path:
        rec = recs.get(t['id'])
        legacy = list(dict.fromkeys(by_key[k]['legacy'] for k in t['keys'] if k in by_key))
        rows.append([_key(t), f'{t["id"]}.html', legacy, t['title'], minutes(rec['data'] if rec else None), bool(rec),
                     weight(t, stats), (rec or {}).get('at', 0)])
    return rows


def render(ck: str, name: str, m: dict, by_key: dict, ctx: dict, dest: Path, have: dict, road: dict | None = None,
           cache: Path | None = None, used: set | None = None) -> int:
    """topics/index.html (the path) + topics/<id>.html, one page per topic. A page's address doesn't depend on its place."""
    s = _site()
    d = dest / 'topics'
    d.mkdir(parents=True, exist_ok=True)
    stats, refs, files = ctx.get('stats') or [], ctx.get('refs') or {}, ctx.get('files') or {}
    used = used if used is not None else set()
    path = order(m['topics'], stats)
    recs = {t['id']: _rec(ck, t, cache) for t in path}
    titles = {t['id']: t for t in path}
    top = s.topbar(s._bar2('topics', have), name, '../', menu=True)
    rows = keys_rows(path, by_key, recs, stats)
    index = []
    for i, t in enumerate(path):
        rec = recs[t['id']]
        stamp = {'sources': _sources_html(t, by_key, files, used), 'exams': _exam_chips(t, refs)}
        body = topic_body(t, rec['data'], stamp) if rec else pending_body(t, stamp)
        w = weight(t, stats)
        need = [titles[r] for r in t['requires'] if r in titles]
        meta = ' · '.join([f'נושא {i + 1} מתוך {len(path)}', f'{minutes(rec["data"] if rec else None)} דקות']
                          + ([f'הופיע ב-{w}% מהמבחנים'] if w else []))
        lean = ('<p class="mute needs">נשען על: ' + ' · '.join(f'<a href="{x["id"]}.html">{html.escape(x["title"])}</a>' for x in need)
                + '</p>') if need else ''
        prev = f'<a class="btn prev" href="{path[i - 1]["id"]}.html">❯ הקודם</a>' if i else '<a class="btn prev" href="./">❯ המסלול</a>'
        nxt = f'<a class="btn next" href="{path[i + 1]["id"]}.html">הבא ❮</a>' if i + 1 < len(path) else ''
        quiz = ' data-quiz=1' if rec else ''
        page = (f'<div class="readbar"><i></i></div><div class="shell">{_side(path, t["id"])}<article class="content"><p class="crumb">{meta}</p>'
                f'<h1 class="title">{html.escape(t["title"])}</h1>{lean}<div class="prose">{body}</div></article><aside class="toc"></aside></div>'
                f'<div class="pager dock">{prev}<span class="pos">{i + 1}/{len(path)}</span><button class="btn mark" data-k="{_key(t)}"{quiz}>'
                f'{s.icon("check")}<span>סמן שסיימתי</span></button>{nxt}</div>')
        (d / f'{t["id"]}.html').write_text(s.page(f'{t["title"]} — {name}', page, top=top, bare=True, course=ck), encoding='utf-8')
        text = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'<(script|style)\b.*?</\1>', '', body, flags=re.S))))
        index.append({'t': t['title'].replace('$', ''), 'u': f'{t["id"]}.html', 'x': text[:6000]})
    (d / 'search.json').write_text(json.dumps(index, ensure_ascii=False), encoding='utf-8')
    (d / 'keys.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    items = []
    for i, (t, r) in enumerate(zip(path, rows), 1):
        w = f' · {r[6]}% מהמבחנים' if r[6] else ''
        wait = '' if r[5] else ' · בכתיבה'
        items.append(f'<a class="tp" href="{t["id"]}.html" data-k="{_key(t)}" data-at="{r[7] * 1000}"><i>{i}</i><span><b>{html.escape(t["title"])}</b>'
                     f'<small>{html.escape(t.get("one_line") or "")}</small></span><em>{r[4]} דק׳{w}{wait}</em></a>')
    total = sum(r[4] for r in rows)
    long = f'כ-{round(total / 60, 1):g} שעות' if total >= 60 else f'{total} דקות'
    out = list(m.get('out') or [])
    out_html = ''
    if out or m.get('skip'):
        rows_html = ''.join(f'<li><b>{html.escape(o["title"])}</b>: {html.escape(o["why"])}</li>' for o in out)
        if m.get('skip'):
            rows_html += f'<li>{len(m["skip"])} קטעים של מנהלות הקורס (ציונים, דרישות, היכרות).</li>'
        out_html = f'<details class="outs"><summary>מה לא במסלול, ולמה</summary><ul>{rows_html}</ul></details>'
    body = (f'<div class="shell tpshell">{_side(path, None)}<div class="tpmain"><section class="hero" data-keys="{ck}" data-track="topics">'
            f'<p class="eyebrow">{len(path)} נושאים · {long} · <span class="pct"></span></p><h1>מסלול הלימוד</h1>'
            '<p class="intro">נושא אחרי נושא, בסדר שהכי נכון ללמוד ולא בסדר ההרצאות. כל נושא הוא רעיון אחד, 10-15 דקות, ומסתיים בבדיקה.</p>'
            '<div class="btns"><a class="btn big resume" href="#"><span>התחל ללמוד</span> ❮</a></div></section>'
            + plan_html(ck) + f'<div class="path">{"".join(items)}{s.road_html(road, len(path) + 1)}</div>{out_html}</div></div>')
    (d / 'index.html').write_text(s.page(f'מסלול הלימוד — {name}', body, top=top, bare=True, course=ck), encoding='utf-8')
    return len(path)


def plan_html(ck: str) -> str:
    """Filled in the browser, which knows what's done: what to do now with the time there is."""
    return (f'<section class="plan" data-plan="{ck}" hidden><h2>כמה זמן יש לך?</h2><div class="seg" role="group" aria-label="זמן פנוי">'
            '<button data-min="10">10 דקות</button><button data-min="20" class="on">20 דקות</button><button data-min="45">45 דקות</button></div>'
            '<ol></ol><p class="cov mute"></p></section>')


def move_maps_aside(courses: list[str] | None = None, cache: Path | None = None) -> None:
    """--remap. The written topics stay: a topic that comes back under the same title keeps its id and its text."""
    root = cache or TOPICS_DIR
    for f in root.glob('*/map.json'):
        if not courses or f.parent.name in courses:
            (f.parent / 'old').mkdir(exist_ok=True)
            f.replace(f.parent / 'old' / f'map.{int(time.time())}.json')


def cost_report(since_iso: str, ledger: Path | None = None) -> str:
    """What this run paid, from the usage ledger (what the Pro subscription answered is free and isn't in it)."""
    rows: dict[str, list] = {}
    try:
        for ln in (ledger or ROOT / 'data' / 'usage.jsonl').read_text(encoding='utf-8').splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if str(r.get('op', '')).startswith('site.topic') and r.get('ts', '') >= since_iso:
                c = rows.setdefault(r['op'], [0, 0.0])
                c[0] += 1
                c[1] += r.get('cost_usd', 0)
    except OSError:
        pass
    total = sum(c[1] for c in rows.values())
    return f'topics: ${total:.2f} on the API · ' + (' · '.join(f'{k} {c[0]} calls ${c[1]:.2f}' for k, c in sorted(rows.items())) or 'no API calls')


def prepare(ck: str, name: str, ls: list[dict], raw: list[list], ctx: dict, ask=None, cache: Path | None = None,
            cap: int | None = NIGHT_CAP, retry: bool = False) -> dict | None:
    """The map, brought up to date, and the topics it's still missing → what render() needs, or None without a map."""
    for r, row in zip(ls, raw):
        r['extra'] = '\n'.join(md for _, md, role, _ in row if role == 'extra')
    secs = sections_of(ls, raw)
    if not secs:
        return None
    m = update_map(ck, name, secs, ctx, ask, cache)
    if not m.get('topics'):
        return None
    by_key = {x['key']: x for x in secs}
    res = refresh(ck, name, m, by_key, ctx, ask, cache, cap, retry) if ask else {}
    return {'map': m, 'by_key': by_key, 'res': res,
            'ready': sum(1 for t in m['topics'] if _rec(ck, t, cache))}
