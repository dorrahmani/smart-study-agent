"""Summaries ("📚 סיכומים") — the pre-redesign list: filters, semantic search,
select + bulk send to any addresses, send-all-for-course, per-summary panels
(send to me / to everyone / to a custom address, sources, preview, download,
tags, actions), flat or course→week view. Friends get the read-only PDF list."""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import streamlit as st

from config.paths import COURSE_NAMES
from dashboard import core
from dashboard.ui.components import esc
from dashboard.parser import load_ratings, save_ratings, SUMMARIES_DIR, get_all_summaries

# Old helper names → dashboard.core (same behaviour, shared with the other views)
_can_see_course = core.can_see_course
_user_role = core.user_role
_parse_email_list = core.parse_email_list
_get_reviewer_email = core.get_reviewer_email
_get_all_emails_for_course = core.get_all_emails_for_course
_pending_approvals = core.pending_approvals
_approve_and_send_all = core.approve_and_send_all
_load_approval_log = core.load_approval_log
_save_approval_log = core.save_approval_log
send_summary_to = core.send_summary_to
source_badges = core.source_badges
load_data = core.load_data
refresh = core.refresh
ROOT = core.ROOT


def _uid(s) -> str:
    """Widget-key id: the same filename can exist twice (two courses / vault
    copies) — filename-only keys crashed with StreamlitDuplicateElementKey.
    The selection set still holds filenames."""
    return f"{s['filename']}~{hashlib.md5(str(s['path']).encode('utf-8')).hexdigest()[:6]}"

import re as _re

TAGS_OPTIONS = ["📌 חשוב למבחן", "✅ עברתי על זה", "❓ לא הבנתי", "🔁 לחזור שוב", "⭐ מצוין"]

def _read_sources_section(path: str) -> str:
    try:
        content = Path(path).read_text(encoding='utf-8', errors='replace')
        m = _re.search(r'(## חומרים שנסרקו.*?)(?=\n## |\Z)', content, _re.DOTALL)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""

def _read_md_preview(path: str) -> str:
    """Returns the summary content without the sources/costs section"""
    try:
        content = Path(path).read_text(encoding='utf-8', errors='replace')
        # vault notes start with YAML frontmatter — don't render it as a heading
        fm = _re.match(r'^---\s*\n.*?\n---\s*\n', content, _re.DOTALL)
        if fm:
            content = content[fm.end():]
        # Cut before the sources section
        cut = _re.search(r'\n## חומרים שנסרקו', content)
        return content[:cut.start()].strip() if cut else content[:4000].strip()
    except Exception:
        return ""

