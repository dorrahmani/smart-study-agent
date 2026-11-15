"""
collectors/weekly_pick.py — one lecture and one tutorial per course per week, his own first (11/11/26).

The owner: "יש כמה הרצאות ותרגולים שעולים לפעמים בימים שונים, אין מקצוע שיש לי 2 הרצאות או 2 תרגולים בשבוע".
Every course has exactly one lecture and one tutorial of his a week (Sun–Sat, Israel time), but his class is sometimes
held / recorded on another day (a makeup, a moved session). Per course, per type, per week ONE recording, chosen:
  slot       — in his weekly slot (my.tau / courses.yaml), or in a dated one-off slot of his group from the calendar;
               two in the slot → the one naming his tutor, else the closest start
  my_tutor   — that week, by HIS lecturer / tutor on another day or time (his name in the title, or the Zoom host in
               the recording's description — "Host: Reuven Almoni" for "מר ראובן אלמוני"); a recording of his tutor made
               BEFORE a recorded slot of the next week, on another day, is the makeup of the previous week when that
               week has none (macro: Reuven's Sunday 1/11 recording after the Tuesday 27/10 that had no classes)
  makeup     — a title saying it is a makeup (השלמה / מוחלף / makeup / rescheduled) and not of another group
  other_group — only then, once his slot of that week is over: another group's recording of the same type, never one
               much shorter than the class (a 10-minute slides clip is not a lecture)
Across nightly runs: what is already in data/panopto (metadata + SRTs) and data/summaries for that week counts. A week
that has his recording gets nothing more; a week that has only another group's (or an extra of the same tutor) gets
his when it appears, and the old one moves aside to replaced/ (never deleted) — never two. No LLM.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

TUT_RX = r'תרגול|תרגיל|recitation|tirgul'
LEC_RX = r'הרצאה|lecture|שיעור'
MAKEUP_RX = re.compile(r'השלמ|מוחלפ|מוחלף|הוחלף|במקום|נדח|make[\s-]?up|re-?scheduled|replacement', re.I)
GROUP_RX = re.compile(r'(?:קבוצה|קב[׳\']|group)\s*0*(\d{1,2})\b', re.I)
STANDIN = '(מהמצגת)'
MINE = ('slot', 'my_tutor', 'makeup')
# an existing item of one of these may be replaced by his own recording of that week
REPLACEABLE = ('other_group', 'other', 'extra', 'short', 'not_type', 'unknown_type')
_DAY = {'sun': 6, 'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5}


def week0(d) -> date:
    """The Sunday of the Israeli week of a date / datetime."""
    d = d.date() if isinstance(d, datetime) else d
    return d - timedelta(days=(d.weekday() + 1) % 7)


def clean(name: str) -> str:
    return re.sub(r'<[^>]+>', '', name or '').strip()


def host(s: dict) -> str:
    """The Zoom host from a recording's description ('• Host: Reuven Almoni'); '' for an uploaded file."""
    m = re.search(r'Host:\s*([^\r\n•]+)', s.get('Abstract') or '')
    return m.group(1).strip() if m else ''


# ── names: Hebrew my.tau names vs the Latin Zoom host, by consonant skeleton ──
_HE = {'א': '', 'ב': 'b', 'ג': 'g', 'ד': 'd', 'ה': '', 'ו': '', 'ז': 'z', 'ח': 'k', 'ט': 't', 'י': '', 'כ': 'k',
       'ך': 'k', 'ל': 'l', 'מ': 'm', 'ם': 'm', 'נ': 'n', 'ן': 'n', 'ס': 's', 'ע': '', 'פ': 'p', 'ף': 'p', 'צ': 'ts',
       'ץ': 'ts', 'ק': 'k', 'ר': 'r', 'ש': 's', 'ת': 't'}


def _squeeze(s: str) -> str:
    return re.sub(r'(.)\1+', r'\1', s)


def skeleton_he(word: str) -> str:
    return _squeeze(''.join(_HE.get(c, '') for c in word))


def skeleton_en(word: str) -> str:
    w = word.lower()
    for a, b in (('ch', 'k'), ('kh', 'k'), ('ph', 'p'), ('sh', 's'), ('tz', 'ts'), ('ck', 'k'), ('c', 'k'), ('q', 'k'),
                 ('f', 'p'), ('v', 'b'), ('w', ''), ('x', 'ks')):
        w = w.replace(a, b)
    return _squeeze(re.sub(r'[^a-z]|[aeiouyh]', '', w))


