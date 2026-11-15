"""NotebookLM — quizzes, podcasts, videos, flashcards, study guides, Q&A."""
from __future__ import annotations

from dashboard import core

# This semester first, then previous ones; a new course gets its notebook on
# first use (NotebookLMAgent.get_or_create_notebook).
from dashboard.views.professor_common import semester_course_map as _nb_courses


def render() -> None:
    from dashboard import notebooklm_panel
    from config.paths import VAULT_ROOT
    notebooklm_panel.render(vault_root=VAULT_ROOT, courses=_nb_courses())
    core.mark("notebooklm:render")
