"""
agent/grades.py — grades and the grader's feedback: one message when it arrives, a log per course, a weakness profile (19/11).

The owner: "כשמחזירים לי פתרון/בדיקה למטלה שהגשתי, אני רוצה לדעת מה המשוב — מה המרצה/הבודק אמר, טוב, רע — כדי שאדע איך
להשתפר. ותשמור את זה, כי ככה תדע על מה אני צריך להתמקד יותר."

    sources   (read only, the existing logged-in Moodle session; nothing here logs in, submits or starts anything)
              • the course's grade report (grade/report/user/index.php?id=<course>): ONE GET per course in the light check
                (scripts/moodle_light_check, 3× a weekday): every graded item, its grade, range and feedback text
              • an assignment whose grade / feedback changed → ONE GET of its page: the feedback table (grade, grader, date,
                comments) and its feedback files (annotated PDFs) → pdftotext. A scanned file with no text layer is
                kept and linked, not read: no model call here (zero new API cost)
              • the nightly scrape's _cache.json (collectors/moodle_assignment_scraper.feedback_of: the same table)
              • quiz reviews already saved by collectors/course_files (Software 1's quick-check quizzes): wrong questions
    log       data/feedback/<course>.json — every graded item: grade, max, comments, the points lost and their topics
    message   a NEW or CHANGED feedback (by fingerprint, against the log) → ONE short message through agent/proactive
              (its daily cap and quiet hours): course + item, grade / max, what the grader wrote, the points lost and why.
              The first run ever records what exists silently. A quiz with every answer right is logged, not announced.
    vault     a note per graded item + a note per course in the bot's own area (👩‍💼 העוזר/🎓 לימודים/📝 משובים/),
              `source: assistant`, linked from the course hub (vault_sync/maps). Never a note of the owner's.
    profile   points lost per course and topic (deterministic keyword rules: agent/exam_plan.RULES, + macro's topics here),
              crossed with the exam statistics (agent/exam_plan) — only when he asks: "על מה אני צריך להתמקד?",
              "איפה אני חלש?", "מה המשוב בתוכנה?". It offers no practice question (feedback_no_unsolicited_study).
The grader's words are third-party text: data, never instructions. They are quoted, cleaned and capped; nothing in them
can add a button, a link of ours or an action. No LLM anywhere in this file.

    python -m agent.grades --backfill     log in (the existing flow), read every course's grades now, record silently
    python -m agent.grades --profile      print the profile from the log (no network)
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parent.parent
DIR = 'feedback'                                   # data/feedback/<course>.json, _state.json, files/<course>/
NOTE_DIR = '👩‍💼 העוזר/🎓 לימודים/📝 משובים'
MOODLE = 'https://moodle.tau.ac.il'
UNCLASSIFIED = 'לא מסווג'
PAUSE = 2.0
MAX_PAGES = 6                                      # assignment pages read per run (the rest next run)
NO_TABLE_RECHECK_DAYS = 7                          # a course group with no grade table: looked at again weekly
QUOTE_MAX = 220
MSG_ITEMS = 4

# macroeconomics has no past exams, so agent/exam_plan has no rules for it: its topics (the course site's topic map, 19/11)
MACRO_RULES = {
    'חשבונאות לאומית ומדידת התוצר': [r'תמ"?ג', r'תל"?ג', r'תוצר', r'GDP', r'ערך מוסף', r'זרם ומלאי', r'מדד'],
    'שוק העבודה ומחזורי עסקים': [r'שוק העבודה', r'אבטלה', r'תעסוקה', r'היצע עבודה', r'ביקוש לעבודה', r'פנאי', r'הגיר', r'מחזור(?:י)? עסקים'],
    'תצרוכת בין-תקופתית והכנסה פרמננטית': [r'בין[- ]תקופת', r'פרמננט', r'PIH', r'החלקת תצרוכת', r'שתי תקופות', r'מגבלת התקציב'],
    'מחזור החיים (LCH)': [r'מחזור החיים', r'LCH', r'פרישה', r'צבירת נכסים'],
    'פונקציית ייצור, הון והשקעה': [r'פונקציית (?:ה)?ייצור', r'עלות ההון', r'user cost', r'השקעה', r'מלאי ההון', r'קוב[- ]דאגלס'],
    'שיווי משקל כללי וזעזועי פריון': [r'זעזוע', r'פריון', r'ריבית ריאלית', r'שיווי משקל כללי'],
    'מודל סולו': [r'סולו', r'solow', r'מצב עמיד', r'steady', r'מלכודת עוני', r'הון לעובד'],
    'כלל הזהב וצמיחה טכנולוגית': [r'כלל הזהב', r'golden', r'טכנולוגי', r'מעצימת עבודה', r'ריבוי טבעי'],
}
NAMES = {'probability': 'הסתברות', 'software_1': 'תוכנה 1', 'microeconomics_2': 'מיקרו כלכלה ב׳', 'macroeconomics': 'מאקרו כלכלה'}
try:                                                     # your semester's courses (SSA_ACTIVE_COURSES + courses.yaml), the TAU example above otherwise
    from config.paths import ACTIVE_COURSES as _ACTIVE, COURSE_NAMES as _CN
    NAMES = {ck: (_CN.get(ck) or NAMES.get(ck) or ck) for ck in _ACTIVE} or NAMES
except Exception:
    pass


def enabled() -> bool:
    """Study domain only (SSA_STUDY=1); SSA_GRADES=0 turns just this off."""
    try:
        from config.features import study_enabled
        if not study_enabled():
            return False
    except Exception:
        return False
    return os.environ.get('SSA_GRADES', '1') != '0'


def _paths(root: Path | None = None) -> Path:
    return (Path(root) if root else ROOT) / 'data' / DIR


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def _name(ck: str) -> str:
    return NAMES.get(ck) or _course_names().get(ck, ck)


def _course_names() -> dict:
    try:
        from config.paths import COURSE_NAMES
        return COURSE_NAMES
    except Exception:
        return {}


# ── third-party text: data, never instructions ──────────────────────────────────────────────────────────────────────
_BIDI = re.compile('[​-‏‪-‮⁦-⁩­﻿]')
_URL = re.compile(r'https?://\S+|www\.\S+', re.I)


def clean(text: str, cap: int = QUOTE_MAX) -> str:
    """The grader's words, safe to show: no hidden characters, no links, no WhatsApp formatting or markup, one line, capped."""
    t = _BIDI.sub('', _html.unescape(text or ''))
    t = _URL.sub('[קישור]', t)
    t = re.sub(r'[*_~`<>{}\[\]|\\]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t if len(t) <= cap else t[:cap - 1].rstrip() + '…'


# ── parsing (pure) ──────────────────────────────────────────────────────────────────────────────────────────────────

def _cell_text(fragment: str) -> str:
    f = re.sub(r'(?s)<img[^>]*>|<span[^>]*userinitials[^>]*>.*?</span>', ' ', fragment or '')
    f = re.sub(r'(?i)<br\s*/?>|</p>|</li>|</div>', '\n', f)
    t = _html.unescape(re.sub(r'<[^>]+>', ' ', f))
    return re.sub(r'\n{2,}', '\n', '\n'.join(re.sub(r'[ \t\xa0]+', ' ', x).strip() for x in t.splitlines())).strip()


def parse_grade_report(page: str) -> list[dict]:
    """The user grade report → [{cmid, module, title, grade, range, feedback}] for every activity row (course totals and
    categories are skipped). grade '-' = not graded yet."""
    out = []
    for tr in re.findall(r'(?s)<tr[^>]*>(.*?)</tr>', page or ''):
        m = re.search(r'class="gradeitemheader[^"]*"[^>]*href="[^"]*/mod/(\w+)/view\.php\?id=(\d+)[^"]*"[^>]*>(.*?)</a>', tr, re.S)
        if not m:
            continue
        cells = {c: _cell_text(v) for c, v in re.findall(r'(?s)<td[^>]*class="[^"]*column-(\w+)[^"]*"[^>]*>(.*?)</td>', tr)}
        out.append({'cmid': m.group(2), 'module': m.group(1), 'title': _cell_text(m.group(3)),
                    'grade': cells.get('grade', ''), 'range': cells.get('range', ''), 'feedback': cells.get('feedback', '')})
    return out


def has_grade_table(page: str) -> bool:
    return 'user-grade' in (page or '')


NUM = r'(\d+(?:[.,]\d+)?)'


def grade_parts(grade: str, rng: str = '') -> tuple[float | None, float | None, str]:
    """('85.00', '0–100') → (85, 100, ''); ('92.00 / 100.00', '') → (92, 100, ''); ('עובר', 'לא עובר–עובר') → (None, None,
    'עובר'); '-' / '' → (None, None, '')."""
    g = (grade or '').strip()
    if not g or g in ('-', '–'):
        return None, None, ''
    f = lambda s: float(s.replace(',', '.'))
    m = re.match(r'^' + NUM + r'\s*(?:/|out of|מתוך)\s*' + NUM, g)
    if m:
        return f(m.group(1)), f(m.group(2)), ''
    m = re.match(r'^' + NUM + r'\s*%?$', g)
    if m:
        r = re.findall(NUM, rng or '')
        return f(m.group(1)), (f(r[-1]) if r else None), ''
    return None, None, clean(g, 40)


def _fmt(x: float | None) -> str:
    return '' if x is None else (str(int(x)) if float(x).is_integer() else f'{x:g}')


def _count(n: int, one: str, many: str) -> str:
    return one if n == 1 else f'{n} {many}'


def grade_label(rec: dict) -> str:
    if rec.get('got') is not None:
        return f"{_fmt(rec['got'])}/{_fmt(rec['max'])}" if rec.get('max') else _fmt(rec['got'])
    return rec.get('label') or ''


# ── topics and the points lost ──────────────────────────────────────────────────────────────────────────────────────

def rules_of(ck: str) -> dict[str, list[str]]:
    if ck == 'macroeconomics':
        return MACRO_RULES
    try:
        from agent import exam_plan
        return exam_plan.RULES.get(ck) or {}
    except Exception:
        return {}


def topics_of(ck: str, text: str) -> list[str]:
    """The course topics whose keyword rules hit the text, most hits first (at most 2). [] = no rule places it."""
    score = {t: sum(len(re.findall(rx, text or '', re.I)) for rx in rxs) for t, rxs in rules_of(ck).items()}
    best = max(score.values(), default=0)
    if not best:
        return []
    return [t for t, s in sorted(score.items(), key=lambda kv: -kv[1]) if s >= max(1, best * 0.5)][:2]


DEDUCT_RX = re.compile(
    r'(?:(?<![\w.\-])-\s?' + NUM + r'(?!\s*%)(?![\d\-])'                                  # "-5", "(-3)"
    r'|הורד(?:ו|ה|נו)?\s+(?:של\s+)?' + NUM + r'\s*(?:נק|נקודות)'                          # "הורדו 5 נקודות"
    r'|' + NUM + r'\s*(?:נק[׳\'"]?|נקודות)\s*(?:ירדו|הורדו|פחות|הורדה)'                    # "5 נק׳ ירדו"
    r'|(?:minus|lost)\s+' + NUM + r'|' + NUM + r'\s*(?:pts|points)\s*(?:off|lost|deducted))', re.I)


def _segments(text: str) -> list[str]:
    parts = re.split(r'\n+|(?<=[.;!?])\s+|\s+•\s+|(?=סעיף\s+[א-ת0-9]\b)', text or '')
    return [p.strip(' -•\t') for p in parts if p and p.strip(' -•\t')]


def lost_points(ck: str, comments: str, got: float | None = None, mx: float | None = None) -> list[dict]:
    """The grader's comments → [{pts, what, topics}]: each sentence with an explicit deduction ("-5", "הורדו 3 נקודות"),
    and what the grade lost beyond them (no detail written → 'בלי פירוט מהבודק'). Topics by the course's rules."""
    out, total = [], 0.0
    for seg in _segments(comments):
        m = DEDUCT_RX.search(seg)
        if not m:
            continue
        pts = float(next(g for g in m.groups() if g).replace(',', '.'))
        if pts <= 0 or (mx and pts > mx):
            continue
        total += pts
        out.append({'pts': pts, 'what': clean(seg, 160), 'topics': topics_of(ck, seg)})
    if got is not None and mx and got < mx:
        rest = round(mx - got - total, 2)
        if rest > 0:
            body = clean(comments, 160) if not out and comments else 'בלי פירוט מהבודק'
            out.append({'pts': rest, 'what': body, 'topics': topics_of(ck, comments) if not out else []})
    return out


# ── records ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def fingerprint(rec: dict) -> str:
    key = json.dumps([grade_label(rec), clean(rec.get('comments', ''), 4000), [(x['pts'], x['what']) for x in rec.get('lost') or []],
                      sorted(f.get('name', '') for f in rec.get('files') or [])], ensure_ascii=False)
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]


