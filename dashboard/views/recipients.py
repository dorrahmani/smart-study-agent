"""Recipients editor — config/recipients.json (global + per-course lists)."""
from __future__ import annotations

import streamlit as st

from config.paths import COURSE_NAMES
from dashboard import core
from dashboard.ui.components import esc, html, page_header, section, empty_state


def _email_row(email: str, key: str, on_remove) -> None:
    c1, c2 = st.columns([5, 1], vertical_alignment="center")
    c1.markdown(
        f'<div class="ssa-row" style="border:1px solid var(--border);border-radius:10px;background:var(--surface);padding:8px 12px">'
        f'<span aria-hidden="true">📧</span><div class="ssa-row-main"><div class="ssa-row-title" '
        f'style="direction:ltr;text-align:right">{esc(email)}</div></div></div>',
        unsafe_allow_html=True,
    )
    if c2.button("הסר", key=key, use_container_width=True):
        on_remove()


def render() -> None:
    recipients = core.load_recipients()
    page_header("נמענים", "מי מקבל את הסיכומים במייל — לכל הקורסים או לקורס מסוים",
                icon="📧", eyebrow="מערכת")

    section("הוספה מהירה", icon="➕")
    with st.form("quick_add"):
        qa1, qa2, qa3 = st.columns([3, 2, 1], vertical_alignment="bottom")
        with qa1:
            qa_emails = st.text_area("מיילים (מופרדים בפסיק או שורה)",
                                     placeholder="email1@example.com, email2@example.com",
                                     height=75, key="qa_emails")
        with qa2:
            qa_target = st.selectbox("יעד", ["גלובלי (כל הקורסים)"] + [f"📚 {n}" for n in COURSE_NAMES.values()],
                                     key="qa_target")
        with qa3:
            qa_submit = st.form_submit_button("➕ הוסף", type="primary", use_container_width=True)

        if qa_submit:
            addrs = core.parse_email_list(qa_emails)
            if not addrs:
                st.warning("⚠️ אין מיילים תקינים")
            else:
                if qa_target.startswith("גלובלי"):
                    target_key = "global"
                else:
                    name = qa_target.replace("📚 ", "").strip()
                    target_key = next((k for k, v in COURSE_NAMES.items() if v == name), None)
                if target_key:
                    cur = recipients.get(target_key, [])
                    added = [a for a in addrs if a not in cur]
                    recipients[target_key] = cur + added
                    core.save_recipients(recipients)
                    if added:
                        st.success(f"✅ נוספו {len(added)}: {', '.join(added)}")
                    else:
                        st.info("כל המיילים כבר קיימים")
                    st.rerun()

    global_list = recipients.get("global", [])
    section("נמענים גלובליים", f"{len(global_list)} · מקבלים את כל הקורסים", icon="🌐")
    if global_list:
        for email in list(global_list):
            def _rm(email=email):
                global_list.remove(email)
                recipients["global"] = global_list
                core.save_recipients(recipients)
                st.rerun()
            _email_row(email, f"rm_global_{email}", _rm)
    else:
        empty_state("📭", "אין נמענים גלובליים", "השתמש בהוספה מהירה למעלה")

    section("נמענים לפי קורס", icon="📚")
    for course_key, course_name in COURSE_NAMES.items():
        course_list = recipients.get(course_key, [])
        count_label = f"({len(course_list)})" if course_list else "(אין)"
        with st.expander(f"📚 {course_name} {count_label}", expanded=False):
            if course_list:
                for email in list(course_list):
                    def _rm(email=email, course_key=course_key, course_list=course_list):
                        course_list.remove(email)
                        recipients[course_key] = course_list
                        core.save_recipients(recipients)
                        st.rerun()
                    _email_row(email, f"rm_{course_key}_{email}", _rm)
            else:
                st.caption("ישלח רק לנמענים הגלובליים")
    core.mark("recipients:render")
