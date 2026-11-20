"""
agent/proactive.py — the noticing layer (The owner 03/11: "כמה שיותר AGI, שארגיש שאני מדבר עם בן אדם עם מוח" — like a
person who STARTS the conversation: a calendar clash, a mail he dropped, something he said he'd do).

What the other ticks already say (warm-ups, exam eve, deadlines, office hours, mail digests, tasks) is left alone.
This only notices the gaps between them:

    clash      two timed events overlap in the next 48h (a lesson vs a personal event, or two personal ones)
    trip       a trip / vacation (all-day) with lessons inside it — "you're on a trip, the lecture is at 10:00"
    travel     campus ↔ an off-campus place back to back with too little time to get there
    mail       a university mail (the brain's 📧 note) that asked the owner to answer / register / pay / fill,
               1–6 days old, not a task already, no reply seen in his sent mail
    promise    "אני צריך להתקשר לבנק מחר" in the chat, with no reminder / task behind it → offer one
    study      the study goal is behind pace and today has a free window of 2h+ → name the window
               (05/11: only with SSA_STUDY_PUSH=1, agent/study_push.py — the owner: learning content on request only)
05/11: what the owner closed, by button or in words, is never noticed again (agent/closure.py).
11/11 the secretary (The owner: "שהבוט יהיה כמו מזכירה אישית… ליצור איתי שיחות"): life admin only, never study content —
    prep       tomorrow's appointment / flight / check-in → when to leave, what to bring: [⏰ 45 דק׳ לפני] [🚨 תעקוב]…
    gift       a wedding / birthday / housewarming in two days → a gift, a greeting: [⏰ מחר ב-10] [✍️ ברכה]
    deadline   "יום אחרון לביטול…" in the calendar in ≤2 days → [✅ טיפלתי] [⏰ מחר ב-10] [🚨 חשוב]
    waiting    a Claude job ready for his ✅ for a day → [🚀 לפרוס] [🔍 מה השתנה]
  These carry one-tap buttons ('sec:<op>:<offer>', answered by answer()), each with [🔕 פחות מזה]. "פחות" lowers the
  daily cap, "תפסיק עם המיילים" / "בלי הכנות" mutes a category for 90 days, "תחזיר את המיילים" brings it back
  (commands(), persisted in the same state file). 🚨 makes a reminder that agent/escalate.py follows up.

notice(now, …data) is pure (every source injected) → [{key, text, prio, buttons}]. pick() applies the budget:
never a key twice (data/proactive_seen.json), ≤3 notes a day, ≥90 min apart, never in quiet hours, one per tick.
It only TALKS — every action it suggests is phrased as the sentence the owner would write, so it goes through the
existing flows (תזכיר לי … / תזיז … → ✅), or is a button that sets one of HIS reminders / closes HIS item. No LLM.
tick() is behind SSA_PROACTIVE=1 (default off).
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'proactive_seen.json'
CONVO = ROOT / 'data' / 'agent_conversations.jsonl'
DAILY_CAP = 3
MIN_GAP = timedelta(minutes=90)
SCAN_EVERY = timedelta(minutes=20)
HORIZON = timedelta(hours=48)
TRAVEL_MIN = 45                     # minutes campus ↔ off campus (Ramat Aviv → the city, door to door)
STUDY_MIN = 120                     # a free window worth naming
STUDY_EVERY = timedelta(days=2) - timedelta(hours=2)   # the study nudge: every other day at most
DAY_FROM, DAY_TO = 9, 22            # the hours a study window may fall in
WEEKDAY_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
PRIO = {'clash': 0, 'trip': 0, 'travel': 1, 'mail': 2, 'promise': 1, 'study': 3,
        'deadline': 0, 'prep': 1, 'gift': 2, 'waiting': 3,
        'oblig_new': 1, 'oblig_remind': 0, 'oblig_late': 1, 'oblig_week': 2, 'oblig_news': 1,
        'grade_feedback': 1}   # 14/11 agent/obligations (a note may set its own)
# a category the owner can mute in words → the kinds in it, and how it is named back to him
GROUPS = {'calendar': ({'clash', 'trip', 'travel'}, 'התנגשויות ביומן'), 'mail': ({'mail'}, 'מיילים שמחכים לך'),
          'promise': ({'promise'}, 'דברים שאמרת שתעשה'), 'prep': ({'prep'}, 'הכנה לתורים ולנסיעות'),
          'gift': ({'gift'}, 'מתנות וברכות לאירועים'), 'deadline': ({'deadline'}, 'דדליינים מהיומן'),
          'waiting': ({'waiting'}, 'דברים שמחכים לאישור שלך'), 'study': ({'study'}, 'חלונות ללמידה'),
          'escalate': (set(), 'מעקב כשאתה לא עונה'),
          'oblig': ({'oblig_new', 'oblig_remind', 'oblig_late', 'oblig_week', 'oblig_news', 'grade_feedback'},
                    'מטלות ובחנים מהמודל')}
GROUP_RX = [('mail', re.compile(r'מייל|דואר')), ('calendar', re.compile(r'התנגשו|חפיפ|חופפ')),
            ('promise', re.compile(r'הבטח|דברים שאמרתי|מה שאמרתי')),
            ('prep', re.compile(r'הכנ[הות]|תורים|נסיעות|טיסות|מתי לצאת')),
            ('gift', re.compile(r'מתנ[הות]|ברכות|ימי הולדת|יום הולדת|חתונות')),
            ('deadline', re.compile(r'דדליי|יום אחרון|ימים אחרונים')), ('waiting', re.compile(r'קלוד|פריס|אישור')),
            ('escalate', re.compile(r'מעקב|לעקוב|נדנוד|לנדנד|הסלמ|התראות מהיומן')),
            ('oblig', re.compile(r'מטלות|מטלה|בחנים|בוחן|קוויז|שיעורי בית|quiz', re.I))]
MUTE_DAYS = 90

TRIP_RX = re.compile(r'חופשה|חופש\b|טיול|נופש|צימר|טיסה|נסיעה ל|חו"ל|חו״ל|מילואים|סופ"ש ב|סופ״ש ב')
CAMPUS_RX = re.compile(r'אוניברסיטת תל.?אביב|קמפוס|tau\.ac|בניין|ביה"ס|ביה״ס|אודיטוריום|קפלון|ברגלס|טרובוביץ|'
                       r'שנקר|דאך|צ\'ק פוינט|צ׳ק פוינט|גילמן|נפתלי|רוזנברג|ספריית|רמת אביב', re.I)
ONLINE_RX = re.compile(r'זום|zoom|meet\.google|teams|אונליין|online', re.I)
MARKER_RX = re.compile(r'^\s*(?:❌|✅|📝|📌)|הגשת ')

MAIL_ACT_RX = re.compile(r'לענות|להשיב|להירשם|לרשום|למלא|לשלם|לאשר|לחתום|לשלוח|להסדיר|לבחור|להצביע|לעדכן|לתאם|'
                         r'לקבוע|להגיש בקשה|להחזיר|ערעור|טופס|סקר')
MAIL_IF_RX = re.compile(r'אם נדרש|במידת הצורך|במידה ו|אם רלוונטי|אם אתה|אם תרצה|לפי הצורך|אם יש צורך|במידה שיש|'
                        r'אם (?:מעוניינ|רוצ|תרצו|ברצונ|את/ה|אתם)')   # 14/11 "לתאם מראש אם רוצים להגיע לשעת קבלה"
REPLY_RX = re.compile(r'לענות|להשיב|לאשר|לתאם')

PROMISE_RX = re.compile(r'(?:^|\s)(?:אני\s+)?(?:צריך|צריכה|חייב|חייבת|אמור|אמורה|מתכנן|מתכננת)\s+'
                        r'(ל[א-ת]{2,}(?:\s+[^\s,.!?]+){0,6})')
FORGET_RX = re.compile(r'(?:^|\s)(?:אסור לי לשכוח|לא לשכוח|שלא אשכח)\s+(?:ש)?(ל?[א-ת]{2,}(?:\s+[^\s,.!?]+){0,6})')
WHEN_RX = re.compile(r'מחר|היום|הערב|בערב|בבוקר|בצהריים|ב-?\d{1,2}(?::\d\d)?\b|ביום\s+[א-ת]+|בשבוע הבא|השבוע|עד\s+\S+|'
                     r'אחרי\s+ה|לפני\s+ה')
NOT_PROMISE_RX = re.compile(r'^\s*(?:תזכיר|תזכירי|תוסיף|תקבע|תשלח|תגיד|תכתוב|תבדוק|מה|איך|למה|האם|מתי|איפה|כמה|'
                            r'אתה|את\b)|לא צריך|לא חייב|\?\s*$')
HANDLED_RX = re.compile(r'⏰|אזכיר|תזכורת|הוספתי|נוסף ליומן|📌')
_STOP = {'את', 'של', 'על', 'עם', 'אני', 'זה', 'גם', 'כל', 'מחר', 'היום', 'הערב', 'צריך', 'חייב', 'אמור', 'לא', 'יש',
         'שאני', 'אם', 'עוד', 'כבר', 'רק', 'לי', 'לך'}


# ── small helpers ───────────────────────────────────────────────────────────

def enabled() -> bool:
    return os.environ.get('SSA_PROACTIVE', '').strip() == '1'


def _span(ev: dict) -> tuple[datetime | None, datetime | None, bool]:
    st, en = ev.get('start') or {}, ev.get('end') or {}
    if 'dateTime' in st:
        a = datetime.fromisoformat(st['dateTime']).replace(tzinfo=None)
        return a, datetime.fromisoformat(en.get('dateTime', st['dateTime'])).replace(tzinfo=None), False
    if st.get('date'):
        a = datetime.fromisoformat(st['date'])
        return a, (datetime.fromisoformat(en['date']) if en.get('date') else a + timedelta(days=1)), True
    return None, None, False


def _is_lesson(ev: dict) -> bool:
    return ((ev.get('extendedProperties') or {}).get('private') or {}).get('tau_src') == 'lessons' \
        or ev.get('_cal_kind') == 'lessons'


def _title(ev: dict) -> str:
    return (ev.get('summary') or '').strip() or 'אירוע'


def _short(ev: dict) -> str:
    """'מאקרו כלכלה – שיעור (קב׳ 03)' → 'מאקרו כלכלה – שיעור' (the group number is noise in a sentence)."""
    return re.sub(r'\s*\(קב[׳\']?\s*\d+\)\s*$', '', _title(ev))


def _when(t: datetime, now: datetime) -> str:
    d = (t.date() - now.date()).days
    day = 'היום' if d == 0 else 'מחר' if d == 1 else f'ביום {WEEKDAY_HE[t.weekday()]} {t:%d/%m}'
    return f'{day} ב-{t:%H:%M}'


def _day(t: datetime, now: datetime) -> str:
    d = (t.date() - now.date()).days
    return 'היום' if d == 0 else 'מחר' if d == 1 else f'ביום {WEEKDAY_HE[t.weekday()]} {t:%d/%m}'


def _words(t: str) -> set[str]:
    ws = {w for w in re.findall(r'[֐-׿\w"׳״]{3,}', t or '')}
    return {w[1:] if w[:1] in 'בלהומש' and len(w) > 3 else w for w in ws} - _STOP


def _place(ev: dict) -> str:
    """'campus' | 'online' | 'off' | '' (no location — can't tell)."""
    loc = (ev.get('location') or '').strip()
    if _is_lesson(ev) or CAMPUS_RX.search(loc):
        return 'campus'
    if ONLINE_RX.search(loc) or ONLINE_RX.search(_title(ev)):
        return 'online'
    return 'off' if loc else ''


def _partners_own(ev: dict, partner: str) -> bool:
    """'בת הזוג ברמון' is the partner's, not the owner's — 'ארוחה עם בת הזוג' / '(המשתמש ובת הזוג)' are his. Review 03/11: the name anywhere."""
    t = _title(ev)
    if not partner or not re.search(rf'(?:^|[\s(])[בלמה]?{re.escape(partner)}(?=$|[\s),.:–-])', t):
        return False
    owner = os.environ.get('WA_OWNER_NAME', 'המשתמש')
    return not re.search(rf'עם\s+{re.escape(partner)}|ו{re.escape(partner)}|{re.escape(owner)}', t)


def _rid(ev: dict) -> str:
    """A weekly event's series id — a recurring clash is said once, not every week (review 03/11)."""
    return ev.get('recurringEventId') or ev.get('id') or _title(ev)


def _countable(ev: dict) -> bool:
    """A timed event that really takes the owner's time: not a ✅/❌/📝 marker, not the optional Zoom office hours."""
    if MARKER_RX.search(_title(ev)):
        return False
    try:
        from agent import office_hours
        if office_hours.is_office_hours(ev):
            return False
    except Exception:
        pass
    return True


_KIND_WORDS = {'שיעור', 'תרגיל', 'תרגול', 'הרצאה', 'מעבדה', 'סדנה', 'סמינר'}


def _hops(timed: list[tuple]) -> list[tuple]:
    """Back-to-back on-campus sessions whose walk doesn't fit the break (agent/campus): [(e1, b1, e2, a2, hop)]."""
    from agent import campus
    out = []
    for (a1, b1, e1), (a2, b2, e2) in zip(timed, timed[1:]):
        if a1.date() != a2.date() or _place(e1) != 'campus' or _place(e2) != 'campus':
            continue
        hop = campus.tight(b1, e1.get('location') or '', a2, e2.get('location') or '')
        if hop:
            out.append((e1, b1, e2, a2, hop))
    return out


def campus_notes(now: datetime, timed: list[tuple]) -> list[dict]:
    """19/11 improvement 14: one note per day with the hops across the campus he can't walk in time, said once per
    weekly pattern (the key is the weekday + times + buildings, so next Tuesday's same hops stay quiet)."""
    by_day: dict = {}
    for e1, b1, e2, a2, hop in _hops([x for x in timed if x[0] >= now - timedelta(hours=3)]):
        if now <= a2 <= now + HORIZON:
            by_day.setdefault(a2.date(), []).append((e1, b1, e2, a2, hop))
    out = []
    for day, hops in sorted(by_day.items()):
        a = hops[0][3]
        lines = [f"• {a2:%H:%M} *{_short(e1)}* ({hop['frm']}) → *{_short(e2)}* ({hop['to']}): ~{hop['walk']} דק׳ הליכה, "
                 + ('בלי הפסקה' if hop['gap'] <= 0 else f"הפסקה של {hop['gap']} דק׳") for e1, _b, e2, a2, hop in hops]
        key = 'walk|' + '|'.join(f"{a2:%a%H%M}{hop['frm']}>{hop['to']}" for _e, _b, _e2, a2, hop in hops)
        out.append({'key': key, 'kind': 'travel',
                    'text': f'🚶 {_day(a, now)} יש מעברים צפופים בקמפוס:\n' + '\n'.join(lines) +
                            '\nכדאי לצאת כמה דקות לפני סוף השיעור. רוצה שורה "כדאי לצאת עכשיו" כשצריך? '
                            'כתוב "תזכיר לי לצאת בין שיעורים".'})
    return out


# ── the noticers (pure) ─────────────────────────────────────────────────────

def calendar_notes(now: datetime, events: list[dict], partner: str = 'בת הזוג') -> list[dict]:
    """clash · trip · travel, from the next few days of events (lessons calendar included)."""
    out: list[dict] = []
    timed = []
    for ev in events:
        a, b, allday = _span(ev)
        if a and not allday and _countable(ev) and not _partners_own(ev, partner):
            timed.append((a, b, ev))
    timed.sort(key=lambda x: x[0])
    seen_pairs: set[tuple] = set()
    for i, (a1, b1, e1) in enumerate(timed):
        for a2, b2, e2 in timed[i + 1:]:
            if a2 >= b1:
                break
            if not (a1 < b2 and a2 < b1) or a2 < now or a1 > now + HORIZON:
                continue
            if _words(_title(e1)) == _words(_title(e2)):   # the same thing twice (two calendars)
                continue
            if (_is_lesson(e1) or _is_lesson(e2)) and _span(e1)[0] == _span(e2)[0] and                     _words(_short(e1)) - _KIND_WORDS & _words(_short(e2)) - _KIND_WORDS:
                continue                                   # 19/11 one session, two calendars ("📘 מיקרו — שיעור" / "(קב׳ 03)")
            if _is_lesson(e1) and _is_lesson(e2):          # two of his own groups — the timetable's business
                continue
            pair = tuple(sorted((_rid(e1), _rid(e2))))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            les = e1 if _is_lesson(e1) else e2 if _is_lesson(e2) else None
            if les is not None:
                other = e2 if les is e1 else e1
                la = _span(les)[0]
                text = (f'👀 שמתי לב: {_when(la, now)} יש לך *{_short(les)}*, וזה חופף ל*{_title(other)}* '
                        f'({_when(_span(other)[0], now).split(" ב-")[-1]}).\n'
                        f'לוותר הפעם על השיעור (יש הקלטה?), או להזיז את השני? אם להזיז — כתוב לי לאן ואסדר, עם ✅ שלך.')
            else:
                text = (f'👀 {_day(a1, now)} יש שני דברים באותו זמן: *{_title(e1)}* ({a1:%H:%M}–{b1:%H:%M}) '
                        f'ו*{_title(e2)}* ({a2:%H:%M}–{b2:%H:%M}).\nאחד מהם זז? תגיד לי לאן ואסדר (עם ✅ שלך).')
            weekly = e1.get('recurringEventId') or e2.get('recurringEventId')
            out.append({'key': f'clash|{pair[0]}|{pair[1]}' + ('' if weekly else f'|{a2:%Y-%m-%d}'),
                        'kind': 'clash', 'text': text})
    for (a1, b1, e1), (a2, b2, e2) in zip(timed, timed[1:]):     # back to back, different places
        if a2 < now or a1 > now + HORIZON or a2 < b1:
            continue
        p1, p2 = _place(e1), _place(e2)
        gap = int((a2 - b1).total_seconds() // 60)
        if gap >= TRAVEL_MIN or {p1, p2} != {'campus', 'off'}:
            continue
        far = e2 if p2 == 'off' else e1
        text = (f'🚶 {_day(a1, now)}: *{_short(e1)}* נגמר ב-{b1:%H:%M} ו*{_short(e2)}* מתחיל ב-{a2:%H:%M}'
                f' — {"בלי הפסקה" if gap <= 0 else f"רק {gap} דק׳ ביניהם"}, ו{"ה" if far is e2 else ""}אחד מהם '
                f'ב{(far.get("location") or "").split(",")[0]}. זה מספיק כדי להגיע? אם לא — אפשר לצאת קצת לפני, או להזיז.')
        weekly = e1.get('recurringEventId') and e2.get('recurringEventId')
        out.append({'key': f'travel|{_rid(e1)}|{_rid(e2)}' + ('' if weekly else f'|{a2:%Y-%m-%d}'),
                    'kind': 'travel', 'text': text})
    out += campus_notes(now, timed)
    lessons = sorted(((s, ev) for ev in events if _is_lesson(ev) for s, _e, allday in [_span(ev)]
                      if s and not allday and not MARKER_RX.search(_title(ev))), key=lambda x: x[0])
    for ev in events:                                         # a trip with lessons inside it
        a, b, allday = _span(ev)
        t = _title(ev)
        if not (a and allday) or _is_lesson(ev) or not TRIP_RX.search(t) or _partners_own(ev, partner):
            continue
        inside = [(s, le) for s, le in lessons if a <= s < b and s > now]
        if not inside or inside[0][0] > now + HORIZON + timedelta(hours=24):
            continue
        names = ', '.join(f'{_short(le)} ({_when(s, now)})' for s, le in inside[:3]) + (' ועוד' if len(inside) > 3 else '')
        out.append({'key': f'trip|{ev.get("id") or t}|{a:%Y-%m-%d}', 'kind': 'trip',
                    'text': f'🧳 ראיתי ביומן *{t}* — ובאמצע יש שיעורים: {names}.\n'
                            f'לוותר עליהם ולראות הקלטות אחרי? אם תרצה, אזכיר לך לצפות בהן כשתחזור — '
                            f'כתוב "תזכיר לי ב… לראות את ההקלטה".'})
    return out


MAIL_HEAD = re.compile(r'^## (\d{1,2})/(\d{1,2}) · (.+?) — (.*)$')


def parse_mail_note(text: str, year: int) -> list[dict]:
    """The brain's monthly 📧 note (agent/brain.note_mail) → [{day, who, subject, summary, action}]."""
    out, cur = [], None
    for line in (text or '').splitlines():
        m = MAIL_HEAD.match(line.strip())
        if m:
            try:
                d = date(year, int(m.group(2)), int(m.group(1)))
            except ValueError:
                cur = None
                continue
            cur = {'day': d, 'who': m.group(3).strip(), 'subject': m.group(4).strip(), 'summary': '', 'action': ''}
            out.append(cur)
        elif cur is not None and line.startswith('👉'):
            cur['action'] = line[1:].strip(' 👉')
        elif cur is not None and line.strip() and not cur['summary'] and not line.startswith(('📎', '🏫', '---')):
            cur['summary'] = line.strip()
    return out


def _subj_words(s: str) -> set[str]:
    s = re.sub(r'^\s*(?:re|fwd?|תגובה|השב)\s*:\s*', '', s or '', flags=re.I)
    s = re.sub(r'^\s*\d{6,}\s*-\s*[^:]+:\s*', '', s)
    return _words(s.lower())


def _past_deadline(m: dict, today: date) -> bool:
    """'עד מחר' in a mail from 28/10 → past on 03/11; an explicit dd/mm all in the past → past."""
    txt = f"{m.get('action', '')} {m.get('summary', '')}"
    if re.search(r'\bהיום\b', txt) and m['day'] < today:
        return True
    if re.search(r'\bמחר\b', txt) and m['day'] + timedelta(days=1) < today:
        return True
    dates = []
    for dd, mm, yy in re.findall(r'(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?', txt):
        try:
            y = int(yy) + (2000 if yy and len(yy) == 2 else 0) if yy else m['day'].year
            dates.append(date(y, int(mm), int(dd)))
        except ValueError:
            pass
    return bool(dates) and max(dates) < today


def mail_candidate(m: dict, today: date) -> bool:
    act = m.get('action') or ''
    return (bool(act) and bool(MAIL_ACT_RX.search(act)) and not MAIL_IF_RX.search(act)
            and 1 <= (today - m['day']).days <= 6 and not _past_deadline(m, today))


def mail_key(m: dict) -> str:
    return 'mail|' + ' '.join(sorted(_subj_words(m.get('subject', ''))))[:120]


def mail_notes(now: datetime, mails: list[dict], sent_subjects: list[str] | None = None,
               tasks: list[dict] | None = None, closed: list[dict] | None = None) -> list[dict]:
    """A university mail that asked for something, a day+ ago, not a task yet, no reply seen, not something the owner already
    closed (05/11: ✅ on the library task at 10:09, this note about the same mail at 10:12) → one gentle nudge."""
    from agent import closure
    today = now.date()
    sent = [_subj_words(s) for s in (sent_subjects or [])]
    task_words = [_words(t.get('text', '')) for t in (tasks or [])]
    out, keys = [], set()
    for m in mails:
        if not mail_candidate(m, today):
            continue
        sw = _subj_words(m['subject'])
        key = mail_key(m)
        if not sw or key in keys:                       # Moodle's mail + its announcement = the same thing
            continue
        if closure.match(closed or [], f"{m['subject']} {m['action']}", m.get('who', ''), when=m['day']):
            continue                                    # he said it's done / irrelevant
        if any(len(_words(m['action']) & tw) >= 2 for tw in task_words):
            continue                                    # it's already a 📌 task (nagged by agent/tasks.py)
        if any(len(sw & s) / max(1, len(sw)) >= 0.6 for s in sent):
            continue                                    # he answered
        keys.add(key)
        who = re.sub(r'^מודל · ', 'המודל של ', m['who'])
        ago = (today - m['day']).days
        when = 'אתמול' if ago == 1 else f'לפני {ago} ימים'
        reply = bool(REPLY_RX.search(m['action'])) and 'noreply' not in m['who'].lower() and 'מודל' not in m['who']
        text = (f'📬 {when} הגיע מ{who} ("{m["subject"][:70]}") משהו שמחכה לך: {m["action"].rstrip(".")}.\n'
                + ('לא ראיתי שענית. ' if reply and sent_subjects is not None else '')
                + 'כבר טיפלת? כתוב "טיפלתי" וזה נסגר. אם לא — כתוב "תזכיר לי מחר ב-10 …" ואדאג שזה לא ייפול.')
        out.append({'key': key, 'kind': 'mail', 'text': text})
    return out


def promise_notes(now: datetime, convo: list[dict], reminders: list[dict] | None = None,
                  tasks: list[dict] | None = None, closed: list[dict] | None = None) -> list[dict]:
    """"אני צריך להתקשר לבנק מחר" (The owner's own words, last 30h) with nothing set behind it → offer a reminder."""
    have = [_words(r.get('text', '')) for r in (reminders or []) if r.get('active', True)]
    have += [_words(t.get('text', '')) for t in (tasks or [])]
    from agent import closure
    out = []
    for r in convo:
        if r.get('role') != 'owner' or r.get('forwarded') or r.get('kind', 'text') != 'text':
            continue
        try:
            ts = datetime.fromisoformat(r['ts'])
        except (KeyError, ValueError):
            continue
        if not (now - timedelta(hours=30) <= ts <= now - timedelta(minutes=10)):
            continue
        said = (r.get('in') or '').strip()
        if not said or len(said) > 300 or NOT_PROMISE_RX.search(said) or not WHEN_RX.search(said):
            continue
        m = PROMISE_RX.search(said) or FORGET_RX.search(said)
        if not m:
            continue
        replies = ' '.join(str(o[1]) if isinstance(o, (list, tuple)) and len(o) > 1 else str(o) for o in r.get('out') or [])
        if HANDLED_RX.search(replies):
            continue                                     # the bot already set a reminder / task for it
        what = m.group(1).strip()
        w = _words(what)
        if any(len(w & h) >= 2 or (len(w) == 1 and w <= h) for h in have):
            continue
        if closure.match(closed or [], said):
            continue
        quote = said if len(said) <= 90 else said[:88] + '…'
        out.append({'key': f'promise|{r["ts"]}', 'kind': 'promise',
                    'text': f'💭 {"אתמול" if ts.date() < now.date() else "קודם"} אמרת: "{quote}"\n'
                            f'להזכיר לך? כתוב למשל "תזכיר לי {what}" עם שעה, ואני על זה.'})
    return out


def free_windows(day: date, events: list[dict], shifts: list[dict] | None = None,
                 start_after: datetime | None = None) -> list[tuple[datetime, datetime]]:
    """Free stretches of ≥STUDY_MIN between DAY_FROM and DAY_TO, around timed events and work shifts."""
    lo = datetime.combine(day, datetime.min.time()).replace(hour=DAY_FROM)
    hi = datetime.combine(day, datetime.min.time()).replace(hour=DAY_TO)
    if start_after and start_after > lo:
        lo = start_after
    busy = []
    for ev in events:
        a, b, allday = _span(ev)
        if a and not allday and a.date() <= day <= b.date() and _countable(ev):
            busy.append((a, b))
    for s in shifts or []:
        try:
            busy.append((datetime.combine(day, datetime.strptime(s['start'], '%H:%M').time()),
                         datetime.combine(day, datetime.strptime(s['end'], '%H:%M').time())))
        except (KeyError, ValueError):
            pass
    out, t = [], lo
    for a, b in sorted(busy):
        if a > t and (a - t) >= timedelta(minutes=STUDY_MIN):
            out.append((t, min(a, hi)))
        t = max(t, b)
        if t >= hi:
            break
    if hi > t and (hi - t) >= timedelta(minutes=STUDY_MIN):
        out.append((t, hi))
    return [(a, b) for a, b in out if (b - a) >= timedelta(minutes=STUDY_MIN)]


def study_notes(now: datetime, events: list[dict], study: dict | None) -> list[dict]:
    """study = {'done': n, 'goal': n, 'shifts': [...]} → today's first free window, when the goal is behind pace."""
    if not study or not study.get('goal'):
        return []
    today = now.date()
    need = study['goal'] - study.get('done', 0)
    days_left = 6 - (today.weekday() + 1) % 7                 # days after today until Saturday (as planner.py)
    if need <= 0 or need <= days_left // 2:
        return []
    if any(_span(ev)[2] and TRIP_RX.search(_title(ev)) and _span(ev)[0].date() <= today < _span(ev)[1].date()
           for ev in events):
        return []                                             # on a trip — not today
    todays = [ev for ev in events if (_span(ev)[0] or now).date() == today]
    wins = free_windows(today, todays, study.get('shifts'), start_after=now + timedelta(minutes=30))
    if not wins:
        return []
    a, b = wins[0]
    if a.date() != today or (a - now) > timedelta(hours=10):
        return []
    win = (f'היום פנוי אצלך כמעט הכול מ-{a:%H:%M}' if (b - a) > timedelta(hours=6)
           else f'יש לך היום חלון פנוי {a:%H:%M}–{b:%H:%M}')
    return [{'key': f'study|{today}', 'kind': 'study',
             'text': f'📚 {win}, ועד עכשיו {study.get("done", 0)}/{study["goal"]} '
                     f'סבבי למידה השבוע. שעה וחצי שם? אחרי — כתוב "למדתי" ואספור.'}]


# ── the secretary (11/11): life admin around the calendar, with one-tap answers ──

FLIGHT_RX = re.compile(r'טיסה|טסים|טס ל|flight|נתב"ג|נתב״ג|טרמינל')
STAY_RX = re.compile(r'צ[\'׳]ק[- ]?אין|check[- ]?in|מלון|צימר')
APPT_RX = re.compile(r'רופא|רופאה|(?<![א-ת])תור(?![א-ת])|בדיקת|(?<![א-ת])בדיקה|שיניים|ראיון|עו"ד|עו״ד|עורך דין|רו"ח|רו״ח|'
                     r'(?<![א-ת])בנק|סניף|משרד הפנים|ביטוח לאומי|מספרה|פיזיותרפ|וטרינר|מוסך|(?<![א-ת])טסט|אופטומטר|'
                     r'קופת חולים|מכבי|כללית|מאוחדת|לאומית|חיסון')
GIFT_RX = re.compile(r'חתונה|בר מצווה|בת מצווה|בר-מצווה|בת-מצווה|ברית|חנוכת בית|יום הולדת|יומולדת|יום נישואין')
BDAY_RX = re.compile(r'(?:יום הולדת|יומולדת)\s+(?:ל|של\s+)?([א-ת][א-ת\'׳"״-]{1,15}(?:\s+[א-ת][א-ת\'׳"״-]{1,15})?)')
DEADLINE_RX = re.compile(r'יום אחרון|דדליין|deadline|אחרון לביטול|ביטול ללא|לביטול|לתשלום|לחידוש|תוקף|לרישום|להרשמה', re.I)
OWN_RX = re.compile(r'(?:יום הולדת|יומולדת)\s+(?:שלי|לי)(?![א-ת])')
PREP_EVENING = (17, 22)             # tomorrow's things: the evening before
PREP_MORNING = (8, 12)              # today's afternoon things not said yesterday (added since)


def _sec_skip(ev: dict, partner: str, closed: list[dict] | None) -> bool:
    from agent import closure
    t = _title(ev)
    if _is_lesson(ev) or MARKER_RX.search(t) or t.startswith('🔔') or _partners_own(ev, partner):
        return True
    priv = (ev.get('extendedProperties') or {}).get('private') or {}
    if priv.get('ssa_src') in ('task', 'escalation') or priv.get('tau_src'):
        return True                                  # tasks.py / escalate.py / the deadlines sync speak for these
    return bool(closed) and bool(closure.match(closed, t))


def _first_name(title: str) -> str:
    m = BDAY_RX.search(title)
    if not m:
        return ''
    name = re.sub(r'[\'׳"״-]', '', m.group(1)).strip()
    return name if re.fullmatch(r'[א-ת]+(?: [א-ת]+)?', name) else ''


def secretary_notes(now: datetime, events: list[dict], partner: str = 'בת הזוג',
                    closed: list[dict] | None = None) -> list[dict]:
    """prep · gift · deadline, from the next days' calendar. Pure. Each note carries an `offer` (what the buttons act
    on) and `opts` [(label, op)] — pick() turns them into 'sec:<op>:<id>' buttons."""
    out: list[dict] = []
    owner = os.environ.get('WA_OWNER_NAME', 'המשתמש')
    seen: set[str] = set()
    for ev in events:
        a, b, allday = _span(ev)
        if not a or _sec_skip(ev, partner, closed):
            continue
        t = _title(ev)
        eid = ev.get('id') or t
        if eid in seen:
            continue
        days = (a.date() - now.date()).days
        offer = {'title': t[:120], 'start': a.isoformat(timespec='minutes'), 'allday': allday, 'eid': ev.get('id', '')}
        loc = (ev.get('location') or '').split(',')[0].strip()
        if DEADLINE_RX.search(t) and 0 <= days <= 2 and 9 <= now.hour < 21:
            if not allday and a <= now:
                continue
            seen.add(eid)
            when = 'היום' if days == 0 else 'מחר' if days == 1 else 'בעוד יומיים'
            hour = '' if allday else f' (עד {a:%H:%M})'
            opts = [('✅ טיפלתי', 'done'), ('⏰ בעוד שעה', 'at1h') if days == 0 else ('⏰ תזכיר לי מחר ב-10', 'tom10'),
                    ('🚨 חשוב — תעקוב שטיפלתי', 'impd')]
            out.append({'key': f'deadline|{eid}|{a:%Y-%m-%d}', 'kind': 'deadline', 'offer': offer, 'opts': opts,
                        'text': f'⏳ {when}{hour}: *{t}*.\nכבר טיפלת בזה?'})
            continue
        if GIFT_RX.search(t) and days == 2 and PREP_EVENING[0] <= now.hour < PREP_EVENING[1] \
                and not OWN_RX.search(t) and not re.search(rf'(?:יום הולדת|יומולדת)\s+(?:ל?{re.escape(owner)}|ל{re.escape(owner[1:])})(?![א-ת])' if owner.startswith('ה') else rf'(?:יום הולדת|יומולדת)\s+ל?{re.escape(owner)}(?![א-ת])', t):
            seen.add(eid)
            name = _first_name(t)
            opts = [('⏰ תזכיר לי מחר ב-10 לסדר', 'tom10')]
            if name:
                offer['name'] = name
                opts.append(('✍️ תנסח לי ברכה', 'draft'))
            opts.append(('👍 מסודר', 'ok'))
            out.append({'key': f'gift|{eid}|{a:%Y-%m-%d}', 'kind': 'gift', 'offer': offer, 'opts': opts,
                        'text': f'🎁 בעוד יומיים ({_day(a, now).replace("ביום ", "")}): *{t}*.\n'
                                f'צריך {"מתנה או ברכה" if name else "מתנה / צ׳ק / ברכה"}? אם תרצה אזכיר לך מחר לסדר.'})
            continue
        if allday:
            continue
        evening = days == 1 and PREP_EVENING[0] <= now.hour < PREP_EVENING[1]
        morning = days == 0 and PREP_MORNING[0] <= now.hour < PREP_MORNING[1] and a.hour >= 13
        if not (evening or morning):
            continue
        when = f'{"מחר" if days == 1 else "היום"} ב-{a:%H:%M}'
        where = f' ({loc})' if loc and not ONLINE_RX.search(loc) else ''
        if FLIGHT_RX.search(t):
            seen.add(eid)
            out.append({'key': f'prep|{eid}|{a:%Y-%m-%d}', 'kind': 'prep', 'offer': {**offer, 'lead': 210},
                        'opts': [('⏰ תזכיר לי לצאת 3.5 שעות לפני', 'lead210'), ('🚨 תעקוב שיצאתי בזמן', 'imp'),
                                 ('👍 מסודר', 'ok')],
                        'text': f'✈️ {when}: *{t}*.\nדרכון, כרטיס עלייה, אישור הזמנה — מסודר? לטיסה לחו״ל כדאי לצאת '
                                f'לפחות 3 שעות לפני.'})
        elif STAY_RX.search(t):
            seen.add(eid)
            out.append({'key': f'prep|{eid}|{a:%Y-%m-%d}', 'kind': 'prep', 'offer': {**offer, 'lead': 120},
                        'opts': [('⏰ תזכיר לי שעתיים לפני', 'lead120'), ('👍 מסודר', 'ok')],
                        'text': f'🧳 {when}: *{t}*{where}.\nאישור ההזמנה בטלפון, ת״ז? להזכיר לך לצאת?'})
        elif APPT_RX.search(t):
            seen.add(eid)
            ask = 'להזכיר לך מתי לצאת?' if where else 'צריך להביא משהו (ת״ז, הפניה, מסמכים)? להזכיר לך לפני?'
            out.append({'key': f'prep|{eid}|{a:%Y-%m-%d}', 'kind': 'prep', 'offer': {**offer, 'lead': 45},
                        'opts': [('⏰ 45 דק׳ לפני', 'lead45'), ('⏰ שעה וחצי לפני', 'lead90'),
                                 ('🚨 חשוב — תעקוב שיצאתי', 'imp'), ('👍 לא צריך', 'ok')],
                        'text': f'🗓️ {when}: *{t}*{where}.\n{ask}'})
    return out


WAIT_AFTER = 20 * 3600


def waiting_notes(now: datetime, jobs: dict | None) -> list[dict]:
    """A Claude job ready for his ✅ for 20h+ (a week at most) → one gentle line with the job's own buttons."""
    out = []
    t = now.timestamp()
    for jid, j in (jobs or {}).items():
        if not isinstance(j, dict) or j.get('status') != 'ready' or not j.get('ready_at'):
            continue
        age = t - float(j['ready_at'])
        if not WAIT_AFTER <= age <= 7 * 86400:
            continue
        try:
            from agent.claude_job import _label
            label = _label(j)
        except Exception:
            label = 'משימה של קלוד'
        out.append({'key': f'waiting|{jid}', 'kind': 'waiting',
                    'buttons': [('🚀 לפרוס', f'cj:deploy:{jid}'), ('🔍 מה השתנה', f'cj:diff:{jid}'),
                                ('⏳ אחר כך', 'noop:w')],
                    'text': f'🛠️ המשימה של קלוד ("{label[:60]}") מוכנה כבר {int(age // 3600)} שעות ומחכה לך. לפרוס?'})
    return out


def _grade_notes(now: datetime, recs: list[dict]) -> list[dict]:
    from agent import grades
    return grades.notes(now, recs)


def notice(now: datetime, events: list[dict] | None = None, mails: list[dict] | None = None,
           sent_subjects: list[str] | None = None, convo: list[dict] | None = None,
           reminders: list[dict] | None = None, tasks: list[dict] | None = None, study: dict | None = None,
           partner: str = 'בת הזוג', closed: list[dict] | None = None, jobs: dict | None = None,
           obligations: dict | None = None, grades: list[dict] | None = None) -> list[dict]:
    """Everything worth starting a conversation about right now, most important first. Pure."""
    from agent import obligations as ob, study_push
    notes = (calendar_notes(now, events or [], partner) + mail_notes(now, mails or [], sent_subjects, tasks, closed)
             + promise_notes(now, convo or [], reminders, tasks, closed)
             + secretary_notes(now, events or [], partner, closed) + waiting_notes(now, jobs)
             + (study_notes(now, events or [], study) if study_push.on() else [])
             + (ob.notes(now, obligations) if obligations else [])
             + (_grade_notes(now, grades) if grades else []))
    for n in notes:
        n.setdefault('buttons', [])
        n['prio'] = n['prio'] if isinstance(n.get('prio'), int) else PRIO.get(n['kind'], 5)
    return sorted(notes, key=lambda n: n['prio'])


# ── the budget ──────────────────────────────────────────────────────────────

def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    d.setdefault('said', {})
    d.setdefault('days', {})
    return d


def _save(d: dict, path: Path | None = None, now: datetime | None = None) -> None:
    now = now or datetime.now()
    d['said'] = {k: v for k, v in d['said'].items() if v >= (now - timedelta(days=90)).isoformat()}
    d['days'] = {k: v for k, v in d['days'].items() if k >= (now - timedelta(days=14)).date().isoformat()}
    p = path or STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')


def _quiet(now: datetime) -> bool:
    from utils.notify import in_quiet_hours
    return in_quiet_hours(now)


def cap(st: dict) -> int:
    """Openers a day: DAILY_CAP unless the owner said "פחות" (down to 1) or "תפסיק ליזום" (0)."""
    c = st.get('cap')
    return DAILY_CAP if not isinstance(c, int) else max(0, min(c, DAILY_CAP + 2))


def group_of(kind: str) -> str:
    return next((g for g, (kinds, _n) in GROUPS.items() if kind in kinds), kind)


def muted(st: dict, group: str, now: datetime | None = None) -> bool:
    until = (st.get('muted') or {}).get(group)
    return bool(until) and until > (now or datetime.now()).isoformat()


def may_speak(st: dict, now: datetime, quiet=None) -> bool:
    if (quiet or _quiet)(now):
        return False
    if st['days'].get(now.date().isoformat(), 0) >= cap(st):
        return False
    return not (st.get('last') and now - datetime.fromisoformat(st['last']) < MIN_GAP)


def _buttons(n: dict, st: dict, now: datetime) -> list[tuple[str, str]]:
    """A note's one-tap answers: its own payloads, or 'sec:<op>:<offer>' kept in the state for answer()."""
    if n.get('buttons'):
        return list(n['buttons'])
    if not n.get('opts'):
        return []
    oid = secrets.token_hex(3)
    offers = st.setdefault('offers', {})
    offers[oid] = {**(n.get('offer') or {}), 'kind': n['kind'], 'ts': now.isoformat(timespec='seconds')}
    cut = (now - timedelta(days=7)).isoformat()
    st['offers'] = {k: v for k, v in offers.items() if v.get('ts', '') >= cut}
    return [(lbl, f'sec:{op}:{oid}') for lbl, op in n['opts']] + [('🔕 פחות מזה', f'sec:mute:{oid}')]


def pick(notes: list[dict], st: dict, now: datetime, quiet=None) -> list[dict]:
    """At most one new note, inside the budget; marks it said (a key is never said twice)."""
    if not may_speak(st, now, quiet):
        return []
    # 03/11 replay on real data: the study window came every morning — a nudge, not a nag: once per STUDY_EVERY
    recent_study = any(k.startswith('study|') and v >= (now - STUDY_EVERY).isoformat() for k, v in st['said'].items())
    fresh = [n for n in notes if n['key'] not in st['said'] and not (n['kind'] == 'study' and recent_study)
             and not muted(st, group_of(n['kind']), now)]
    if fresh and quiet is None:                       # 17/11 his rhythm (agent/rhythm): live only, tests pass `quiet`
        from agent import rhythm
        r = rhythm.load(now)
        fresh = [n for n in fresh if not rhythm.backed_off(group_of(n['kind']), st['said'], now, r)
                 and not rhythm.hold(n['kind'], n.get('prio', PRIO.get(n['kind'], 3)), now, r)]
    if not fresh:
        return []
    n = dict(fresh[0])
    n['buttons'] = _buttons(n, st, now)
    st['said'][n['key']] = now.isoformat(timespec='seconds')
    day = now.date().isoformat()
    st['days'][day] = st['days'].get(day, 0) + 1
    st['last'] = now.isoformat(timespec='seconds')
    st['last_kind'] = n['kind']
    return [n]


# ── live data (best-effort; a source that fails is just empty) ─────────────

def _events(now: datetime) -> list[dict]:
    from integrations import gcal
    if not gcal.configured():
        return []
    out = []
    for i in range(4):
        out += gcal.events_on((now + timedelta(days=i)).date())
    return with_timetable(now, out)


def with_timetable(now: datetime, events: list[dict], sessions=None, cancelled=None) -> list[dict]:
    """19/11 improvement 14: the calendar + his sessions from the university site that it doesn't have (agent/fastpath
    sessions: the my.tau cache with the university's changes), without the sessions the university cancelled — a
    cancelled tutorial is no clash and no walk."""
    try:
        if cancelled is None:
            from agent import schedule
            cancelled = schedule.cancelled
        events = [ev for ev in events if not (_is_lesson(ev) and cancelled(ev))]
    except Exception as e:
        print(f'  ⚠️ proactive cancelled: {e!r}'[:200])
    try:
        if sessions is None:
            from agent import fastpath
            sessions = fastpath.sessions(now)
    except Exception as e:
        print(f'  ⚠️ proactive timetable: {e!r}'[:200])
        return events
    have = {(_span(ev)[0], frozenset(_words(_short(ev)) - _KIND_WORDS)) for ev in events if _is_lesson(ev)}
    starts = {s for s, _w in have}
    for s in sessions or []:
        if (s.get('override') or {}).get('status') == 'cancelled' or s['at'] > now + timedelta(days=4):
            continue
        cw = _words(s['course']) - _KIND_WORDS
        if s['at'] in starts and any(st == s['at'] and (w & cw or not w) for st, w in have):
            continue
        events.append({'id': f"tau|{s['at']:%Y%m%d%H%M}|{s.get('ck') or ''}", 'summary': f"{s['course']} — {s['kind']}",
                       'start': {'dateTime': s['at'].isoformat()}, 'end': {'dateTime': s['end'].isoformat()},
                       'location': s.get('place') or '', '_cal_kind': 'lessons'})
    return events


def _mails(now: datetime) -> list[dict]:
    from agent import brain
    d = brain.brain_dir() / '📧 מיילים'
    out = []
    for m in {now.date().replace(day=1), (now.date().replace(day=1) - timedelta(days=1)).replace(day=1)}:
        p = d / f'{m:%Y-%m}.md'
        if p.exists():
            out += parse_mail_note(p.read_text(encoding='utf-8'), m.year)
    return out


def _sent_subjects(service=None) -> list[str] | None:
    """Subjects of the owner's sent mail (10 days) — headers only (review 03/11: not the full bodies)."""
    if service is None:
        from integrations import tau_mail
        if not tau_mail.configured():
            return None
        service = tau_mail._service()
    msgs = service.users().messages()
    ids = msgs.list(userId='me', q='in:sent newer_than:10d', maxResults=40).execute().get('messages') or []
    out = []
    for m in ids:
        r = msgs.get(userId='me', id=m['id'], format='metadata', metadataHeaders=['Subject']).execute()
        out += [h.get('value', '') for h in (r.get('payload') or {}).get('headers') or [] if h.get('name') == 'Subject']
    return out


def _convo(now: datetime, path: Path | None = None) -> list[dict]:
    p = path or CONVO
    try:
        with open(p, 'rb') as f:                         # the tail is enough (30h)
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode('utf-8', errors='replace').splitlines()[1:]
    except OSError:
        return []
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def gather(chat: str, now: datetime, st: dict | None = None) -> dict:
    data: dict = {}
    for name, fn in (('events', lambda: _events(now)), ('mails', lambda: _mails(now)),
                     ('convo', lambda: _convo(now))):
        try:
            data[name] = fn()
        except Exception as e:
            print(f'  ⚠️ proactive {name}: {e!r}'[:200])
    try:
        from agent import tasks
        data['tasks'] = tasks.open_tasks(chat)
    except Exception:
        pass
    try:
        from agent import closure
        data['closed'] = closure.items(chat)
    except Exception as e:
        print(f'  ⚠️ proactive closed: {e!r}'[:200])
    try:
        from agent import life
        data['reminders'] = [r for r in life._load()['reminders'] if r.get('chat') == chat and r.get('active')]
    except Exception:
        pass
    said = (st or {}).get('said') or {}
    if any(mail_candidate(m, now.date()) and mail_key(m) not in said for m in data.get('mails') or []):
        # Gmail only for a mail not nudged yet (review 03/11: it was every 20 min for 6 days)
        try:
            data['sent_subjects'] = _sent_subjects()
        except Exception as e:
            print(f'  ⚠️ proactive sent mail: {e!r}'[:200])
    try:
        from agent import planner, study_push
        g = next((g for g in planner.goals(chat) if g['id'] == 'study'), None)
        if study_push.on() and g and g.get('per_week'):
            today = now.date()
            ws = planner.week_start(today)
            cal = {}
            for ev in data.get('events') or []:
                s = _span(ev)[0]
                if s and ws <= s.date() <= today:
                    cal.setdefault(s.date(), []).append(ev)
            data['study'] = {'goal': g['per_week'], 'done': planner.progress(chat, today, cal).get('study', 0),
                             'shifts': planner.shifts_on(chat, today)}
    except Exception as e:
        print(f'  ⚠️ proactive study: {e!r}'[:200])
    try:                                              # 11/11 what waits for his ✅ (read only, no lock needed)
        from agent import claude_job
        data['jobs'] = claude_job._load()
    except Exception as e:
        print(f'  ⚠️ proactive jobs: {e!r}'[:200])
    try:                                              # 14/11 what he still owes in Moodle (agent/obligations)
        from agent import obligations
        data['obligations'] = obligations.refresh(now, chat)
    except Exception as e:
        print(f'  ⚠️ proactive obligations: {e!r}'[:200])
    try:                                              # 19/11 a grade / the grader's feedback that came back (agent/grades)
        from agent import grades
        data['grades'] = grades.pending() if grades.enabled() else []
    except Exception as e:
        print(f'  ⚠️ proactive grades: {e!r}'[:200])
    data['partner'] = os.environ.get('WA_PARTNER_NAME', 'בת הזוג')
    return data


def tick(now: datetime | None = None, state_path: Path | None = None, gather_fn=None, quiet=None,
         chat: str | None = None) -> list[tuple[str, 'Out']]:
    """The worker's hook. Off unless SSA_PROACTIVE=1; scans every 20 min, speaks inside the budget only."""
    if not enabled():
        return []
    try:                                              # review 03/11: nothing here may ever break the worker's batch
        if chat is None:
            from whatsapp import outbox
            chat = outbox.owner_chat()
        if not chat:
            return []
        now = now or datetime.now()
        st = _load(state_path)
        if not isinstance(st.get('said'), dict) or not isinstance(st.get('days'), dict):
            st = {'said': {}, 'days': {}}             # a broken state file: start clean rather than crash
        if not may_speak(st, now, quiet):
            return []
        if st.get('scan') and now - datetime.fromisoformat(st['scan']) < SCAN_EVERY:
            return []
        st['scan'] = now.isoformat(timespec='seconds')
        try:
            picked = pick(notice(now, **(gather_fn or gather)(chat, now, st)), st, now, quiet)
        except Exception as e:
            print(f'  ⚠️ proactive notice: {e!r}'[:200])
            picked = []
        _save(st, state_path, now)                    # before sending: a key is never said twice
        if not picked:
            return []
        for n in picked:                              # 14/11 an obligation note: its items are announced / nudged
            if str(n.get('kind', '')).startswith('oblig'):
                try:
                    from agent import obligations
                    obligations.mark_said(n, now)
                except Exception as e:
                    print(f'  ⚠️ obligations mark: {e!r}'[:200])
            if n.get('kind') == 'grade_feedback':     # 19/11 its items are said (agent/grades' log)
                try:
                    from agent import grades
                    grades.mark_said(n, now)
                except Exception as e:
                    print(f'  ⚠️ grades mark: {e!r}'[:200])
        from agent.core import Out
        for n in picked:
            print(f"  💡 proactive: {n['key'][:80]}")
        return [(chat, Out('buttons', n['text'], buttons=n['buttons']) if n.get('buttons') else Out('text', n['text']))
                for n in picked]
    except Exception as e:
        print(f'  ⚠️ proactive tick: {e!r}'[:200])
        return []


# ── the owner's say over it: "פחות", "תפסיק עם המיילים", "תחזיר את ההכנות" (11/11) ──

LESS_RX = re.compile(r'^\s*(?:פחות|פחות הודעות|פחות יוזמה|פחות פניות|תיזום פחות|תפנה פחות|יותר מדי הודעות)\s*[.!]*\s*$')
LESS_THIS_RX = re.compile(r'^\s*(?:פחות|בלי|די)\s+(?:מזה|מאלה|כאלה|את זה)\s*[.!]*\s*$')
MORE_RX = re.compile(r'^\s*(?:יותר יוזמה|תיזום יותר|תחזור ליזום|תחזיר (?:את )?(?:ה)?יוזמ(?:ה|ות)|תפנה אליי שוב)\s*[.!]*\s*$')
STOP_ALL_RX = re.compile(r'^\s*(?:תפסיק|די|מספיק|בלי)\s+(?:ל)?(?:יזום|יוזמות|לפנות אליי|לפתוח (?:איתי )?שיחות)\s*[.!]*\s*$')
STOP_RX = re.compile(r'^\s*(?:תפסיק|די|מספיק)\s+(?:עם\s+|לשלוח\s+(?:לי\s+)?|להציע\s+(?:לי\s+)?|להזכיר\s+(?:לי\s+)?|'
                     r'לשאול\s+(?:אותי\s+)?|לעדכן\s+(?:אותי\s+)?)?(?:על\s+|את\s+)?([^,?]{2,40}?)\s*[.!]*\s*$')
BLI_RX = re.compile(r'^\s*בלי\s+(?:עוד\s+)?([^\s,?]+(?:\s+[^\s,?]+)?)\s*[.!]*\s*$')   # "בלי הכנות", not "בלי מייל, תשלח…"
BACK_RX = re.compile(r'^\s*(?:תחזיר|תחזור\s+(?:ל)?(?:שלוח|להזכיר|לשאול|לעדכן)(?:\s+על)?)\s+(?:את\s+)?(.{2,40}?)\s*[.!]*\s*$')


def _group_in(words: str) -> str:
    return next((g for g, rx in GROUP_RX if rx.search(words or '')), '')


def is_muted(group: str, path: Path | None = None, now: datetime | None = None) -> bool:
    return muted(_load(path), group, now)


def commands(text: str, path: Path | None = None, now: datetime | None = None) -> str | None:
    """→ the reply, or None when the message isn't about the bot's initiative. Deterministic, persisted."""
    t = (text or '').strip()
    if not t or len(t) > 60:
        return None
    now = now or datetime.now()
    st = _load(path)
    reply = None
    if LESS_RX.match(t):
        c = max(1, cap(st) - 1)
        st['cap'] = c
        reply = (f'🔕 בסדר — מעכשיו עד {"פנייה אחת" if c == 1 else f"{c} פניות"} ביוזמתי ביום. '
                 '"תפסיק ליזום" עוצר הכול, "יותר יוזמה" מחזיר.')
    elif LESS_THIS_RX.match(t) and st.get('last_kind') and st.get('last', '') >= (now - timedelta(hours=12)).isoformat():
        reply = _mute(st, group_of(st['last_kind']), now)
    elif STOP_ALL_RX.match(t):
        st['cap'] = 0
        reply = '🔕 הפסקתי לפנות ביוזמתי (תזכורות שביקשת ממשיכות כרגיל). "תחזור ליזום" מחזיר.'
    elif MORE_RX.match(t):
        st['cap'] = DAILY_CAP
        reply = f'🔔 חוזר ליזום — עד {DAILY_CAP} פניות ביום, רק כשיש משהו שימושי.'
    elif (m := BACK_RX.match(t)) and (g := _group_in(m.group(1))):
        (st.get('muted') or {}).pop(g, None)
        reply = f'🔔 מחזיר: {GROUPS[g][1]}.'
    elif (m := STOP_RX.match(t) or BLI_RX.match(t)) and (g := _group_in(m.group(1))):
        reply = _mute(st, g, now)
    if reply is not None:
        _save(st, path, now)
    return reply


def _mute(st: dict, group: str, now: datetime) -> str:
    until = now + timedelta(days=MUTE_DAYS)
    st.setdefault('muted', {})[group] = until.isoformat(timespec='seconds')
    name = GROUPS.get(group, (None, group))[1]
    word = {'mail': 'המיילים', 'calendar': 'ההתנגשויות', 'promise': 'ההבטחות', 'prep': 'ההכנות', 'gift': 'המתנות',
            'deadline': 'הדדליינים', 'waiting': 'האישורים', 'escalate': 'המעקב', 'study': 'הלמידה',
            'oblig': 'המטלות'}.get(group, group)
    return f'🔕 בסדר — בלי {name} ביוזמתי (עד {until:%d/%m}). "תחזיר את {word}" מחזיר.'


def answer(chat: str, op: str, oid: str, now: datetime | None = None, path: Path | None = None,
           life_path: Path | None = None, closed_path: Path | None = None) -> tuple[str, str]:
    """A tap on a secretary note → (reply, text to route as if the owner wrote it, or ''). Acts on HIS things only:
    a reminder of his, a closed item of his, a mute. Nothing goes to anyone else from here."""
    from agent import closure, life
    now = now or datetime.now()
    st = _load(path)
    o = (st.get('offers') or {}).get(oid)
    if not o:
        return 'ההצעה הזו כבר לא בתוקף.', ''
    title = o.get('title') or 'זה'
    try:
        start = datetime.fromisoformat(o['start'])
    except (KeyError, ValueError):
        start = None
    if op == 'mute':
        reply = _mute(st, group_of(o.get('kind', '')), now)
        _save(st, path, now)
        return reply, ''
    if op == 'ok':
        return '👍', ''
    if op == 'done':
        closure.record(chat, title, 'done', origin='note', now=now, path=closed_path)
        life.close_matching(chat, title, life_path)
        return f'✅ סגרתי: {title}. לא אזכיר יותר.', ''
    if op in ('odone', 'oirr') and o.get('oid'):        # 14/11 an obligation: closed here, in closure and deadlines
        from agent import obligations
        r = obligations.close(chat, o['oid'], 'done' if op == 'odone' else 'irrelevant', now=now, closed_path=closed_path)
        if not r:
            return 'לא מצאתי את זה ברשימה — כנראה כבר נסגר.', ''
        return (f'✅ מעולה — סימנתי ש{title} נעשה. לא אזכיר יותר.' if op == 'odone'
                else f'🔕 בסדר — {title} לא רלוונטי, לא אזכיר יותר.'), ''
    if op in ('awy', 'awn'):                            # 20/11 "are these your trip dates?" (agent/away)
        from agent import away
        return away.confirm(chat, o, op == 'awy', now=now), ''
    if op == 'draft':
        name = o.get('name') or ''
        return ('', f'תנסח לי ברכת יום הולדת קצרה וחמה ל{name}') if name else ('👍', '')

    def remind(text: str, at: datetime, important: bool = False) -> str:
        if at <= now + timedelta(minutes=2):
            return f'⌛ כבר מאוחר מדי לתזכורת על {title}.'
        rec = life.add_reminder(chat, {'text': text, 'when': at.isoformat(timespec='minutes'), 'important': important},
                                life_path)
        return life.describe_reminder(rec) + '\n\nנקבע ✅'

    if op.startswith('lead') and start:
        try:
            lead = int(op[4:])
        except ValueError:
            return '🤷', ''
        return remind(f'לצאת ל{title}', start - timedelta(minutes=lead)), ''
    if op == 'imp' and start:
        return remind(f'לצאת ל{title}', start - timedelta(minutes=int(o.get('lead') or 60)), important=True), ''
    if op in ('tom10', 'impd', 'at1h'):
        at = now + timedelta(hours=1) if op == 'at1h' else (now + timedelta(days=1)).replace(hour=10, minute=0,
                                                                                             second=0, microsecond=0)
        if op == 'impd' and start and start.date() <= at.date():
            at = now + timedelta(hours=1)                   # due today/tomorrow: don't wait for tomorrow 10:00
        what = f'לסדר מתנה / ברכה: {title}' if o.get('kind') == 'gift' else f'לטפל: {title}'
        return remind(what, at, important=(op == 'impd')), ''
    return '🤷', ''


def waiting_text(chat: str) -> str:
    """11/11 "מה מחכה לי?": everything that waits for the owner — his ✅, his tasks, what the bot follows up. Read only."""
    out = []
    try:
        from whatsapp import outbox
        qs = outbox.open_questions(chat)
        if qs:
            out += ['שאלות שמחכות לתשובה שלך (כפתורים):'] + [f"• {(q.get('text') or '').splitlines()[0][:120]}" for q in qs[-5:]]
    except Exception:
        pass
    try:
        from agent import tasks
        ts = tasks.open_tasks(chat)
        if ts:
            out += ['משימות פתוחות:'] + [f"• {t.get('text', '')}" + (f" (עד {t['due']})" if t.get('due') else '') for t in ts[:10]]
    except Exception:
        pass
    try:
        from agent import life
        soon = (datetime.now() + timedelta(hours=36)).isoformat()
        rs = sorted((r for r in life._load()['reminders'] if r.get('chat') == chat and r.get('active')
                     and str(r.get('due', '')) <= soon), key=lambda r: r['due'])
        if rs:
            out += ['תזכורות ב-36 השעות הקרובות:'] + [life.describe_reminder(r) for r in rs[:8]]
    except Exception:
        pass
    try:
        from agent import escalate
        es = escalate.open_items(chat)
        if es:
            out += ['במעקב כי סימנת חשוב ולא הגבת:'] + [f"• {i['text']}" for i in es]
    except Exception:
        pass
    try:                                              # 14/11 Moodle: what he still owes (agent/obligations)
        from agent import obligations
        now_ = datetime.now()
        ob = obligations.open_items(obligations.load(), now_)
        if ob:
            out += ['מטלות ובחנים פתוחים במודל (פירוט: "מה חסר לי"):'] + [obligations.line(r, now_, link=False) for r in ob[:6]]
    except Exception:
        pass
    try:
        from agent import claude_job
        ready = [j for j in claude_job._load().values() if isinstance(j, dict) and j.get('status') == 'ready']
        if ready:
            out += ['משימות של קלוד שמחכות לפריסה (✅ שלך):'] + [f"• {claude_job._label(j)}" for j in ready[:5]]
    except Exception:
        pass
    try:
        from agent import coordinator
        now = datetime.now().isoformat(timespec='seconds')
        co = [t for t in coordinator.load().values() if isinstance(t, dict) and t.get('status') in ('active', 'waiting_owner')
              and t.get('expires', '') > now]
        if co:
            out += ['תיאומים פתוחים מול אנשים:'] + [f"• {t.get('name', '')}: " + ('מחכה לך' if t['status'] == 'waiting_owner'
                                                                                  else 'מחכה לתשובה שלהם') for t in co[:5]]
    except Exception:
        pass
    return '\n'.join(out) or 'אין כרגע שום דבר שמחכה למשתמש'



# ── dry run: what it would have said ────────────────────────────────────────

def replay(days: int = 7, chat: str | None = None, until: datetime | None = None) -> list[tuple[datetime, dict]]:
    """Walk the last `days` days (09:00 / 13:00 / 18:00) on the real data, with a throwaway budget. Nothing saved."""
    if chat is None:
        from whatsapp import outbox
        chat = outbox.owner_chat()
    until = until or datetime.now()
    st = {'said': {}, 'days': {}}
    out = []
    for i in range(days, -1, -1):
        for h in (9, 13, 18):
            t = (until - timedelta(days=i)).replace(hour=h, minute=0, second=0, microsecond=0)
            if t > until:
                continue
            for n in pick(notice(t, **gather(chat, t, st)), st, t, quiet=lambda _n: False):
                out.append((t, n))
    return out


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=True)
    except ImportError:
        pass
    n = int(sys.argv[sys.argv.index('--replay') + 1]) if '--replay' in sys.argv else 0
    if n:
        for t, note in replay(n):
            print(f"\n[{t:%a %d/%m %H:%M}] {note['key']}\n{note['text']}")
    else:
        from whatsapp import outbox
        c = outbox.owner_chat()
        now = datetime.now()
        for note in notice(now, **gather(c, now)):
            print(f"\n{note['key']}\n{note['text']}")
