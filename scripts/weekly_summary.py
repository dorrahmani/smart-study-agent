"""scripts/weekly_summary.py — Send weekly summary on WhatsApp (Friday).

What week did we have? AI-generated insight + numbers.
Cron: 0 18 * * 5  /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Weekly Summary" bash -c "cd /home/ubuntu/smart_study_agent && venv/bin/python3 scripts/weekly_summary.py"
"""
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

from config.paths import SUMMARIES_DIR, COURSE_NAMES


today = date.today()
week_start = today - timedelta(days=6)  # last 7 days (incl. today)

# ── Count summaries created this week ────────────────────────────────────
recent = []  # list of (course, sub, path, mtime)
for ck in COURSE_NAMES:
    for sub in ('lectures', 'tutorials'):
        d = SUMMARIES_DIR / ck / sub
        if not d.exists(): continue
        for md in d.glob('*.md'):
            if md.stat().st_size < 5000: continue
            mtime = datetime.fromtimestamp(md.stat().st_mtime).date()
            if mtime >= week_start:
                recent.append((ck, sub, md, md.stat().st_mtime))

# Per-course counts
by_course: dict[str, dict] = {}
for ck, sub, _, _ in recent:
    if ck not in by_course:
        by_course[ck] = {'lectures': 0, 'tutorials': 0}
    by_course[ck][sub] += 1

# ── Upcoming deadlines (next 14 days) ────────────────────────────────────
upcoming = []
cache_path = ROOT / 'data' / 'assignments' / '_cache.json'
if cache_path.exists():
    try:
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        items = cache.get('assignments', [])
        now = datetime.now()
        cutoff = now + timedelta(days=14)
        for a in items:
            if not isinstance(a, dict) or a.get('submitted'): continue
            due = a.get('due_date')
            if not due: continue
            try:
                dt = datetime.fromisoformat(due)
                if now < dt < cutoff:
                    upcoming.append((dt, a))
            except Exception: continue
        upcoming.sort()
    except Exception:
        pass

# ── Costs this week ──────────────────────────────────────────────────────
try:
    from utils.usage_tracker import summary as cost_summary
    costs = cost_summary('7d')
    cost_total = costs.get('total_usd', 0)
    cost_audio_min = costs.get('audio_minutes_total', 0)
except Exception:
    cost_total = 0
    cost_audio_min = 0

# ── AI insight via Gemini ────────────────────────────────────────────────
insight = ''
try:
    from google import genai
    from google.genai import types as genai_types
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if key:
        client = genai.Client(api_key=key)
        # Build a compact prompt with the week's data
        course_breakdown = '; '.join(
            f"{COURSE_NAMES.get(ck, ck)}: {d['lectures']} הרצאות + {d['tutorials']} תרגולים"
            for ck, d in by_course.items()
        ) or 'אין סיכומים חדשים השבוע'
        upcoming_brief = '; '.join(
            f"{a.get('title','?')[:30]} ({(dt.date()-today).days} ימים)"
            for dt, a in upcoming[:5]
        ) or 'אין דדליינים קרובים'

        prompt = (
            f"אתה עוזר לימוד אישי. כתוב סיכום שבועי קצר בעברית (2-4 משפטים בלבד) "
            f"שמסכם את השבוע בלימודים, מאיר נקודה אחת מעודדת ונותן עצה מעשית אחת.\n\n"
            f"השבוע ({week_start.strftime('%d/%m')}-{today.strftime('%d/%m')}):\n"
            f"- סיכומים שנוצרו: {len(recent)}\n"
            f"- פירוט לפי קורס: {course_breakdown}\n"
            f"- דדליינים בשבועיים הקרובים: {upcoming_brief}\n\n"
            f"הסיכום בטון חברי, ענייני, לא מתחנף. בלי כותרות, רק טקסט זורם."
        )
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=400, temperature=0.7,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        try:
            from utils.usage_tracker import log_gemini
            log_gemini(resp, 'gemini-2.5-flash', op='weekly_summary.insight')
        except Exception: pass
        insight = (resp.text or '').strip()
except Exception as e:
    print(f'insight failed: {e}')

# ── Build the message ───────────────────────────────────────────────
lines = [f'📊 *סיכום שבועי — {week_start.strftime("%d/%m")}-{today.strftime("%d/%m")}*', '']

if recent:
    lines.append(f'📚 *{len(recent)} סיכומים חדשים השבוע:*')
    for ck in sorted(by_course):
        d = by_course[ck]
        cname = COURSE_NAMES.get(ck, ck)
        parts = []
        if d['lectures']: parts.append(f"{d['lectures']} הרצאות")
        if d['tutorials']: parts.append(f"{d['tutorials']} תרגולים")
        lines.append(f"  • {cname}: {' + '.join(parts)}")
    lines.append('')
else:
    lines.append('📚 לא נוצרו סיכומים חדשים השבוע.')
    lines.append('')

if upcoming:
    lines.append(f'⏰ *{len(upcoming)} דדליינים בשבועיים הקרובים:*')
    for dt, a in upcoming[:8]:
        days_left = (dt.date() - today).days
        label = 'מחר' if days_left == 1 else (f'בעוד {days_left} ימים' if days_left > 1 else 'היום!')
        ck = a.get('course_key', '?')
        title = (a.get('title') or '?')[:60]
        lines.append(f'  • [{label}] {COURSE_NAMES.get(ck, ck)}: {title}')
    lines.append('')

if cost_total > 0:
    lines.append(f'💰 עלות AI השבוע: *${cost_total:.2f}*' +
                 (f' ({cost_audio_min:.0f} דקות תמלול)' if cost_audio_min > 0 else ''))
    lines.append('')

if insight:
    lines.append('💡 *תובנה השבוע:*')
    lines.append(insight)
    lines.append('')

lines.append('—')
lines.append('שבת שלום! 🕯️ אם רוצה תיקון/מטלה — אני זמין כל הזמן.')

msg = '\n'.join(lines)

# ── Send ─────────────────────────────────────────────────────────────────
# A failed digest exits non-zero so run_with_alert.sh records it.
from utils.notify import deliver  # noqa: E402  — WhatsApp only (10/11: Telegram deleted)
if not deliver(msg, markdown=True):
    raise SystemExit('weekly summary: WhatsApp send failed (kept in data/undelivered_alerts.jsonl)')
print(f'✅ Weekly summary sent ({len(msg)} chars)')
