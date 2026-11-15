"""scripts/verify_backup.py — Weekly verification that Drive backup actually worked.

Lists the latest backup folder in Drive, counts summaries there, compares to local.
Alerts via WhatsApp if mismatch > 5% or backup is older than 2 days.

Cron suggestion (Saturday 09:00 Israel = 06:00 UTC):
  0 6 * * 6  /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Backup Verify" \
    bash -c "cd /home/ubuntu/smart_study_agent && venv/bin/python3 scripts/verify_backup.py"
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

REMOTE = 'gdrive:smart-study-agent-backup'


def _send(text: str) -> None:
    from utils.notify import deliver            # WhatsApp only (10/11: Telegram deleted)
    deliver(text, markdown=True)


def _rclone_lsd(path: str) -> list[dict]:
    """List directories in a remote path. Returns list of {Name, Path, ModTime}."""
    try:
        result = subprocess.run(
            ['rclone', 'lsjson', path, '--dirs-only', '--no-modtime', '--max-depth', '1'],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout)
    except Exception as e:
        print(f'  rclone lsd error: {e}')
        return []


def _rclone_count_files(path: str, glob: str = '*.md') -> int:
    """Count files matching glob recursively in a remote path."""
    try:
        result = subprocess.run(
            ['rclone', 'lsf', path, '--include', glob, '-R', '--files-only'],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return -1
        return sum(1 for line in result.stdout.splitlines() if line.strip())
    except Exception as e:
        print(f'  rclone count error: {e}')
        return -1


def _count_local_summaries() -> int:
    from config.paths import SUMMARIES_DIR
    return sum(1 for _ in Path(SUMMARIES_DIR).rglob('*.md'))


# ─── Run verification ──────────────────────────────────────────────────
print(f'🔍 Verifying backup at {REMOTE} ...')

# 1. List backup folders (each is a date stamp like 2026-05-23)
dirs = _rclone_lsd(REMOTE)
if not dirs:
    msg = ('⛔ *גיבוי לא נמצא!*\n\n'
           f'הריצה בריקלון לא החזירה תיקיות מ-`{REMOTE}`.\n'
           'בדוק את rclone config וכי הגיבוי הריצה היומית עברה.')
    print(msg)
    _send(msg)
    sys.exit(2)

# 2. Find newest backup folder (by name — they're YYYY-MM-DD)
date_dirs = [d for d in dirs if len(d.get('Name', '')) == 10 and d['Name'].count('-') == 2]
date_dirs.sort(key=lambda d: d['Name'], reverse=True)
if not date_dirs:
    msg = ('⚠️ *גיבוי קיים אבל ללא תיקיות תאריך מזוהות*\n\n'
           f'מצאתי {len(dirs)} תיקיות תחת {REMOTE} אבל אף אחת בפורמט YYYY-MM-DD.')
    print(msg)
    _send(msg)
    sys.exit(1)

latest = date_dirs[0]
latest_name = latest['Name']
latest_path = f'{REMOTE}/{latest_name}'
print(f'  Latest backup: {latest_name}')

# 3. Age check
try:
    latest_date = datetime.strptime(latest_name, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - latest_date).total_seconds() / 86400
except Exception:
    age_days = -1

# 4. Count summaries in backup vs local
# Try the canonical path first; fall back to /data/ if backup is flat
backup_count = _rclone_count_files(f'{latest_path}/data/summaries', '*.md')
if backup_count < 0:
    # Backup script flattens summaries under data/ (without summaries subdir)
    backup_count = _rclone_count_files(f'{latest_path}/data', '*.md')
local_count = _count_local_summaries()
print(f'  Summaries — local: {local_count} | backup: {backup_count}')

# 5. Decide status
issues: list[str] = []
if age_days > 2:
    issues.append(f'⚠️ הגיבוי האחרון מ-{latest_name} ({age_days:.1f} ימים אחורה)')
if backup_count < 0:
    issues.append('⛔ לא הצלחתי לספור קבצים בגיבוי')
elif local_count > 0:
    ratio = backup_count / local_count
    if ratio < 0.85:
        issues.append(f'⛔ בגיבוי רק {backup_count}/{local_count} סיכומים ({ratio*100:.0f}%)')
    elif ratio < 0.95:
        issues.append(f'⚠️ בגיבוי {backup_count}/{local_count} סיכומים ({ratio*100:.0f}%) — חוסר חלקי')

if not issues:
    msg = (f'✅ *גיבוי תקין*\n\n'
           f'📅 תיקייה אחרונה: `{latest_name}` ({age_days:.0f} ימים)\n'
           f'📚 סיכומים בגיבוי: {backup_count}/{local_count}\n'
           f'🟢 הכל תאם.')
    print(msg)
    if os.environ.get('BACKUP_VERIFY_ALWAYS_NOTIFY') == '1':
        _send(msg)
    sys.exit(0)
else:
    severity = '⛔' if any('⛔' in i for i in issues) else '⚠️'
    msg = (f'{severity} *בעיות בגיבוי השבועי:*\n\n' +
           '\n'.join(f'• {i}' for i in issues) +
           f'\n\nתיקייה: `{latest_name}` ({age_days:.1f} ימים)')
    print(msg)
    _send(msg)
    sys.exit(2 if severity == '⛔' else 1)
