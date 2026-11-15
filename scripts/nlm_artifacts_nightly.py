"""scripts/nlm_artifacts_nightly.py — Nightly cron: regenerate NotebookLM Study Guides
for courses currently in exam mode (defined by data/exam_mode.json).

This way, when the student wakes up before an exam, a freshly-generated study guide
based on lectures+PDFs is already available without waiting 3-5 min on demand.

Cron suggestion (02:00 UTC = 05:00 Israel, after backups but before morning briefing):
  0 2 * * *  /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "NLM Artifacts" \
    bash -c "cd /home/ubuntu/smart_study_agent && venv/bin/python3 scripts/nlm_artifacts_nightly.py"
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

from config.paths import COURSE_NAMES


EXAM_MODE_PATH = ROOT / 'data' / 'exam_mode.json'


def _alert(text: str) -> None:
    from utils.notify import deliver            # WhatsApp only (10/11: Telegram deleted)
    deliver(text, markdown=True)


# Only run during exam mode (otherwise too expensive to maintain daily)
if not EXAM_MODE_PATH.exists():
    print('No exam_mode.json — skipping NLM artifact pregen.')
    sys.exit(0)

try:
    state = json.loads(EXAM_MODE_PATH.read_text(encoding='utf-8'))
    courses = state.get('courses', {}) or {}
except Exception:
    courses = {}

if not courses:
    print('No courses in exam mode — skipping.')
    sys.exit(0)

# Run pregen
print(f'Pregenerating Study Guides for {len(courses)} exam-mode courses...')

from generators.notebooklm_agent import NotebookLMAgent
agent = NotebookLMAgent()

generated = []
failed = []
for ck in courses:
    ck_he = COURSE_NAMES.get(ck, ck)
    try:
        print(f'  {ck} — generating study guide...')
        path = agent.generate_study_guide(ck, 'lectures', language='he')
        generated.append((ck_he, path))
        print(f'    ✅ {path}')
    except Exception as e:
        failed.append((ck_he, str(e)))
        print(f'    ❌ {e}')

# WhatsApp summary
lines = [f'🌙 *NotebookLM artifacts מעודכנים — {datetime.now().strftime("%d/%m")}*', '']
if generated:
    lines.append(f'✅ {len(generated)} מדריכי לימוד חדשים:')
    for ck_he, _ in generated:
        lines.append(f'  • {ck_he}')
    lines.append('')
    lines.append('שלח לי "תייצר מדריך לימוד של [קורס]" כדי לקבל את הקובץ.')
if failed:
    lines.append('')
    lines.append(f'⚠️ נכשלו {len(failed)}:')
    for ck_he, err in failed:
        lines.append(f'  • {ck_he}: {err[:80]}')

if generated or failed:
    _alert('\n'.join(lines))
