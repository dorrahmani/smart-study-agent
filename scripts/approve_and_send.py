"""
approve_and_send.py — sends approved summaries to all recipients
Runs after the user has approved the summaries that were sent to him for review.
Sends only summaries that have not been sent yet (tracked via config/sent_log.json).

Usage:
    python scripts/approve_and_send.py                    # sends only new ones
    python scripts/approve_and_send.py calculus_2         # only a specific course
    python scripts/approve_and_send.py calculus_2 "הרצאה 3"  # a specific summary
"""
import sys, os, json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

os.chdir(str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(override=True)

from generators.gmail_sender import GmailSender

# ── args ──────────────────────────────────────────────────────────────────────
course_filter = sys.argv[1] if len(sys.argv) > 1 else None
title_filter  = sys.argv[2] if len(sys.argv) > 2 else None

# ── load full recipients list ─────────────────────────────────────────────────
full_recipients_path = Path('config/recipients_full.json')
if not full_recipients_path.exists():
    print("❌ לא נמצא config/recipients_full.json")
    sys.exit(1)

recipients_full = json.loads(full_recipients_path.read_text(encoding='utf-8'))
from config.paths import owner_email  # noqa: E402
reviewer = (recipients_full.get('global') or [owner_email()])[0]

# ── sent log ────────────────────────────────────────────────────────────────
sent_log_path = Path('config/sent_log.json')
if sent_log_path.exists():
    sent_log = json.loads(sent_log_path.read_text(encoding='utf-8'))
else:
    sent_log = {}

def mark_sent(md_path: str):
    """Marks a summary as sent in the log"""
    sent_log[md_path] = datetime.now().isoformat()
    sent_log_path.write_text(json.dumps(sent_log, ensure_ascii=False, indent=2), encoding='utf-8')

def already_sent(md_path: str) -> bool:
    return md_path in sent_log

# ── course name mapping ────────────────────────────────────────────────────────
COURSE_NAMES = {
    'linear_algebra_2': 'אלגברה לינארית 2ב',
    'calculus_2': 'חשבון דיפרנציאלי ואינטגרלי 2ב',
    'discrete_math_2': 'מתמטיקה דיסקרטית 2',
    'microeconomics': 'מיקרו כלכלה א',
    'intro_to_cs': 'מבוא מורחב למדמ"ח',
}

# ── find summaries to send ────────────────────────────────────────────────────
summaries_dir = Path('data/summaries')
to_send = []
skipped_sent = 0
skipped_error = 0

for course_dir in sorted(summaries_dir.iterdir()):
    if not course_dir.is_dir():
        continue
    course_key = course_dir.name

    if course_filter and course_key != course_filter:
        continue

    # build recipient list for this course
    global_list = [r for r in recipients_full.get('global', []) if r != reviewer]
    course_list = recipients_full.get(course_key, [])
    all_recipients = list({*global_list, *course_list})
    if not all_recipients:
        print(f"⚠️  אין נמענים נוספים עבור {course_key} — מדלג")
        continue

    course_name = COURSE_NAMES.get(course_key, course_key)

    for sub in ['lectures', 'tutorials']:
        sub_dir = course_dir / sub
        if not sub_dir.exists():
            continue
        for md_file in sorted(sub_dir.glob('*.md')):
            if title_filter and title_filter not in md_file.stem:
                continue

            md_key = str(md_file).replace('\\', '/')

            # skip if already sent
            if already_sent(md_key):
                skipped_sent += 1
                continue

            pdf_file = md_file.with_suffix('.pdf')
            stem = md_file.stem
            parts = stem.split(' - ', 1)
            title = parts[1] if len(parts) == 2 else stem

            # skip if the summary is an error
            content = md_file.read_text(encoding='utf-8', errors='replace')
            if 'שגיאה ביצירת סיכום' in content or 'לא ניתן להתחבר ל-API' in content:
                skipped_error += 1
                print(f"⚠️  דילוג (שגיאה): {md_file.name}")
                continue

            to_send.append({
                'course_key': course_key,
                'course_name': course_name,
                'title': title,
                'md_path': str(md_file),
                'md_key': md_key,
                'pdf_path': str(pdf_file) if pdf_file.exists() else None,
                'recipients': all_recipients,
            })

# ── send ──────────────────────────────────────────────────────────────────────
if skipped_sent:
    print(f"ℹ️  {skipped_sent} סיכומים כבר נשלחו בעבר — מדולגים")

if not to_send:
    print("✅ אין סיכומים חדשים לשליחה.")
    sys.exit(0)

print(f"\n📤 שולח {len(to_send)} סיכומים חדשים לנמענים:\n")
mailer = GmailSender()
success = 0

for item in to_send:
    print(f"  → [{item['course_key']}] {item['title']}")
    print(f"     נמענים: {', '.join(item['recipients'])}")
    ok = mailer.send_summary(
        course_name=item['course_name'],
        lecture_title=item['title'],
        summary_md_path=item['md_path'],
        summary_pdf_path=item['pdf_path'],
        recipients=item['recipients'],
    )
    if ok:
        success += 1
        mark_sent(item['md_key'])  # record in the log only after success
        print(f"     ✅ נשלח!")
    else:
        print(f"     ❌ שגיאה בשליחה")

print(f"\n✅ {success}/{len(to_send)} נשלחו בהצלחה")
