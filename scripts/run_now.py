"""
run_now.py — ריצה חכמה לפי זמן מאז הריצה האחרונה
מחשב days_back אוטומטית ומריץ את כל הקורסים.

הרצה: לחץ פעמיים על run_now.bat בשולחן העבודה
"""

import sys
import math
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

CONFIG_PATH = 'config/courses.yaml'
LAST_RUN_FILE = Path('data/.last_run')
MAX_DAYS_BACK = 30
MIN_DAYS_BACK = 1


def get_days_since_last_run(default_days: int = 2) -> int:
    """מחשב כמה ימים עברו מאז הריצה האחרונה"""
    if not LAST_RUN_FILE.exists():
        # אין היסטוריה — שמור היום והשתמש בברירת מחדל
        save_last_run()
        print(f"ℹ️  ריצה ראשונה — מחפש {default_days} ימים אחורה")
        return default_days

    try:
        last_run_ts = float(LAST_RUN_FILE.read_text().strip())
        last_run = datetime.fromtimestamp(last_run_ts, tz=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        elapsed_days = (now - last_run).total_seconds() / 86400
        days_back = max(MIN_DAYS_BACK, min(MAX_DAYS_BACK, math.ceil(elapsed_days)))
        last_run_str = last_run.astimezone().strftime('%d/%m/%Y %H:%M')
        print(f"ℹ️  ריצה אחרונה: {last_run_str}")
        print(f"ℹ️  עברו {elapsed_days:.1f} ימים — מחפש {days_back} ימים אחורה")
        return days_back
    except Exception as e:
        print(f"⚠️  שגיאה בקריאת last_run: {e} — משתמש ב-{MIN_DAYS_BACK} יום")
        return MIN_DAYS_BACK


def save_last_run():
    """שומר timestamp של הריצה הנוכחית"""
    LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_FILE.write_text(str(datetime.now(tz=timezone.utc).timestamp()))


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("📚 Smart Study Agent — הרצה חכמה")
    print("=" * 60 + "\n")

    import yaml
    with open(CONFIG_PATH, encoding='utf-8') as f:
        _cfg = yaml.safe_load(f)
    default_days = _cfg['settings']['schedule'].get('days_back', 2)

    days_back = get_days_since_last_run(default_days=default_days)

    from main import SmartStudyAgent
    agent = SmartStudyAgent(CONFIG_PATH)

    # override days_back בזיכרון בלבד (לא כותב לקובץ)
    agent.config['settings']['schedule']['days_back'] = days_back

    try:
        agent.run_all()
    finally:
        save_last_run()
        now_str = datetime.now().strftime('%d/%m/%Y %H:%M')
        print(f"\n✅ last_run עודכן: {now_str}")
