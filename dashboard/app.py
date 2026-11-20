"""
Smart Study Agent — Dashboard
הרצה: streamlit run dashboard/app.py

One page, one row of tabs: password gate → the look (dashboard/ui/theme.py) →
the top bar → alerts → tabs. Tabs are lazy (st.tabs on_change="rerun"): only the open tab's code runs
on a rerun. Each tab body lives in dashboard/views/<name>.py; shared helpers in
dashboard/core.py.
"""

import sys
from pathlib import Path

# טעינת .env לפני הכל — כדי שמפתחות API יהיו זמינים
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / '.env', override=True)
except ImportError:
    pass

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

st.set_page_config(
    page_title="Smart Study Agent",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="collapsed",
)

from dashboard import core  # noqa: E402
from dashboard.ui import theme  # noqa: E402
from dashboard.ui.components import BRAND_SVG, esc  # noqa: E402

core.mark_reset()

# ─── password gate (fail-closed: see core.check_password) ────────────────────
if not st.session_state.get('auth_ok'):
    theme.inject_login()
if not core.check_password():
    st.stop()

ROLE = core.user_role()
theme.inject(ROLE)


# ─── the top bar: what today is, and the way to the study site ──────────────
SITE_URL = 'https://your-server.your-tailnet.ts.net/learn/'
DAYS_HE = ('שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון')


def _topbar() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo('Asia/Jerusalem'))
    sem = core.semester_info()
    bits = [f"יום {DAYS_HE[now.weekday()]} {now:%d/%m}", sem['label']] + ([sem['detail']] if sem.get('detail') else [])
    role = '<span class="role">צפייה בלבד</span>' if ROLE == 'friend' else ''
    site = (f'<a class="go" href="{SITE_URL}" target="_blank" rel="noopener">📖<span>אתר הלימוד</span></a>'
            if ROLE == 'admin' else '')
    with st.container(key="topbar"):
        col_m, col_out = st.columns([10, 1.3], vertical_alignment="center")
        with col_m:
            st.markdown(
                f'<div class="ssa-head"><div class="mk">{BRAND_SVG}</div><div class="tx"><div class="nm">Smart Study</div>'
                f'<div class="now">{esc(" · ".join(bits))}</div></div>{role}{site}</div>',
                unsafe_allow_html=True,
            )
        with col_out:
            if st.button("יציאה", key="ssa_logout", use_container_width=True):
                core.logout()


# ─── alerts (admin only; the friend CSS hides alerts anyway) ─────────────────
def _alerts() -> None:
    errors, warnings, infos = [], [], []
    try:
        bud = core.budget_status()
        if bud['locked']:
            reason = bud['lock'].get('reason') or ''
            errors.append("⛔ **התקציב החודשי ננעל** — סיכומים ומיילים חדשים מוקפאים עד תחילת החודש הבא. "
                          f"{reason} · לביטול: שלח לבוט ״בטל מגבלת תקציב״ או מחק את "
                          "`data/budget_lock.json` ב-VM.")
    except Exception:
        pass
    try:
        warn = core.pipeline_status().get('warning')
        if warn:
            (errors if warn.startswith('🚨') else warnings).append(warn)
    except Exception:
        pass
    try:                                   # 10/11: what WhatsApp couldn't take (no Telegram fallback any more)
        from utils.notify import undelivered
        und = undelivered(7)
        if und:
            warnings.append(f"📭 **{len(und)} התראות לא נמסרו בוואטסאפ** (7 ימים) — האחרונה: "
                            f"{(und[0].get('text') or '')[:160]} · `data/undelivered_alerts.jsonl`")
    except Exception:
        pass
    for kind, msg in core.collect_alerts():
        {'error': errors, 'warning': warnings}.get(kind, infos).append(msg)

    # show errors always, collapse the rest
    for msg in errors:
        st.error(msg)
    if warnings or infos:
        with st.expander(f"⚠️ {len(warnings + infos)} התראות", expanded=bool(errors or warnings)):
            for msg in warnings:
                st.warning(msg)
            for msg in infos:
                st.info(msg)


_topbar()
if ROLE == 'admin':
    _alerts()
core.mark("shell")


# ─── tabs ────────────────────────────────────────────────────────────────────
def _professor() -> None:
    from dashboard.views import homework, tips
    st.markdown('<div class="section-title">👨‍🏫 פרופסור AI</div>', unsafe_allow_html=True)
    # Friend sees ONLY the homework checker. Admin sees both.
    if ROLE != 'admin':
        homework.render()
        return
    t_tips, t_hw = st.tabs(["💡 הוסף טיפים לסיכום", "📝 בדוק שיעורי בית"],
                           key="prof_tabs", on_change="rerun")
    if t_tips.open:
        with t_tips:
            tips.render()
    if t_hw.open:
        with t_hw:
            homework.render()


def _view(module: str, **kw):
    def _run():
        import importlib
        importlib.import_module(f"dashboard.views.{module}").render(**kw)
    return _run


# (key, label, renderer). key = core.ROLE_PERMISSIONS tab key, so the friend
# role still sees exactly: summaries + the professor tab (homework checker).
_ALL_TAB_DEFS = [
    ('today',       "🎯 היום",        _view("today")),
    ('chat',        "💬 צ'אט",        _view("chat")),
    ('overview',    "🏠 סקירה",       _view("overview")),
    ('summaries',   "📚 סיכומים",     _view("summaries")),
    ('assignments', "📅 מטלות",       _view("assignments")),
    ('notebooklm',  "🎓 NotebookLM",  _view("notebooklm")),
    ('view',        "📖 צפייה",       _view("viewer")),
    ('health',      "🔗 בריאות",      _view("vault")),
    ('system',      "🩺 מערכת",       _view("system")),
    ('recipients',  "📧 נמענים",      _view("recipients")),
    ('costs',       "💰 עלויות",      _view("costs", providers=False)),
    ('run',         "▶️ הרץ עכשיו",   _view("run")),
    ('professor',   "👨‍🏫 פרופסור",    _professor),
]
_visible = [d for d in _ALL_TAB_DEFS if core.can_see_tab(d[0])]
current_key = ""
if _visible:
    _tabs = st.tabs([label for _, label, _ in _visible], key="ssa_tabs", on_change="rerun")
    for (key, _label, render), tab in zip(_visible, _tabs):
        if not tab.open:
            continue
        current_key = key
        with tab:
            try:
                render()
            except Exception as e:  # one broken tab must not take the page down
                st.error(f"שגיאה בטעינת הלשונית: {e}")
                if ROLE == 'admin':
                    import traceback
                    st.code(traceback.format_exc())
core.mark("page")

# ─── DASH_PROFILE=1: timings for this run ────────────────────────────────────
if core.PROFILE:
    core.marks_flush(current_key)
    if ROLE == "admin":
        with st.expander("⏱ DASH_PROFILE — זמני רינדור", expanded=False):
            rep = core.marks_report()
            st.caption(f"סה\"כ {sum(d for _, d in rep):.2f} שנ' · נכתב גם ל-dash_marks.txt בתיקיית ה-temp")
            st.table({"שלב": [k for k, _ in rep], "שניות": [f"{d:.3f}" for _, d in rep]})