def make(ck: str, cmid: str, kind: str, title: str, grade: str = '', rng: str = '', comments: str = '', grader: str = '',
         graded_at: str = '', files: list[dict] | None = None, url: str = '', lost: list[dict] | None = None,
         extra: dict | None = None) -> dict:
    got, mx, label = grade_parts(grade, rng)
    text_all = '\n'.join([comments or ''] + [f.get('text', '') for f in files or [] if f.get('text')])
    rec = {'id': f'{ck}:{cmid}', 'course': ck, 'cmid': str(cmid), 'kind': kind, 'title': clean(title, 120),
           'url': url or f'{MOODLE}/mod/{kind if kind in ("assign", "quiz") else "assign"}/view.php?id={cmid}',
           'grade_raw': (grade or '').strip(), 'got': got, 'max': mx, 'label': label,
           'comments': (comments or '').strip()[:4000], 'grader': clean(grader, 60), 'graded_at': clean(graded_at, 60),
           'files': files or [], 'lost': lost if lost is not None else lost_points(ck, text_all, got, mx), **(extra or {})}
    rec['fp'] = fingerprint(rec)
    return rec


def graded(rec: dict) -> bool:
    return bool(rec.get('got') is not None or rec.get('label') or rec.get('comments') or rec.get('files')
                or rec.get('kind') == 'quiz')


