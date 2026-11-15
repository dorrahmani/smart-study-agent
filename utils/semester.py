"""
utils/semester.py — the ONE semester gate: should the pipeline run today?

Single source of truth = config/courses.yaml → settings.schedule
(semester_start, semester_end, no_class_dates). scripts/run_daily.sh used to
carry its own hardcoded copy of the dates; now it asks this module:

    venv/bin/python3 -m utils.semester --daily   # exit 0 = run, 3 = skip (reason on stdout)

Overrides: SMART_STUDY_FORCE=1 runs regardless; SMART_STUDY_DISABLE=1 never runs.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SKIP_EXIT = 3
# Recordings upload ~a day late, so the evening run after the last teaching day
# still has a lecture to collect.
UPLOAD_LAG_DAYS = 2


def _d(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def semester_active(config: dict, today: date | None = None,
                    respect_no_class_dates: bool = True) -> tuple[bool, str]:
    """(run?, reason). Unparseable dates never block — fail-open with a reason."""
    if os.getenv('SMART_STUDY_DISABLE') == '1':
        return False, 'כבוי ידנית (SMART_STUDY_DISABLE=1)'
    if os.getenv('SMART_STUDY_FORCE') == '1':
        return True, 'הופעל בכוח (SMART_STUDY_FORCE=1)'
    sched = (config.get('settings') or {}).get('schedule') or {}
    today = today or date.today()

    start = _d(sched.get('semester_start'))
    if start and today < start:
        return False, f'הסמסטר עוד לא התחיל (מתחיל {start}); היום {today}'

    raw_end = sched.get('semester_end')
    if not raw_end:
        return True, 'אין semester_end בקונפיג'
    end = _d(raw_end)
    if end is None:
        return True, f'semester_end לא תקין ({raw_end!r}) — מריץ ליתר ביטחון'
    if today > end:
        return False, f'הסמסטר נגמר ({end}); היום {today} — מצב מבחנים, אין הרצאות חדשות'

    if respect_no_class_dates and str(today) in {str(x) for x in (sched.get('no_class_dates') or [])}:
        return False, f'אין לימודים היום ({today})'

    return True, f'בתוך הסמסטר (מסתיים {end})'


def daily_pipeline_should_run(config: dict, today: date | None = None) -> tuple[bool, str]:
    """The evening collection run. Differs from semester_active in two ways:
    * a no-class day does NOT skip it — that evening still collects the
      PREVIOUS day's recording (Panopto uploads a day late);
    * it keeps running UPLOAD_LAG_DAYS after semester_end for the same reason.
    """
    today = today or date.today()
    sched = (config.get('settings') or {}).get('schedule') or {}
    end = _d(sched.get('semester_end'))
    probe = today - timedelta(days=UPLOAD_LAG_DAYS) if end and end < today <= end + timedelta(days=UPLOAD_LAG_DAYS) else today
    ok, why = semester_active(config, probe, respect_no_class_dates=False)
    if ok and probe != today:
        why = f'אחרי סוף הסמסטר ({end}) — ריצת השלמה להקלטות אחרונות'
    return ok, why


def load_config(root: Path | None = None) -> dict:
    import yaml
    root = root or Path(__file__).resolve().parent.parent
    with open(root / 'config' / 'courses.yaml', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    cfg = load_config()
    fn = daily_pipeline_should_run if '--daily' in sys.argv else semester_active
    ok, why = fn(cfg)
    print(why)
    sys.exit(0 if ok else SKIP_EXIT)
