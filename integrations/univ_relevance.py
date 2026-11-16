"""
integrations/univ_relevance.py — does a university update concern the owner's OWN groups? (15/11)

The owner got "📬 עדכונים מהאוניברסיטה" about the SUNDAY macro tutorial groups merging on 22.11 (room בניין לדוגמה 210, Eitan); his
macro tutorial is Tuesday 14:00 with Avner. "אני רוצה שהודעות שלא רלוונטיות אליי אל תשלח".

    his groups   config/courses.yaml my_sessions + data/tau_calendar.json (kind, group, weekday, hours, lecturer, room)
    the others   courses.yaml other_staff / my_staff, panopto other_group_hosts, Panopto metadata hosts (other_group)
    rule first   every sentence of the update (subject first) is read for a day / date / today / tomorrow / hours /
                 group number / person / room, with the session kind it talks about, and checked against his sessions:
                   names his group / day / hour / tutor / room, or "all the groups"      → mine     → relevant
                   another tutor on HIS day/hour                                         → unsure   → sent + one line
                   names only another day / hour / group (or the subject itself does)    → other    → not sent
                   a person only (another lecturer, no day/hour)                         → the triage model decides
                   nothing specific (exam, homework, material, grades, forms)            → course-wide → relevant
    the model    only then: the existing triage call (api_only) gets his timetable and answers relevant yes/no/unsure.
                 No extra LLM call when the rule decides.
    log          what wasn't sent → data/univ_filtered.jsonl (why, how); "מה סיננת?" and the weekly report read it.
Mail and Moodle text are someone else's words: only read, never followed. No LLM here.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILTERED = ROOT / 'data' / 'univ_filtered.jsonl'
WD_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
KIND_HE = {'lecture': 'להרצאה', 'tutorial': 'לתרגול'}
CODE_DAYS = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
ALIASES = {'software_1': ['Intro2SE', 'Software Engineering', 'מבוא להנדסת תוכנה']}

# ── reading a sentence ──────────────────────────────────────────────────────

_DAY = r'(ראשון|שני|שלישי|רביעי|חמישי|שישי)'
DAY_HE_RX = re.compile(r'(?<![א-ת])(?:[בלמשו]|של\s+|מ)?(?:יום|ימי|יומי)\s+' + _DAY + r'(?![א-ת])'
                       r'((?:\s*(?:,|ו-?|או)\s*(?:יום\s+|ה)?(?:ראשון|שני|שלישי|רביעי|חמישי|שישי)(?![א-ת]))*)')
DAY_HE = {'ראשון': 6, 'שני': 0, 'שלישי': 1, 'רביעי': 2, 'חמישי': 3, 'שישי': 4}
DAY_LETTER_RX = re.compile(r'(?<![א-ת])(?:[בלמשו]|של\s+|מ)?(?:יום|ימי)\s+([אבגדהו])[׳\']')     # "החל מיום ג'"
DAY_LETTER = {'א': 6, 'ב': 0, 'ג': 1, 'ד': 2, 'ה': 3, 'ו': 4}
WEEK_OF_RX = re.compile(r'(?:שבוע|week)(?:\s+(?:של|of|starting|from|ה|מ))?\s*[-–]?\s*$', re.I)   # "בשבוע של 18/10": no day
CODE_GROUP_RX = re.compile(r'(?<!\d)\d{7,8}-(\d{2})(?!\d)')                       # "03682157-03": course number-group
PLURAL_RX = re.compile(r'(?<![א-ת])(?:ו|ש|ב|כ)?(?:ה)?(?:תרגולים|הרצאות|שיעורים)(?![א-ת])|\bthe\s+(?:recitations|tutorials|lectures|classes)\b', re.I)
REPLY_RX = re.compile(r'(?:^|[:\-]\s*)(?:תשובה ל|השב|re|fwd?|הועבר)\s*:', re.I)
DAY_EN_RX = re.compile(r'\b(sun|mon|tue|tues|wed|wednes|thu|thur|thurs|fri)(?:day)?s?\b', re.I)
DAY_EN = {'sun': 6, 'mon': 0, 'tue': 1, 'tues': 1, 'wed': 2, 'wednes': 2, 'thu': 3, 'thur': 3, 'thurs': 3, 'fri': 4}
REL_RX = [(re.compile(r'(?<![א-ת])(?:ו|ש|ל)?(?:היום|הערב)(?![א-ת])|\btoday\b|\btonight\b', re.I), 0),
          (re.compile(r'(?<![א-ת])(?:ו|ש|ל)?מחר(?![א-ת])|\btomorrow\b', re.I), 1),
          (re.compile(r'(?<![א-ת])(?:ו|ש)?אתמול(?![א-ת])|\byesterday\b', re.I), -1)]
DATE_RX = re.compile(r'(?<![\d:])(\d{1,2})[./](\d{1,2})(?:[./](\d{2,5}))?(?![\d:])')
HM_RX = re.compile(r'(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?!:\d)(?:\s*(?:-|–|עד|to|until)\s*([01]?\d|2[0-3]):([0-5]\d))?')
HHMM_RX = re.compile(r'(?<!\d)([01]\d|2[0-3])([0-5]0)\s*[-–]\s*([01]\d|2[0-3])([0-5]0)(?!\d)')
BARE_RX = re.compile(r'(?<![\d:./])([7-9]|1\d|2[0-2])\s*[-–]\s*([8-9]|1\d|2[0-3])(?![\d:./])')
AT_RX = re.compile(r'(?:בשעה|בשעות|משעה|at|from)\s+([7-9]|1\d|2[0-2])(?![\d:])', re.I)
NOT_HOURS_RX = re.compile(r'(?:שקפים|שקף|slides?|שאלות|שאלה|questions?|עמודים|עמ[׳\']|pages?|פרקים|chapters?|סעיפים)\s*$', re.I)
GROUP_RX = re.compile(r'(?:קבוצה|קבוצת|קב[׳\']|group|section)\s*(?:מס[׳\'.]?\s*|no\.?\s*|#)?(\d{1,2})(?!\d)', re.I)
GROUPS_RX = re.compile(r'(?:groups|קבוצות|sections)\s*\(?\s*(\d{1,2})((?:\s*(?:,|and|&|ו-?)\s*(?:group\s*)?\d{1,2}(?!\d))*)', re.I)
ROOM_RX = re.compile(r'(?:חדר|כיתה|כיתת|בכיתה|בחדר|room|hall)\s*(?:מס[׳\']?\s*)?(\d{2,4})(?!\d)', re.I)
LECT_RX = re.compile(r'הרצא|שיעור(?!י\s*(?:ה)?בית)|\blectures?\b|\bclass(?:es)?\b|active learning|למידה פעילה', re.I)
TUT_RX = re.compile(r'תרגול|מתרגל|\brecitations?\b|\btutorials?\b|(?-i:\bTAs?\b)', re.I)
GROUPWORD_RX = re.compile(r'קבוצ|\bgroups?\b|\bsections?\b', re.I)
ALL_RX = re.compile(r'כל\s+(?:ה)?(?:תרגולים|קבוצות|הרצאות|שיעורים|מפגשים)|כלל\s+(?:ה)?(?:קבוצות|תרגולים|הרצאות)|'
                    r'לכל\s+(?:ה)?קבוצות|שתי\s+הקבוצות|בכל\s+(?:ה)?(?:קבוצות|תרגולים)|'
                    r'\ball\s+(?:the\s+)?(?:groups|recitations|tutorials|sections|lectures|classes)\b|\bboth\s+groups\b|'
                    r'\bevery\s+(?:group|recitation|tutorial|section)\b', re.I)
STRONG_RX = re.compile(r'מבחן|בחינ|בוחן|\bquiz|\bexam|midterm|הגש|להגיש|\bdue\s+(?:on|by|date|at|\d)|\bis\s+due\b|deadline|'
                       r'submission|assignment|homework|'
                       r'שיעורי\s*(?:ה)?בית|תרגיל(?:י)?\s*(?:ה)?בית|מטל|ציון|ציונים|\bgrade|ערעור|appeal', re.I)
CONTENT_RX = re.compile(STRONG_RX.pattern + r'|הקלט|recording|מצגת|שקפים|slides|חומר|material|סילבוס|syllabus|טופס|\bform\b|'
                        r'קבלת קהל|קבלות קהל|שעות קבלה|office hour|פורום|forum|panopto|פנופטו|זום|zoom|מילואים', re.I)
OFFICE_RX = re.compile(r'קבלת קהל|קבלות קהל|שעות קבלה|office hour', re.I)
CHANGE_RX = re.compile(r'בוטל|מבוטל|יבוטל|מתבטל|לבטל|ביטול|cancel|יעבור|עובר|הוזז|יוזז|נדחה|במקום|instead|חדר|room|'
                       r'כיתה|יתקיים|יתקיימו|will take place|ישתנה|שינוי|שנוי|change|מאוחד|ביחד|together|merge|substitut|'
                       r'מחליף|יועבר|taught by|zoom only|בזום|לא יהיה|לא תתקיים|no (?:lecture|recitation|class)', re.I)
NO_CHANGE_RX = re.compile(r'לא משתנ|לא מושפע|כרגיל|ללא שינוי|אין שינוי|לא נוגע|לא רלוונטי|לא קשור|no change|unchanged|as usual', re.I)
GREET_RX = re.compile(r'^\s*(?:שלום|היי|הי|בוקר טוב|ערב טוב|בברכה|תודה|נתראה|hi|hello|dear|best|regards|thanks|cheers)\b', re.I)
TITLE_RX = re.compile(r'^(?:מר|גב[׳\']?|ד["״]ר|פרופ[׳\']?|prof\.?|dr\.?|mr\.?|ms\.?)$', re.I)


def _strip(word: str) -> str:
    return re.sub(r'^[׳\'"״]+|[׳\'"״.,:;()]+$', '', word)


def _name_keys(name: str) -> list[str]:
    """'מר אבנר כהנוביץ' → ['אבנר', 'כהנוביץ']: the first name, and the last one when it can't be an everyday word
    ('גל' is a wave, 'אילת' a city): 5+ letters."""
    toks = [t for t in (_strip(w) for w in re.split(r'\s+', name or '')) if t and not TITLE_RX.match(t)]
    if not toks:
        return []
    keys = [toks[0]] + [t for t in toks[1:] if len(t) >= 5]
    return [k for k in keys if len(k) >= 2]


def _name_in(key: str, text: str) -> bool:
    if re.search(r'[A-Za-z]', key):
        return re.search(rf'\b{re.escape(key)}\b', text, re.I) is not None
    return re.search(rf'(?<![א-ת])[ולבהמש]?{re.escape(key)}(?![א-ת])', text) is not None


def _mins(hh, mm='0') -> int:
    return int(hh) * 60 + int(mm or 0)


def _resolve_date(d: int, m: int, ref: date) -> date | None:
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            c = date(y, m, d)
        except ValueError:
            continue
        if best is None or abs((c - ref).days) < abs((best - ref).days):
            best = c
    return best if best and abs((best - ref).days) <= 75 else None


def read(seg: str, ref: date, relative: bool = True) -> dict:
    """One sentence → what it pins down. Pure. relative=False: 'היום' / 'מחר' don't count (a reply's subject keeps the
    first post's "מחר", written a day earlier)."""
    t = seg or ''
    days, dates = set(), []
    for m in DAY_HE_RX.finditer(t):
        days.add(DAY_HE[m.group(1)])
        days |= {DAY_HE[x] for x in re.findall(_DAY, m.group(2) or '')}
    for m in DAY_EN_RX.finditer(t):
        days.add(DAY_EN[m.group(1).lower()])
    for m in DAY_LETTER_RX.finditer(t):
        days.add(DAY_LETTER[m.group(1)])
    for rx, off in REL_RX:
        if relative and rx.search(t):
            dd = ref + timedelta(days=off)
            dates.append(dd)
            days.add(dd.weekday())
    for m in DATE_RX.finditer(t):
        d, mo = int(m.group(1)), int(m.group(2))
        if WEEK_OF_RX.search(t[max(0, m.start() - 16):m.start()]):
            continue                                            # 18/10 "התרגולים בשבוע של 18/10 בוטלו": the week, all days
        if 1 <= d <= 31 and 1 <= mo <= 12 and (dd := _resolve_date(d, mo, ref)):
            dates.append(dd)
            days.add(dd.weekday())
    hours, taken = [], []
    for m in HM_RX.finditer(t):
        a = _mins(m.group(1), m.group(2))
        b = _mins(m.group(3), m.group(4)) if m.group(3) else None
        hours.append((a, b if b and b > a else None))
        taken.append(m.span())
    for m in HHMM_RX.finditer(t):
        a, b = _mins(m.group(1), m.group(2)), _mins(m.group(3), m.group(4))
        if b > a:
            hours.append((a, b))
            taken.append(m.span())
    free = lambda sp: not any(s <= sp[0] < e for s, e in taken)
    for m in BARE_RX.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        if free(m.span()) and 1 <= b - a <= 4 and not NOT_HOURS_RX.search(t[max(0, m.start() - 14):m.start()]):
            hours.append((a * 60, b * 60))
            taken.append(m.span())
    for m in AT_RX.finditer(t):
        if free(m.span(1)):
            hours.append((int(m.group(1)) * 60, None))
    groups = {int(g) for g in GROUP_RX.findall(t)} | {int(g) for g in CODE_GROUP_RX.findall(t)}
    for m in GROUPS_RX.finditer(t):
        groups |= {int(x) for x in re.findall(r'\d{1,2}', m.group(0))}
    kinds = ({'lecture'} if LECT_RX.search(t) else set()) | ({'tutorial'} if TUT_RX.search(t) else set())
    return {'days': days, 'dates': sorted(set(dates)), 'hours': hours, 'groups': groups,
            'rooms': set(ROOM_RX.findall(t)), 'kinds': kinds,
            'groupword': bool(GROUPWORD_RX.search(t) or CODE_GROUP_RX.search(t)),
            'all': bool(ALL_RX.search(t)), 'plural': bool(PLURAL_RX.search(t)),
            'strong': bool(STRONG_RX.search(t)), 'content': bool(CONTENT_RX.search(t)),
            'office': bool(OFFICE_RX.search(t)), 'change': bool(CHANGE_RX.search(t))}