def from_report_row(ck: str, row: dict) -> dict | None:
    """An assignment's row. A quiz's row is left to its saved review (collectors/course_files): one source per item, so
    marks and right-answer counts never take turns as "a new grade"."""
    if row['module'] != 'assign':
        return None
    rec = make(ck, row['cmid'], row['module'], row['title'], row['grade'], row['range'], row['feedback'],
               extra={'report_fb': clean(row['feedback'], 4000)})
    return rec if graded(rec) and (rec['got'] is not None or rec['label'] or rec['comments']) else None


def from_cache(root: Path | None = None) -> list[dict]:
    """The nightly scrape's assignments with a feedback block (data/assignments/_cache.json)."""
    base = Path(root) if root else ROOT
    cache = _load(base / 'data' / 'assignments' / '_cache.json', {})
    out = []
    for a in cache.get('assignments') or []:
        fb = a.get('feedback') if isinstance(a, dict) else None
        if not fb or a.get('kind') not in ('assign', None):
            continue
        if 'comments' not in fb and 'grade_text' not in fb and not fb.get('grade'):
            continue                                       # an old-format dump (before 19/11): the report has it
        rec = make(a.get('course_key', ''), str(a.get('assign_id')), 'assign', a.get('title', ''),
                   fb.get('grade') or fb.get('grade_text') or a.get('grade') or '', '', fb.get('comments', ''),
                   fb.get('grader', ''), fb.get('graded_at', ''), url=a.get('url', ''),
                   files=[{'name': _fname(u, n), 'url': u} for n, u in enumerate(fb.get('files') or [], 1)])
        if graded(rec):
            out.append(rec)
    return out


def from_quizzes(root: Path | None = None, courses=None) -> list[dict]:
    """His finished quiz reviews (collectors/course_files) → a record per quiz: right answers / questions, each wrong
    answer a lost question tagged by topic (Moodle hides these quizzes' marks; a question is the unit)."""
    from agent import obligations
    base = (Path(root) / 'data' / 'moodle') if root else None
    out = []
    for ck in courses or list(NAMES):
        try:
            revs = obligations.quiz_reviews(ck, base) if base else obligations.quiz_reviews(ck)
        except Exception:
            continue
        for cmid, r in revs.items():
            if not r.get('n'):
                continue
            wrong = r.get('wrong') or []
            lost = [{'pts': 1.0, 'unit': 'q', 'what': f"שאלה {q.get('num', '?')}: " + clean(q.get('text', ''), 140),
                     'topics': topics_of(ck, f"{q.get('text', '')} {q.get('right', '')}")} for q in wrong]
            rec = make(ck, cmid, 'quiz', r.get('title', ''), f"{r['n'] - len(wrong)}/{r['n']}", '', '', lost=lost,
                       extra={'unit': 'q'})
            out.append(rec)
    return out


