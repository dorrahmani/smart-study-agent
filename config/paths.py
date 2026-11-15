"""
config/paths.py — Single source of truth for all filesystem paths.

Everything is configurable via env vars so that future migration to
Oracle Cloud Object Storage / S3 is just a matter of setting them
to mounted paths or wrapping in a fsspec/cloudpathlib URL.

Convention: all paths are pathlib.Path objects, all are absolute.
"""

from __future__ import annotations
import os
from pathlib import Path

# ─── project root ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(os.environ.get(
    "SMART_STUDY_ROOT",
    Path(__file__).resolve().parent.parent
))

# ─── data (transient caches written by collectors) ───────────────────────
DATA_DIR = Path(os.environ.get("SMART_STUDY_DATA_DIR", PROJECT_ROOT / "data"))

# ─── config / state (small JSON state written by the pipeline) ────────────
CONFIG_DIR = Path(os.environ.get("SMART_STUDY_CONFIG_DIR", PROJECT_ROOT / "config"))

SUMMARIES_DIR     = DATA_DIR / "summaries"
ASSIGNMENTS_DIR   = DATA_DIR / "assignments"
ASSIGNMENTS_CACHE = ASSIGNMENTS_DIR / "_cache.json"
MOODLE_DIR        = DATA_DIR / "moodle"
PANOPTO_DIR       = DATA_DIR / "panopto"
NOTEBOOKLM_DIR    = DATA_DIR / "notebooklm"
RAG_INDEX_PATH    = DATA_DIR / "vault_index.faiss"
RAG_META_PATH     = DATA_DIR / "vault_index_meta.json"

# Single source of truth for "was this summary already sent?". The production
# pipeline writes config/sent_log.json (dict: {absolute_path: iso_timestamp}).
# data/summaries/sent_log.json is a legacy list frozen on 2026-04-24 (the
# mass-resend incident); its entries were migrated to reviewer_sent_log.json.
# It is kept on disk for history but must NOT be read.
SENT_LOG_PATH     = CONFIG_DIR / "sent_log.json"
RATINGS_PATH      = DATA_DIR / "ratings.json"
LAST_RUN_PATH     = DATA_DIR / ".last_run"
WEAKNESS_PATH     = DATA_DIR / "weakness_scores.json"

# ─── Obsidian Vault (canonical DB) ───────────────────────────────────────
VAULT_ROOT = Path(os.environ.get(
    "SMART_STUDY_VAULT",
    Path.home() / "Documents" / "Obsidian Vault"
))
VAULT_DIR = VAULT_ROOT / "Smart Study Agent"

VAULT_CONCEPTS_DIR     = VAULT_DIR / "מושגים"
VAULT_ASSIGNMENTS_DIR  = VAULT_DIR / "📋 מטלות"
VAULT_ANNOUNCEMENTS_DIR= VAULT_DIR / "📢 הודעות"
VAULT_NOTEBOOKLM_DIR   = VAULT_DIR / "🎓 NotebookLM"
VAULT_QUIZZES_DIR      = VAULT_DIR / "חידונים"

# ─── logs ────────────────────────────────────────────────────────────────
LOGS_DIR = PROJECT_ROOT / "logs"


def ensure_dirs() -> None:
    """Create all directories that should exist on first run."""
    for d in (
        DATA_DIR, SUMMARIES_DIR, ASSIGNMENTS_DIR,
        MOODLE_DIR, PANOPTO_DIR, NOTEBOOKLM_DIR,
        VAULT_DIR, VAULT_CONCEPTS_DIR, VAULT_ASSIGNMENTS_DIR,
        VAULT_ANNOUNCEMENTS_DIR, VAULT_NOTEBOOKLM_DIR,
        LOGS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


# ─── course identity (shared by collectors, exporters, dashboard) ────────
COURSE_NAMES = {
    # --- סמסטר ב' תשפ"ו (הסתיים 07/2026) — נשמר להיסטוריה, אין למחוק ---
    'calculus_2':       'חשבון דיפרנציאלי ואינטגרלי 2ב',
    'discrete_math_2':  'מתמטיקה בדידה 2',
    'linear_algebra_2': 'אלגברה לינארית 2ב',
    'microeconomics':   "מיקרו כלכלה א'",
    'intro_to_cs':      'מבוא מורחב למדמ"ח',
    # --- סמסטר א' תשפ"ז (17.10.2026 – 18.01.2027) ---
    'probability':      'הסתברות וסטטיסטיקה',
    'software_1':       'תוכנה 1',
    'microeconomics_2': 'מיקרו כלכלה ב׳',
    'macroeconomics':   'מאקרו כלכלה',
}
COURSE_ORDER = list(COURSE_NAMES.keys())

# Courses actively collected this semester (history stays in COURSE_NAMES).
# This semester's course keys (must exist in config/courses.yaml). Override in .env: SSA_ACTIVE_COURSES=key1,key2
ACTIVE_COURSES = [k.strip() for k in os.environ.get('SSA_ACTIVE_COURSES', 'probability,software_1,microeconomics_2,macroeconomics').split(',') if k.strip()]
# other names a course goes by (12/10: CS secretariat — 'תוכנה 1' is renamed 'מבוא להנדסת תוכנה' from 2026/1;
# same course number). Matching by course_code (courses.yaml) comes first; these help searches and the chat.
COURSE_ALT_NAMES = {'software_1': ['מבוא להנדסת תוכנה']}

def owner_email() -> str:
    """Where "send it to me" goes: RECIPIENT_EMAIL, else settings.email.recipient
    in courses.yaml. Returns '' when neither is set — callers must then fail
    rather than fall back to a hardcoded address (with more than one user, a
    hardcoded fallback silently mails one person's material to another)."""
    env = os.environ.get("RECIPIENT_EMAIL", "").strip()
    if env:
        return env
    try:
        import yaml
        with open(CONFIG_DIR / "courses.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return str(((cfg.get("settings") or {}).get("email") or {}).get("recipient") or "").strip()
    except Exception:
        return ""


# Map Hebrew course folder name (in vault) -> course_key
VAULT_COURSE_FOLDER_TO_KEY = {
    'חשבון דיפרנציאלי ואינטגרלי 2ב': 'calculus_2',
    'מתמטיקה בדידה 2':                'discrete_math_2',
    'אלגברה לינארית 2ב':              'linear_algebra_2',
    "מיקרו כלכלה א'":                 'microeconomics',
    'מבוא מורחב למדמח':              'intro_to_cs',
    'מבוא מורחב למדמ"ח':             'intro_to_cs',
    # סמסטר א' תשפ"ז
    'הסתברות וסטטיסטיקה':            'probability',
    'תוכנה 1':                        'software_1',
    'מבוא להנדסת תוכנה':              'software_1',
    'הנדסת תוכנה':                    'software_1',
    'מיקרו כלכלה ב׳':                 'microeconomics_2',
    "מיקרו כלכלה ב'":                 'microeconomics_2',  # גרש רגיל (תאימות)
    'מאקרו כלכלה':                    'macroeconomics',
}
