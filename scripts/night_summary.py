"""scripts/night_summary.py — Daily 23:00 push: what was learned/done today.

RETIRED 10/11/2026: it only ever went to Telegram (deleted), so by default it no longer sends — moving it to WhatsApp
would start a new daily message the owner never asked for there. `--send` sends it on WhatsApp. The cron line can go.

Cron suggestion (20:00 UTC = 23:00 Israel):
  0 20 * * *  /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Night Summary" \
    bash -c "cd /home/ubuntu/smart_study_agent && venv/bin/python3 scripts/night_summary.py"
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

if '--send' not in sys.argv:
    print('night summary: retired 10/11 (it only ever went to Telegram, now deleted) — not sending')
    sys.exit(0)

from config.paths import SUMMARIES_DIR, COURSE_NAMES


today = date.today()
today_str = today.isoformat()

# Skip on no-class days
nc_path = ROOT / 'data' / 'no_class_days.json'
if nc_path.exists():
    try:
        nc = json.loads(nc_path.read_text(encoding='utf-8'))
        if today_str in (nc.get('dates') or []):
            print(f'⛔ {today_str} no-class — skipping.')
            sys.exit(0)
    except Exception: pass

# ── 1. Summaries created/modified today ──────────────────────────────────
today_summaries: list[tuple[str, str, Path]] = []  # (course, kind, path)
for ck in COURSE_NAMES:
    for sub in ('lectures', 'tutorials'):
        d = SUMMARIES_DIR / ck / sub
        if not d.exists(): continue
        for md in d.glob('*.md'):
            if md.stat().st_size < 5000: continue
            mt = datetime.fromtimestamp(md.stat().st_mtime).date()
            if mt == today:
                today_summaries.append((ck, sub, md))

# ── 2. Flashcards reviewed today ─────────────────────────────────────────
fc_reviewed = 0
fc_correct = 0
deck_path = ROOT / 'data' / 'flashcards_deck.json'
if deck_path.exists():
    try:
        deck = json.loads(deck_path.read_text(encoding='utf-8'))
        for c in deck:
            if c.get('last_review') == today_str:
                fc_reviewed += 1
                if (c.get('last_grade') or 0) >= 3:
                    fc_correct += 1
    except Exception: pass

# ── 3. Costs today ──────────────────────────────────────────────────────
try:
    from utils.usage_tracker import summary as cost_sum
    s = cost_sum('today')
    cost_today = float(s.get('total_usd', 0))
    audio_today = float(s.get('audio_minutes_total', 0))
except Exception:
    cost_today = 0
    audio_today = 0

# ── 4. Deadlines coming up (next 3 days) ────────────────────────────────
upcoming = []
cache_path = ROOT / 'data' / 'assignments' / '_cache.json'
if cache_path.exists():
    try:
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        now = datetime.now()
        cutoff = now + timedelta(days=3)
        for a in cache.get('assignments', []) or []:
            if not isinstance(a, dict) or a.get('submitted'): continue
            due = a.get('due_date')
            if not due: continue
            try:
                dt = datetime.fromisoformat(due)
                if now < dt < cutoff:
                    upcoming.append((dt, a))
            except: continue
        upcoming.sort()
    except Exception: pass

# ── 5. Exam mode courses ────────────────────────────────────────────────
exam_courses = {}
exam_state_path = ROOT / 'data' / 'exam_mode.json'
if exam_state_path.exists():
    try:
        exam_state = json.loads(exam_state_path.read_text(encoding='utf-8'))
        exam_courses = exam_state.get('courses', {}) or {}
    except Exception: pass

# ── Build message ────────────────────────────────────────────────────────
lines = [f'🌙 *סיכום היום — {today.strftime("%d/%m")}*', '']

# Productivity stats
stats: list[str] = []
if today_summaries:
    stats.append(f'📚 {len(today_summaries)} סיכומים חדשים נוצרו')
if fc_reviewed:
    pct = round(100 * fc_correct / fc_reviewed) if fc_reviewed else 0
    stats.append(f'🎯 {fc_reviewed} כרטיסיות חזרתי ({fc_correct} ✓ = {pct}%)')
if audio_today > 0:
    stats.append(f'🎙️ {audio_today:.0f} דקות הקלטה תומללו')
if cost_today > 0.01:
    stats.append(f'💰 עלות AI: ${cost_today:.2f}')

if stats:
    lines.append('*מה קרה היום:*')
    for s in stats:
        lines.append(f'  • {s}')
    lines.append('')
else:
    lines.append('☕ יום שקט — בלי פעילות גדולה.')
    lines.append('')

# Highlight today's summaries
if today_summaries:
    lines.append('*סיכומים חדשים:*')
    for ck, sub, md in today_summaries[:5]:
        cname = COURSE_NAMES.get(ck, ck)
        icon = '📝' if sub == 'tutorials' else '📖'
        lines.append(f'  {icon} {cname}: {md.stem}')
    if len(today_summaries) > 5:
        lines.append(f'  …ועוד {len(today_summaries)-5}')
    lines.append('')

# Exam mode reminder
if exam_courses:
    lines.append('🚨 *מצב בחינה:*')
    for ck, info in sorted(exam_courses.items(), key=lambda x: x[1].get('days_left', 999)):
        ck_he = COURSE_NAMES.get(ck, ck)
        lines.append(f'  • {ck_he}: עוד {info.get("days_left","?")} ימים')
    lines.append('')

# Upcoming deadlines
if upcoming:
    lines.append(f'⏰ *דדליינים ב-3 ימים הקרובים:*')
    for dt, a in upcoming[:5]:
        days_left = (dt.date() - today).days
        label = 'מחר' if days_left == 1 else (f'בעוד {days_left} ימים' if days_left > 0 else 'היום!')
        ck = a.get('course_key', '?')
        title = (a.get('title') or '?')[:60]
        lines.append(f'  • [{label}] {COURSE_NAMES.get(ck, ck)}: {title}')
    lines.append('')

# Encouragement
if today_summaries or fc_reviewed >= 3:
    lines.append('💪 *יום פרודקטיבי!* לילה טוב.')
elif upcoming and any((dt.date() - today).days <= 1 for dt, _ in upcoming):
    lines.append('⚠️ *יש דדליין בקרוב.* בוקר טוב לפעולה.')
else:
    lines.append('😴 *תשנו טוב.* מחר ביום חדש.')

msg = '\n'.join(lines)

# ── Send ─────────────────────────────────────────────────────────────────
# WhatsApp only (10/11: Telegram deleted). A failed digest exits non-zero so run_with_alert.sh records it.
from utils.notify import deliver  # noqa: E402
if not deliver(msg, markdown=True):
    raise SystemExit('night summary: WhatsApp send failed (kept in data/undelivered_alerts.jsonl)')
print(f'✅ night summary sent ({len(msg)} chars)')