# ── the log ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def load_log(ck: str, root: Path | None = None) -> dict:
    d = _load(_paths(root) / f'{ck}.json', {})
    d.setdefault('course', ck)
    d.setdefault('items', {})
    return d


def load_state(root: Path | None = None) -> dict:
    return _load(_paths(root) / '_state.json', {})


def save_state(st: dict, root: Path | None = None) -> None:
    _save(_paths(root) / '_state.json', st)


def _merge_same(old: dict | None, new: dict) -> dict:
    """A source that knows less (the report has no grader / files) never erases what a richer one saw."""
    if not old:
        return new
    keep = dict(new)
    for k in ('grader', 'graded_at', 'files', 'comments', 'report_fb'):
        if not keep.get(k) and old.get(k):
            keep[k] = old[k]
    names = lambda fs: sorted(f.get('name', '') for f in fs or [])
    if keep.get('files') and names(keep['files']) == names(old.get('files')) and not any(f.get('text') or f.get('scanned') for f in keep['files']):
        keep['files'] = old['files']                   # the cache knows the file's name; the log has read it
    if keep.get('files') != new.get('files') or keep.get('comments') != new.get('comments'):
        got, mx = keep.get('got'), keep.get('max')
        text_all = '\n'.join([keep.get('comments') or ''] + [f.get('text', '') for f in keep.get('files') or []])
        if keep.get('kind') != 'quiz':
            keep['lost'] = lost_points(keep['course'], text_all, got, mx)
    keep['fp'] = fingerprint(keep)
    return keep


def ingest(recs: list[dict], now: datetime | None = None, root: Path | None = None) -> list[dict]:
    """Merge records into the per-course logs. → the records that are new or changed and wait to be said. The very first
    ingest records everything silently (no burst on deploy); an all-right quiz is never announced."""
    now = now or datetime.now()
    stamp = now.isoformat(timespec='minutes')
    st = load_state(root)
    first = not st.get('init')
    logs: dict[str, dict] = {}
    fresh = []
    for r in recs:
        if not r.get('course'):
            continue
        log = logs.setdefault(r['course'], load_log(r['course'], root))
        old = log['items'].get(r['id'])
        rec = _merge_same(old, r)
        if old and old.get('fp') == rec['fp']:
            continue
        same_words = (bool(old) and bool(old.get('said')) and grade_label(old) == grade_label(rec)
                      and clean(old.get('comments', ''), 4000) == clean(rec.get('comments', ''), 4000)
                      and [x['pts'] for x in old.get('lost') or []] == [x['pts'] for x in rec.get('lost') or []])
        quiet = first or (rec['kind'] == 'quiz' and not rec['lost']) or same_words   # a grader / date / file name only
        rec.update(first_seen=(old or {}).get('first_seen') or stamp, updated=stamp,
                   said=stamp if quiet else '', history=((old or {}).get('history') or [])[-5:]
                   + ([{'at': old.get('updated', ''), 'grade': grade_label(old), 'fp': old.get('fp')}] if old else []))
        log['items'][rec['id']] = rec
        if not rec['said']:
            fresh.append(rec)
    for ck, log in logs.items():
        _save(_paths(root) / f'{ck}.json', log)
    if first:
        st['init'] = stamp
        save_state(st, root)
    return fresh


def all_items(root: Path | None = None) -> list[dict]:
    out = []
    for ck in NAMES:
        out += list(load_log(ck, root)['items'].values())
    return out


def pending(root: Path | None = None) -> list[dict]:
    return sorted([r for r in all_items(root) if not r.get('said')], key=lambda r: r.get('updated', ''))


# ── the message (one, for whatever arrived) ─────────────────────────────────────────────────────────────────────────

def _lost_line(x: dict) -> str:
    pts = f"{_fmt(x['pts'])} נק׳" if x.get('unit') != 'q' else 'שאלה'
    top = f" ({', '.join(x['topics'])})" if x.get('topics') else ''
    if x.get('unit') == 'q':
        return f"• ❌ {x['what']}{top}"
    return f"• −{pts}: {x['what']}{top}"


def item_text(rec: dict) -> str:
    name = _name(rec['course'])
    g = grade_label(rec)
    L = [f"📝 *{name} — {rec['title']}*" + (f": {g}" if g else '')]
    if rec.get('kind') == 'quiz':
        L.append(f"טעית ב{_count(len(rec['lost']), 'שאלה אחת', 'שאלות')}:" if rec['lost'] else 'הכול נכון ✅')
    elif rec.get('comments'):
        who = f" ({rec['grader']})" if rec.get('grader') else ''
        general = [x for x in _segments(rec['comments']) if not DEDUCT_RX.search(x)] if rec.get('lost') else [rec['comments']]
        if general:                                    # the deductions are listed below, word for word
            L.append(f"הבודק{who} כתב: «{clean(' '.join(general))}»")
    else:
        L.append('בלי הערות כתובות.')
    if rec.get('lost'):
        if rec.get('kind') != 'quiz':
            L.append('איפה ירדו נקודות:')
        L += [_lost_line(x) for x in rec['lost'][:4]] + ([f"ועוד {len(rec['lost']) - 4}."] if len(rec['lost']) > 4 else [])
    elif rec.get('kind') != 'quiz' and rec.get('got') is not None and rec.get('max') and rec['got'] >= rec['max']:
        L.append('ציון מלא 💪')
    scanned = [f for f in rec.get('files') or [] if f.get('scanned')]
    readable = [f for f in rec.get('files') or [] if f.get('text') and not f.get('scanned')]
    if readable and not rec.get('lost'):
        L.append(f"📎 יש קובץ משוב ({readable[0]['name']}) — כתוב בו: «{clean(readable[0]['text'], 160)}»")
    if scanned:
        L.append(f"📎 יש קובץ משוב עם הערות בכתב יד ({scanned[0]['name']}) — לא קראתי אותו, כדאי להציץ.")
    return '\n'.join(L)