@st.cache_data
def _semester_start() -> datetime:
    """Reads the semester start date from the config"""
    try:
        import yaml
        with open(ROOT / 'config/courses.yaml', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        s = cfg['settings']['schedule'].get('semester_start', '')
        if s:
            return datetime.strptime(str(s), '%Y-%m-%d')
    except Exception:
        pass
    # fallback: infer from the earliest summary
    try:
        all_s = get_all_summaries()
        dates = [s['date'] for s in all_s if s['date']]
        if dates:
            earliest = min(dates)
            dt = datetime.strptime(earliest, '%Y-%m-%d')
            return dt - __import__('datetime').timedelta(days=dt.weekday())
    except Exception:
        pass
    return datetime(2026, 4, 6)

def _week_label(date_str: str) -> str:
    """Returns a week label relative to the semester — an Israeli week: Sunday to Saturday"""
    try:
        import datetime as _dt
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        sem_start = _semester_start()
        # The Israeli week starts on Sunday: weekday()=6→Sunday, 0→Monday, ..., 5→Saturday
        days_since_sunday = (dt.weekday() + 1) % 7
        week_start = dt - _dt.timedelta(days=days_since_sunday)
        week_num = max(1, ((week_start - sem_start).days) // 7 + 1)
        week_thu = week_start + _dt.timedelta(days=4)  # Thursday = Sunday + 4
        return f"שבוע {week_num} ({week_start.strftime('%d/%m')} – {week_thu.strftime('%d/%m')})"
    except Exception:
        return "תאריך לא ידוע"


def render() -> None:
    summaries, _ = load_data()
    ratings = load_ratings()

    # Role-based filter: friend sees only allowed courses' summaries.
    summaries = [s for s in summaries if _can_see_course(s.get('course', ''))]

    # ── Global semantic search (RAG over all summaries) ──────────────────────
    with st.expander("🔍 חיפוש סמנטי בכל הסיכומים", expanded=False):
        _gs_q = st.text_input(
            "שאל שאלה / חפש מושג",
            placeholder='לדוגמה: "מה זה משפט קיילי-המילטון?"',
            key="global_search_q",
            label_visibility="collapsed",
        )
        if _gs_q and len(_gs_q) > 2:
            try:
                from rag.vault_indexer import search as _rag_search
                # Filter to user's allowed courses
                _gs_hits = _rag_search(_gs_q, k=10)
                _gs_hits = [h for h in _gs_hits if _can_see_course(h.get('course_key', ''))]
            except Exception as _e:
                st.error(f"שגיאת חיפוש: {_e}")
                _gs_hits = []
            if not _gs_hits:
                st.info("🔍 לא נמצאו תוצאות מתאימות.")
            else:
                st.caption(f"נמצאו {len(_gs_hits)} תוצאות (מסודר לפי רלוונטיות)")
                for h in _gs_hits[:8]:
                    _gs_course = COURSE_NAMES.get(h.get('course_key', ''), h.get('course_key', '?'))
                    _gs_stem   = h.get('stem', '?')
                    _gs_score  = h.get('score', 0)
                    _gs_snippet = " ".join((h.get('text', '') or '')[:300].split())  # one line: no markdown headings
                    st.markdown(
                        f"**{esc(_gs_stem)}**  \n"
                        f"<span style='color:#888;font-size:0.85em'>"
                        f"{esc(_gs_course)} · רלוונטיות {_gs_score:.2f}</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f"<div style='background:var(--surface);padding:8px 12px;border-radius:var(--radius);"
                        f"font-size:0.9em;margin:4px 0;'>{esc(_gs_snippet)}...</div>",
                        unsafe_allow_html=True,
                    )
    _allowed_course_names = [
        COURSE_NAMES[k] for k in COURSE_NAMES.keys() if _can_see_course(k)
    ]

    # ── Simple read-only view for non-admin users ─────────────────────────────
    # Friend sees: clean list with PDF download buttons. No filters/ratings.
    _is_friend = (_user_role() != 'admin')
    if _is_friend:
        st.markdown(f'### 📚 סיכומים זמינים ({len(summaries)})')
        if not summaries:
            st.info('אין סיכומים זמינים כרגע.')
        else:
            # Sort + type filter for friend view
            fs1, fs2 = st.columns([2, 2])
            with fs1:
                _sort_order = st.radio(
                    'מיון',
                    ['חדש לישן', 'ישן לחדש', 'לפי שם'],
                    horizontal=True, key='friend_sort',
                )
            with fs2:
                _type_filter = st.radio(
                    'סוג',
                    ['הכל', 'הרצאה', 'תרגול'],
                    horizontal=True, key='friend_type',
                )
            # Apply type filter
            if _type_filter != 'הכל':
                summaries = [s for s in summaries if s.get('type_label') == _type_filter]
            # Apply sort
            from datetime import datetime as _dt
            def _date_key(s):
                d = s.get('date', '')
                try: return _dt.strptime(d, '%Y-%m-%d')
                except: return _dt.min
            if _sort_order == 'חדש לישן':
                summaries_sorted = sorted(summaries, key=_date_key, reverse=True)
            elif _sort_order == 'ישן לחדש':
                summaries_sorted = sorted(summaries, key=_date_key)
            else:  # by name
                summaries_sorted = sorted(summaries, key=lambda s: s.get('title', ''))
            st.caption(f'מציג {len(summaries_sorted)} סיכומים')
            st.markdown('---')
            for s in summaries_sorted:
                cname = COURSE_NAMES.get(s.get('course', ''), s.get('course', ''))
                kind = '📝 תרגול' if s.get('type_label') == 'תרגול' else '📖 הרצאה'
                with st.container():
                    c1, c2 = st.columns([5, 1])
                    with c1:
                        st.markdown(f"**{esc(s.get('title', ''))}**  \n"
                                    f"<span style='color:#888;font-size:0.85em'>"
                                    f"{kind} · {esc(cname)} · {esc(s.get('date', ''))}</span>",
                                    unsafe_allow_html=True)
                    # Friend gets PDF download only (no MD).
                    stem_no_ext = s.get('filename', '').replace('.md', '')
                    pdf_data_path = SUMMARIES_DIR / s.get('course','') / s.get('type_folder','lectures') / f'{stem_no_ext}.pdf'
                    with c2:
                        if pdf_data_path.exists():
                            st.download_button(
                                '⬇️ PDF', data=pdf_data_path.read_bytes(),
                                file_name=pdf_data_path.name, mime='application/pdf',
                                key=f"friend_pdf_{_uid(s)}",
                                use_container_width=True,
                            )
                        else:
                            st.caption('PDF חסר')
                    st.markdown('<hr style="margin:8px 0;border:none;border-top:1px solid #eee">',
                                unsafe_allow_html=True)
        return  # the admin UI below is never rendered for friends

    if 'selected' not in st.session_state:
        st.session_state.selected = set()

    # ── Filters + view toggle ─────────────────────────────────────────────────
    def _reset_page():
        st.session_state.page = 0

    fc1, fc2, fc3, fc4, fc5 = st.columns([2, 1.5, 1.5, 2, 1.5])
    with fc1:
        search_q = st.text_input("🔍 חיפוש", placeholder="חפש בכותרת / תוכן...", key="sf_search", on_change=_reset_page)
    with fc2:
        course_filter = st.selectbox("קורס", ["הכל"] + _allowed_course_names, key="sf_course", on_change=_reset_page)
    with fc3:
        type_filter = st.selectbox("סוג", ["הכל", "הרצאה", "תרגול"], key="sf_type", on_change=_reset_page)
    with fc4:
        sent_filter = st.selectbox("סטטוס", ["הכל", "נשלח", "לא נשלח"], key="sf_sent", on_change=_reset_page)
    with fc5:
        view_mode = st.selectbox("תצוגה", ["רגיל", "לפי קורס + שבוע"], key="sf_view", on_change=_reset_page)

    filtered = summaries
    if course_filter != "הכל":
        filtered = [s for s in filtered if s['course_name'] == course_filter]
    if type_filter != "הכל":
        filtered = [s for s in filtered if s['type_label'] == type_filter]
    if sent_filter == "נשלח":
        filtered = [s for s in filtered if s['sent']]
    elif sent_filter == "לא נשלח":
        filtered = [s for s in filtered if not s['sent']]
    if search_q:
        q = search_q.lower()
        def _matches(s):
            if q in s['title'].lower() or q in s['course_name'].lower():
                return True
            # search inside file content
            try:
                return q in Path(s['path']).read_text(encoding='utf-8', errors='replace').lower()
            except Exception:
                return False
        filtered = [s for s in filtered if _matches(s)]

    # ── Send all for course button ────────────────────────────────────────────
    if course_filter != "הכל":
        with st.expander(f"📤 שלח את כל סיכומי {course_filter} ({len(filtered)} סיכומים)", expanded=False):
            with st.form(f"send_all_{course_filter}"):
                bulk_course_recip = st.text_area(
                    "כתובות מייל (מופרדות בפסיק, נקודה-פסיק, או שורה)",
                    placeholder="email1@example.com, email2@example.com",
                    height=80
                )
                if st.form_submit_button(f"📧 שלח {len(filtered)} סיכומים"):
                    addrs = _parse_email_list(bulk_course_recip)
                    if not addrs:
                        st.warning("⚠️ אין כתובות מייל תקינות")
                    else:
                        total = len(filtered) * len(addrs)
                        prog = st.progress(0)
                        errs = []
                        done = 0
                        for s in filtered:
                            for addr in addrs:
                                ok = send_summary_to(s, addr)
                                if not ok:
                                    errs.append(f"{s['title']} → {addr}")
                                done += 1
                                prog.progress(done / total)
                        if errs:
                            st.error(f"נכשלו {len(errs)}: {', '.join(errs[:3])}...")
                        else:
                            st.success(f"✅ נשלחו {len(filtered)} סיכומים ל-{len(addrs)} נמענים ({total} מיילים)")

    # ── Bulk send panel — supports multiple emails ───────────────────────────
    n_selected = len(st.session_state.selected)
    with st.container():
        bc, bd, bb, ba = st.columns([3.5, 1.3, 0.55, 0.55])
        with ba:
            if st.button("☑ הכל", key="sel_all_btn", help="בחר את כל הסיכומים המסוננים"):
                st.session_state.selected = {s['filename'] for s in filtered}
                for s in filtered:
                    st.session_state[f"cb_{_uid(s)}"] = True
                st.rerun()
        with bb:
            if st.button("✖ נקה", key="desel_all_btn", help="בטל את כל הבחירות"):
                st.session_state.selected = set()
                for s in filtered:
                    st.session_state[f"cb_{_uid(s)}"] = False
                st.rerun()
        with bc:
            bulk_recip = st.text_area(
                f"שלח {n_selected} נבחרים לכתובות הבאות:" if n_selected else "בחר סיכומים ואז הזן כתובות שליחה:",
                placeholder="email1@example.com, email2@example.com",
                key="bulk_recip",
                height=75,
                label_visibility="visible"
            )
        with bd:
            st.markdown("<div style='padding-top:28px'>", unsafe_allow_html=True)
            parsed_addrs = _parse_email_list(bulk_recip)
            btn_disabled = (n_selected == 0 or not parsed_addrs)
            btn_label = f"📧 שלח ({n_selected}×{len(parsed_addrs)})" if parsed_addrs else f"📧 שלח ({n_selected})"
            if st.button(btn_label, disabled=btn_disabled, type="primary"):
                sel_summaries = [s for s in filtered if s['filename'] in st.session_state.selected]
                total = len(sel_summaries) * len(parsed_addrs)
                progress = st.progress(0)
                errors = []
                done = 0
                for s in sel_summaries:
                    for addr in parsed_addrs:
                        with st.spinner(f"שולח {done+1}/{total}: {s['title']} → {addr}"):
                            ok = send_summary_to(s, addr)
                            if not ok:
                                errors.append(f"{s['title']} → {addr}")
                        done += 1
                        progress.progress(done / total)
                if errors:
                    st.error(f"נכשלו {len(errors)}: {', '.join(errors[:3])}")
                else:
                    st.success(f"✅ נשלחו {len(sel_summaries)} סיכומים ל-{len(parsed_addrs)} נמענים")
                st.session_state.selected = set()
            st.markdown("</div>", unsafe_allow_html=True)

    # ── Export CSV ───────────────────────────────────────────────────────────
    def _build_csv(summaries_list) -> bytes:
        import io as _io, csv as _csv
        rows = [{
            'תאריך':   s['date'],
            'קורס':    s['course_name'],
            'סוג':     s['type_label'],
            'כותרת':   s['title'],
            'תמלול':   s['sources'].get('transcript') or '',
            'PDF':      s['sources'].get('pdf') or '',
            'פריימי לוח': s['sources'].get('board_frames') or 0,
            'עלות ($)': s['costs']['total'],
            'נשלח':    'כן' if s['sent'] else 'לא',
            'דירוג':   {'up':'טוב','down':'צריך שיפור'}.get(s['rating'],''),
            'הערות':   s['notes'],
        } for s in summaries_list]
        buf = _io.StringIO()
        if rows:
            w = _csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return buf.getvalue().encode('utf-8-sig')

    exp_col, cnt_col = st.columns([1, 4])
    with exp_col:
        st.download_button(
            "📥 ייצוא CSV",
            data=_build_csv(filtered),
            file_name=f"summaries_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )
    with cnt_col:
        st.markdown(f"<div style='color:var(--muted);font-size:12px;padding-top:10px'>{len(filtered)} סיכומים</div>",
                    unsafe_allow_html=True)

    # ── Summary card renderer ─────────────────────────────────────────────────
    # Only the ONE "active" card renders full content — all others are lightweight rows.
    def _render_summary_card(s, ratings, grouped=False):
        icon = '🎓' if s['type_folder'] == 'lectures' else '✏️'
        sent_mark = ' ✓' if s['sent'] else ''
        date_short = s['date'][8:10] + '/' + s['date'][5:7] if s['date'] and len(s['date']) == 10 else s['date']
        if grouped:
            label = f"{icon} {s['title']}  ·  {date_short}{sent_mark}"
        else:
            label = f"{icon} {s['course_name']} — {s['title']}  ·  {date_short}{sent_mark}"

        cb_key  = f"cb_{_uid(s)}"
        exp_key = f"exp_{_uid(s)}"
        if cb_key not in st.session_state:
            st.session_state[cb_key] = s['filename'] in st.session_state.selected
        if exp_key not in st.session_state:
            st.session_state[exp_key] = False

        is_expanded = st.session_state[exp_key]

        # ── compact header row ────────────────────────────────────────────────
        sent_badge = '<span class="badge badge-sent">✓ נשלח</span>' if s['sent'] \
                     else '<span class="badge badge-unsent">✗ לא נשלח</span>'
        rating_badge = ""
        if s['rating'] == 'up':   rating_badge = '<span class="badge badge-up">👍</span>'
        elif s['rating'] == 'down': rating_badge = '<span class="badge badge-down">👎</span>'

        tag_badges = ""
        for t in (ratings.get(s['rating_key'], {}).get('tags') or []):
            tag_badges += f'<span class="badge" style="background:rgba(99,102,241,.1);color:var(--indigo-l);border-color:rgba(99,102,241,.25)">{t}</span> '

        cost_str = f"${s['costs']['total']:.2f}" if s['costs']['total'] > 0.001 else ""
        expand_arrow = "▲" if is_expanded else "▼"

        _row_type = s['type_folder']  # 'lectures' or 'tutorials'
        _row_cls = f"summary-row {'lecture' if _row_type == 'lectures' else 'tutorial'}{' expanded' if is_expanded else ''}"

        row_l, row_cb, row_btn = st.columns([0.88, 0.05, 0.07])
        with row_l:
            st.markdown(f"""
            <div class="{_row_cls}" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
              <span style="font-size:13.5px;font-weight:500;color:var(--text)">{esc(label)}</span>
              &nbsp;{sent_badge}{rating_badge}{tag_badges}
              {source_badges(s['sources'])}
              <span style="color:var(--muted);font-size:11px;margin-inline-start:auto">{cost_str}</span>
            </div>
            """, unsafe_allow_html=True)
        with row_cb:
            checked = st.checkbox("בחר", key=cb_key, label_visibility="collapsed")
            if checked: st.session_state.selected.add(s['filename'])
            else:       st.session_state.selected.discard(s['filename'])
        with row_btn:
            btn_text = ("▲ סגור" if is_expanded else "▼ פתח")
            if st.button(btn_text, key=f"toggle_card_{_uid(s)}", use_container_width=True):
                st.session_state[exp_key] = not is_expanded
                st.rerun()

        # ── expanded content (lazy — only renders when open) ──────────────────
        if is_expanded:
            with st.container():
                # Tab-style button selector (not radio) — only chosen panel renders
                panel_key = f"panel_{_uid(s)}"
                if panel_key not in st.session_state:
                    st.session_state[panel_key] = "שליחה"

                PANELS = [
                    ("שליחה",  "📧"),
                    ("מקורות", "📋"),
                    ("תצוגה",  "👁"),
                    ("הורדה",  "⬇️"),
                    ("תגיות",  "🏷️"),
                    ("פעולות", "⚙️"),
                ]
                chosen = st.session_state[panel_key]

                # Pill tabs — HTML visual + Streamlit buttons for click handling
                pills_html = '<div style="display:flex;gap:5px;flex-wrap:wrap;margin:8px 0 6px 0">'
                for name, emoji in PANELS:
                    active = (chosen == name)
                    bg  = 'var(--overlay)' if active else 'transparent'
                    col = 'var(--text)' if active else 'var(--muted)'
                    brd = 'var(--border2)' if active else 'var(--border)'
                    fw  = '600'    if active else '500'
                    pills_html += (f'<span style="background:{bg};color:{col};border:1px solid {brd};'
                                   f'padding:4px 14px;border-radius:var(--radius);font-size:11px;font-weight:{fw};'
                                   f'text-transform:uppercase;letter-spacing:0.12em">'
                                   f'{emoji} {name}</span>')
                pills_html += '</div>'
                st.markdown(pills_html, unsafe_allow_html=True)

                # Streamlit buttons — hidden by CSS (minimum height), but functional
                st.markdown('<div style="display:flex;gap:4px;margin-bottom:4px">', unsafe_allow_html=True)
                _pcols = st.columns(len(PANELS))
                for i, (name, emoji) in enumerate(PANELS):
                    with _pcols[i]:
                        if st.button(f"{emoji}", key=f"panelbtn_{_uid(s)}_{i}",
                                     use_container_width=True,
                                     help=name):
                            st.session_state[panel_key] = name
                            st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div style="border-top:1px solid var(--border);padding-top:10px;margin:4px 0 10px 0"></div>',
                            unsafe_allow_html=True)

                # ── Send ──
                if chosen == "שליחה":
                    send_result_key = f"send_result_{_uid(s)}"

                    # ── Quick send buttons ─────────────────────────────────
                    _reviewer = _get_reviewer_email()
                    _all_course = _get_all_emails_for_course(s.get('course', ''))
                    _sc1, _sc2 = st.columns(2)
                    with _sc1:
                        if st.button(f"📨 שלח רק אליי", key=f"send_me_{_uid(s)}", use_container_width=True,
                                     help=f"שולח ל-{_reviewer} בלבד"):
                            ok = send_summary_to(s, _reviewer)
                            if ok:
                                st.success(f"✅ נשלח אליך ({_reviewer})")
                            else:
                                st.error("❌ שליחה נכשלה")
                    with _sc2:
                        _n_all = len(_all_course)
                        if st.button(f"📢 שלח לכולם ({_n_all})", key=f"send_all_{_uid(s)}", use_container_width=True,
                                     help=", ".join(_all_course),
                                     type="primary"):
                            _results_all = [(addr, send_summary_to(s, addr)) for addr in _all_course]
                            _ok = [a for a, ok in _results_all if ok]
                            _fail = [a for a, ok in _results_all if not ok]
                            if _ok:
                                st.success(f"✅ נשלח ל-{len(_ok)} נמענים: {', '.join(_ok)}")
                            if _fail:
                                st.error(f"❌ נכשל: {', '.join(_fail)}")

                    st.markdown("<div style='margin:8px 0;border-top:1px solid var(--border)'></div>", unsafe_allow_html=True)

                    # ── Manual send to a custom address ───────────────────
                    with st.form(f"send_{_uid(s)}"):
                        recip_raw = st.text_area(
                            "או שלח לכתובת ספציפית:",
                            placeholder="email1@example.com, email2@example.com",
                            key=f"recip_{_uid(s)}",
                            height=60
                        )
                        submitted = st.form_submit_button("📧 שלח")
                        if submitted:
                            addrs = _parse_email_list(recip_raw)
                            invalid = [a for a in (recip_raw or '').replace(',', '\n').replace(';', '\n').split('\n')
                                       if a.strip() and a.strip() not in addrs]
                            if not addrs:
                                st.session_state[send_result_key] = ('warn', None, invalid)
                            else:
                                results = []
                                for addr in addrs:
                                    ok = send_summary_to(s, addr)
                                    results.append((addr, ok))
                                st.session_state[send_result_key] = ('done', results, invalid)
                    # render result outside the form
                    result = st.session_state.get(send_result_key)
                    if result:
                        kind = result[0]
                        if kind == 'warn':
                            st.warning("⚠️ אין כתובות מייל תקינות")
                        elif kind == 'done':
                            results, invalid = result[1], result[2]
                            ok_count = sum(1 for _, ok in results if ok)
                            fail = [addr for addr, ok in results if not ok]
                            if ok_count:
                                st.success(f"✅ נשלח ל-{ok_count} נמענים: {', '.join(a for a, ok in results if ok)}")
                            if fail:
                                st.error(f"❌ נכשל ל: {', '.join(fail)}")
                            if invalid:
                                st.warning(f"⚠️ כתובות לא תקינות: {', '.join(invalid)}")
                    st.markdown("<div style='font-size:11px;color:var(--muted);margin:10px 0 4px'>האם הסיכום איכותי?</div>", unsafe_allow_html=True)
                    ra, rb, _ = st.columns([0.6, 0.6, 5])
                    with ra:
                        if st.button("👍 מצוין", key=f"up_{_uid(s)}"):
                            r = load_ratings()
                            r[s['rating_key']] = {
                                **r.get(s['rating_key'], {}),
                                'rating': 'up',
                                'notes': 'הסיכום הקודם היה מצוין (👍). שמור על אותו סגנון, רמת פירוט ומבנה בסיכום הבא.',
                                'feedback_pending': True,
                            }
                            save_ratings(r); refresh()
                    with rb:
                        if st.button("👎 יש בעיות", key=f"dn_{_uid(s)}"):
                            r = load_ratings()
                            existing_notes = r.get(s['rating_key'], {}).get('notes', '')
                            r[s['rating_key']] = {
                                **r.get(s['rating_key'], {}),
                                'rating': 'down',
                                'notes': existing_notes or 'הסיכום קיבל דירוג שלילי — נא לפרט הערות בלשונית פעולות ולהפעיל מחדש.',
                                'feedback_pending': True,
                            }
                            save_ratings(r); refresh()

                # ── Sources ──
                elif chosen == "מקורות":
                    sources_raw = _read_sources_section(s['path'])
                    if sources_raw:
                        st.markdown(sources_raw)
                    else:
                        st.info("אין מידע על מקורות בסיכום זה")

                # ── Preview ──
                elif chosen == "תצוגה":
                    preview = _read_md_preview(s['path'])
                    if preview:
                        st.markdown(preview[:6000])
                        if len(preview) > 6000:
                            st.caption("(הסיכום המלא חתוך — פתח את הקובץ לצפייה מלאה)")
                    else:
                        st.info("לא ניתן לקרוא את הסיכום")

                # ── Download ──
                elif chosen == "הורדה":
                    d1, d2 = st.columns(2)
                    md_abs  = ROOT / s['path'] if not Path(s['path']).is_absolute() else Path(s['path'])
                    pdf_abs = Path(str(md_abs).replace('.md', '.pdf'))
                    with d1:
                        if md_abs.exists():
                            st.download_button("⬇️ הורד Markdown (.md)", data=md_abs.read_bytes(),
                                               file_name=md_abs.name, mime="text/markdown",
                                               key=f"dl_md_{_uid(s)}")
                        else:
                            st.warning(f"קובץ לא נמצא: {md_abs}")
                    with d2:
                        if pdf_abs.exists():
                            st.download_button("⬇️ הורד PDF (.pdf)", data=pdf_abs.read_bytes(),
                                               file_name=pdf_abs.name, mime="application/pdf",
                                               key=f"dl_pdf_{_uid(s)}")
                        else:
                            st.info("אין PDF זמין")

                # ── Tags ──
                elif chosen == "תגיות":
                    cur_tags = ratings.get(s['rating_key'], {}).get('tags', [])
                    new_tags = st.multiselect("תגיות:", TAGS_OPTIONS,
                                              default=cur_tags, key=f"tags_{_uid(s)}")
                    if st.button("💾 שמור תגיות", key=f"savetags_{_uid(s)}"):
                        r = load_ratings()
                        r[s['rating_key']] = {**r.get(s['rating_key'],{}), 'tags': new_tags}
                        save_ratings(r); refresh()

                # ── Actions ──
                elif chosen == "פעולות":
                    st.markdown(f"**קובץ:** `{s['path']}`")
                    col_x, col_y = st.columns(2)
                    with col_x:
                        cur_notes = st.text_area("הערות לשיפור (לסיכום מחדש):",
                                                  value=s['notes'],
                                                  key=f"rnotes_{_uid(s)}", height=80)
                        if st.button("♻️ הפעל מחדש בריצה הבאה", key=f"redo_{_uid(s)}"):
                            r = load_ratings()
                            r[s['rating_key']] = {**r.get(s['rating_key'],{}),
                                                   'notes': cur_notes, 'rating':'down',
                                                   'feedback_pending': True}
                            save_ratings(r)
                            Path(s['path']).unlink(missing_ok=True)
                            pdf_p = Path(s['path'].replace('.md','.pdf'))
                            if pdf_p.exists(): pdf_p.unlink()
                            st.success("✅ יוצר מחדש בריצה הבאה"); refresh()
                    with col_y:
                        st.markdown("")
                        if st.button("🗑️ מחק (לא ניתן לשחזור)", key=f"del_{_uid(s)}"):
                            Path(s['path']).unlink(missing_ok=True)
                            pdf_p = Path(s['path'].replace('.md','.pdf'))
                            if pdf_p.exists(): pdf_p.unlink()
                            refresh()

    # ── Render: flat or grouped ───────────────────────────────────────────────
    PAGE_SIZE = 5

    if not filtered:
        st.markdown("""
        <div style="text-align:center; padding: 60px 20px; color: var(--muted)">
          <div style="font-size: 48px; margin-bottom: 14px">📭</div>
          <div style="font-size: 17px; font-weight: 500">אין סיכומים עם הפילטרים האלה</div>
          <div style="font-size: 13px; margin-top: 8px">נסה לאפס פילטר אחד ולבדוק שוב</div>
        </div>
        """, unsafe_allow_html=True)
    elif view_mode == "רגיל":
        total_pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
        if 'page' not in st.session_state:
            st.session_state.page = 0
        # reset page if filters changed and current page is out of range
        if st.session_state.page >= total_pages:
            st.session_state.page = 0

        page_start = st.session_state.page * PAGE_SIZE
        page_end   = page_start + PAGE_SIZE
        for s in filtered[page_start:page_end]:
            _render_summary_card(s, ratings, grouped=False)

        # pagination controls
        if total_pages > 1:
            pc1, pc2, pc3 = st.columns([1, 3, 1])
            # RTL: pc1=right (previous with ▶ pointing right), pc3=left (next with ◀)
            with pc1:
                if st.button("▶ הקודם", disabled=(st.session_state.page == 0),
                              key="pg_prev", use_container_width=True):
                    st.session_state.page -= 1
                    st.rerun()
            with pc2:
                st.markdown(
                    f"<div style='text-align:center;color:var(--muted);font-size:13px;padding-top:8px'>"
                    f"עמוד {st.session_state.page + 1} מתוך {total_pages} &nbsp;·&nbsp; {len(filtered)} סיכומים</div>",
                    unsafe_allow_html=True
                )
            with pc3:
                if st.button("הבא ◀", disabled=(st.session_state.page >= total_pages - 1),
                              key="pg_next", use_container_width=True):
                    st.session_state.page += 1
                    st.rerun()
    else:
        # group by course → week
        from collections import defaultdict
        grouped_data = defaultdict(lambda: defaultdict(list))
        for s in filtered:
            grouped_data[s['course_name']][_week_label(s['date'])].append(s)

        # Open/close-all toggle button — on the same row as the counter
        all_open = all(st.session_state.get(f"course_open_{cn}", True) for cn in grouped_data)
        toggle_label = "סגור הכל ▲" if all_open else "פתח הכל ▼"
        if st.button(toggle_label, key="toggle_all_courses"):
            new_state = not all_open
            for cn in grouped_data:
                st.session_state[f"course_open_{cn}"] = new_state
            st.rerun()

        for course_name, weeks in grouped_data.items():
            key = f"course_open_{course_name}"
            if key not in st.session_state:
                st.session_state[key] = True  # open by default

            n_items = sum(len(v) for v in weeks.values())
            n_weeks = len(weeks)

            # Course header — one row with all the buttons
            all_in_course = [s for wk in weeks.values() for s in wk]
            is_open = st.session_state[key]

            # RTL: first column=right, last=left → header first (right), buttons last (left)
            hcol_title, hcol_sel, hcol_toggle = st.columns([7, 0.85, 0.75])
            with hcol_title:
                st.markdown(
                    f'<div class="section-title" style="margin:10px 0 4px 0">📚 {course_name} '
                    f'<span style="font-size:11px;color:var(--text-2);font-weight:400">'
                    f'&nbsp;{n_weeks} שבועות · {n_items} פריטים</span></div>',
                    unsafe_allow_html=True
                )
            with hcol_sel:
                if st.button("☑ הכל", key=f"selall_{course_name}"):
                    for s in all_in_course:
                        st.session_state.selected.add(s['filename'])
                        st.session_state[f"cb_{_uid(s)}"] = True
                    st.rerun()
            with hcol_toggle:
                btn_label = "סגור ▲" if is_open else "פתח ▼"
                if st.button(btn_label, key=f"toggle_{course_name}"):
                    st.session_state[key] = not is_open
                    st.rerun()

            if st.session_state[key]:
                for week_lbl, week_summaries in sorted(weeks.items()):
                    w_key = f"week_open_{course_name}_{week_lbl}"
                    if w_key not in st.session_state:
                        st.session_state[w_key] = False  # closed by default

                    is_week_open = st.session_state[w_key]

                    # Week header with a toggle — not st.expander (which always runs)
                    wcol_title, wcol_sel, wcol_desel, wcol_tog = st.columns([6.5, 0.75, 0.6, 0.7])
                    with wcol_title:
                        st.markdown(
                            f'<div style="padding:6px 0 2px 0;color:var(--text-2);font-size:13px">'
                            f'📅 {week_lbl} &nbsp;<span style="font-size:11px">· {len(week_summaries)} פריטים</span></div>',
                            unsafe_allow_html=True
                        )
                    with wcol_sel:
                        if st.button("☑ שבוע", key=f"selweek_{w_key}"):
                            for s in week_summaries:
                                st.session_state.selected.add(s['filename'])
                                st.session_state[f"cb_{_uid(s)}"] = True
                            st.rerun()
                    with wcol_desel:
                        if st.button("בטל", key=f"deselweek_{w_key}"):
                            for s in week_summaries:
                                st.session_state.selected.discard(s['filename'])
                                st.session_state[f"cb_{_uid(s)}"] = False
                            st.rerun()
                    with wcol_tog:
                        if st.button("▲" if is_week_open else "▼", key=f"tog_{w_key}"):
                            st.session_state[w_key] = not is_week_open
                            st.rerun()

                    # Only if the week is open — renders the cards
                    if is_week_open:
                        for s in week_summaries:
                            _render_summary_card(s, ratings, grouped=True)
                    st.markdown('<hr style="margin:4px 0;border-color:var(--border)">', unsafe_allow_html=True)

    core.mark("summaries:render")
