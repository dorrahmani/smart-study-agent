"""Vault viewer — render any vault note inline with clickable wikilinks."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import streamlit as st

from dashboard import core
from dashboard.ui.components import page_header, empty_state


def render() -> None:
    try:
        from dashboard import vault_reader as vr
    except Exception as e:
        st.error(f'שגיאה בטעינת vault_reader: {e}')
        return

    page_header("צפייה ב-Vault", "בחר סוג ופתק. wikilinks קליקבילים פותחים פתקים אחרים כאן, או ב-Obsidian.",
                icon="📖", eyebrow="ידע")

    with st.container(border=True, key="card_viewer_pick"):
        col1, col2, col3 = st.columns([1, 2.4, 1], vertical_alignment="bottom")
        with col1:
            kind = st.selectbox("סוג", ["סיכומים", "מטלות", "הודעות", "מושגים", "NotebookLM"],
                                key="viewer_kind")
        try:
            if kind == "סיכומים":
                items = vr.list_summaries()
            elif kind == "מטלות":
                items = vr.list_assignments()
            elif kind == "הודעות":
                items = vr.list_announcements()
            elif kind == "מושגים":
                items = vr.list_concepts()
            else:
                items = vr.list_notebooklm()
        except Exception as e:
            items = []
            st.warning(f'שגיאה בטעינת רשימה: {e}')

        try:
            items = sorted(items, key=lambda n: str(n.get('date') or ''), reverse=True)
        except Exception:
            pass

        sel = None
        with col2:
            labels = []
            for n in items:
                lbl = n.get('title') or Path(n.get('path', '')).stem
                d = n.get('date') or ''
                ck = n.get('course') or n.get('course_key') or ''
                labels.append(f"{d}  ·  {lbl}  ·  {ck}".strip(' · '))
            target = st.session_state.get('viewer_target_path')
            default_idx = 0
            if target:
                for i, n in enumerate(items):
                    if str(n.get('path')) == str(target):
                        default_idx = i
                        break
            sel = st.selectbox("פתק", list(range(len(items))) or [0],
                               format_func=lambda i: labels[i] if 0 <= i < len(labels) else "—",
                               index=default_idx if labels else 0,
                               key="viewer_sel") if labels else None
        with col3:
            if items and sel is not None and 0 <= sel < len(items):
                p_open = items[sel].get('path', '')
                if p_open:
                    st.markdown(f"[פתח ב-Obsidian ↗](obsidian://open?path={quote(str(p_open))})")

    if not items or sel is None:
        empty_state("🗒️", "אין פתקים מסוג זה עדיין.")
        return

    note = vr.get_note(items[sel]['path']) or items[sel]
    fm_keys = ('course', 'date', 'week', 'type', 'concepts',
               'topics_from_first_page', 'sender', 'status',
               'due_date', 'grade', 'panopto_url', 'sources_used',
               'cost_usd', 'weakness_score')
    with st.expander("📋 frontmatter", expanded=False):
        meta = {k: note.get(k) for k in fm_keys if note.get(k) not in (None, [], '')}
        if note.get('related'):
            meta['related'] = note['related']
        st.json(meta)

    try:
        stem_to_path = {Path(n['path']).stem: n['path']
                        for n in (vr.list_summaries() + vr.list_assignments()
                                  + vr.list_announcements() + vr.list_concepts()
                                  + vr.list_notebooklm())}
    except Exception:
        stem_to_path = {}

    body = note.get('body') or ''
    wl = re.compile(r'\[\[([^\[\]\|\n]+?)(?:\|([^\[\]\n]+?))?\]\]')
    parts = []
    last = 0
    for m in wl.finditer(body):
        if m.start() > last:
            parts.append(('md', body[last:m.start()]))
        parts.append(('wl', m.group(1).strip(), (m.group(2) or m.group(1)).strip()))
        last = m.end()
    if last < len(body):
        parts.append(('md', body[last:]))

    with st.container(border=True, key="card_viewer_note"):
        for idx, part in enumerate(parts):
            if part[0] == 'md':
                if part[1].strip():
                    st.markdown(part[1])
            else:
                _, target_stem, label = part
                target_path = stem_to_path.get(target_stem)
                cols = st.columns([1.3, 4], vertical_alignment="center")
                with cols[0]:
                    if target_path:
                        if st.button(f"🔗 {label}", key=f"wl_{idx}_{target_stem}", use_container_width=True):
                            st.session_state['viewer_target_path'] = target_path
                            st.rerun()
                    else:
                        st.caption(f"⚠️ {label} (לא נמצא)")
                with cols[1]:
                    st.caption(f"`[[{target_stem}]]`")
    core.mark("viewer:render")