def message(recs: list[dict]) -> str:
    shown = recs[:MSG_ITEMS]
    body = '\n\n'.join(item_text(r) for r in shown)
    more = len(recs) - len(shown)
    tail = f'\n\nועוד {more} — "מה המשוב ב<קורס>?" ואפרט.' if more > 0 else ''
    return body + tail


def notes(now: datetime, recs: list[dict] | None = None) -> list[dict]:
    """proactive-shaped: ONE note for everything that waits. Its key is the items' fingerprints, so it is said once."""
    if not enabled():
        return []
    recs = pending() if recs is None else recs
    if not recs:
        return []
    key = 'grades|' + '+'.join(f"{r['id']}@{r['fp'][:6]}" for r in recs)[:180]
    return [{'key': key, 'kind': 'grade_feedback', 'prio': 1, 'ids': [r['id'] for r in recs],
             'fps': {r['id']: r['fp'] for r in recs}, 'buttons': [], 'text': message(recs)}]


def mark_said(note: dict, now: datetime | None = None, root: Path | None = None) -> None:
    if note.get('kind') != 'grade_feedback':
        return
    stamp = (now or datetime.now()).isoformat(timespec='minutes')
    by: dict[str, list[str]] = defaultdict(list)
    for i in note.get('ids') or []:
        by[i.split(':', 1)[0]].append(i)
    for ck, ids in by.items():
        log = load_log(ck, root)
        for i in ids:
            r = log['items'].get(i)
            if r and r.get('fp') == (note.get('fps') or {}).get(i, r.get('fp')):
                r['said'] = stamp
        _save(_paths(root) / f'{ck}.json', log)


# ── fetching (the light check's session; GET only) ──────────────────────────────────────────────────────────────────

def pdf_text(path: Path) -> tuple[str, bool]:
    """(text, scanned). pdftotext (free); a PDF with almost no text layer is 'scanned' (handwriting / a photo)."""
    cache = path.with_suffix(path.suffix + '.txt')
    if cache.exists():
        t = cache.read_text(encoding='utf-8', errors='replace')
    elif path.suffix.lower() == '.pdf' and shutil.which('pdftotext'):
        try:
            t = subprocess.run(['pdftotext', '-layout', str(path), '-'], capture_output=True, timeout=60).stdout.decode(
                'utf-8', 'replace')
        except Exception:
            t = ''
        cache.write_text(t, encoding='utf-8')
    elif path.suffix.lower() in ('.txt', '.md'):
        t = path.read_text(encoding='utf-8', errors='replace')
    else:
        return '', False
    t = re.sub(r'[ \t]+', ' ', t).strip()
    return t, len(re.sub(r'\s', '', t)) < 40


def _fname(url: str, n: int) -> str:
    return re.sub(r'[<>:"/\\|?*]', '', unquote(url.split('?')[0].rsplit('/', 1)[-1]))[:60] or f'file{n}'


def _files(session, ck: str, cmid: str, urls: list[str], root: Path | None, sleep) -> list[dict]:
    d = _paths(root) / 'files' / ck
    out = []
    for n, u in enumerate(urls, 1):
        name = _fname(u, n)
        p = d / f'{cmid}_{n}_{name}'
        if not (p.exists() and p.stat().st_size):
            try:
                sleep(PAUSE)
                r = session.get(u, timeout=60, allow_redirects=True)
                if r.status_code == 200 and r.content:
                    d.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(r.content)
            except Exception as e:
                print(f'  ⚠️ feedback file {name}: {e!r}'[:160])
        text, scanned = pdf_text(p) if p.exists() else ('', False)
        out.append({'name': name, 'path': str(p.relative_to(Path(root) if root else ROOT)) if p.exists() else '',
                    'text': text[:3000], 'scanned': scanned and p.exists()})
    return out


def assignment_record(session, ck: str, row: dict, root: Path | None, sleep) -> dict:
    """One GET of the assignment page: its feedback table and files, merged with the report's row."""
    from collectors.moodle_assignment_scraper import feedback_of
    url = f"{MOODLE}/mod/assign/view.php?id={row['cmid']}"
    sleep(PAUSE)
    r = session.get(url, timeout=30)
    fb = feedback_of(r.text) if getattr(r, 'status_code', 200) == 200 else {}
    files = _files(session, ck, row['cmid'], fb.get('files') or [], root, sleep) if fb.get('files') else []
    grade = row.get('grade') if (row.get('grade') or '-') != '-' else (fb.get('grade') or fb.get('grade_text') or '')
    return make(ck, row['cmid'], 'assign', row['title'], grade, row.get('range', ''), fb.get('comments') or row.get('feedback', ''),
                fb.get('grader', ''), fb.get('graded_at', ''), files, url,
                extra={'report_fb': clean(row.get('feedback', ''), 4000)})