def person_tokens(lecturer: str) -> list[str]:
    """'מר ראובן אלמוני' → ['ראובן', 'אלמוני']."""
    name = re.sub(r'(פרופ|ד"ר|דר|גב|מר|Prof|Dr|Mr|Ms|Mrs)[\'׳.]*(?=\s)', ' ', f" {lecturer or ''} ")
    return [w for w in re.split(r'[\s,()\-]+', name) if len(w) >= 2]


def names(s: dict, lecturer: str) -> bool:
    """Does the recording name this person — in its title (Hebrew, as before), or as its Zoom host / a Latin word
    of its title by consonant skeleton (last name, plus the first name when the last name is that short)."""
    toks = person_tokens(lecturer)
    if not toks:
        return False
    title = clean(s.get('SessionName', ''))
    if any(t in title for t in toks):
        return True
    latin = {skeleton_en(w) for w in re.findall(r'[A-Za-z]+', f"{host(s)} {title}")} - {''}
    last, first = skeleton_he(toks[-1]), skeleton_he(toks[0]) if len(toks) > 1 else ''
    return bool(last) and last in latin and (len(last) >= 3 or (bool(first) and first in latin))


def same_person(a: str, b: str) -> bool:
    ta, tb = person_tokens(a), person_tokens(b)
    return bool(ta and tb) and skeleton_he(ta[-1]) == skeleton_he(tb[-1])


def host_key(h: str) -> str:
    """'lecturer_c' / 'yoav  lecturer_n' → one key for the same Zoom account."""
    return ' '.join(sorted(k for w in re.findall(r'[A-Za-z]+', h or '') if (k := skeleton_en(w))))


def parallel_hosts(recs: list[dict], me: str) -> set[str]:
    """14/11: two groups of macro's tutorial meet at the same hour (Tue 14:00 — Reuven's, his, and Yehuda's). In a week that
    has both, his named one wins the slot; Yehuda's is learned as the parallel group's, so a week with only Yehuda's is
    another group's too, not his slot (10/11 "תרגול שני מרצה_פ (קבוצת יום שלישי)" was taken as his)."""
    out = set()
    by_week: dict[date, list[dict]] = {}
    for r in recs:
        if r['kind'] == 'slot':
            by_week.setdefault(r['week'], []).append(r)
    for rs in by_week.values():
        if not any(r['rank'][0] == 0 for r in rs):
            continue
        for r in rs:
            h = host(r['s'])
            if r['rank'][0] == 1 and h and not (me and names(r['s'], me)):
                out.add(host_key(h))
    return out - {''}


# ── slots ──
def _mins(t) -> int | None:
    try:
        hh, mm = str(t or '').split(':')
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def slot_length(slot: dict) -> int | None:
    a, b = _mins(slot.get('start')), _mins(slot.get('end'))
    return b - a if a is not None and b and b > a else None


