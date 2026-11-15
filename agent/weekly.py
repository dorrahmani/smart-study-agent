"""
agent/weekly.py — Sunday's morning message adds the week ahead, the system's health, and what the bot cost.

13/10: the weekly OAuth check had failed two Sundays in a row (NotebookLM cookie) and the alerts went to Telegram,
which the owner doesn't read. No LLM: calendar, the my.tau cache, watcher states, /tmp/cron_health.log, data/usage.jsonl.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRON_HEALTH = Path('/tmp/cron_health.log')
MONTH_CAP = 50.0                                     # utils/budget lock


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def health_lines(now: datetime | None = None, cron_log: Path | None = None) -> list[str]:
    """Only what's wrong (and one ✅ line when nothing is)."""
    now = now or datetime.now()
    bad = []
    tw = _json(ROOT / 'data' / 'tau_watch_state.json')
    last = tw.get('last_ok')
    if not last or now - datetime.fromisoformat(last) > timedelta(hours=30):
        bad.append(f"⚠️ אתר האוניברסיטה: הבדיקה האחרונה שהצליחה — {last[:16].replace('T', ' ') if last else 'אף פעם'}")
    from integrations import tau_mail
    if tau_mail.configured():
        ml = _json(tau_mail.STATE).get('last_ok')
        if not ml or now - datetime.fromisoformat(ml) > timedelta(hours=3):
            bad.append(f"⚠️ מייל האוניברסיטה לא נבדק מאז {ml[:16].replace('T', ' ') if ml else 'אף פעם'}")
    nlm = _json(ROOT / 'data' / 'nlm_health.json').get('failed_at')       # agent/nlm_ask: a failed NotebookLM answer
    if nlm and now - datetime.fromisoformat(nlm) < timedelta(days=7):
        bad.append(f"⚠️ NotebookLM התנתק ({nlm[8:10]}/{nlm[5:7]}) — שאלות עוברות לחיפוש הרגיל. "
                   f"להתחברות מחדש: \"תתחבר מחדש ל-NotebookLM\" אצל קלוד (5 דקות)")
    fails: dict[str, int] = {}
    last_ok: dict[str, bool] = {}
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d %H:%M')
        for ln in (cron_log or CRON_HEALTH).read_text(encoding='utf-8', errors='replace').splitlines():
            m = re.match(r'\[(\S+ \S+) → [^\]]+\] (.+) exit=(\d+)', ln)
            if not m:
                continue
            last_ok[m.group(2)] = m.group(3) == '0'
            if m.group(1) >= since and m.group(3) != '0':
                fails[m.group(2)] = fails.get(m.group(2), 0) + 1
    except OSError:
        pass
    fails = {k: v for k, v in fails.items() if not last_ok.get(k)}   # 15/10: fixed since (its latest run passed) → quiet
    for name, n in sorted(fails.items(), key=lambda x: -x[1]):
        hint = ' — העוגייה של NotebookLM פגה, צריך התחברות מחדש בשרת' if name == 'OAuth Health' else ''
        bad.append(f'⚠️ {name}: נכשל {n} פעמים השבוע{hint}')
    return bad or ['✅ הכל תקין — כל הבדיקות האוטומטיות עבדו השבוע']


def cost_lines(now: datetime | None = None, usage: Path | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    week, month = (now - timedelta(days=7)).isoformat(), now.strftime('%Y-%m')
    w = m = 0.0
    try:
        for ln in (usage or ROOT / 'data' / 'usage.jsonl').read_text(encoding='utf-8').splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            c = float(r.get('cost_usd') or 0)
            if r.get('ts', '') >= week:
                w += c
            if r.get('ts', '').startswith(month):
                m += c
    except OSError:
        return []
    return [f'💰 השבוע: ${w:.2f} · החודש: ${m:.2f} (מתוך תקרה של ${MONTH_CAP:.0f})']


def week_text(today: date | None = None, u: dict | None = None) -> str:
    """The week ahead: the calendar (lessons included once the timetable syncs) — the exams/deadlines are in the brief."""
    from integrations import gcal
    today = today or date.today()
    parts = []
    try:
        if gcal.configured():
            parts.append(gcal.week_text(today + timedelta(days=1), 6).replace('הימים הקרובים', 'הימים הבאים'))
    except Exception:
        pass
    from integrations import gcal as _g                    # 15/10: lessons live in the owner's studies calendar already
    if not (u or {}).get('timetable_sync') and not any(c.get('kind') == 'lessons' for c in _g.read_calendars()):
        try:
            from integrations import tau_portal
            from agent import timetable
            events, _, _ = tau_portal.schedule(live=False)
            end = (today + timedelta(days=7)).isoformat()
            n = len([e for e in timetable.lessons(events or [], today) if e['date'] < end])
            if n:
                parts.append(f'🎓 השבוע {n} שיעורים ותרגולים לפי אתר האוניברסיטה (לא ביומן — "תוסיף את מערכת השעות ליומן")')
        except Exception:
            pass
    return '\n\n'.join(parts)


def sunday_extra(now: datetime | None = None, u: dict | None = None) -> str:
    now = now or datetime.now()
    sections = [week_text(now.date(), u), '*⚙️ מצב המערכת*\n' + '\n'.join(health_lines(now) + cost_lines())]
    return '\n\n'.join(s for s in sections if s.strip())