def check(session, now: datetime | None = None, sleep=time.sleep, courses: list[str] | None = None,
          root: Path | None = None, ids_of=None, timeout: int = 20, vault_dir: Path | None = None) -> dict:
    """The light check's step: each course's grade report (one GET per course group that has a table), an assignment
    page only for a row that changed, then ingest (+ the nightly cache and the saved quiz reviews). Raises on a network
    error (the light check backs off); → {'read', 'pages', 'new'}."""
    now = now or datetime.now()
    if ids_of is None:
        from collectors.announcement_scraper import moodle_ids
        ids_of = lambda ck: moodle_ids(ck, None)
    st = load_state(root)
    no_table = st.setdefault('no_table', {})
    have = {r['id']: r for r in all_items(root)}
    recs, read, pages = [], 0, 0
    for ck in courses or list(NAMES):
        for cid in ids_of(ck):
            seen = no_table.get(cid)
            if seen and (now - datetime.fromisoformat(seen)).days < NO_TABLE_RECHECK_DAYS:
                continue
            sleep(PAUSE)
            r = session.get(f'{MOODLE}/grade/report/user/index.php?id={cid}', timeout=timeout)
            if getattr(r, 'status_code', 200) >= 400:
                raise RuntimeError(f'grade report {cid}: HTTP {r.status_code}')
            read += 1
            if not has_grade_table(r.text):
                no_table[cid] = now.isoformat(timespec='minutes')
                continue
            no_table.pop(cid, None)
            for row in parse_grade_report(r.text):
                rec = from_report_row(ck, row)
                if not rec:
                    continue
                old = have.get(rec['id'])
                changed = not old or grade_label(old) != grade_label(rec) or \
                    old.get('report_fb', clean(old.get('comments', ''), 4000)) != rec['report_fb']
                if rec['kind'] == 'assign' and changed and pages < MAX_PAGES:
                    try:
                        rec = assignment_record(session, ck, row, root, sleep)
                        pages += 1
                    except Exception as e:
                        print(f"  ⚠️ feedback page {row['title'][:30]}: {e!r}"[:200])
                elif old and not changed:
                    continue
                recs.append(rec)
    save_state({**load_state(root), 'no_table': no_table, 'last_check': now.isoformat(timespec='minutes')}, root)
    new = ingest(recs + from_cache(root) + from_quizzes(root), now, root)
    _notes(vault_dir, root)
    return {'read': read, 'pages': pages, 'new': [r['id'] for r in new]}


def _notes(vault_dir: Path | None, root: Path | None) -> None:
    if root is not None and vault_dir is None:
        return                                     # a test's data root never writes the real vault
    try:
        write_vault(vault_dir, root)
    except Exception as e:
        print(f'  ⚠️ feedback notes: {e!r}'[:200])


def refresh_local(now: datetime | None = None, root: Path | None = None, vault_dir: Path | None = None) -> list[dict]:
    """No network: the nightly cache + the saved quiz reviews → the log (vault_assistant runs it twice a day)."""
    new = ingest(from_cache(root) + from_quizzes(root), now, root)
    _notes(vault_dir, root)
    return new


# ── the profile (only on his request) ───────────────────────────────────────────────────────────────────────────────

def profile(root: Path | None = None) -> dict:
    """{course: {'items', 'pts', 'qs', 'topics': {topic: {'pts', 'qs', 'n', 'items'}}, 'max_pts'}} from the log."""
    out = {}
    for ck in NAMES:
        items = [r for r in load_log(ck, root)['items'].values()]
        if not items:
            continue
        c = {'items': len(items), 'pts': 0.0, 'qs': 0, 'max_pts': 0.0, 'quizzes': 0, 'questions': 0, 'topics': {}}
        for r in items:
            if r.get('kind') == 'quiz':
                c['quizzes'] += 1
                c['questions'] += int(r.get('max') or 0)
            elif r.get('max'):
                c['max_pts'] += r['max']
            for x in r.get('lost') or []:
                q = x.get('unit') == 'q'
                for t in x.get('topics') or [UNCLASSIFIED]:
                    e = c['topics'].setdefault(t, {'pts': 0.0, 'qs': 0, 'n': 0, 'items': []})
                    e['qs' if q else 'pts'] += 1 if q else x['pts'] / max(1, len(x.get('topics') or [1]))
                    e['n'] += 1
                    if r['title'] not in e['items']:
                        e['items'].append(r['title'])
                if q:
                    c['qs'] += 1
                else:
                    c['pts'] += x['pts']
        out[ck] = c
    return out


def _weight(e: dict) -> float:
    return e['pts'] + 3 * e['qs']


def _topic_word(t: str, e: dict) -> str:
    bits = ([f"{_fmt(round(e['pts'], 1))} נק׳"] if e['pts'] else []) + ([f"{e['qs']} שאלות" if e['qs'] > 1 else 'שאלה אחת']
                                                                      if e['qs'] else [])
    return f"{t} ({', '.join(bits)})"


def _exam_rows(ck: str) -> list[dict]:
    try:
        from agent import exam_plan
        s = exam_plan.load(ck)
        ct = (s.get('coverage') or {}).get('topics') or {}
        return [r for r in s.get('rows') or [] if ct.get(r['topic'], {}).get('state') != 'cut'
                and not ct.get(r['topic'], {}).get('excluded')]
    except Exception:
        return []


