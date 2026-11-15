"""
dashboard/vault_reader.py — Read Obsidian Vault as the canonical DB.

API:
    list_summaries(course=None, week=None, type=None) -> list[dict]
    list_assignments(status=None, course=None) -> list[dict]
    list_concepts() -> list[dict]
    list_announcements(course=None) -> list[dict]
    list_notebooklm(course=None, artifact_type=None) -> list[dict]
    get_note(path) -> dict | None
    query_by_tag(tag) -> list[dict]
    query_by_concept(concept) -> list[dict]

Each note dict has:
    path:        str (absolute)
    rel_path:    str (relative to vault dir)
    filename:    str
    body:        str (md without frontmatter)
    raw:         str (full md including frontmatter)
    fm:          dict (parsed YAML frontmatter, or {})
    mtime:       float
    + everything from frontmatter as top-level keys (course, type, date, ...)

Caching: re-parses a file only if its mtime changed. Module-level dict cache.
Falls back gracefully to filename-regex parsing if YAML is missing.
"""

from __future__ import annotations

import re
import yaml
from pathlib import Path
from datetime import datetime, date as _date
from typing import Optional, Iterable

from config.paths import (
    VAULT_DIR,
    VAULT_CONCEPTS_DIR,
    VAULT_ASSIGNMENTS_DIR,
    VAULT_ANNOUNCEMENTS_DIR,
    VAULT_NOTEBOOKLM_DIR,
    COURSE_NAMES,
    COURSE_ORDER,
    VAULT_COURSE_FOLDER_TO_KEY,
)

# ─── cache ─────────────────────────────────────────────────────────────────
# path_str -> (mtime, parsed_dict)
_CACHE: dict[str, tuple[float, dict]] = {}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_FILENAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*-\s*(.+)$")


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}
    return fm, m.group(2)


def _parse_note(path: Path) -> Optional[dict]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    fm, body = _parse_frontmatter(raw)

    # Filename fallback: extract date + title
    stem = path.stem
    fm_date = fm.get("date")
    fm_title = fm.get("title")
    if not fm_date or not fm_title:
        m = _FILENAME_DATE_RE.match(stem)
        if m:
            fm_date = fm_date or m.group(1)
            fm_title = fm_title or m.group(2)
        else:
            fm_title = fm_title or stem

    # Normalize date to ISO string
    if isinstance(fm_date, (datetime, _date)):
        fm_date = fm_date.isoformat()[:10]

    # Course key — prefer YAML, fall back to vault folder
    course_key = fm.get("course_key")
    course_name = fm.get("course")
    if not course_key:
        # walk up to find vault course folder
        try:
            rel = path.relative_to(VAULT_DIR)
            top = rel.parts[0] if rel.parts else ""
            course_key = VAULT_COURSE_FOLDER_TO_KEY.get(top)
            if course_key and not course_name:
                course_name = COURSE_NAMES.get(course_key, top)
        except ValueError:
            pass

    note = {
        "path":     str(path),
        "rel_path": str(path.relative_to(VAULT_DIR)) if path.is_absolute() else str(path),
        "filename": path.name,
        "stem":     stem,
        "body":     body,
        "raw":      raw,
        "fm":       fm,
        "mtime":    path.stat().st_mtime,
        "date":     fm_date or "",
        "title":    fm_title or stem,
        "course":   course_name,
        "course_key": course_key,
    }

    # Hoist common frontmatter fields
    for k in (
        "type", "week", "topics", "concepts", "tags", "related",
        "sources_used", "cost_usd", "tokens", "panopto_url", "pdf_path",
        "transcription_method", "weakness_score",
        # assignments
        "status", "due_date", "grade", "feedback", "submission_files",
        # announcements
        "sender", "moodle_url",
        # notebooklm
        "generated_from", "artifact_type", "file_path",
    ):
        if k in fm:
            note[k] = fm[k]

    return note


def _load_with_cache(path: Path) -> Optional[dict]:
    sp = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _CACHE.get(sp)
    if cached and cached[0] == mtime:
        return cached[1]
    parsed = _parse_note(path)
    if parsed is not None:
        _CACHE[sp] = (mtime, parsed)
    return parsed


def invalidate_cache() -> None:
    _CACHE.clear()


# ─── public API ────────────────────────────────────────────────────────────