def occurrence(slot: dict, w0: date) -> datetime | None:
    """When this slot happens in the week starting Sunday w0 (a dated one-off slot: only in its own week)."""
    m = _mins(slot.get('start'))
    if m is None:
        return None
    if slot.get('date'):
        try:
            d = date.fromisoformat(str(slot['date'])[:10])
        except ValueError:
            return None
        if week0(d) != w0:
            return None
    else:
        wd = _DAY.get(str(slot.get('day', '')).lower()[:3])
        if wd is None:
            return None
        d = w0 + timedelta(days=(wd + 1) % 7)
    return datetime(d.year, d.month, d.day, m // 60, m % 60)


def slot_distance(dt: datetime, slot: dict, tolerance_min: int = 45) -> float | None:
    """Minutes from the slot's start when dt is inside it (min(45, half the slot)), else None."""
    occ = occurrence(slot, week0(dt))
    if not occ or occ.date() != dt.date():
        return None
    length = slot_length(slot)
    tol = min(tolerance_min, length // 2) if length else tolerance_min
    diff = abs((dt - occ).total_seconds()) / 60
    return diff if diff <= tol else None


def person_for(slots: list[dict]) -> str:
    return next((s.get('lecturer') for s in slots if s.get('lecturer')), '') or ''


# ── one recording → what it is for the owner ──
def classify(s: dict, dt: datetime, typ: str, slots: list[dict], me: str = '', other: str = '',
             my_group: str = '', tolerance_min: int = 45, foreign_hosts: set | None = None) -> tuple[str, tuple]:
    """→ (kind, rank) — kind ∈ slot | my_tutor | makeup | other | short | not_type | unknown_type.
    foreign_hosts: host_key()s of another group's lecturer / tutor — their recording in his slot is not his class
    (a parallel group at the same hour, or a class of theirs moved there)."""
    name = clean(s.get('SessionName', ''))
    length = next((slot_length(x) for x in slots if slot_length(x)), None)
    dur = float(s.get('Duration') or 0) / 60
    if dur and dur < (0.4 * length if length else 20):
        return 'short', ()
    tut = bool(re.search(TUT_RX, name, re.I))
    if typ == 'lecture' and tut or typ == 'tutorial' and re.search(r'הרצאה|lecture', name, re.I) and not tut:
        return 'not_type', ()
    if other and not same_person(me, other) and names(s, other):
        return 'not_type', ()                     # micro: Issachar's tutorials are titled with the course name only
    mine = bool(me) and names(s, me)
    foreign = not mine and bool(foreign_hosts) and host_key(host(s)) in foreign_hosts
    for x in slots if not foreign else []:
        d = slot_distance(dt, x, tolerance_min)
        if d is not None:
            return 'slot', (0 if mine else 1, d)
    m = _mins((slots[0] if slots else {}).get('start'))
    tod = abs(dt.hour * 60 + dt.minute - m) if m is not None else 0
    if mine:
        titled = any(t in name for t in person_tokens(me))
        return 'my_tutor', (0 if titled else 1, 0 if (tut or typ == 'lecture') else 1, tod, dt)
    if MAKEUP_RX.search(name):
        g = GROUP_RX.search(name)
        if not g or not my_group or g.group(1).lstrip('0') == str(my_group).lstrip('0'):
            return 'makeup', (tod, dt)
    if typ == 'tutorial' and tut or typ == 'lecture' and (re.search(LEC_RX, name, re.I) or dur >= 90):
        return 'other', ()
    return 'unknown_type', ()


def plan(sessions: list[dict], slots: list[dict], typ: str, now: datetime, parse, me: str = '', other: str = '',
         my_group: str = '', semester_start: date | None = None, tolerance_min: int = 45,
         foreign_hosts: set | None = None) -> dict:
    """→ {'weeks': {Sunday: rec}, 'recs': {id(session): rec}, 'undated': [sessions], 'foreign': host keys};
    rec = {'s', 'dt', 'kind', 'week', 'match'?}. `parse` turns a StartTime into the wall-clock datetime.
    foreign_hosts: names of another group's lecturer / tutor (config other_group_hosts); the hosts of a parallel group
    in his slot are learned on top (parallel_hosts)."""
    recs, undated = [], []
    given = {host_key(h) for h in foreign_hosts or ()} - {''}
    for s in sessions:
        dt = parse(s.get('StartTime', ''))
        if not dt:
            undated.append(s)
            continue
        kind, rank = classify(s, dt, typ, slots, me, other, my_group, tolerance_min, given)
        recs.append({'s': s, 'dt': dt, 'kind': kind, 'rank': rank, 'week': week0(dt)})
    foreign = given | parallel_hosts(recs, me)
    if foreign != given:
        for r in recs:
            r['kind'], r['rank'] = classify(r['s'], r['dt'], typ, slots, me, other, my_group, tolerance_min, foreign)
    weeks: dict[date, dict] = {}
    for r in recs:
        if r['kind'] == 'slot' and (r['week'] not in weeks or r['rank'] < weeks[r['week']]['rank']):
            weeks[r['week']] = r
    for r in recs:
        if r['kind'] == 'slot' and weeks.get(r['week']) is not r:
            r['kind'] = 'extra'                                   # a second one in his slot that week
    start_w = week0(semester_start) if semester_start else None
    off = sorted((r for r in recs if r['kind'] in ('my_tutor', 'makeup')),
                 key=lambda r: (r['kind'] != 'my_tutor', r['week'], r['rank']))
    for r in off:
        w, prev = r['week'], r['week'] - timedelta(days=7)
        if w not in weeks:
            weeks[w] = r
            continue
        cur = weeks[w]
        if (cur['kind'] == 'slot' and prev not in weeks and (start_w is None or prev >= start_w)
                and r['dt'] < cur['dt'] and r['dt'].date() != cur['dt'].date()):
            r['moved_back'] = True                                # the previous week's class, made up
            weeks[prev] = r
            continue
        r['kind'] = 'extra'
    for r in sorted((r for r in recs if r['kind'] == 'other'), key=lambda r: r['dt'], reverse=True):
        w = r['week']
        if w in weeks:
            continue
        happened = any((occ := occurrence(x, w)) and occ + timedelta(hours=3) <= now for x in slots)
        if happened:
            weeks[w] = r
    for w, r in weeks.items():
        r['match'] = 'other_group' if r['kind'] == 'other' else r['kind']
        r['assigned_week'] = w
    return {'weeks': weeks, 'recs': {id(r['s']): r for r in recs}, 'undated': undated, 'foreign': foreign}


def usual_weeks(p: dict, now: datetime, semester_start: date | None) -> tuple[set, list]:
    """(weeks his own recording covers, the semester's finished weeks) — is his class usually recorded at all?"""
    cur = week0(now)
    start = week0(semester_start + timedelta(days=1)) if semester_start else min(p['weeks'], default=cur)  # a Saturday
    past = []
    w = start
    while w < cur:
        past.append(w)
        w += timedelta(days=7)
    return {w for w, r in p['weeks'].items() if r['match'] in MINE and w in past}, past


# ── what's already there ──
def _is_tut_title(t: str) -> bool:
    return bool(re.search(TUT_RX, t, re.I))


def _summary_of(ck: str, d: str, title: str, summaries: Path) -> Path | None:
    safe = re.sub(r'[<>:"/\\|?*]', '', title).strip()          # as AISummarizer.build_summary_path
    for sub in ('tutorials', 'lectures'):
        p = summaries / ck / sub / f"{d} - {safe}.md"
        if p.exists():
            return p
    return None


def coverage(ck: str, typ: str, panopto: Path, summaries: Path, meta: list[dict]) -> list[dict]:
    """This course's {lecture|tutorial} items still in place: metadata entries whose SRT is in data/panopto, SRTs
    without metadata (the lecturer's site, by hand), summaries without a live SRT. Moved-aside / stand-in ones aren't."""
    items, srts = [], set()
    for m in meta or []:
        if m.get('type') != typ or m.get('replaced_by') or not m.get('srt_path') or not m.get('date'):
            continue
        srt = panopto / Path(str(m['srt_path']).replace('\\', '/')).name
        if not srt.exists():
            continue
        srts.add(srt.name)
        try:
            wk = date.fromisoformat(m['week']) if m.get('week') else week0(date.fromisoformat(m['date'][:10]))
        except ValueError:
            continue
        title = srt.stem[len(ck) + 12:]
        items.append({'week': wk, 'video_id': m.get('video_id'), 'srt': srt, 'date': m['date'][:10], 'title': title,
                      'md': _summary_of(ck, m['date'][:10], title, summaries), 'other_group': bool(m.get('other_group')),
                      'match': m.get('match')})
    for srt in sorted(panopto.glob(f'{ck}_*.srt')) if panopto.exists() else []:
        mm = re.match(r'(\d{4}-\d{2}-\d{2})_(.*)$', srt.stem[len(ck) + 1:])
        if srt.name in srts or not mm or _is_tut_title(mm.group(2)) != (typ == 'tutorial'):
            continue
        d = mm.group(1)
        items.append({'week': week0(date.fromisoformat(d)), 'video_id': None, 'srt': srt, 'date': d,
                      'title': mm.group(2), 'md': _summary_of(ck, d, mm.group(2), summaries), 'other_group': False})
    have = {(i['date'], i['title']) for i in items}
    sub = summaries / ck / ('tutorials' if typ == 'tutorial' else 'lectures')
    for md in sorted(sub.glob('*.md')) if sub.exists() else []:
        if STANDIN in md.stem or not re.match(r'\d{4}-\d{2}-\d{2} - ', md.stem):
            continue
        d, title = md.stem[:10], md.stem[13:]
        if (d, title) in have or any(i['md'] == md for i in items):
            continue
        items.append({'week': week0(date.fromisoformat(d)), 'video_id': None, 'srt': None, 'date': d, 'title': title,
                      'md': md, 'other_group': False})
    return items


def reconcile(p: dict, items: list[dict], vid, collectable: set | None = None) -> dict:
    """Plan vs what's there → {'collect': [(rec, [items it replaces])], 'retire': [(item, rec that stays)],
    'relabel': {video_id: Sunday}, 'skip': [(rec, why)]}. `vid` = a session's video id; `collectable` = id()s of the
    sessions inside the lookback — a week whose pick is older is left as it is."""
    weeks, recs = p['weeks'], p['recs'].values()
    planned = {vid(r['s']): w for w, r in weeks.items()}
    by_vid = {vid(r['s']): r for r in recs}
    out = {'collect': [], 'retire': [], 'relabel': {}, 'skip': [], 'have': []}
    for it in items:
        v = it.get('video_id')
        if v and v in planned and planned[v] != it['week']:
            out['relabel'][v] = planned[v]
            it['week'] = planned[v]

    def kind_of(it):
        v = it.get('video_id')
        if v and v in planned:
            return weeks[planned[v]]['match']
        if v and v in by_vid:
            return by_vid[v]['kind']
        return 'other_group' if it.get('other_group') else 'unknown'
    for w, r in sorted(weeks.items()):
        if collectable is not None and id(r['s']) not in collectable:
            continue
        have = [it for it in items if it['week'] == w]
        mine = r['match'] in MINE
        if any(it.get('video_id') == vid(r['s']) for it in have):
            out['have'].append(r)                                 # already collected — nothing to do
            if mine:
                out['retire'] += [(it, r) for it in have if it.get('video_id') != vid(r['s'])
                                  and kind_of(it) in REPLACEABLE]
            elif r['match'] == 'other_group':                     # a slides clip next to the full lecture
                out['retire'] += [(it, r) for it in have if it.get('video_id') != vid(r['s']) and kind_of(it) == 'short']
            continue
        if not have:
            out['collect'].append((r, []))
        elif mine and all(kind_of(it) in REPLACEABLE for it in have):
            out['collect'].append((r, have))
        elif r['match'] == 'other_group' and all(kind_of(it) == 'short' for it in have):
            # 14/11: macro's lecturer uploads 10–30 minute slides clips, never the class; a clip under 40% of the class
            # never beats another group's full lecture of that week (the clip's summary moves aside)
            out['collect'].append((r, have))
        else:
            out['skip'].append((r, 'השבוע כבר יש ' + ', '.join(f"{it['date']} {it['title'][:30]} ({kind_of(it)})"
                                                               for it in have)))
    return out


def retire(item: dict, log=print, move_srt=None, retire_md=None) -> None:
    """Move an item aside to replaced/ (SRT + summary + vault, here and on Drive) — never deleted."""
    if move_srt is None or retire_md is None:
        from pipelines.slides_summaries import move_srt_aside, retire as _retire
        move_srt, retire_md = move_srt or move_srt_aside, retire_md or _retire
    if item.get('srt') and Path(item['srt']).exists():
        move_srt(Path(item['srt']))
    if item.get('md') and Path(item['md']).exists():
        retire_md(Path(item['md']), log)


# ── the studies calendar: his group's class on another day this week ──
def calendar_slots(course_label: str, typ: str, group: str, weekly: list[dict], days, events_on) -> tuple[list, list]:
    """Google 'לימודי תשפ"ז' events of his group of this course and type ('מאקרו כלכלה – תרגיל (קב׳ 08)') on the
    given days. → (dated one-off slots that differ from his weekly ones — a moved class, [cancelled dates])."""
    words = re.findall(r'[א-ת]{3,}', course_label or '')[:2]
    # the class itself — "<course> – תרגיל (קב׳ 08)" — not "✅ הגשת תרגיל 2 — הסתברות" or office hours
    kind_rx = (r'[–-]\s*(?:תרגיל|תרגול)\s*\(קב' if typ == 'tutorial' else r'[–-]\s*(?:שיעור|הרצאה)\s*\(קב')
    moved, cancelled = [], []
    for d in days:
        for ev in events_on(d) or []:
            summ = ev.get('summary') or ''
            if not words or not all(w in summ for w in words) or not re.search(kind_rx, summ):
                continue
            g = GROUP_RX.search(summ)
            if not g or (group and g.group(1).lstrip('0') != str(group).lstrip('0')):
                continue
            st = (ev.get('start') or {}).get('dateTime') or ''
            en = (ev.get('end') or {}).get('dateTime') or ''
            if not st:
                continue
            if summ.lstrip().startswith('❌') or 'בוטל' in summ:
                cancelled.append(d)
                continue
            hhmm, end = st[11:16], en[11:16]
            wd = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'][d.weekday()]
            if any(str(x.get('day', ''))[:3] == wd and x.get('start') == hhmm for x in weekly):
                continue
            moved.append({'type': typ, 'date': d.isoformat(), 'start': hhmm, 'end': end, 'group': group,
                          'lecturer': person_for(weekly), 'moved': True})
    return moved, cancelled