def profile_text(ck: str | None = None, root: Path | None = None) -> str:
    """"על מה אני צריך להתמקד?" / "איפה אני חלש?" — where he lost points, by course and topic, crossed with what the
    past exams ask most. Read only; no practice question."""
    prof = profile(root)
    courses = [ck] if ck else [c for c in NAMES]
    L = ['🎯 *איפה איבדת נקודות — לפי המשובים והבחנים שלך*', '']
    clean_courses, weak_any, both = [], False, []
    for c in courses:
        p = prof.get(c)
        if not p:
            if ck:
                L.append(f'ב{_name(c)} עוד לא חזר אליי שום משוב או ציון.')
            continue
        topics = sorted(((t, e) for t, e in p['topics'].items()), key=lambda te: -_weight(te[1]))
        if not topics:
            what = []
            if p['items'] - p['quizzes']:
                what.append(_count(p['items'] - p['quizzes'], 'מטלה אחת', 'מטלות'))
            if p['quizzes']:
                what.append(f"{_count(p['quizzes'], 'בוחן אחד', 'בחנים')} ({p['questions']} שאלות)")
            clean_courses.append(f"{_name(c)} ({' + '.join(what)})")
            continue
        weak_any = True
        L.append(f"• *{_name(c)}*: הכי הרבה על " + '; '.join(_topic_word(t, e) for t, e in topics[:3]))
        rows = {r['topic']: r for r in _exam_rows(c)}
        for t, e in topics[:3]:
            if t in rows:
                both.append((c, t, rows[t]))
    if clean_courses:
        L.append(('✅ בלי הורדות: ' if weak_any else '✅ עד עכשיו בלי שום הורדה: ') + ', '.join(clean_courses) + '.')
    if both:
        L += ['', '⚠️ *גם חלש אצלך וגם חוזר במבחן:*']
        L += [f"• {_name(c)} — {t}: {r['recent_pct']}% מהשאלות בשנים האחרונות" for c, t, r in both[:4]]
    elif not weak_any:
        tops = []
        for c in courses:
            rows = _exam_rows(c)
            if rows:
                tops.append(f"{_name(c)}: {rows[0]['topic']} ({rows[0]['recent_pct']}%)")
        if tops:
            L += ['', 'אז מהמשובים אין עדיין חולשה. לפי המבחנים הקודמים, מה שהכי חוזר:'] + [f'• {x}' for x in tops[:4]]
    if len(L) <= 2:
        return 'עוד לא חזר אליי שום משוב או ציון על מטלה, אז אין לי עדיין תמונה של איפה אתה מאבד נקודות.'
    link = course_note_link(ck) if ck else ''
    return '\n'.join(L) + (f'\n\n📎 הפירוט: {link}' if link else '')


def feedback_text(ck: str | None = None, root: Path | None = None, limit: int = 6) -> str:
    """"מה המשוב בתוכנה?" — the latest graded items of the course (or all), each with grade, the grader's words and the
    points lost."""
    items = [r for r in all_items(root) if not ck or r['course'] == ck]
    if not items:
        return f"עוד לא חזר אליי משוב{' ב' + _name(ck) if ck else ''} — כשיחזור, אשלח לך מיד מה נכתב."
    assigns = sorted([r for r in items if r.get('kind') != 'quiz'], key=lambda r: r.get('updated', ''), reverse=True)
    quizzes = [r for r in items if r.get('kind') == 'quiz']
    L = []
    for r in assigns[:limit]:
        L.append(item_text(r))
    if quizzes:
        bad = [q for q in quizzes if q.get('lost')]
        tot = sum(int(q.get('max') or 0) for q in quizzes)
        lost = sum(len(q['lost']) for q in quizzes)
        L.append(f"🧪 *בחנים ב{_name(quizzes[0]['course'])}*: {_count(len(quizzes), 'בוחן אחד', 'בחנים')}, {tot - lost}/{tot} נכונות"
                 + ('' if not bad else '\n' + '\n'.join(_lost_line(x) for q in bad for x in q['lost'][:3])))
    more = len(assigns) - limit
    link = course_note_link(ck) if ck else ''
    return '\n\n'.join(L) + (f'\n\nועוד {more} בפתק.' if more > 0 else '') + (f'\n\n📎 {link}' if link else '')


FOCUS_RX = re.compile(r'על מה (?:אני )?(?:צריך|צריכה|כדאי לי|חייב|אמור) (?:ל)?(?:התמקד|להתמקד|להשקיע|לעבוד|לשפר)|'
                      r'(?:איפה|במה) אני (?:הכי )?(?:חלש|חלשה|נופל|מאבד נקודות)|(?:ה)?חולשות שלי|נקודות (?:ה)?חולשה שלי|'
                      r'(?:איפה|על מה|במה) (?:אני )?(?:מאבד|איבדתי) (?:הכי הרבה )?נקודות|מה אני צריך לשפר')
FEEDBACK_RX = re.compile(r'מה (?:ה)?משוב(?:ים)?\s*(?:ב|על|של|שקיבלתי)|מה (?:ה)?(?:בודק|בודקת|מתרגל|מתרגלת|מרצה) (?:כתב|כתבה|אמר|אמרה)|'
                         r'(?:ה)?משובים שלי|(?:ה)?ציונים שלי|איזה ציון(?:ים)? קיבלתי|מה (?:ה)?ציון(?:ים)? (?:שלי |שקיבלתי )?(?:ב|על)|'
                         r'מה קיבלתי (?:ב|על)(?:תרגיל|מטלה|שיעורי|עבודה)|חזר(?:ו)? (?:לי )?(?:ה)?(?:משוב|ציון|בדיקה)')
DAY_RX = re.compile(r'\b(?:היום|מחר|הערב|השבוע)\b|ביומן')


def course_of(text: str) -> str | None:
    try:
        from agent import exam_plan
        ck = exam_plan.course_of(text)
        return ck if ck in NAMES else None
    except Exception:
        return None


