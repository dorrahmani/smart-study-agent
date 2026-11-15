"""
agent/study_push.py — learning content comes when the owner asks for it (05/11).

The owner: nice questions, no time for all of them; only when he asks. At most the bot may OFFER, when he struggles with a topic
or says he feels weak in it. So:

    on()      the single switch for everything that pushes study at him on its own: the morning page's study items,
              the "0/4 סבבי למידה" note, the exam plan / resume offers, the eve-of-exam review button, the idle session
              nudge, the weekly practice set, Friday's "מה ללמוד", the daily flashcards. Default off; SSA_STUDY_PUSH=1
              brings them back. What he asks for ("תן לי תרגילים", "תתחיל חזרה", a session he scheduled) never depends on it.
    offer()   one line under a reply, only after "קשה לי בבייס" / "אני חלש בהסתברות" or the third question on one topic in a
              week. At most once per topic per week and once in two days overall. It names the sentence to write and
              never sends a question by itself. No LLM.

scripts/pre_lecture_primer.py is deliberately not behind this switch (it has its own owner).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'study_offers.json'
TOPIC_EVERY = timedelta(days=7)
ANY_EVERY = timedelta(days=2)
REPEAT = 3                          # the third question on one topic within REPEAT_DAYS
REPEAT_DAYS = 7

STRUGGLE_RX = re.compile(r'קשה\s+לי|מתקש[הת]|מסתבכ|מסובך\s+לי|לא\s+(?:מבינ[הת]?|הבנתי|קולט|קולטת|מצליח|מצליחה|הולך\s+לי|'
                         r'מסתדר\s+לי|יורד\s+לי)|(?:אני|מרגיש|מרגישה)\s+(?:\S+\s+)?חלש|חלשה?\s+ב|אבוד\s+ב|נופל\s+ב')
ASKED_RX = re.compile(r'תן\s+לי|תביא\s+לי|תעשה\s+לי|בחן\s+אותי|תבחן|נתרגל|תתרגל|חידון|תתחיל|נמשיך')
FINDING_RX = re.compile(r'(?<![א-ת])(?:מתי|איפה|חדר|קובץ|קבצים|הקלטה|מצגת|שעות|שעה|להיכנס|למצוא|להוריד)(?![א-ת])')
QUESTION_RX = re.compile(r'\?|^\s*(?:מה|איך|למה|מדוע|תסביר|תסבירי|הסבר|אפשר\s+הסבר)(?![א-ת])')
LOGISTICS_RX = re.compile(r'(?<![א-ת])(?:מתי|איפה|חדר|מועד|הגשה|להגיש|ציון|ציונים|שעות|שעה|קובץ|קבצים|סיכום|הקלטה|מצגת|'
                          r'מרצה|מתרגל|מתרגלת|מבחן|קורס)(?![א-ת])')


def on() -> bool:
    return os.environ.get('SSA_STUDY_PUSH', '').strip() == '1'


def _courses() -> dict[str, str]:
    """{the course's first long word: its name} for this semester's courses ('הסתברות', 'מיקרו', 'מאקרו', 'תוכנה')."""
    from config.paths import ACTIVE_COURSES, COURSE_NAMES
    out = {}
    for ck in ACTIVE_COURSES:
        name = COURSE_NAMES.get(ck, '')
        first = re.findall(r'[א-ת]{4,}', name)[:1]
        if first:
            out[first[0]] = re.sub(r'\s+[א-ת]׳$', '', name)
    return out


def topic_of(text: str, rows_by_course: dict | None = None) -> dict | None:
    """{'key', 'label', 'kind': 'topic'|'course'} — a unit of the exam map first, else a course named outright."""
    from agent import exam_weight as ew
    t = text or ''
    courses = _courses()
    bare = t
    for word in courses:
        bare = re.sub(rf'[בלהו]?{word}', ' ', bare)
    bare = bare + ' ' + ' '.join(w[1:] for w in re.findall(r'[א-ת]{5,}', bare) if w[0] in 'בלהומ')
    maps = rows_by_course if rows_by_course is not None else {ck: ew.load(ck).get('stats') or [] for ck in ew.courses()}
    for ck, rows in maps.items():
        r = ew.find(bare, rows, ck) if rows else None
        if r:
            short = r['topic'].split(' (')[0]
            return {'key': f'{ck}|{r["topic"]}', 'label': short, 'kind': 'topic'}
    for word, name in courses.items():
        if word in t:
            return {'key': f'course|{word}', 'label': name, 'kind': 'course'}
    return None


def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save(d: dict, path: Path | None = None) -> None:
    p = path or STATE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    except OSError as e:
        print(f'  ⚠️ study offers: {e}')


def _repeated(topic: dict, text: str, now: datetime, convo: list[dict], rows_by_course: dict | None) -> bool:
    """This is at least the REPEAT-th question of his on the topic within REPEAT_DAYS."""
    cut = (now - timedelta(days=REPEAT_DAYS)).isoformat()
    n = 1
    for r in convo or []:
        said = (r.get('in') or '').strip()
        if r.get('role') != 'owner' or r.get('forwarded') or r.get('kind') not in ('text', 'voice') \
                or r.get('ts', '') < cut or said == text.strip():
            continue
        if QUESTION_RX.search(said) and not LOGISTICS_RX.search(said) and \
                (t := topic_of(said, rows_by_course)) and t['key'] == topic['key']:
            n += 1
    return n >= REPEAT


def offer(chat: str, text: str, now: datetime | None = None, path: Path | None = None, convo: list[dict] | None = None,
          rows_by_course: dict | None = None) -> str:
    """One line to put under the reply, or ''. Saves that it offered, so it stays rare."""
    t = (text or '').strip()
    if not t or len(t) > 400 or ASKED_RX.search(t):
        return ''
    struggling = bool(STRUGGLE_RX.search(t)) and not FINDING_RX.search(t)    # "לא מצליח למצוא את המצגת" isn't a weak topic
    if not struggling and (not QUESTION_RX.search(t) or LOGISTICS_RX.search(t)):
        return ''
    topic = topic_of(t, rows_by_course)
    if not topic:
        return ''
    now = now or datetime.now()
    st = _load(path)
    me = st.setdefault(chat, {})
    seen = me.setdefault('topics', {})
    if (last := me.get('last')) and now - datetime.fromisoformat(last) < ANY_EVERY:
        return ''
    if (at := seen.get(topic['key'])) and now - datetime.fromisoformat(at) < TOPIC_EVERY:
        return ''
    if not struggling:
        if convo is None:
            from agent import proactive
            convo = proactive._convo(now)
        if not _repeated(topic, t, now, convo, rows_by_course):
            return ''
    stamp = now.isoformat(timespec='seconds')
    seen[topic['key']] = me['last'] = stamp
    me['topics'] = {k: v for k, v in seen.items() if v >= (now - timedelta(days=60)).isoformat()}
    _save(st, path)
    ask = f'תביא לי שאלה על {topic["label"]}' if topic['kind'] == 'topic' else f'תן לי תרגילים ב{topic["label"]}'
    return f'💡 אם בא לך לתרגל את זה קצת, כתוב "{ask}".'
