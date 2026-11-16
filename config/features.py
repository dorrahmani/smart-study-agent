"""
config/features.py — which life "domains" are switched on.

The core secretary (calendar, reminders, tasks, "what's waiting for me", messages to contacts,
bookings behind a ✅, links/files/images, partner mode, proactive nudges) always runs.
Domains on top of it are opt-in:

    SSA_STUDY=1   the student domain: courses, Moodle/Panopto, university mail, obligations,
                  summaries, course site, NotebookLM, review sessions.
    SSA_STUDY=0   assistant only. Also what an unset SSA_STUDY means: a domain is never on by accident
                  (running the tests copies an example courses.yaml into place — that must not switch it on).

Read lazily on every call (several modules re-load .env), so a running process follows the file.
Tests pin it with set_study(True/False).
"""
from __future__ import annotations

import os
from pathlib import Path

_CONFIG_DIR = Path(os.environ.get("SMART_STUDY_CONFIG_DIR", Path(__file__).resolve().parent))
_override: bool | None = None


def set_study(on: bool | None) -> None:
    """Tests / tools: force the student domain on or off (None = back to the environment)."""
    global _override
    _override = on


def _from_dotenv(key: str) -> str:
    """A cron script that never loads .env still follows it: the value straight from <root>/.env."""
    p = Path(os.environ.get("SMART_STUDY_ROOT", _CONFIG_DIR.parent)) / ".env"
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            k, _, v = line.strip().partition("=")
            if k.strip() == key and not line.lstrip().startswith("#"):
                return v.split(" #", 1)[0].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def study_enabled() -> bool:
    if _override is not None:
        return _override
    v = os.environ["SSA_STUDY"] if "SSA_STUDY" in os.environ else _from_dotenv("SSA_STUDY")
    return v.strip().lower() in ("1", "true", "yes", "on")


def courses_config() -> dict:
    """config/courses.yaml as a dict — {} when the student domain is off or the file is missing/broken.
    Every reader of the course config goes through here, so assistant-only never needs the file."""
    if not study_enabled():
        return {}
    p = _CONFIG_DIR / "courses.yaml"
    try:
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


STUDY_OFF_TEXT = ('🎓 תוסף הסטודנט כבוי אצלך (SSA_STUDY=0), אז אין לי קורסים, מודל או סיכומים. '
                  'אפשר להפעיל אותו ב-.env, ראו SETUP.md חלק ב׳.')