# ── his sessions and the people around them ─────────────────────────────────

def _yaml() -> dict:
    import yaml
    try:
        return yaml.safe_load((ROOT / 'config' / 'courses.yaml').read_text(encoding='utf-8')) or {}
    except (OSError, ValueError):
        return {}


def _data_dir() -> Path:
    try:
        from config.paths import DATA_DIR
        return Path(DATA_DIR)
    except Exception:
        return ROOT / 'data'


def _tau_events() -> list[dict]:
    try:
        return json.loads((_data_dir() / 'tau_calendar.json').read_text(encoding='utf-8')).get('events') or []
    except (OSError, ValueError, AttributeError):
        return []


def _hm(s) -> int | None:
    m = re.match(r'(\d{1,2}):(\d{2})', str(s or ''))
    return _mins(m.group(1), m.group(2)) if m else None


def _label(s: dict) -> str:
    who = (_name_keys(s['people'][0]) or [''])[0] if s.get('people') else ''
    return f"{WD_HE[s['wd']]} {s['start'] // 60:02d}:{s['start'] % 60:02d}" + (f', {who}' if who else '')


def load_sessions(cfg: dict | None = None, events: list[dict] | None = None) -> dict[str, list[dict]]:
    """{course_key: [{kind, wd, start, end, group, rooms, people, label}]} — the university site first (it has the
    lecturer and the real room), the config's my_sessions as the fallback for a course the site doesn't show."""
    cfg = _yaml() if cfg is None else cfg
    courses = cfg.get('courses', cfg) or {}
    try:
        from config.paths import ACTIVE_COURSES
    except Exception:
        ACTIVE_COURSES = list(courses)
    events = _tau_events() if events is None else events
    out: dict[str, list[dict]] = {}
    for ck in ACTIVE_COURSES:
        c = courses.get(ck) or {}
        code = str(c.get('course_code') or '').lstrip('0')
        sess, seen = [], set()
        for e in events:
            if not code or str(e.get('course_number') or '').lstrip('0') != code:
                continue
            kind = 'lecture' if 'שיעור' in (e.get('kind') or '') else 'tutorial' if 'תרגי' in (e.get('kind') or '') else ''
            start, end = _hm(e.get('time')), _hm(e.get('end_time'))
            try:
                wd = date.fromisoformat(e['date']).weekday()
            except (KeyError, ValueError):
                continue
            if not kind or start is None or (kind, wd, start) in seen:
                continue
            seen.add((kind, wd, start))
            g = re.sub(r'\D', '', str(e.get('group') or ''))
            sess.append({'kind': kind, 'wd': wd, 'start': start, 'end': end or start + 60, 'group': int(g) if g else None,
                         'rooms': set(re.findall(r'\d{2,4}', str(e.get('room') or ''))),
                         'people': [e['lecturer']] if e.get('lecturer') else []})
        if not sess:
            for s in c.get('my_sessions') or []:
                wd, start = CODE_DAYS.get(str(s.get('day'))), _hm(s.get('start'))
                if wd is None or start is None:
                    continue
                g = re.sub(r'\D', '', str(s.get('group') or ''))
                sess.append({'kind': 'lecture' if s.get('type') == 'lecture' else 'tutorial', 'wd': wd, 'start': start,
                             'end': _hm(s.get('end')) or start + 60, 'group': int(g) if g else None,
                             'rooms': set(re.findall(r'\d{2,4}', str(s.get('location') or ''))), 'people': []})
        for s in sess:
            s['label'] = _label(s)
        if sess:
            out[ck] = sess
    return out