def answer(text: str, root: Path | None = None) -> str | None:
    """His own question → the profile / the feedback. None = not this question."""
    t = text or ''
    if FEEDBACK_RX.search(t):
        return feedback_text(course_of(t), root)
    if FOCUS_RX.search(t) and not DAY_RX.search(t):
        return profile_text(course_of(t), root)
    return None


# ── the vault: the bot's own area, linked from the course hub ───────────────────────────────────────────────────────

def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', '', s or '').strip()[:80] or 'ללא שם'


def course_note_rel(ck: str) -> str:
    n = _safe(_name(ck).replace('"', '״'))
    return f'{NOTE_DIR}/{n}/📝 משובים — {n}.md'


def item_note_rel(rec: dict) -> str:
    return f"{NOTE_DIR}/{_safe(_name(rec['course']))}/{_safe(rec['title'])}.md"


def course_note_link(ck: str | None) -> str:
    if not ck:
        return ''
    try:
        from config.paths import VAULT_ROOT, VAULT_DIR
        rel = f'{VAULT_DIR.name}/{course_note_rel(ck)}'
        return f'obsidian://open?vault={quote(VAULT_ROOT.name)}&file={quote(rel[:-3])}'
    except Exception:
        return ''


def _item_body(rec: dict, back: str) -> str:
    L = [f"# 📝 {_name(rec['course'])} — {rec['title']}", '', back, '']
    g = grade_label(rec)
    L += [f'- ציון: {g or "—"}'] + ([f"- נבדק על-ידי: {rec['grader']}"] if rec.get('grader') else []) \
        + ([f"- נבדק ב: {rec['graded_at']}"] if rec.get('graded_at') else []) + [f"- במודל: {rec['url']}", '']
    if rec.get('comments'):
        L += ['## מה הבודק כתב', '', '> ' + clean(rec['comments'], 3000), '']
    if rec.get('lost'):
        L += ['## איפה ירדו נקודות', ''] + [_lost_line(x) for x in rec['lost']] + ['']
    for f in rec.get('files') or []:
        L += [f"## 📎 {f.get('name', '')}", '', 'קובץ סרוק / בכתב יד — לא נקרא אוטומטית.' if f.get('scanned')
              else ('> ' + clean(f.get('text', ''), 1500) if f.get('text') else ''), '']
    L.append('_נכתב בקוד מהמשוב במודל, בלי AI. דברי הבודק מצוטטים כמו שהם._')
    return '\n'.join(L) + '\n'


def write_vault(vault_dir: Path | None = None, root: Path | None = None) -> dict:
    if vault_dir is None:
        from config.paths import VAULT_DIR as vault_dir
    from vault_sync.notewriter import Writer
    w = Writer(Path(vault_dir))
    prof = profile(root)
    for ck in NAMES:
        items = sorted(load_log(ck, root)['items'].values(), key=lambda r: (r.get('kind') == 'quiz', r.get('title', '')))
        if not items:
            continue
        hub = ''
        try:
            from agent import exam_plan
            hub = exam_plan.hub_rel(ck, Path(vault_dir))
        except Exception:
            pass
        cn = course_note_rel(ck)
        back = f"↩️ [[{cn[:-3]}|📝 משובים — {_name(ck)}]]" + (f" · [[{hub}|🧭 {_name(ck)} — מפה]]" if hub else '')
        for r in items:
            w.note(item_note_rel(r), {'type': 'feedback', 'title': f"{_name(ck)} — {r['title']}", 'course_key': ck,
                                      'grade': grade_label(r) or None, 'source': 'assistant',
                                      'tags': ['משוב', ck]}, _item_body(r, back))
        p = prof.get(ck) or {}
        L = [f'# 📝 משובים — {_name(ck)}', '', f"↩️ [[{hub}|🧭 {_name(ck)} — מפה]]" if hub else '', '',
             'כל מה שחזר מהבודקים, עם הציון, מה נכתב ואיפה ירדו נקודות. נבנה בקוד מהמודל, בלי AI.', '', '## הפריטים', '']
        L += [f"- [[{item_note_rel(r)[:-3]}|{r['title']}]] — {grade_label(r) or '—'}"
              + (f" · ירדו {len(r['lost'])}" if r.get('lost') else '') for r in items]
        tops = sorted((p.get('topics') or {}).items(), key=lambda te: -_weight(te[1]))
        L += ['', '## איפה איבדתי נקודות (לפי נושא)', ''] + ([f'- {_topic_word(t, e)} — ב: {", ".join(e["items"][:4])}'
                                                              for t, e in tops] or ['- עד עכשיו בלי הורדות.'])
        w.note(cn, {'type': 'feedback_index', 'title': f'משובים — {_name(ck)}', 'course_key': ck, 'source': 'assistant',
                    'tags': ['משוב', 'MOC', ck]}, '\n'.join(L))
    return dict(w.stats)


def backfill(now: datetime | None = None, session=None) -> dict:
    """Read every course's grades now with the existing login flow and record them silently (the profile starts real)."""
    if session is None:
        from collectors.moodle_assignment_scraper import login_and_get_session
        session = login_and_get_session()
    return check(session, now)                        # the very first ingest is silent by itself


if __name__ == '__main__':
    import argparse
    import sys
    sys.path.insert(0, str(ROOT))
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill', action='store_true')
    ap.add_argument('--profile', action='store_true')
    a = ap.parse_args()
    if a.backfill:
        print(json.dumps(backfill(), ensure_ascii=False))
    if a.profile or not a.backfill:
        print(json.dumps(profile(), ensure_ascii=False, indent=1))
        print(profile_text())
