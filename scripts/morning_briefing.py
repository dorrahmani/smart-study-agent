"""scripts/morning_briefing.py — the old Telegram morning briefing. RETIRED 10/11/2026 (Telegram bot deleted).
WhatsApp already has its own morning message (agent.core.morning_brief_text), so this one no longer sends by
default — it would only duplicate it. `--send` still sends it on WhatsApp (utils.notify). The cron line can go.
Includes: new summaries since yesterday, upcoming deadlines, today's lectures."""
from __future__ import annotations
import os, sys, json, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime, timedelta, date

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

if '--send' not in sys.argv:
    print('morning briefing: retired 10/11 (Telegram deleted; WhatsApp has its own morning message) — not sending')
    sys.exit(0)

from config.paths import SUMMARIES_DIR, COURSE_NAMES


today = date.today()
yesterday = today - timedelta(days=1)

# Skip briefing on user-marked "no class" days
nc_path = ROOT / 'data' / 'no_class_days.json'
if nc_path.exists():
    try:
        nc_data = json.loads(nc_path.read_text(encoding='utf-8'))
        if today.isoformat() in (nc_data.get('dates') or []):
            print(f'⛔ {today} marked as no-class day — skipping briefing.')
            sys.exit(0)
    except Exception:
        pass

# ── 1. New summaries since yesterday ──────────────────────────────────────
recent_summaries = []
for ck in COURSE_NAMES:
    for sub in ('lectures', 'tutorials'):
        d = SUMMARIES_DIR / ck / sub
        if not d.exists(): continue
        for md in d.glob('*.md'):
            if md.stat().st_size < 5000: continue
            # File mtime within last 24h?
            mtime = datetime.fromtimestamp(md.stat().st_mtime).date()
            if mtime >= yesterday:
                recent_summaries.append((ck, sub, md))
recent_summaries.sort(key=lambda x: x[2].stat().st_mtime, reverse=True)

# ── 2. Upcoming deadlines (next 7 days) ──────────────────────────────────
# Only what is NOT submitted yet (Moodle status, or "✅ הגשתי" in a reminder) — see utils/deadlines.
upcoming = []
try:
    from utils.deadlines import pending
    upcoming = [(a['due'], a) for a in pending(horizon_days=7)]
except Exception as e:
    print(f'deadlines read error: {e}')

# ── 3. Today's lectures (per Panopto metadata if any new today) ───────────
from config.paths import PANOPTO_DIR
today_lectures = []
for srt in PANOPTO_DIR.glob('*.srt'):
    if srt.name.endswith('.bad') or srt.name.endswith('.bak'): continue
    stem = srt.stem
    for ck in sorted(COURSE_NAMES, key=len, reverse=True):
        if stem.startswith(ck + '_'):
            rest = stem[len(ck)+1:]
            if len(rest) >= 10 and rest[4]=='-' and rest[7]=='-':
                date_p = rest[:10]
                if date_p == today.strftime('%Y-%m-%d'):
                    today_lectures.append((ck, rest[11:]))
            break

# ── Exam mode check ─────────────────────────────────────────────────────
exam_state_path = ROOT / 'data' / 'exam_mode.json'
exam_courses: dict = {}
if exam_state_path.exists():
    try:
        exam_state = json.loads(exam_state_path.read_text(encoding='utf-8'))
        exam_courses = exam_state.get('courses', {}) or {}
    except Exception:
        exam_courses = {}

# ── Build message ────────────────────────────────────────────────────────
lines = [f'🌅 *בוקר טוב!* — {today.strftime("%d/%m/%Y")}', '']

# Dead-man switch: did last evening's pipeline actually run and finish?
try:
    from utils.pipeline_health import missed_run_warning
    from utils.semester import load_config
    _w = missed_run_warning(ROOT / 'data', load_config(ROOT))
    if _w:
        lines += [_w, '']
except Exception as _e:
    lines += [f'⚠️ בדיקת הריצה האחרונה נכשלה: {_e}', '']

# Exam mode banner — highest priority
if exam_courses:
    lines.append('🚨 *מצב בחינה:*')
    for ck, info in sorted(exam_courses.items(),
                           key=lambda x: x[1].get('days_left', 999)):
        ck_he = COURSE_NAMES.get(ck, ck)
        days = info.get('days_left', '?')
        lines.append(f'  • {ck_he} — בעוד {days} ימים ({info.get("exam_date","?")})')
    lines.append('')

if recent_summaries:
    lines.append(f'📚 *{len(recent_summaries)} סיכומים חדשים מאתמול:*')
    for ck, sub, md in recent_summaries[:10]:
        cname = COURSE_NAMES.get(ck, ck)
        kind = '📝' if sub == 'tutorials' else '📖'
        lines.append(f'  {kind} {cname}: {md.stem}')
    if len(recent_summaries) > 10:
        lines.append(f'  ועוד {len(recent_summaries)-10}...')
    lines.append('')

if today_lectures:
    lines.append('🎓 *הרצאות שעלו היום:*')
    for ck, title in today_lectures:
        lines.append(f'  • {COURSE_NAMES.get(ck, ck)}: {title}')
    lines.append('')

if upcoming:
    lines.append('⏰ *דדליינים השבוע:*')
    for dt, a in upcoming[:8]:
        days_left = (dt.date() - today).days
        label = 'היום!' if days_left == 0 else (
                'מחר' if days_left == 1 else f'בעוד {days_left} ימים')
        ck = a.get('course_key', '?')
        title = a.get('title', '?')
        lines.append(f'  • [{label}] {COURSE_NAMES.get(ck, ck)}: {title}')
    lines.append('')

if not recent_summaries and not today_lectures and not upcoming:
    lines.append('☕ אין חדש. יום שקט!')

lines.append('—')
lines.append('שאל אותי כל דבר: "מה למדנו השבוע?" / "תסכם את ההרצאה האחרונה"')

msg = '\n'.join(lines)

# Send — WhatsApp only (10/11: Telegram deleted). A failed digest exits non-zero so run_with_alert.sh records it.
from utils.notify import deliver  # noqa: E402
if not deliver(msg, markdown=True):
    raise SystemExit('morning briefing: WhatsApp send failed (kept in data/undelivered_alerts.jsonl)')
print(f'✅ Briefing sent ({len(msg)} chars)')
