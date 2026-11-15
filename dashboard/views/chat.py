"""Smart chat — deadlines, RAG over summaries, homework review, NotebookLM triggers."""
from __future__ import annotations

import streamlit as st

from dashboard import core


def render() -> None:
    try:
        from dashboard import smart_chat as _smart_chat
        _smart_chat.render_smart_chat()
    except Exception as e:
        st.error(f'שגיאה בטעינת הצ\'אט: {e}')
        import traceback
        st.code(traceback.format_exc())
    core.mark("chat:render")
