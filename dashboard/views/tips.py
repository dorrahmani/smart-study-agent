"""Professor tips — add exam tips / FAQs / common mistakes to summaries (admin only)."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from dashboard import core
from dashboard.ui.components import page_header
from dashboard.views.professor_common import COURSE_KEYS


def _has_tips(md_path):
    try:
        content = Path(md_path).read_text(encoding='utf-8', errors='replace')
        return '## 👨‍🏫 הערות הפרופסור' in content
    except Exception:
        return False


def render() -> None:
    page_header("טיפי פרופסור", "בחר סיכום ולחץ — הפרופסור יוסיף טיפים, שאלות נפוצות בבחינות וטעויות שכיחות.",
                icon="💡", eyebrow="כלי לימוד")
    summaries, _ = core.load_data()

    with st.container(border=True, key="card_tips_filter"):
        col_filter1, col_filter2 = st.columns([2, 1], vertical_alignment="bottom")
        with col_filter1:
            tip_course_filter = st.selectbox("קורס", ["הכל"] + list(COURSE_KEYS.values()), key="tip_course")
        with col_filter2:
            show_all_tips = st.checkbox("הצג גם עם טיפים", value=False, key="show_all_tips")

    filtered = []
    for s in summaries:
        course_name = COURSE_KEYS.get(s.get('course_key', ''), s.get('course_key', ''))
        if tip_course_filter != "הכל" and course_name != tip_course_filter:
            continue
        if not show_all_tips and _has_tips(s.get('path', '')):
            continue
        filtered.append(s)

    label = 'ממתינים לטיפים' if not show_all_tips else 'סה"כ'
    st.caption(f"{len(filtered)} סיכומים {label}")

    if filtered:
        for s in filtered[:20]:
            with st.expander(f"{'✅' if _has_tips(s.get('path', '')) else '⬜'} [{s.get('course_key', '')}] "
                             f"{s.get('title', '')} — {s.get('date', '')}"):
                if _has_tips(s.get('path', '')):
                    st.success("טיפים כבר נוספו לסיכום זה")
                    try:
                        content = Path(s['path']).read_text(encoding='utf-8', errors='replace')
                        tips_start = content.find('## 👨‍🏫 הערות הפרופסור')
                        if tips_start >= 0:
                            st.markdown(content[tips_start:tips_start + 2000])
                    except Exception:
                        pass
                else:
                    # key includes a path hash: the same filename can appear twice
                    # (StreamlitDuplicateElementKey with the old filename-only key)
                    import hashlib
                    _h = hashlib.md5(str(s.get('path', '')).encode('utf-8')).hexdigest()[:6]
                    if st.button("💡 הוסף טיפים", key=f"tips_{s.get('filename', '')}~{_h}"):
                        with st.spinner("הפרופסור חושב..."):
                            try:
                                from generators.professor_agent import ProfessorAgent
                                agent = ProfessorAgent()
                                result = agent.add_tips_to_summary(s['path'], s.get('course_key', ''))
                                st.success(f"✅ נוספו טיפים! עלות: ${result.get('cost_usd', 0):.4f}")
                                core.refresh()
                            except Exception as e:
                                st.error(f"❌ שגיאה: {e}")
    else:
        st.info("כל הסיכומים כבר קיבלו טיפים! 🎉")

    st.markdown("---")
    if st.button("💡 הוסף טיפים לכל הסיכומים החדשים", type="primary", use_container_width=True, key="tips_all"):
        with st.spinner("הפרופסור עובד על כולם..."):
            try:
                from generators.professor_agent import ProfessorAgent
                agent = ProfessorAgent()
                all_summaries, _ = core.load_data()
                total_cost = 0.0
                done = 0
                for s in all_summaries:
                    if not _has_tips(s.get('path', '')):
                        result = agent.add_tips_to_summary(s['path'], s.get('course_key', ''))
                        total_cost += result.get('cost_usd', 0)
                        done += 1
                st.success(f"✅ נוספו טיפים ל-{done} סיכומים | עלות: ${total_cost:.4f}")
                core.refresh()
            except Exception as e:
                st.error(f"❌ שגיאה: {e}")
    core.mark("tips:render")
