"""
agent/domains/student.py — the first life domain: the owner's degree (16/11).

What here is student-only moved out of the core prompts. The rest of the student machinery (courses in
config/courses.yaml, Moodle / Panopto / university-portal collectors, NotebookLM per course, obligations, exam dates,
the pre-class primer, the timetable sync) is still wired in directly; docs/FEATURES.md lists it.

Everything personal comes from your own settings, never from the code:
    SSA_UNIVERSITY        the university's name in Hebrew, e.g. "אוניברסיטת תל אביב" (TAU is the worked example)
    SSA_COURSE_SITE_URL   your private course site (scripts/build_course_site.py), e.g. a Tailscale URL; empty = none
    config/student_lines.txt  optional extra lines for the assistant (a lecturer's own site, where a course posts
                          its files …). Git-ignored by default, like courses.yaml.
On only with SSA_STUDY=1 (config/features.py).
"""
from __future__ import annotations

import os
from pathlib import Path

NAME = 'student'
_LINES_FILE = Path(__file__).resolve().parents[2] / 'config' / 'student_lines.txt'


def _university() -> str:
    return os.environ.get('SSA_UNIVERSITY', '').strip()


def identity_clause() -> str:
    u = _university()
    return f'סטודנט ב{u}' if u else 'סטודנט'


def mind_lines() -> str:
    out = []
    site = os.environ.get('SSA_COURSE_SITE_URL', '').strip()
    if site:
        out.append(f'אתר הקורסים הפרטי (נפתח רק ברשת הפרטית, למשל Tailscale): {site} . מכשיר חדש: מתקינים את אותה רשת '
                   'ומתחברים לאותו חשבון של המשתמש. אדם אחר: המשתמש משתף לו את המכשיר, לא נותן לו את החשבון. '
                   "סיסמאות לא שולחים בצ'אט.")
    try:
        extra = _LINES_FILE.read_text(encoding='utf-8').strip()
        if extra:
            out.append(extra)
    except OSError:
        pass
    return '\n'.join(out)


def __getattr__(name):          # IDENTITY / MIND_LINES read the environment when asked, not at import
    if name == 'IDENTITY':
        return identity_clause()
    if name == 'MIND_LINES':
        return mind_lines()
    raise AttributeError(name)


def active() -> bool:
    try:
        from config.features import study_enabled
        return study_enabled()
    except Exception:
        return False
