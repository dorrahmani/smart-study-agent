"""
agent/loops.py — closing loops by itself (The owner 20/10: "לא שישאל אותי כל דבר… שאם קרה משהו שיסמן לבד לסגור;
אם הגשתי — סמן שהגשתי, אבל תוודא שהגשתי גם").

    verify(key)        "✅ הגשתי" / "הגשתי את תרגיל 2" → Moodle is checked live before anything is closed:
                       submitted → closed (reminders off + the calendar deadline marked ✅); Moodle says NOT
                       submitted → one short warning, nothing closed; no way to check (a lecturer-site exercise,
                       a file resource) → closed on his word.
    close_tick()       every 30 min: whatever Moodle (or the owner) already shows as submitted → its "📝 הגשת …" event in
                       the calendar becomes "✅ …" — silently except one short line when something was closed.
No questions are asked here, and nothing outside the owner's own calendar is touched. No LLM.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'loops_state.json'
SUBMIT_RX = re.compile(r'^\s*(?:כבר\s+)?(?:הגשתי|הגשנו|העליתי|שלחתי)\s+(?:את\s+)?(?:ה)?(?:תרגיל|מטלה|עבודה|ממ"ן|תרגיל בית|ש\.?ב\.?)?\s*(\d{1,2})?'
                       r'|^\s*(?:הגשתי|הגשנו)\s*[.!]*\s*$')


def _load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save(d: dict, path: Path | None = None) -> None:
    p = path or STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')


def live_status(item: dict, fetch=None) -> str:
    """'yes' | 'no' | 'unknown' — Moodle's own answer for a Moodle assignment, right now."""
    url = item.get('url') or ''
    if 'moodle' not in url or item.get('kind', 'assign') != 'assign':
        return 'unknown'
    try:
        if fetch is None:
            from collectors.moodle_assignment_scraper import login_and_get_session, fetch_assignment_details
            s = login_and_get_session()
            if not s:
                return 'unknown'
            d = fetch_assignment_details(s, url) or {}
        else:
            d = fetch(url) or {}
    except Exception as e:
        print(f'  ⚠️ submission check failed: {e!r}'[:200])
        return 'unknown'
    if not d:
        return 'unknown'
    return 'yes' if d.get('submitted') else 'no'


def find_item(text: str, now: datetime | None = None, items: list[dict] | None = None) -> dict | None:
    """"הגשתי את תרגיל 2 בהסתברות" → the matching open assignment (due in the last 3 days or the next 30)."""
    from utils import deadlines as dl
    now = now or datetime.now()
    items = items if items is not None else dl.load_items()
    st = dl.load_state()
    cand = []
    for a in items:
        due = dl._due(a)
        if not due or not (now - timedelta(days=3) <= due <= now + timedelta(days=30)) or dl.is_done(a, st):
            continue
        cand.append(a)
    m = re.search(r'(\d{1,2})', text or '')
    if m:
        cand = [a for a in cand if dl._ex_num(a) == m.group(1)] or cand
    try:
        from config.paths import COURSE_NAMES
        named = [a for a in cand if any(w in (text or '') for w in re.findall(r'[א-ת]{4,}', COURSE_NAMES.get(a.get('course_key'), ''))[:1])]
        cand = named or cand
    except Exception:
        pass
    cand.sort(key=lambda a: dl._due(a))
    return cand[0] if len(cand) == 1 or (cand and m) else (cand[0] if len(cand) == 1 else None)


def verify(item: dict, fetch=None, state_path: Path | None = None) -> str:
    """The owner says he submitted → check, then close (or warn). → the reply (short)."""
    from utils import deadlines as dl
    title = item.get('title', 'המטלה')
    status = live_status(item, fetch)
    if status == 'no':
        return (f'⚠️ במודל עוד לא רואים הגשה של *{title}*. אולי הקובץ נשמר כטיוטה בלי "הגשה"?\n'
                f'הגשת בדרך אחרת? כתוב "הגשתי בכל זאת".')
    dl.mark_done(dl.key_of(item), state_path)
    closed = close_in_calendar(item)
    if status == 'yes':
        return f'✅ ראיתי במודל שהגשת את *{title}* — סגרתי' + (' (גם ביומן).' if closed else '.')
    return f'✅ סימנתי ש*{title}* הוגש' + (' וסגרתי ביומן' if closed else '') + ' (אין לי איך לבדוק את זה במודל).'


def _sig(s: str) -> set[str]:
    return {w for w in re.findall(r'[א-תA-Za-z]{2,}|\d+', s or '') if w not in ('הגשת', 'תרגיל', 'מטלה')}


def close_in_calendar(item: dict, events_on=None, apply=None) -> int:
    """The item's "📝 …" deadline event(s) around its due date → "✅ …". → how many were renamed."""
    from utils import deadlines as dl
    due = dl._due(item)
    if not due:
        return 0
    if events_on is None:
        from integrations import gcal
        if not gcal.configured():
            return 0
        events_on = gcal.events_on
    if apply is None:
        from agent import calendar_edit
        apply = calendar_edit.apply
    want = _sig(item.get('title', '')) | ({dl._ex_num(item)} - {''})
    n = 0
    for day in {due.date(), (due - timedelta(hours=3)).date()}:
        try:
            evs = events_on(day) or []
        except Exception:
            continue
        for ev in evs:
            s = ev.get('summary') or ''
            if not s.startswith('📝') and 'הגשת' not in s:
                continue
            if s.startswith('✅') or not (want & _sig(s)):
                continue
            try:
                apply({'event': ev, 'title': '✅ ' + re.sub(r'^📝\s*', '', s)})
                n += 1
            except Exception as e:
                print(f'  ⚠️ close in calendar failed: {e!r}'[:200])
    return n


def close_tick(now: datetime | None = None, state_path: Path | None = None) -> list[str]:
    """Submitted (Moodle's scrape / live check, or the owner's verified word) → close the calendar deadline, once."""
    from utils import deadlines as dl
    now = now or datetime.now()
    st = _load(state_path)
    if st.get('last') and now - datetime.fromisoformat(st['last']) < timedelta(minutes=30):
        return []
    st['last'] = now.isoformat(timespec='seconds')
    closed = set(st.get('closed') or [])
    dst = dl.load_state()
    out, newly = [], False
    for a in dl.load_items():
        due = dl._due(a)
        k = dl.key_of(a)
        if not due or k in closed or not (now - timedelta(days=3) <= due <= now + timedelta(days=60)):
            continue
        if not dl.is_done(a, dst):
            continue
        closed.add(k)
        newly = True
        try:
            if close_in_calendar(a):
                out.append(f"✅ סגרתי ביומן: {a.get('title', '')} — הוגש.")
        except Exception as e:
            print(f'  ⚠️ close tick: {e!r}'[:200])
    st['closed'] = sorted(closed)[-500:]
    _save(st, state_path)
    if newly and state_path is None:          # the course site shows ✅ now — even with no calendar event to close
        try:
            from scripts.build_course_site import refresh_soon
            refresh_soon()
        except Exception:
            pass
    return out