def load_roster(cfg: dict | None = None, data_dir: Path | None = None) -> dict[str, dict[str, list[str]]]:
    """{course_key: {'mine': [names], 'other': [names]}} — other groups' tutors / lecturers: courses.yaml other_staff and
    panopto other_group_hosts, and the Zoom hosts the Panopto picker marked as another group's (other_group)."""
    cfg = _yaml() if cfg is None else cfg
    courses = cfg.get('courses', cfg) or {}
    dd = data_dir or _data_dir()
    out = {}
    for ck, c in courses.items():
        if not isinstance(c, dict):
            continue
        mine = list(c.get('my_staff') or [])
        other = list(c.get('other_staff') or []) + list(((c.get('sources') or {}).get('panopto') or {}).get('other_group_hosts') or [])
        try:
            for x in json.loads((dd / 'panopto' / f'{ck}_metadata.json').read_text(encoding='utf-8')):
                if isinstance(x, dict) and x.get('host') and x.get('other_group'):
                    other.append(x['host'])
        except (OSError, ValueError, TypeError):
            pass
        if mine or other:
            out[ck] = {'mine': mine, 'other': sorted(set(other) - set(mine))}
    return out


_CACHE: dict = {}


def sessions() -> dict[str, list[dict]]:
    if 'sessions' not in _CACHE or datetime.now() - _CACHE.get('at', datetime.min) > timedelta(minutes=30):
        _CACHE.update(sessions=load_sessions(), roster=load_roster(), at=datetime.now())
    return _CACHE['sessions']