def _iter_md(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for p in root.rglob("*.md"):
        if p.name.startswith("_"):
            # _index.md, _drafts, etc. — skip from listings
            continue
        yield p


def _all_notes() -> list[dict]:
    out = []
    for p in _iter_md(VAULT_DIR):
        n = _load_with_cache(p)
        if n is not None:
            out.append(n)
    return out


def _is_lecture_or_tutorial(n: dict) -> bool:
    t = n.get("type")
    if t in ("lecture", "tutorial", "הרצאה", "תרגול"):
        return True
    # Heuristic: under a course folder, not in special subfolders
    rel = n.get("rel_path", "")
    parts = Path(rel).parts
    if not parts:
        return False
    top = parts[0]
    if top in COURSE_NAMES.values() or top in VAULT_COURSE_FOLDER_TO_KEY:
        # exclude special files
        if "מטלות" in rel or "הודעות" in rel or "NotebookLM" in rel or "מושגים" in rel or "חידונים" in rel:
            return False
        return True
    return False


def _normalize_type(n: dict) -> str:
    """Return 'lecture' or 'tutorial' or whatever else."""
    t = n.get("type")
    if t in ("lecture", "הרצאה"):
        return "lecture"
    if t in ("tutorial", "תרגול"):
        return "tutorial"
    # fallback — guess by filename
    name = (n.get("title") or n.get("stem") or "").lower()
    if "תרגול" in name or "tutorial" in name or " tut" in name:
        return "tutorial"
    if "הרצאה" in name or "lecture" in name:
        return "lecture"
    return t or "unknown"


def list_summaries(course: Optional[str] = None,
                   week: Optional[int] = None,
                   type: Optional[str] = None) -> list[dict]:
    """All lecture/tutorial notes, optionally filtered."""
    notes = [n for n in _all_notes() if _is_lecture_or_tutorial(n)]
    if course:
        notes = [n for n in notes if n.get("course_key") == course or n.get("course") == course]
    if week is not None:
        notes = [n for n in notes if n.get("week") == week]
    if type:
        wanted = "lecture" if type in ("lecture", "lectures", "הרצאה") else "tutorial"
        notes = [n for n in notes if _normalize_type(n) == wanted]

    course_idx = {k: i for i, k in enumerate(COURSE_ORDER)}
    notes.sort(key=lambda n: (
        course_idx.get(n.get("course_key", ""), 99),
        n.get("date", ""),
        0 if _normalize_type(n) == "lecture" else 1,
    ))
    return notes


def list_assignments(status: Optional[str] = None,
                     course: Optional[str] = None) -> list[dict]:
    """Assignments stored as md under VAULT_ASSIGNMENTS_DIR."""
    notes = [_load_with_cache(p) for p in _iter_md(VAULT_ASSIGNMENTS_DIR)]
    notes = [n for n in notes if n is not None]
    if status:
        notes = [n for n in notes if n.get("status") == status]
    if course:
        notes = [n for n in notes if n.get("course_key") == course]
    notes.sort(key=lambda n: n.get("due_date") or n.get("date") or "")
    return notes


def list_exams(course: Optional[str] = None) -> list[dict]:
    """Past-exam notes (OCR'd scans) under VAULT_DIR/'🧾 מבחנים'."""
    d = VAULT_DIR / '🧾 מבחנים'
    if not d.exists():
        return []
    notes = [_load_with_cache(p) for p in _iter_md(d)]
    notes = [n for n in notes if n is not None]
    if course:
        notes = [n for n in notes if n.get('course_key') == course]
    return notes


def list_concepts() -> list[dict]:
    notes = [_load_with_cache(p) for p in _iter_md(VAULT_CONCEPTS_DIR)]
    return [n for n in notes if n is not None]


def list_announcements(course: Optional[str] = None) -> list[dict]:
    notes = [_load_with_cache(p) for p in _iter_md(VAULT_ANNOUNCEMENTS_DIR)]
    notes = [n for n in notes if n is not None]
    if course:
        notes = [n for n in notes if n.get("course_key") == course]
    notes.sort(key=lambda n: n.get("date", ""), reverse=True)
    return notes


def list_notebooklm(course: Optional[str] = None,
                    artifact_type: Optional[str] = None) -> list[dict]:
    notes = [_load_with_cache(p) for p in _iter_md(VAULT_NOTEBOOKLM_DIR)]
    notes = [n for n in notes if n is not None]
    if course:
        notes = [n for n in notes if n.get("course_key") == course]
    if artifact_type:
        notes = [n for n in notes if n.get("artifact_type") == artifact_type]
    return notes


def get_note(path: str | Path) -> Optional[dict]:
    p = Path(path)
    if not p.is_absolute():
        p = VAULT_DIR / p
    return _load_with_cache(p)


def query_by_tag(tag: str) -> list[dict]:
    tag = tag.lstrip("#")
    out = []
    for n in _all_notes():
        tags = n.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        norm = [str(t).lstrip("#") for t in tags]
        if tag in norm:
            out.append(n)
    return out


def query_by_concept(concept: str) -> list[dict]:
    out = []
    for n in _all_notes():
        concepts = n.get("concepts") or []
        if isinstance(concepts, str):
            concepts = [concepts]
        if concept in concepts:
            out.append(n)
    return out


def get_totals(notes: list[dict]) -> dict:
    """Aggregate stats compatible with parser.get_totals()."""
    return {
        "count":       len(notes),
        "lectures":    sum(1 for n in notes if _normalize_type(n) == "lecture"),
        "tutorials":   sum(1 for n in notes if _normalize_type(n) == "tutorial"),
        "claude_cost": sum(float(n.get("cost_usd") or 0) for n in notes),
        "openai_cost": 0.0,
        "total_cost":  sum(float(n.get("cost_usd") or 0) for n in notes),
        "with_pdf":    sum(1 for n in notes if n.get("pdf_path")),
        "with_board":  sum(1 for n in notes if (n.get("sources_used") or 0) >= 3),
        "sent":        sum(1 for n in notes if n.get("sent")),
    }


# ─── per-brain (per-course) aggregates ────────────────────────────────────

def course_stats(course_key: str) -> dict:
    """Live per-course counts used by the 5-Brains UI cards.

    Returns:
        n_lectures, n_tutorials, n_assignments, n_pending, n_announcements,
        n_concepts, n_notebooklm, n_weak, latest_lecture_date,
        next_due_date, course_name
    """
    lectures = list_summaries(course=course_key, type="lecture")
    tutorials = list_summaries(course=course_key, type="tutorial")
    assignments = list_assignments(course=course_key)
    announces = list_announcements(course=course_key)
    notebooklm = list_notebooklm(course=course_key)

    # concepts: matched via course_key OR via tags (which is how concept
    # notes are actually written by the relinker — tags=['concept', course_key])
    concepts = []
    for c in list_concepts():
        if c.get("course_key") == course_key:
            concepts.append(c)
            continue
        tags = c.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        if course_key in [str(t).lstrip("#") for t in tags]:
            concepts.append(c)

    # weakness count — read from the global tracker. Only entries scoped
    # to this course (`course_topics` keyed `{course_key}::{topic}`) count.
    n_weak = 0
    try:
        from learning.weakness_tracker import _load
        d = _load() or {}
        prefix = f"{course_key}::"
        for k, v in (d.get("course_topics") or {}).items():
            try:
                score = float(v)
            except (TypeError, ValueError):
                continue
            if score >= 0.10 and k.startswith(prefix):
                n_weak += 1
    except Exception:
        pass

    pending = [a for a in assignments
               if (a.get("status") or "").lower() in ("pending", "ממתין")]

    latest = ""
    if lectures or tutorials:
        all_lecs = lectures + tutorials
        all_lecs = sorted(all_lecs,
                          key=lambda n: n.get("date", ""), reverse=True)
        latest = all_lecs[0].get("date", "")

    next_due = ""
    pending_with_due = [a for a in pending if a.get("due_date")]
    if pending_with_due:
        pending_with_due.sort(key=lambda a: a.get("due_date") or "")
        next_due = pending_with_due[0].get("due_date") or ""

    return {
        "course_key":           course_key,
        "course_name":          COURSE_NAMES.get(course_key, course_key),
        "n_lectures":           len(lectures),
        "n_tutorials":          len(tutorials),
        "n_assignments":        len(assignments),
        "n_pending":            len(pending),
        "n_announcements":      len(announces),
        "n_concepts":           len(concepts),
        "n_notebooklm":         len(notebooklm),
        "n_weak":               n_weak,
        "latest_lecture_date":  latest,
        "next_due_date":        next_due,
    }


def all_course_stats() -> list[dict]:
    """course_stats for every course in COURSE_ORDER."""
    return [course_stats(ck) for ck in COURSE_ORDER]


def meta_stats() -> dict:
    """Cross-course aggregates for the meta-brain."""
    courses = all_course_stats()
    return {
        "n_lectures":      sum(c["n_lectures"] for c in courses),
        "n_tutorials":     sum(c["n_tutorials"] for c in courses),
        "n_assignments":   sum(c["n_assignments"] for c in courses),
        "n_pending":       sum(c["n_pending"] for c in courses),
        "n_announcements": sum(c["n_announcements"] for c in courses),
        "n_concepts":      sum(c["n_concepts"] for c in courses),
        "n_notebooklm":    sum(c["n_notebooklm"] for c in courses),
        "n_weak":          sum(c["n_weak"] for c in courses),
        "courses":         courses,
    }
