"""
exporters/_frontmatter_lint.py — shared frontmatter validator.

Every exporter calls validate_frontmatter(meta, kind) right before writing.
Missing required keys raise FrontmatterValidationError loudly (caller decides
to log + skip vs. crash).

`kind` matches plan §B1 type vocabulary:
    'lecture' | 'tutorial' | 'assignment' | 'announcement' |
    'concept' | 'nlm_artifact' | 'weekly_digest'
"""

from __future__ import annotations


class FrontmatterValidationError(ValueError):
    """Raised when a note's frontmatter is missing required keys."""


# Per-kind required keys. `type` and `tags` are required everywhere.
_REQUIRED: dict[str, set[str]] = {
    'lecture':       {'type', 'title', 'course', 'course_key', 'date', 'tags'},
    'tutorial':      {'type', 'title', 'course', 'course_key', 'date', 'tags'},
    'assignment':    {'type', 'title', 'course', 'course_key', 'status', 'tags'},
    'announcement':  {'type', 'title', 'course', 'course_key', 'date', 'tags'},
    'concept':       {'type', 'title', 'tags'},
    'nlm_artifact':  {'type', 'artifact_type', 'title', 'course_key', 'date', 'tags'},
    'weekly_digest': {'type', 'iso_year', 'iso_week', 'tags'},
}


def validate_frontmatter(meta: dict, kind: str, *, strict: bool = False) -> list[str]:
    """
    Returns a list of missing keys (empty list = valid).
    If strict=True, raises FrontmatterValidationError when any are missing.
    """
    required = _REQUIRED.get(kind, {'type', 'tags'})
    missing = sorted(k for k in required if not meta.get(k))
    if missing and strict:
        raise FrontmatterValidationError(
            f'{kind}: missing required frontmatter keys {missing}. '
            f'Got: {sorted(meta.keys())}'
        )
    return missing