def roster() -> dict:
    sessions()
    return _CACHE['roster']


def my_slot(ck: str, kinds=None, sess: dict | None = None) -> str:
    """'שלישי 14:00, אבנר' — his session(s) of that course (of that kind when known)."""
    ss = (sess if sess is not None else sessions()).get(ck) or []
    pick = [s for s in ss if not kinds or s['kind'] in kinds] or ss
    return ' / '.join(s['label'] for s in pick)


def timetable_text(sess: dict | None = None) -> str:
    """For the triage prompt: his own groups, one line per course."""
    from config.paths import COURSE_NAMES
    sess = sessions() if sess is None else sess
    rows = []
    for ck, ss in sess.items():
        parts = []
        for s in ss:
            k = 'הרצאה' if s['kind'] == 'lecture' else 'תרגול'
            parts.append(f"{k} יום {WD_HE[s['wd']]} {s['start'] // 60:02d}:{s['start'] % 60:02d}–{s['end'] // 60:02d}:"
                         f"{s['end'] % 60:02d}" + (f" קב׳ {s['group']:02d}" if s['group'] else '')
                         + (f" ({s['people'][0]})" if s['people'] else '') + (f" חדר {'/'.join(sorted(s['rooms']))}" if s['rooms'] else ''))
        rows.append(f"• {COURSE_NAMES.get(ck, ck)}: " + '; '.join(parts))
    return '\n'.join(rows)


