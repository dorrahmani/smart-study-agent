"""
dashboard/vault_health.py — UI for vault hygiene: orphans, dead links, duplicates.

Pulls live from scripts/find_orphans.scan_vault() (no caching beyond the
function call) and renders three sections in a single Streamlit panel.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from config.paths import VAULT_DIR
from scripts.find_orphans import scan_vault


# ─── Public renderer ───────────────────────────────────────────────────────

def render() -> None:
    from dashboard.ui.components import page_header
    page_header(
        'בריאות ה-Vault',
        'יתומים, קישורים שבורים וכפילויות — אבחון בלבד; אין מחיקה אוטומטית. '
        'לחיצה על "פתח ב-Obsidian" תפתח את הקובץ אם Obsidian מותקן.',
        icon='🔗', eyebrow='מערכת',
    )

    if st.button('🔄 סרוק עכשיו', key='vh_rescan'):
        st.session_state.pop('vh_report', None)

    if 'vh_report' not in st.session_state:
        with st.spinner('סורק את הוולט...'):
            st.session_state['vh_report'] = scan_vault()

    rep = st.session_state['vh_report']

    c1, c2, c3, c4 = st.columns(4)
    c1.metric('סה"כ פתקים', rep.get('total_notes', 0))
    c2.metric('🪦 יתומים', rep.get('orphan_count', 0))
    c3.metric('💀 קישורים שבורים', rep.get('dead_link_count', 0))
    c4.metric('👥 כפילויות', len(rep.get('duplicates_by_stem', {}))
                      + len(rep.get('duplicates_by_title', {})))

    st.divider()

    tabs = st.tabs(['🪦 יתומים', '💀 קישורים שבורים', '👥 כפילויות'])

    # ── Orphans ─────────────────────────────────────────────────────
    with tabs[0]:
        orphans = rep.get('orphans', [])
        if not orphans:
            st.success('🎉 אפס יתומים. כל פתק מקושר לפחות פעם אחת.')
        else:
            st.caption(f'{len(orphans)} פתקים ללא שום קישור (נכנס/יוצא).')
            for o in orphans[:50]:
                cols = st.columns([5, 2, 2])
                cols[0].markdown(
                    f'**{o["stem"]}**  '
                    f'· `{o.get("type") or "?"}`'
                    + (f' · _{o.get("course_key")}_' if o.get('course_key') else '')
                )
                cols[1].caption(o['rel_path'])
                cols[2].markdown(_obsidian_link(o['rel_path']))
            if len(orphans) > 50:
                st.caption(f'(מציג 50 מתוך {len(orphans)})')

    # ── Dead links ──────────────────────────────────────────────────
    with tabs[1]:
        dead = rep.get('dead_links', [])
        if not dead:
            st.success('🎉 אפס קישורים שבורים.')
        else:
            # Group by source for cleaner reading
            by_source: dict[str, list[str]] = {}
            for d in dead:
                by_source.setdefault(d['source'], []).append(d['target'])
            st.caption(f'{len(dead)} קישורים שמצביעים לפתקים שלא קיימים.')
            for src, targets in sorted(by_source.items()):
                with st.expander(f'**{src}** ({len(targets)} שבורים)'):
                    for t in targets:
                        st.markdown(f'- `[[{t}]]`')

    # ── Duplicates ─────────────────────────────────────────────────
    with tabs[2]:
        dup_stem  = rep.get('duplicates_by_stem', {})
        dup_title = rep.get('duplicates_by_title', {})
        if not dup_stem and not dup_title:
            st.success('🎉 אין כפילויות.')
        if dup_stem:
            st.markdown('**כפילויות לפי שם קובץ (stem)**')
            for stem, paths in dup_stem.items():
                with st.expander(stem):
                    for p in paths:
                        cols = st.columns([5, 2])
                        cols[0].code(p)
                        cols[1].markdown(_obsidian_link(p))
        if dup_title:
            st.markdown('**כפילויות לפי כותרת (title)**')
            for title, paths in dup_title.items():
                with st.expander(title):
                    for p in paths:
                        cols = st.columns([5, 2])
                        cols[0].code(p)
                        cols[1].markdown(_obsidian_link(p))

    # ── Export raw ──────────────────────────────────────────────────
    with st.expander('📥 הורד דוח JSON גולמי'):
        st.download_button(
            'הורד vault_health.json',
            data=json.dumps(rep, ensure_ascii=False, indent=2),
            file_name='vault_health.json',
            mime='application/json',
        )


def _obsidian_link(rel_path: str) -> str:
    """Build an obsidian:// link for the given vault-relative path."""
    import urllib.parse
    vault_name = VAULT_DIR.parent.name  # the parent IS the Obsidian Vault root
    file_param = urllib.parse.quote(rel_path.replace('\\', '/'))
    vault_param = urllib.parse.quote(vault_name)
    url = f'obsidian://open?vault={vault_param}&file={file_param}'
    return f'[פתח ב-Obsidian]({url})'
