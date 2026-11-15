"""
first_run.py — ריצה ראשונה של הסמסטר
מריץ את כל הקורסים עם days_back=7 (שבוע שלם) ומשחזר ל-2 בסיום.
"""

import sys
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

CONFIG_PATH = 'config/courses.yaml'
DAYS_BACK_FIRST_RUN = 7
DAYS_BACK_NORMAL = 2


def set_days_back(days: int):
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    config['settings']['schedule']['days_back'] = days
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


if __name__ == '__main__':
    print(f"\n{'='*60}")
    print(f"🚀 ריצה ראשונה — סמסטר ב' תשפ\"ו")
    print(f"   מחפש הרצאות מ-{DAYS_BACK_FIRST_RUN} ימים אחרונים")
    print(f"{'='*60}\n")

    # שלב 1: הגדר days_back=7
    set_days_back(DAYS_BACK_FIRST_RUN)
    print(f"✅ days_back הוגדר ל-{DAYS_BACK_FIRST_RUN}")

    try:
        from main import SmartStudyAgent
        agent = SmartStudyAgent(CONFIG_PATH)
        agent.run_all()
    finally:
        # שלב 2: שחזר ל-2 תמיד — גם אם הייתה שגיאה
        set_days_back(DAYS_BACK_NORMAL)
        print(f"\n✅ days_back שוחזר ל-{DAYS_BACK_NORMAL} (לריצות יומיות עתידיות)")