# ── which course ────────────────────────────────────────────────────────────

def course_of(text: str, cfg: dict | None = None) -> str | None:
    """The active course an update is about: its university number (Moodle mail subjects: '1011210199 - מאקרו…'),
    else its name. Two courses named → None (can't tell)."""
    from config.paths import ACTIVE_COURSES, COURSE_ALT_NAMES, COURSE_NAMES
    cfg = _yaml() if cfg is None else cfg
    courses = cfg.get('courses', cfg) or {}
    t = (text or '').replace('׳', "'")
    for ck in ACTIVE_COURSES:
        code = str((courses.get(ck) or {}).get('course_code') or '')
        if code.isdigit() and re.search(rf'(?<!\d)0?{code.lstrip("0")}\d{{0,2}}(?!\d)', t):
            return ck
    hits = set()
    for ck in ACTIVE_COURSES:
        for n in [COURSE_NAMES.get(ck, ''), *COURSE_ALT_NAMES.get(ck, []), *ALIASES.get(ck, [])]:
            n = (n or '').replace('׳', "'")
            if n and (n in t or n.replace("'", '') in t.replace("'", '')):
                hits.add(ck)
    return hits.pop() if len(hits) == 1 else None


# ── the rule ────────────────────────────────────────────────────────────────

def _overlap(h: tuple, s: dict) -> bool:
    a, b = h
    return s['start'] <= a < s['end'] if b is None else (a < s['end'] and b > s['start'])


