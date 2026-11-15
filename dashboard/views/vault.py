"""Vault health — orphans, dead links, duplicates (diagnostic only)."""
from __future__ import annotations

import streamlit as st

from dashboard import core


def render() -> None:
    try:
        from dashboard import vault_health as _vault_health
        _vault_health.render()
    except Exception as e:
        st.error(f'שגיאה בטעינת בריאות ה-Vault: {e}')
        import traceback
        st.code(traceback.format_exc())
    core.mark("vault:render")
