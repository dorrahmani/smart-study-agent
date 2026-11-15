"""Today ("🎯 היום") — sync / diff / ask strip, last-sync diff, then the brains
grid (this semester first) or a focused brain / the meta-brain."""
from __future__ import annotations

import streamlit as st

from dashboard import core


def render() -> None:
    try:
        from dashboard import brains as _brains
    except Exception as e:
        st.error(f'שגיאה בטעינת המוחות: {e}')
        return

    _brains.render_top_strip()
    st.markdown('<hr class="qc-rule" />', unsafe_allow_html=True)
    _brains.render_diff_panel()
    st.markdown('<hr class="qc-rule" />', unsafe_allow_html=True)

    active = st.session_state.get('active_brain')
    if active and active != '__meta__':
        _brains.render_brain_view(active)
    elif active == '__meta__':
        _brains.render_meta_view()
    else:
        _brains.render_brains_grid()
    core.mark("today:render")