def _persons(seg: str, names: list[str]) -> bool:
    return any(_name_in(k, seg) for n in names for k in _name_keys(n))


def judge(r: dict, seg: str, ss: list[dict], ros: dict) -> tuple[str, dict | None]:
    """One read sentence against his sessions → ('mine'|'other'|'unsure'|'person'|'neutral', the session)."""
    cands = [s for s in ss if not r['kinds'] or s['kind'] in r['kinds']] or ss
    if r['groups']:
        gm = [s for s in cands if s['group'] in r['groups']]
        if gm:
            return 'mine', gm[0]
        if not r['all']:
            return 'other', None
    if r['all'] and (not r['days'] or any(s['wd'] in r['days'] for s in cands)):
        return 'mine', cands[0] if cands else None
    if r['plural'] and not (r['days'] or r['hours'] or r['groups']) and not _persons(seg, ros.get('other') or []):
        return 'mine', cands[0] if cands else None             # "התרגולים השבוע בוטלו" — all of them, his too
    pool = cands
    if r['days']:
        pool = [s for s in cands if s['wd'] in r['days']]
        if not pool:
            return 'other', None
    if r['hours']:
        pool = [s for s in pool if any(_overlap(h, s) for h in r['hours'])]
        if not pool:
            return 'other', None
    mine_p = [s for s in pool if _persons(seg, s['people'])] or (pool[:1] if _persons(seg, ros.get('mine') or []) else [])
    if mine_p:
        return 'mine', mine_p[0]
    if _persons(seg, ros.get('other') or []):
        return ('unsure', pool[0]) if (r['days'] or r['hours']) else ('person', None)
    if r['rooms'] and any(r['rooms'] & s['rooms'] for s in pool):
        return 'mine', next(s for s in pool if r['rooms'] & s['rooms'])
    if r['days'] or r['hours']:
        return 'mine', pool[0]
    return 'neutral', None


def _segments(subject: str, body: str) -> list[tuple[str, str]]:
    out = [('subject', re.sub(r'^\s*\d{6,}\s*-\s*', '', subject or '').strip())]
    for line in (body or '').splitlines():
        line = line.strip()
        if not line:
            out.append(('break', ''))
            continue
        if line.startswith('(כתב/ה:') or line.startswith('[פתח ב-Moodle]') or re.fullmatch(r'-{3,}', line):
            continue
        out += [('body', s.strip()) for s in re.split(r'(?<=[.!?])\s+', line) if s.strip()]
    return out


def _desc(r: dict, seg: str) -> str:
    kind = 'תרגול' if r['kinds'] == {'tutorial'} else 'הרצאה' if r['kinds'] == {'lecture'} else 'מפגש'
    bits = []
    if r['days']:
        bits.append('ביום ' + '/'.join(WD_HE[d] for d in sorted(r['days'])))
    if r['dates']:
        bits.append(r['dates'][0].strftime('%d/%m'))
    if r['hours']:
        a = r['hours'][0][0]
        bits.append(f'{a // 60:02d}:{a % 60:02d}')
    if r['groups']:
        bits.append('קב׳ ' + ','.join(f'{g:02d}' for g in sorted(r['groups'])))
    return f"{kind} {' '.join(bits)}".strip()


