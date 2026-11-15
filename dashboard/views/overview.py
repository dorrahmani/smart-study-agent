"""Overview ("🏠 סקירה") — the pre-redesign tracking page: approvals waiting
for the owner, the hero stat line, recent activity feed, per-course table and the
providers' real billing."""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from config.paths import ACTIVE_COURSES, COURSE_NAMES
from dashboard import core
from dashboard.ui.components import esc
from dashboard.parser import load_last_run

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


def render() -> None:
    summaries, totals = load_data()

    # ── APPROVAL BANNER ────────────────────────────────────────────────────────
    _pending = _pending_approvals()
    if _pending:
        st.markdown(
            '<div style="background:var(--raised);border:1px solid var(--amber);border-radius:var(--radius);'
            'padding:16px 20px;margin-bottom:20px;direction:rtl">'
            '<div style="font-family:var(--mono);font-size:11px;letter-spacing:.6px;'
            'text-transform:uppercase;color:var(--amber);margin-bottom:10px">⏳ ממתין לאישורך</div>',
            unsafe_allow_html=True
        )
        for _entry in _pending:
            _ecols = st.columns([5, 1, 1])
            with _ecols[0]:
                st.markdown(
                    f'<div style="color:var(--text);font-size:14px;font-weight:600">{esc(_entry.get("title",""))}</div>'
                    f'<div style="color:var(--muted);font-size:12px">{esc(_entry.get("course_name",""))} · '
                    f'נשלח אליך: {(_entry.get("sent_to_reviewer","")[:10])}</div>',
                    unsafe_allow_html=True
                )
            with _ecols[1]:
                if st.button("✅ אשר ושלח לכולם", key=f"approve_{_entry['key']}", type="primary",
                             use_container_width=True):
                    with st.spinner("שולח לכולם..."):
                        _s, _f = _approve_and_send_all(_entry)
                    if _s:
                        all_emails = _get_all_emails_for_course(_entry.get('course_key', ''))
                        st.success(f"✅ נשלח ל-{_s} נמענים: {', '.join(all_emails)}")
                    if _f:
                        st.error(f"❌ {_f} נכשלו")
                    st.rerun()
            with _ecols[2]:
                if st.button("🗑 דלג", key=f"skip_{_entry['key']}", use_container_width=True,
                             help="סמן כ'אושר' ללא שליחה"):
                    core.skip_approval(_entry)
                    st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    last_run = load_last_run()
    from collections import defaultdict
    import datetime as _dt

    # ── compute week boundaries ────────────────────────────────────────────────
    _today      = datetime.now()
    _week_start = (_today - _dt.timedelta(days=_today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    _week_sums  = [s for s in summaries
                   if s.get('date') and datetime.strptime(s['date'], '%Y-%m-%d') >= _week_start]

    # ── per-course stats ───────────────────────────────────────────────────────
    by_course = defaultdict(lambda: {'lectures': 0, 'tutorials': 0, 'last_date': ''})
    for s in summaries:
        ck = s['course']
        by_course[ck]['lectures']  += 1 if s['type_folder'] == 'lectures'  else 0
        by_course[ck]['tutorials'] += 1 if s['type_folder'] == 'tutorials' else 0
        d = s.get('date', '')
        if d and d > by_course[ck]['last_date']:
            by_course[ck]['last_date'] = d

    # ── HERO STAT LINE ─────────────────────────────────────────────────────────
    _hero_stats = [
        ("סה\u05f4כ סיכומים", str(totals['count']),        "var(--text)"),
        ("השבוע",             str(len(_week_sums)),         "var(--amber)" if _week_sums else "var(--muted)"),
        ("הרצאות",            str(totals['lectures']),      "var(--text)"),
        ("תרגולים",           str(totals['tutorials']),     "var(--text)"),
        ("נשלחו",             str(totals['sent']),          "var(--green)" if totals['sent'] else "var(--muted)"),
        ("עדכון אחרון",       last_run,                    "var(--text-2)"),
    ]
    _hero_cells = ""
    for _lbl, _val, _clr in _hero_stats:
        # the date/time cell: smaller + isolated LTR so "27/04/2026 09:27" doesn't wrap mid-number
        _size = '2rem' if _val.isdigit() else '1.15rem'
        _hero_cells += (
            '<div style="flex:1;min-width:130px;background:var(--raised);padding:20px 24px;border-left:1px solid var(--border);">'
            '<div style="font-family:var(--mono);font-size:10px;letter-spacing:.8px;'
            'text-transform:uppercase;color:var(--muted);margin-bottom:8px;">' + _lbl + '</div>'
            '<div dir="auto" style="font-family:var(--font);font-size:' + _size + ';font-weight:700;'
            'unicode-bidi:isolate;color:' + _clr + ';letter-spacing:-0.04em;line-height:1.1;">' + _val + '</div>'
            '</div>'
        )
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;align-items:stretch;border-radius:var(--radius);overflow:hidden;'
        'margin-bottom:28px;border:1px solid var(--border);">'
        + _hero_cells +
        '</div>',
        unsafe_allow_html=True,
    )

    # ── TWO-COLUMN LAYOUT: ACTIVITY + COURSES ─────────────────────────────────
    # RTL: CSS direction:rtl on stHorizontalBlock reverses column order automatically.
    # First column in code = RIGHT side on screen. Feed (primary) first → RIGHT. Courses second → LEFT.
    _col_feed, _col_courses = st.columns([3, 2], gap="large")

    with _col_feed:
        st.markdown('<div class="section-title">פעילות אחרונה</div>', unsafe_allow_html=True)

        recent = sorted(summaries, key=lambda x: x.get('date', ''), reverse=True)[:15]
        if not recent:
            st.markdown('<p style="color:var(--muted);font-size:var(--text-sm);">אין סיכומים עדיין</p>',
                        unsafe_allow_html=True)
        else:
            rows_html = ""
            _wk_start_str = _week_start.strftime('%Y-%m-%d')
            for s in recent:
                _date   = s.get('date', '')
                _dstr   = _date[8:10] + '/' + _date[5:7] if len(_date) >= 10 else '—'
                _cname  = COURSE_NAMES.get(s['course'], s['course'])
                _title  = s.get('title', '—')
                _is_lec = s.get('type_folder') == 'lectures'
                _sent   = s.get('sent', False)
                _is_this_week = _date >= _wk_start_str
                _dot_clr = 'var(--amber)' if _is_lec else 'var(--green)'
                _row_bg  = 'var(--overlay)' if _is_this_week else 'transparent'
                _sent_badge = (
                    '<span style="font-family:var(--mono);font-size:9px;'
                    'letter-spacing:.4px;padding:2px 6px;border-radius:var(--radius);'
                    'background:transparent;color:var(--green);'
                    'border:1px solid var(--green);">נשלח</span>'
                    if _sent else ''
                )
                rows_html += (
                    '<div class="feed-row" style="'
                    'display:flex;align-items:center;gap:12px;'
                    'padding:10px 14px;border-bottom:1px solid var(--border);'
                    f'background:{_row_bg};'
                    '">'
                    f'<span style="width:6px;height:6px;border-radius:50%;background:{_dot_clr};display:inline-block;flex-shrink:0;"></span>'
                    f'<span style="font-family:var(--mono);font-size:11px;color:var(--muted);flex-shrink:0;width:36px;">{_dstr}</span>'
                    f'<span style="font-size:11px;color:var(--amber);flex-shrink:0;min-width:70px;font-weight:600;">{esc(_cname[:10])}</span>'
                    f'<span style="font-size:13px;color:var(--text-2);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{esc(_title)}</span>'
                    f'{_sent_badge}'
                    '</div>'
                )

            _feed_legend = (
                '<div style="display:flex;gap:16px;padding:10px 14px;border-bottom:1px solid var(--border);background:var(--surface);">'
                '<span style="display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;color:var(--muted);">'
                '<span style="width:6px;height:6px;border-radius:50%;background:var(--amber);display:inline-block;"></span> הרצאה'
                '</span>'
                '<span style="display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;color:var(--muted);">'
                '<span style="width:6px;height:6px;border-radius:50%;background:var(--green);display:inline-block;"></span> תרגול'
                '</span>'
                '<span style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-right:auto;">השבוע מודגש</span>'
                '</div>'
            )
            st.markdown(
                '<div style="background:var(--raised);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;">'
                + _feed_legend
                + rows_html
                + '</div>',
                unsafe_allow_html=True,
            )

    with _col_courses:
        st.markdown('<div class="section-title">קורסים</div>', unsafe_allow_html=True)

        courses_rows = ""
        # this semester's courses first, then the archive
        for ck in [*ACTIVE_COURSES, *(k for k in COURSE_NAMES if k not in ACTIVE_COURSES)]:
            cname = COURSE_NAMES.get(ck, ck)
            d    = by_course[ck]
            tot  = d['lectures'] + d['tutorials']
            last = d['last_date']
            last_str = (last[8:10] + '/' + last[5:7] + '/' + last[2:4]) if len(last) >= 10 else '—'
            if tot == 0:
                continue
            # Single-line styles — multi-line style="" breaks Streamlit's markdown parser
            courses_rows += (
                '<div style="display:grid;grid-template-columns:1fr auto auto auto;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border);">'
                f'<div style="font-size:13px;font-weight:600;color:var(--text);">{cname}</div>'
                '<div style="text-align:center;">'
                '<div style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-bottom:2px;">הרצ׳</div>'
                f'<div style="font-size:16px;font-weight:700;color:var(--text);">{d["lectures"]}</div>'
                '</div>'
                '<div style="text-align:center;">'
                '<div style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-bottom:2px;">תרג׳</div>'
                f'<div style="font-size:16px;font-weight:700;color:var(--text);">{d["tutorials"]}</div>'
                '</div>'
                '<div style="text-align:right;">'
                '<div style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-bottom:2px;">עדכון</div>'
                f'<div style="font-family:var(--mono);font-size:11px;color:var(--text-2);">{last_str}</div>'
                '</div>'
                '</div>'
            )

        if not courses_rows:
            courses_rows = '<div style="padding:20px;color:var(--muted);font-size:12px;">אין נתונים</div>'

        # Header row + data rows — all single-line styles
        _courses_header = (
            '<div style="display:grid;grid-template-columns:1fr auto auto auto;gap:12px;padding:10px 16px;background:var(--surface);border-bottom:1px solid var(--border);">'
            '<div style="font-family:var(--mono);font-size:10px;letter-spacing:.6px;color:var(--muted);">קורס</div>'
            '<div style="font-family:var(--mono);font-size:10px;letter-spacing:.6px;color:var(--muted);text-align:center;">הרצאות</div>'
            '<div style="font-family:var(--mono);font-size:10px;letter-spacing:.6px;color:var(--muted);text-align:center;">תרגולים</div>'
            '<div style="font-family:var(--mono);font-size:10px;letter-spacing:.6px;color:var(--muted);">עדכון</div>'
            '</div>'
        )
        st.markdown(
            '<div style="background:var(--raised);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;">'
            + _courses_header
            + courses_rows
            + '</div>',
            unsafe_allow_html=True,
        )

    # ── API COSTS ──────────────────────────────────────────────────────────────
    _month_label = datetime.now().strftime("%B %Y")
    _hdr, _btn_col = st.columns([8, 1])
    _hdr.markdown(
        f'<div class="section-title">עלויות API — מתחילת {_month_label}</div>',
        unsafe_allow_html=True,
    )
    if _btn_col.button("↺", key="refresh_api_costs", help="רענן"):
        st.cache_data.clear()
        st.rerun()

    from dashboard.views.costs import _fetch_all_costs  # shared 5-min cache
    with st.spinner("טוען חיוב מהספקים..."):
        try:
            _openai_data, _claude_data, _google_data = _fetch_all_costs()
        except Exception:
            _openai_data = _claude_data = _google_data = None
    _fetched_at = datetime.now().strftime("%H:%M")

    def _cost_cell(label, data, accent):
        # Single-line HTML only — multi-line f-strings break Streamlit markdown parser
        if data is None:
            return (
                '<div style="flex:1;background:var(--raised);border:1px solid var(--border);border-radius:var(--radius);padding:16px 20px;opacity:.5;">'
                f'<div style="font-family:var(--mono);font-size:10px;letter-spacing:.6px;color:var(--muted);margin-bottom:8px;">{label}</div>'
                '<div style="font-size:16px;color:var(--muted);">לא מוגדר</div>'
                '</div>'
            )
        total = data.get("total_usd")
        if total is None:
            return (
                '<div style="flex:1;background:var(--raised);border:1px solid var(--border);border-radius:var(--radius);padding:16px 20px;">'
                f'<div style="font-family:var(--mono);font-size:10px;letter-spacing:.6px;color:var(--muted);margin-bottom:8px;">{label}</div>'
                '<div style="font-size:13px;color:var(--amber);">נדרשת הגדרה</div>'
                '<div style="font-family:var(--mono);font-size:9px;color:var(--muted);margin-top:6px;">יש להגדיר GOOGLE_CLOUD_BILLING_ACCOUNT ב-.env</div>'
                '</div>'
            )
        clr = "var(--red)" if total > 5 else ("var(--amber)" if total > 1 else "var(--green)")
        n   = max(1, len(data.get("by_date", {})) or datetime.now().day)
        avg = total / n
        return (
            f'<div style="flex:1;background:var(--raised);border:1px solid var(--border);border-top:2px solid {accent};border-radius:var(--radius);padding:16px 20px;">'
            f'<div style="font-family:var(--mono);font-size:10px;letter-spacing:.6px;color:var(--muted);margin-bottom:10px;">{label}</div>'
            f'<div style="font-family:var(--font);font-size:1.9rem;font-weight:700;color:{clr};letter-spacing:-0.04em;line-height:1;">${total:.3f}</div>'
            f'<div style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:8px;">יומי ~${avg:.4f} &nbsp;·&nbsp; עודכן {_fetched_at}</div>'
            '</div>'
        )

    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:4px;">'
        + _cost_cell("Gemini · Google",     _google_data, "var(--amber)")
        + _cost_cell("Whisper · OpenAI",    _openai_data, "var(--green)")
        + _cost_cell("Claude · Anthropic (לא בשימוש)", _claude_data, "var(--blue)")
        + '</div>',
        unsafe_allow_html=True,
    )
    core.mark("overview:render")
