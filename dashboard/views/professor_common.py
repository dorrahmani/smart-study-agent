"""Shared constants for the professor views (homework checker + tips)."""

def semester_course_map() -> dict:
    """course_key -> Hebrew name: this semester's courses (ACTIVE_COURSES) first,
    then previous semesters (old feedback reports / notebooks still use them).
    Built from config/paths.py so a new semester needs no dashboard edit."""
    from config.paths import ACTIVE_COURSES, COURSE_NAMES
    m = {k: COURSE_NAMES[k] for k in ACTIVE_COURSES if k in COURSE_NAMES}
    m.update({k: v for k, v in COURSE_NAMES.items() if k not in m})
    return m


COURSE_KEYS = semester_course_map()


def score_is_good(score) -> bool:
    """Same heuristic as before: a leading digit >= 8 counts as a good score."""
    s = str(score)
    return any(c.isdigit() and int(c) >= 8 for c in s[:2])