def classify(ck: str | None, subject: str, body: str, ref: date | None = None, sess: dict | None = None,
             ros: dict | None = None) -> dict:
    """→ {rel: 'yes'|'no'|'unsure'|'na'|None, why, kinds, mine (the sentence that names his session), change}.
    None = the rule can't tell: the triage model decides. 'na' = not about one of his courses."""
    ref = ref or date.today()
    sess = sessions() if sess is None else sess
    ros = roster() if ros is None else ros
    text = f'{subject}\n{body}'
    if not ck:
        r = read(text, ref)
        if (r['kinds'] or r['groupword']) and (r['days'] or r['hours'] or r['groups']) and sess:
            return {'rel': None, 'why': 'לא ברור באיזה קורס', 'kinds': r['kinds']}
        return {'rel': 'na', 'why': ''}
    ss = sess.get(ck) or []
    if not ss:
        return {'rel': 'na', 'why': ''}
    cr = ros.get(ck) or {}
    states, ctx_days, ctx_kinds = [], None, set()
    kinds_seen = set()
    for where, seg in _segments(subject, body):
        if where == 'break':                    # 20/10 "my recitation of 12-13 is cancelled ⏎⏎ come to my other one of
            continue                            # 16-17": that one is on the same (other) day — context carries on
        r = read(seg, ref, relative=not (where == 'subject' and REPLY_RX.search(seg)))
        own_days, own_kinds = set(r['days']), set(r['kinds'])
        specific = bool(r['days'] or r['hours'] or r['groups'] or r['rooms'] or r['all'] or r['plural'] or
                        _persons(seg, (cr.get('mine') or []) + (cr.get('other') or []) + [p for s in ss for p in s['people']]))
        if not r['kinds'] and ctx_kinds:
            r['kinds'] = set(ctx_kinds)
        if not r['days'] and ctx_days and (r['hours'] or r['groups']):
            r['days'] = set(ctx_days)
        sessiony = bool(r['kinds'] or r['groupword'])
        s = None
        short = where == 'body' and len(seg.split()) <= 4
        if short and ((not own_kinds and not own_days and not r['hours']) or GREET_RX.match(seg)):
            state = 'sig'                                       # "בברכה, יוסי" / "Igor" / "נתראה מחר,"
        elif r['strong'] and not (r['groups'] or (r['groupword'] and own_days)):
            state = 'content'                                   # exam / homework / deadline: everyone's
        elif specific and sessiony:
            state, s = judge(r, seg, ss, cr)
        elif r['content'] and not GREET_RX.match(seg):
            state = 'content'
        else:
            state = 'neutral'
        if state == 'content' and r['office'] and _persons(seg, cr.get('other') or []) \
                and not _persons(seg, (cr.get('mine') or []) + [p for x in ss for p in x['people']]):
            state = 'other'                                     # "Yaron's office hours…": another tutor's
        states.append((where, state, seg, r, s))
        kinds_seen |= r['kinds']
        if own_days:
            ctx_days = own_days
        if own_kinds:
            ctx_kinds = own_kinds
    pick = lambda st: [x for x in states if x[1] == st]
    if (m := pick('mine')):
        ch = next((x for x in m if x[3]['change'] and not NO_CHANGE_RX.search(x[2])), None)
        _, _, seg, r, s = ch or m[0]                            # the sentence that changes HIS session speaks first
        return {'rel': 'yes', 'kinds': r['kinds'], 'mine': seg, 'change': seg if ch else '', 'slot': s['label'] if s else '',
                'why': f"נוגע {KIND_HE.get(s['kind'], 'למפגש')} שלך ({s['label']})" if s else 'נוגע לקבוצה שלך'}
    slot = my_slot(ck, kinds_seen, sess)
    if (u := pick('unsure')):
        return {'rel': 'unsure', 'kinds': u[0][3]['kinds'], 'slot': my_slot(ck, u[0][3]['kinds'], sess),
                'why': 'מתרגל/מרצה אחר ביום/בשעה שלך'}
    others = pick('other')
    subj = next((x for x in states if x[0] == 'subject'), None)
    if subj and subj[1] == 'other':
        return {'rel': 'no', 'kinds': subj[3]['kinds'], 'why': f"{_desc(subj[3], subj[2])} — שלך: {slot}"}
    if others:
        if pick('content'):
            return {'rel': 'yes', 'kinds': kinds_seen, 'why': 'כללי לקורס (חלק ממנו לקבוצה אחרת)'}
        # another group's day/hour, and otherwise only other staff named (08/11 "taught by Igor instead of Yaron:
        # 11/11 10:10…") — nothing of his
        return {'rel': 'no', 'kinds': others[0][3]['kinds'], 'why': f"{_desc(others[0][3], others[0][2])} — שלך: {slot}"}
    if pick('person'):
        return {'rel': None, 'kinds': kinds_seen, 'why': 'מוזכר אדם מצוות הקורס, בלי יום/שעה'}
    return {'rel': 'yes', 'kinds': kinds_seen, 'why': 'כללי לקורס'}


