"""
agent/consistency.py — the nightly consistency check (19/11, improvement 19). No LLM.

Every store keeps its own idea of the truth; this looks for places where two of them disagree:

    reminder_closed   an active reminder about something the owner closed (agent/closure)
    reopened          a closed task with a newer open task of the same mail / thread / words; an obligation he closed
                      that is open again
    schedule          a session the university cancelled (agent/schedule, from Moodle / mail) still on the calendar as
                      happening; a change for a session the university site doesn't have; a courses.yaml slot that the
                      site's timetable doesn't have
    missed            an obligation whose close and cut-off both passed, never closed (nobody nudges those any more)
    duplicate         two calendar events the bot created for the same thing (same tag key)
    orphan_tmp        a half-written *.tmp under data/ older than 2 days (its writer replaced or died)
    orphan_file       a data file no code names, untouched for 30 days (reported in the file only)

Fixes — only what is provably safe and reversible, each one logged in data/consistency_fixes.jsonl with its undo:
    • a one-off reminder whose words are exactly a closed item's → deactivated (active=False, closed_by='consistency')
    • an orphan *.tmp → moved aside to data/_moved_aside/consistency-<date>/ (never deleted)
Everything else is reported. The owner hears about it at most once a day, only new non-trivial findings
(scripts/consistency_check.py, through utils.notify: quiet hours → the morning digest).
Third-party text (titles, mail subjects) is data only.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
REPORT = DATA / 'consistency_report.json'
FIXES = DATA / 'consistency_fixes.jsonl'
STATE = DATA / 'consistency_state.json'
ASIDE = DATA / '_moved_aside'
TMP_AGE = timedelta(days=2)
ORPHAN_AGE = timedelta(days=30)
TRIVIAL = {'orphan_tmp', 'orphan_file'}
WEEKDAYS = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}


def _f(kind: str, key: str, text: str, **kw) -> dict:
    return {'kind': kind, 'key': f'{kind}|{key}', 'text': text, **kw}


def _norm(t: str) -> str:
    return re.sub(r'[\W_]+', ' ', (t or '').lower()).strip()


# ── the checks (pure: every source passed in) ───────────────────────────────

def reminders_for_closed(reminders: list[dict], closed: list[dict]) -> list[dict]:
    from agent import closure
    out = []
    for r in reminders:
        if not r.get('active'):
            continue
        c = closure.match([x for x in closed if not x.get('chat') or x.get('chat') == r.get('chat')], r.get('text', ''))
        if not c:
            continue
        exact = r.get('repeat', 'none') == 'none' and _norm(r.get('text')) == _norm(c.get('text'))
        out.append(_f('reminder_closed', r.get('id', ''), f"תזכורת פעילה על משהו שסגרת: \"{r.get('text', '')[:60]}\"",
                      fix='deactivate' if exact else '', rid=r.get('id'), chat=r.get('chat')))
    return out


def reopened(tasks: list[dict], store: dict | None) -> list[dict]:
    from agent import closure
    out = []
    done = [t for t in tasks if t.get('status') != 'open']
    for t in tasks:
        if t.get('status') != 'open':
            continue
        for c in done:
            same_id = (t.get('mail') and t.get('mail') == c.get('mail')) or (t.get('thread') and t.get('thread') == c.get('thread'))
            if (same_id or closure.same(t.get('text', ''), c.get('text', ''))) and \
                    str(t.get('created', '')) > str(c.get('closed') or c.get('created') or ''):
                out.append(_f('reopened', f"task:{t.get('id')}",
                              f"משימה שסגרת נפתחה שוב: \"{t.get('text', '')[:60]}\" (נסגרה {str(c.get('closed', ''))[:10]})"))
                break
    for oid, r in ((store or {}).get('items') or {}).items():
        if r.get('closed_why') and not r.get('closed') and not r.get('gone'):
            out.append(_f('reopened', f'oblig:{oid}', f"מטלה שסגרת פתוחה שוב: {r.get('title', '')[:60]}"))
    return out


def missed(store: dict | None, now: datetime) -> list[dict]:
    from agent import obligations
    out = []
    for oid, r in ((store or {}).get('items') or {}).items():
        if obligations.status(r, now) == 'missed' and not r.get('unmarked'):
            out.append(_f('missed', oid, f"{r.get('title', '')[:60]} ({obligations._course_name(r.get('course', ''))}) — "
                                         f"הגשה נסגרה {str(r.get('due', ''))[:10]} ועדיין פתוחה אצלי. הגשת? אם כן — "
                                         f"\"הגשתי\" ואסגור"))
    return out


def schedule_issues(overrides: list[dict], lessons_cal: list[dict], portal: list[dict], my_sessions: dict,
                    today: date) -> list[dict]:
    """overrides: data/schedule_overrides.json · lessons_cal: the calendar's lesson events (any calendar) ·
    portal: the my.tau cache events · my_sessions: courses.yaml {ck: {'code': course_code, 'slots': my_sessions}}."""
    from agent import schedule
    out = []
    site = {(e['date'], (e.get('time') or '')[:5]) for e in portal if not e.get('is_exam') and e.get('time')}
    for o in overrides:
        if str(o.get('date', '')) < today.isoformat():
            continue
        if (o['date'], o.get('start', '')) not in site and portal:
            out.append(_f('schedule', f"nosite:{o['date']}:{o.get('ck')}:{o.get('start')}",
                          f"שינוי מ{o.get('source') or 'הודעה'} ל-{o['date'][8:]}/{o['date'][5:7]} {o.get('start')} "
                          f"— אבל באתר האוניברסיטה אין לך שיעור בשעה הזו", trivial=False))
        if o.get('status') != 'cancelled':
            continue
        for ev in lessons_cal:
            hit = schedule.for_event(ev, rows=[o])
            if hit and not (ev.get('summary') or '').lstrip().startswith('❌'):
                out.append(_f('schedule', f"cal:{ev.get('id')}:{o['date']}",
                              f"{ev.get('summary', '')} ב-{o['date'][8:]}/{o['date'][5:7]} {o.get('start')} בוטל "
                              f"({o.get('source') or 'הודעה'}), אבל ביומן הוא עדיין מופיע כרגיל"))
    if portal:                                      # courses.yaml slots the university site doesn't have
        horizon = (today + timedelta(days=14)).isoformat()
        for ck, c in (my_sessions or {}).items():
            code, slots = str(c.get('code') or '').lstrip('0'), c.get('slots') or []
            mine = [e for e in portal if not e.get('is_exam') and e.get('time') and today.isoformat() <= e['date'] <= horizon
                    and code and str(e.get('course_number') or '').lstrip('0') == code]
            if not mine:
                continue
            have = {(date.fromisoformat(e['date']).weekday(), e['time'][:5]) for e in mine}
            for s in slots:
                wd = WEEKDAYS.get(str(s.get('day', ''))[:3].lower())
                st = str(s.get('start', ''))[:5].zfill(5)
                if wd is not None and (wd, st) not in have:
                    out.append(_f('schedule', f'yaml:{ck}:{wd}:{st}',
                                  f"ב-config/courses.yaml רשום {ck} {s.get('type', '')} ביום {s.get('day')} {st}, "
                                  f"ובאתר האוניברסיטה אין את השעה הזו בשבועיים הקרובים"))
    return out


def duplicates(tagged: dict[str, list[dict]]) -> list[dict]:
    """tagged: {'tau_src=lessons': [events], 'ssa_src=task': [...], …} → the same key twice."""
    keyfield = {'tau_src=lessons': 'tau_key', 'ssa_src=task': 'task_id', 'ssa_src=office_hours': 'oh_key'}
    out = []
    for tag, evs in tagged.items():
        groups: dict = {}
        for ev in evs:
            priv = (ev.get('extendedProperties') or {}).get('private') or {}
            k = priv.get(keyfield.get(tag, '')) or f"{ev.get('summary')}|{(ev.get('start') or {}).get('dateTime') or (ev.get('start') or {}).get('date')}"
            groups.setdefault(k, []).append(ev)
        for k, g in groups.items():
            if len(g) > 1:
                out.append(_f('duplicate', f'{tag}:{k}', f"ביומן פעמיים (נוצר על ידי הבוט): {g[0].get('summary', '')} "
                                                         f"{str((g[0].get('start') or {}).get('dateTime') or (g[0].get('start') or {}).get('date') or '')[:16].replace('T', ' ')}",
                              ids=[e.get('id') for e in g]))
    return out


def orphans(data: Path, now: datetime, sources: str) -> list[dict]:
    """*.tmp older than TMP_AGE (to move aside) and top-level data files no source names (report only)."""
    out = []
    for p in data.rglob('*'):
        try:
            if not p.is_file() or ASIDE in p.parents or 'venv' in p.parts:
                continue
            age = now - datetime.fromtimestamp(p.stat().st_mtime)
        except OSError:
            continue
        rel = p.relative_to(data).as_posix()
        if re.search(r'\.tmp$|\.\d+\.tmp$|\.tmp\.\d+$', p.name) and age > TMP_AGE:
            out.append(_f('orphan_tmp', rel, f'קובץ זמני נטוש: data/{rel}', fix='move_aside', path=str(p), rel=rel))
        elif p.parent == data and age > ORPHAN_AGE and not re.search(r'\.(?:gz|bak|log|lock)$|\.bak\.|^\.|backup', p.name):
            stem = re.sub(r'\.(?:json|jsonl|txt|ics|md|csv|apkg)$', '', p.name)
            if p.name not in sources and stem not in sources:
                out.append(_f('orphan_file', rel, f'קובץ שאף קוד לא קורא: data/{rel}'))
    return out


# ── fixes (reversible, logged) ──────────────────────────────────────────────

def _log_fix(entry: dict, path: Path | None = None) -> None:
    p = path or FIXES
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def apply(findings: list[dict], now: datetime, life_path: Path | None = None, aside: Path | None = None,
          log_path: Path | None = None) -> list[dict]:
    """Do the safe fixes; → the findings that were fixed (each marked fixed=True and logged with its undo)."""
    done = []
    stamp = now.isoformat(timespec='seconds')
    for f in findings:
        try:
            if f.get('fix') == 'deactivate':
                from agent import life
                d = life._load(life_path)
                rec = next((r for r in d['reminders'] if r.get('id') == f['rid'] and r.get('active')), None)
                if not rec:
                    continue
                rec.update(active=False, closed=now.isoformat(timespec='minutes'), closed_by='consistency')
                life._save(d, life_path)
                _log_fix({'ts': stamp, 'kind': f['kind'], 'what': 'reminder deactivated', 'id': f['rid'],
                          'undo': "agent_life.json: set active=true, drop closed/closed_by"}, log_path)
            elif f.get('fix') == 'move_aside':
                src = Path(f['path'])
                if not src.exists():
                    continue
                base = (aside or ASIDE) / f"consistency-{now:%Y%m%d}"
                dst = base / f['rel']
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                _log_fix({'ts': stamp, 'kind': f['kind'], 'what': 'moved aside', 'from': str(src), 'to': str(dst),
                          'undo': f'mv "{dst}" "{src}"'}, log_path)
            else:
                continue
            f['fixed'] = True
            done.append(f)
        except Exception as e:
            print(f'  ⚠️ consistency fix {f.get("key")}: {e!r}'[:200])
    return done


# ── the report to the owner ───────────────────────────────────────────────────────

LABEL = {'reminder_closed': 'תזכורות על דברים סגורים', 'reopened': 'דברים שסגרת ונפתחו שוב',
         'schedule': 'המערכת לא תואמת לעדכוני האוניברסיטה', 'missed': 'מטלות שעבר המועד שלהן ועדיין פתוחות',
         'duplicate': 'כפילויות ביומן'}


def message(findings: list[dict], state: dict, today: date) -> tuple[str, list[str]]:
    """'' when there is nothing new and non-trivial, or he already heard today. → (text, the keys it reports)."""
    if state.get('sent_day') == today.isoformat():
        return '', []
    said = state.get('reported') or {}
    new = [f for f in findings if f['kind'] not in TRIVIAL and not f.get('fixed') and f['key'] not in said]
    if not new:
        return '', []
    lines = ['🔎 *בדיקת עקביות לילית* — מצאתי כמה דברים שלא מסתדרים:']
    for kind in LABEL:
        group = [f for f in new if f['kind'] == kind]
        if group:
            lines.append(f"\n*{LABEL[kind]}*")
            lines += [f"• {f['text']}" for f in group[:4]] + ([f'• ועוד {len(group) - 4}'] if len(group) > 4 else [])
    return '\n'.join(lines), [f['key'] for f in new]
