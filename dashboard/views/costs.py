"""
Costs — monthly budget vs the hard cap (new), live provider billing (moved here
from the old overview), and the original costs tab: calculated totals, real
OpenAI/Anthropic spend, per-course, weekly and per-summary breakdowns.
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from datetime import datetime

import pandas as pd
import streamlit as st

from dashboard import core
from dashboard.ui.components import (
    esc, grid, meter, page_header, pill, section, stat_card, empty_state,
)

ROOT = core.ROOT


def _last_30_days() -> dict:
    """The old tab called fetch_*_real_cost(days=30), a keyword those functions
    never accepted (TypeError → the section always showed a warning). Pass the
    intended 30-day window as the start/end dates they do accept."""
    from datetime import timedelta, timezone
    today = datetime.now(tz=timezone.utc).date()
    return {"start_date": (today - timedelta(days=30)).isoformat(), "end_date": today.isoformat()}


def _chart_color() -> str:
    return "#4f6bd8"  # the accent; a Vega chart needs a plain colour (dashboard/ui/theme.py)


# ─── NEW: monthly budget (same ledger + limits the budget_check cron uses) ───

def _budget() -> None:
    bud = core.budget_status()
    section("תקציב חודשי", "מתוך data/usage.jsonl — אותו מקור שבודק התקרה היומי משתמש בו", icon="🎯")
    if not bud['available']:
        empty_state("📊", "אין נתוני שימוש החודש", "data/usage.jsonl ריק או חסר")
        return
    lock_txt = ""
    if bud['locked']:
        lock_txt = "⛔ נעול — הסיכומים מוקפאים"
    elif bud['stale_lock']:
        lock_txt = "נעילה מחודש קודם (תתנקה אוטומטית)"
    remaining = max(0.0, bud['hard'] - bud['total'])
    day = datetime.now().day
    import calendar
    days_in_month = calendar.monthrange(datetime.now().year, datetime.now().month)[1]
    projected = bud['total'] / max(1, day) * days_in_month
    grid([
        stat_card("הוצאה החודש", "", tone=bud['tone'], icon="💰",
                  value_html=f"<span dir=\"ltr\">${bud['total']:.2f} <small>/ ${bud['hard']:.0f}</small></span>",
                  extra_html=meter(bud['frac'], bud['tone'], tick=bud['soft'] / bud['hard'] if bud['hard'] else None,
                                   label="ניצול התקציב"),
                  sub=lock_txt or f"{bud['frac'] * 100:.0f}% מהתקרה · התראה רכה ב-${bud['soft']:.0f}"),
        stat_card("נשאר עד התקרה", f"${remaining:.2f}", icon="🧮",
                  tone='danger' if remaining <= 0 else ('warning' if bud['total'] >= bud['soft'] else 'success'),
                  sub="מעל התקרה — סיכומים חדשים נעצרים" if remaining <= 0 else "לפני נעילה אוטומטית"),
        stat_card("צפי לסוף החודש", f"${projected:.2f}", icon="📈",
                  tone='danger' if projected >= bud['hard'] else ('warning' if projected >= bud['soft'] else 'success'),
                  sub=f"לפי הקצב של {day} הימים הראשונים"),
        stat_card("קריאות API", str(bud['n_calls']), icon="🔌", tone='primary', sub="מתחילת החודש"),
    ], min_width=200)

    daily = core.usage_daily_this_month()
    c1, c2 = st.columns([3, 2], gap="large")
    with c1:
        if daily:
            df = pd.DataFrame({"יום": list(daily.keys()), "עלות ($)": list(daily.values())}).set_index("יום")
            st.caption("הוצאה יומית החודש ($)")
            st.bar_chart(df, color=_chart_color(), height=220)
        else:
            st.caption("אין הוצאות מתועדות החודש.")
    with c2:
        if bud['by_service']:
            st.caption("לפי שירות")
            rows = "".join(
                f'<div class="ssa-row"><div class="ssa-row-main"><div class="ssa-row-title">{esc(k)}</div></div>'
                f'<div class="ssa-row-end">{pill(f"${v:.2f}", "primary")}</div></div>'
                for k, v in bud['by_service'].items()
            )
            st.markdown(f'<div class="ssa-list">{rows}</div>', unsafe_allow_html=True)


# ─── live provider billing (was on the overview tab) ─────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _fetch_all_costs():
    try:
        sys.path.insert(0, str(ROOT))
        from generators.cost_tracker import (
            fetch_openai_real_cost, fetch_claude_real_cost, fetch_google_real_cost,
        )
    except ImportError:
        return None, None, None
    return fetch_openai_real_cost(), fetch_claude_real_cost(), fetch_google_real_cost()


def _provider_cells() -> None:
    month_label = datetime.now().strftime("%B %Y")
    c_head, c_btn = st.columns([6, 1], vertical_alignment="bottom")
    with c_head:
        section(f"עלויות API — מתחילת {month_label}", "חיוב אמיתי מהספקים (מתעדכן כל 5 דקות)", icon="🧾")
    with c_btn:
        if st.button("↺ רענן", key="refresh_api_costs", help="רענן", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
    with st.spinner("טוען חיוב מהספקים..."):
        openai_data, claude_data, google_data = _fetch_all_costs()
    fetched_at = datetime.now().strftime("%H:%M")

    def _cell(label, data, icon):
        if data is None:
            return stat_card(label, "לא מוגדר", icon=icon, sub="אין מפתח / אין נתונים")
        total = data.get("total_usd")
        if total is None:
            return stat_card(label, "נדרשת הגדרה", icon=icon, tone='warning',
                             sub="יש להגדיר GOOGLE_CLOUD_BILLING_ACCOUNT ב-.env")
        tone = "danger" if total > 5 else ("warning" if total > 1 else "success")
        n = max(1, len(data.get("by_date", {})) or datetime.now().day)
        return stat_card(label, f"${total:.3f}", icon=icon, tone=tone,
                         sub=f"יומי ~${total / n:.4f} · עודכן {fetched_at}")

    grid([
        _cell("Gemini · Google", google_data, "✨"),
        _cell("Whisper · OpenAI", openai_data, "🎙️"),
        _cell("Claude · Anthropic (לא בשימוש)", claude_data, "🅰️"),
    ], min_width=220)


# ─── original costs tab ──────────────────────────────────────────────────────

def _original(summaries: list, totals: dict) -> None:
    section("סיכום עלויות (מחושב מקבצי הסיכום)", icon="🧮")
    prof_cost_total = sum(s['costs'].get('professor', 0) for s in summaries)
    estimated_total = totals['total_cost'] + prof_cost_total
    cc1, cc2, cc3, cc4 = st.columns(4)
    # totals['claude_cost'] is a legacy key name — it holds the summarizer's
    # (Gemini) cost parsed from each summary footer, not Anthropic spend.
    cc1.metric("סה\"כ Gemini (סיכומים)", f"${totals['claude_cost']:.3f}")
    cc2.metric("סה\"כ ויספר (OpenAI)", f"${totals['openai_cost']:.3f}")
    cc3.metric("סה\"כ טיפי פרופסור", f"${prof_cost_total:.3f}")
    cc4.metric("סה\"כ הכל (מחושב)", f"${estimated_total:.3f}")

    # ── Real OpenAI cost ──
    r_head, r_btn = st.columns([6, 1], vertical_alignment="bottom")
    with r_head:
        section("עלות ויספר אמיתית (מ-OpenAI)", icon="🎙️")
    CACHE_KEY, CACHE_TS_KEY = "_openai_real_cost_data", "_openai_real_cost_ts"
    cached_result = st.session_state.get(CACHE_KEY)
    with r_btn:
        if st.button("↺", key="btn_refresh_openai_cost", help="רענן", use_container_width=True):
            st.session_state.pop(CACHE_KEY, None)
            st.session_state.pop(CACHE_TS_KEY, None)
            cached_result = None
    if cached_result is None:
        with st.spinner("טוען נתוני עלות מ-OpenAI..."):
            try:
                from generators.cost_tracker import fetch_openai_real_cost
                result = fetch_openai_real_cost(**_last_30_days())
            except Exception as e:
                result = None
                st.warning(f"שגיאה בטעינת cost_tracker: {e}")
        if result is not None:
            st.session_state[CACHE_KEY] = result
            st.session_state[CACHE_TS_KEY] = time.time()
            cached_result = result
        else:
            st.warning("⚠️ לא ניתן לטעון נתוני עלות מ-OpenAI — בדוק את מפתח ה-API ואת החיבור לאינטרנט")
    if cached_result is not None:
        real_usd = cached_result["total_usd"]
        age_min = int((time.time() - st.session_state.get(CACHE_TS_KEY, time.time())) / 60)
        diff = real_usd - estimated_total
        diff_str = f"+${diff:.3f}" if diff >= 0 else f"-${abs(diff):.3f}"
        st.markdown(
            f"**מחושב:** ${estimated_total:.3f} &nbsp;|&nbsp; **OpenAI אמיתי:** ${real_usd:.3f} &nbsp;|&nbsp; "
            f"**הפרש:** {diff_str} &nbsp;&nbsp;<span style='color:var(--text-2);font-size:12px'>"
            f"(נכון ל-{cached_result.get('start_date', '')} – {cached_result.get('end_date', '')}; עודכן לפני {age_min} דק')</span>",
            unsafe_allow_html=True)
        by_date = cached_result.get("by_date", {})
        if by_date:
            rows = [{"תאריך": d, "עלות OpenAI ($)": v} for d, v in sorted(by_date.items(), reverse=True) if v > 0][:14]
            if rows:
                with st.expander("פירוט יומי (עלות OpenAI אמיתית)"):
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.warning("לא ניתן לטעון נתוני OpenAI — בדוק את OPENAI_API_KEY ב-.env")

    # ── Real Anthropic cost (legacy; only with a key) ──
    a_head, a_btn = st.columns([6, 1], vertical_alignment="bottom")
    with a_head:
        section("עלות Anthropic אמיתית (מ-API)", icon="🅰️")
    ANT_KEY, ANT_TS = "_anthropic_real_cost_data", "_anthropic_real_cost_ts"
    ant_cached = st.session_state.get(ANT_KEY)
    with a_btn:
        if st.button("↺", key="btn_refresh_anthropic_cost", help="רענן מ-Anthropic", use_container_width=True):
            st.session_state.pop(ANT_KEY, None)
            st.session_state.pop(ANT_TS, None)
            ant_cached = None
    # Legacy: the system is Gemini-only; don't call Anthropic billing (and
    # don't warn every page load) unless a key is actually configured.
    if ant_cached is None and not os.getenv("ANTHROPIC_API_KEY"):
        st.caption("לא בשימוש — המערכת עוברת רק דרך Gemini (אין ANTHROPIC_API_KEY).")
    elif ant_cached is None:
        with st.spinner("מושך נתוני עלות מ-Anthropic..."):
            try:
                from generators.cost_tracker import fetch_claude_real_cost
                ant_result = fetch_claude_real_cost(**_last_30_days())
            except Exception as ae:
                ant_result = None
                st.warning(f"שגיאה בטעינת cost_tracker (Anthropic): {ae}")
        if ant_result is not None:
            st.session_state[ANT_KEY] = ant_result
            st.session_state[ANT_TS] = time.time()
            ant_cached = ant_result
        else:
            st.warning("⚠️ לא ניתן לטעון נתוני עלות מ-Anthropic — בדוק את ה-ANTHROPIC_API_KEY ואת החיבור לאינטרנט")
    if ant_cached is not None:
        ant_real = ant_cached["total_usd"]
        ant_age = int((time.time() - st.session_state.get(ANT_TS, time.time())) / 60)
        ant_calc = totals['claude_cost']
        ant_diff = ant_real - ant_calc
        ant_diff_str = f"+${ant_diff:.3f}" if ant_diff >= 0 else f"-${abs(ant_diff):.3f}"
        st.markdown(
            f"**מחושב:** ${ant_calc:.3f} &nbsp;|&nbsp; **Anthropic אמיתי:** ${ant_real:.3f} &nbsp;|&nbsp; "
            f"**הפרש:** {ant_diff_str} &nbsp;&nbsp;<span style='color:var(--text-2);font-size:12px'>"
            f"(נכון ל-{ant_cached.get('start_date', '')} – {ant_cached.get('end_date', '')}; עודכן לפני {ant_age} דק')</span>",
            unsafe_allow_html=True)
        ant_by_date = ant_cached.get("by_date", {})
        if ant_by_date:
            rows = [{"תאריך": d, "עלות Anthropic ($)": v} for d, v in sorted(ant_by_date.items(), reverse=True) if v > 0][:14]
            if rows:
                with st.expander("פירוט יומי (עלות Anthropic אמיתית)"):
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    elif os.getenv("ANTHROPIC_API_KEY"):
        st.warning("לא ניתן לטעון נתוני Anthropic — בדוק את ANTHROPIC_API_KEY ב-.env")

    # ── by course / weekly ──
    color = _chart_color()
    c1, c2 = st.columns(2, gap="large")
    with c1:
        section("עלות לפי קורס", icon="📚")
        by_course_cost = defaultdict(float)
        for s in summaries:
            by_course_cost[s['course_name']] += s['costs']['total']
        if by_course_cost:
            df = pd.DataFrame(list(by_course_cost.items()), columns=['קורס', 'עלות ($)'])
            df = df.sort_values('עלות ($)', ascending=False)
            # Horizontal bars so long Hebrew course names stay readable
            # (altair ships with Streamlit — no new dependency).
            import altair as alt
            chart = alt.Chart(df).mark_bar(color=color, cornerRadiusEnd=4).encode(
                x=alt.X('עלות ($):Q', title='עלות ($)'),
                y=alt.Y('קורס:N', sort='-x', title=None, axis=alt.Axis(labelLimit=260, labelPadding=8)),
                tooltip=['קורס', alt.Tooltip('עלות ($):Q', format='$.3f')],
            ).properties(height=max(120, 34 * len(df)))
            st.altair_chart(chart, use_container_width=True)
        else:
            empty_state("📊", "אין נתונים")
    with c2:
        section("עלות שבועית", icon="🗓️")
        weekly = defaultdict(float)
        for s in summaries:
            if s['date'] and s['costs']['total'] > 0:
                try:
                    dt = datetime.strptime(s['date'], '%Y-%m-%d')
                    week_start = dt - __import__('datetime').timedelta(days=dt.weekday())
                    weekly[week_start.strftime('%d/%m')] += s['costs']['total']
                except Exception:
                    pass
        if weekly:
            wdf = pd.DataFrame(sorted(weekly.items()), columns=['שבוע (מ-)', 'עלות ($)'])
            st.bar_chart(wdf.set_index('שבוע (מ-)'), color=color, height=260)
        else:
            st.info("אין מספיק נתונים לגרף שבועי")

    section("עלות לכל סיכום", icon="🧾")
    cost_rows = []
    for s in summaries:
        if s['costs']['total'] > 0:
            prof_cost = s['costs'].get('professor', 0.0)
            cost_rows.append({
                'תאריך': s['date'],
                'קורס':  s['course_name'],
                'כותרת': s['title'],
                'סוג':   s['type_label'],
                'Gemini ($)': f"${s['costs']['claude']:.3f}",
                'OpenAI ($)': f"${s['costs']['openai']:.3f}",
                'פרופסור ($)': f"${prof_cost:.4f}" if prof_cost > 0 else "—",
                'סה\"כ ($)': f"${s['costs']['total']:.3f}",
            })
    if cost_rows:
        df_rows = pd.DataFrame(cost_rows)
        # RTL: data grids lay columns out left→right, so reverse them to put the date on the right
        st.dataframe(df_rows[df_rows.columns[::-1]], use_container_width=True, hide_index=True)
    else:
        st.info("אין נתוני עלויות עדיין")


def render(providers: bool = True) -> None:
    """providers=False: the live billing cells are shown on the overview tab."""
    summaries, totals = core.load_data()
    page_header("עלויות", "תקציב חודשי מול התקרה, חיוב אמיתי מהספקים ופירוט לפי סיכום",
                icon="💰", eyebrow="מערכת")
    _budget()
    core.mark("costs:budget")
    if providers:
        _provider_cells()
        core.mark("costs:providers")
    _original(summaries, totals)
    core.mark("costs:original")