def for_me(ck: str | None, text: str, ref: date | None = None, **kw) -> bool:
    """False only when the rule is sure it's another group's (a quiz / submission box for group 16, Wednesday's…)."""
    try:
        return classify(ck, text, '', ref, **kw)['rel'] != 'no'
    except Exception as e:
        print(f'  ⚠️ relevance: {e!r}'[:200])
        return True


def ref_date(v) -> date:
    """The day an update was written ('היום' / 'מחר' in it mean that day's): an ISO date, an RFC-2822 Date header."""
    s = str(v or '').strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
        if d.tzinfo:
            from zoneinfo import ZoneInfo
            d = d.astimezone(ZoneInfo('Asia/Jerusalem'))
        return d.date()
    except (TypeError, ValueError, IndexError):
        return date.today()


def unsure_line(slot: str) -> str:
    return f'❓ לא בטוח שזה נוגע לקבוצה שלך ({slot})' if slot else '❓ לא בטוח שזה נוגע לקבוצה שלך'


def affects_line(text: str) -> str:
    """The model's / the rule's "how it changes HIS session" — nothing when it says nothing of his changes."""
    t = re.sub(r'\s+', ' ', str(text or '')).strip()
    return '' if not t or NO_CHANGE_RX.search(t) else t


# ── what wasn't sent ────────────────────────────────────────────────────────

def log_filtered(row: dict, path: Path | None = None) -> None:
    p = path or FILTERED
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': datetime.now().isoformat(timespec='seconds'), **row}, ensure_ascii=False) + '\n')
    except OSError as e:
        print(f'  ⚠️ filtered log: {e!r}'[:200])


def filtered(days: int = 7, path: Path | None = None, now: datetime | None = None) -> list[dict]:
    since = ((now or datetime.now()) - timedelta(days=days)).isoformat()
    try:
        rows = [json.loads(l) for l in (path or FILTERED).read_text(encoding='utf-8').splitlines() if l.strip()]
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and str(r.get('ts', '')) >= since]


WHAT_RX = re.compile(r'^\s*(?:ו?מה|אילו|איזה|תראה(?: לי)?(?: מה)?)\s+(?:הודעות\s+|עדכונים\s+)?(?:כבר\s+)?'
                     r'(?:סיננת|סיננתי|לא שלחת|לא שלחת לי|הסתרת|זרקת)(?:\s+לי)?(?:\s+(?:השבוע|מהאוניברסיטה|מהמודל|מהמייל))*\s*[?!.]*\s*$')


def filtered_text(days: int = 7, path: Path | None = None, now: datetime | None = None) -> str:
    rows = filtered(days, path, now)
    if not rows:
        return f'🔇 ב-{days} הימים האחרונים לא סיננתי שום עדכון מהאוניברסיטה — כל מה שהגיע נשלח.'
    from config.paths import COURSE_NAMES
    lines = []
    for r in rows[-12:]:
        when = str(r.get('ts', ''))[8:10] + '/' + str(r.get('ts', ''))[5:7]
        course = COURSE_NAMES.get(r.get('course') or '', '')
        lines.append(f"• {when} {course + ' — ' if course else ''}{str(r.get('subject') or '')[:80]}"
                     + (f"\n   ↳ {r['why']}" if r.get('why') else ''))
    more = f'\n…ועוד {len(rows) - 12}' if len(rows) > 12 else ''
    return (f'🔇 *לא שלחתי לך {len(rows)} עדכונים* ({days} ימים) — הם על קבוצה אחרת, לא שלך:\n'
            + '\n'.join(lines) + more + '\n\nאם אחד מהם כן נוגע אליך — תגיד לי ואתקן.')
