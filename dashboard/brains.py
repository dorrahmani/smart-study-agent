"""
dashboard/brains.py — "5 Brains + 1 Meta-Brain" UI for the Today tab.

Each course is one brain. Together they form the meta-brain — a graph of
cross-course concepts and combined deadlines.

Public render functions (called from app.py):

    render_top_strip()                    — sync_all + diff + ask
    render_brains_grid()                  — 5 brain cards + meta card
    render_brain_view(course_key)         — focused per-course view
    render_meta_view()                    — cross-course view
    render_diff_panel()                   — "what's new since last sync"
    render_sidebar_ask()                  — global "ask the brain" widget
    course_emoji(course_key) -> str

Design language: dashboard/ui/theme.py. NO hardcoded colors,
NO box-shadows, NO gradients. Use CSS variables only.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import streamlit as st

from config.paths import COURSE_NAMES, COURSE_ORDER, VAULT_DIR, VAULT_ROOT
from dashboard import vault_reader as vr


# ─── per-course visual signatures ────────────────────────────────────────
_BRAIN_EMOJI = {
    "calculus_2":       "📐",
    "linear_algebra_2": "📊",
    "discrete_math_2":  "🔢",
    "microeconomics":   "💰",
    "intro_to_cs":      "💻",
    "probability":      "🎲",
    "software_1":       "☕",
    "microeconomics_2": "📈",
    "macroeconomics":   "🏦",
}

_BRAIN_SHORT = {
    "calculus_2":       "חדו\"א 2",
    "linear_algebra_2": "לינ\"א 2",
    "discrete_math_2":  "בדידה 2",
    "microeconomics":   "מיקרו",
    "intro_to_cs":      "מבוא מדמ\"ח",
    "probability":      "הסתברות",
    "software_1":       "תוכנה 1",
    "microeconomics_2": "מיקרו ב׳",
    "macroeconomics":   "מאקרו",
}


def course_emoji(course_key: str) -> str:
    return _BRAIN_EMOJI.get(course_key, "🧠")


def course_short(course_key: str) -> str:
    return _BRAIN_SHORT.get(course_key, COURSE_NAMES.get(course_key, course_key))


# ─── Obsidian deep links ──────────────────────────────────────────────────
def _obsidian_link(rel_path_in_vault: Path | str) -> str:
    """Build an obsidian:// URI that opens the given file inside the active
    vault (parent of VAULT_DIR — the SubFolder mode of Smart Study Agent)."""
    vault_name = Path(VAULT_ROOT).name
    rel = str(rel_path_in_vault).replace("\\", "/")
    return (
        f"obsidian://open"
        f"?vault={urllib.parse.quote(vault_name)}"
        f"&file={urllib.parse.quote(rel)}"
    )


def _obsidian_open_button(label: str, abs_path: str | Path,
                          key: str | None = None) -> None:
    """Render a link styled as a hairline button — ink-on-bone."""
    p = Path(abs_path)
    try:
        rel = p.relative_to(VAULT_ROOT)
    except ValueError:
        try:
            rel = p.relative_to(VAULT_DIR)
            rel = Path(VAULT_DIR.name) / rel
        except ValueError:
            return
    href = _obsidian_link(rel)
    st.markdown(
        f'<a class="qc-link" href="{href}" target="_blank">{label}</a>',
        unsafe_allow_html=True,
    )


# ─── top action strip ─────────────────────────────────────────────────────
def render_top_strip() -> None:
    """Three big actions: sync_all, diff, ask."""
    c1, c2, c3 = st.columns(3)

    with c1:
        if st.button("🚀 סנכרן הכל עכשיו",
                     use_container_width=True, key="brains_sync_all"):
            _run_sync_all()

    with c2:
        if st.button("📊 מה חדש מאז אתמול",
                     use_container_width=True, key="brains_diff_btn"):
            st.session_state["show_brains_diff"] = True

    with c3:
        if st.button("💬 שאל את כל המוחות",
                     use_container_width=True, key="brains_ask_meta"):
            st.session_state["active_brain"] = "__meta__"
            st.rerun()


def _run_sync_all() -> None:
    """Foreground sync_all with progress bar."""
    bar = st.progress(0.0, text="מתחיל סנכרון...")
    log = st.empty()

    def _cb(msg: str, frac: float) -> None:
        try:
            bar.progress(min(max(frac, 0.0), 1.0), text=msg)
        except Exception:
            pass

    try:
        from pipelines.sync_all import sync_all
        result = sync_all(
            budget_usd=2.0,
            include_panopto=True,
            include_whisper=False,   # never auto-Whisper from UI
            include_summaries=False, # opt-in only
            headless=True,
            progress_callback=_cb,
        )
        bar.progress(1.0, text=result.get("summary_text", "הושלם"))
        n_new = result.get("n_new", 0)
        cost = result.get("total_cost_usd", 0.0)
        st.success(
            f"✅ סנכרון הושלם — {n_new} פריטים חדשים · "
            f"${cost:.3f} · {result.get('total_duration_s', 0):.0f} שנ'"
        )
        with st.expander("פרטי שלבים"):
            for s in result.get("steps", []):
                if "error" in s:
                    log.error(f"❌ {s['name']}: {s['error']}")
                    st.caption(f"❌ **{s['name']}** — {s['error']}")
                else:
                    st.caption(
                        f"✓ **{s['name']}** — "
                        f"{s.get('items', 0)} פריטים · "
                        f"${(s.get('cost_usd') or 0):.4f} · "
                        f"{s.get('duration_s', 0)}s"
                    )
        # Invalidate vault cache so brain counts refresh
        vr.invalidate_cache()
    except Exception as e:
        st.error(f"שגיאה בסנכרון: {e}")


# ─── brain card grid ──────────────────────────────────────────────────────
def _card_grid(course_keys: list[str], per_row: int = 4) -> None:
    for i in range(0, len(course_keys), per_row):
        chunk = course_keys[i:i + per_row]
        cols = st.columns(per_row)
        for col, ck in zip(cols, chunk):
            with col:
                _render_brain_card(ck)


def render_brains_grid() -> None:
    """Brain cards — this semester's courses, then earlier semesters —
    plus the meta-brain card below."""
    from config.paths import ACTIVE_COURSES

    active = [ck for ck in COURSE_ORDER if ck in ACTIVE_COURSES]
    archive = [ck for ck in COURSE_ORDER if ck not in ACTIVE_COURSES]

    st.markdown(
        '<h2 class="qc-section-title">🧠 מוחות הסמסטר + מוח-העל</h2>',
        unsafe_allow_html=True,
    )
    _card_grid(active, per_row=max(4, len(active)))
    if archive:
        st.markdown(
            f'<div class="qc-h3">🗄️ סמסטרים קודמים · {len(archive)} קורסים — נשמרים לחזרה למבחנים</div>',
            unsafe_allow_html=True,
        )
        _card_grid(archive, per_row=5)

    st.markdown('<hr class="qc-rule" />', unsafe_allow_html=True)
    _render_meta_card()


def _render_brain_card(course_key: str) -> None:
    s = vr.course_stats(course_key)
    emoji = course_emoji(course_key)
    short = course_short(course_key)

    st.markdown(
        f'''
<div class="qc-brain-card">
  <div class="qc-brain-head">
    <span class="qc-brain-emoji">{emoji}</span>
    <span class="qc-brain-title">{short}</span>
  </div>
  <table class="qc-brain-stats">
    <tr><td>הרצאות</td><td>{s["n_lectures"] + s["n_tutorials"]}</td></tr>
    <tr><td>מטלות</td><td>{s["n_assignments"]}</td></tr>
    <tr><td>ממתינות</td><td>{s["n_pending"]}</td></tr>
    <tr><td>חולשות</td><td>{s["n_weak"]}</td></tr>
    <tr><td>מושגים</td><td>{s["n_concepts"]}</td></tr>
  </table>
</div>
        ''',
        unsafe_allow_html=True,
    )

    if st.button("⭕ הכנס", key=f"enter_brain_{course_key}",
                 use_container_width=True):
        st.session_state["active_brain"] = course_key
        st.rerun()


def _render_meta_card() -> None:
    m = vr.meta_stats()
    st.markdown(
        f'''
<div class="qc-meta-card">
  <div class="qc-meta-head">
    <span class="qc-brain-emoji">🌐</span>
    <span class="qc-brain-title">מוח-העל</span>
    <span class="qc-meta-sub">קישור בין כל הקורסים</span>
  </div>
  <div class="qc-meta-row">
    <span><b>{m["n_lectures"] + m["n_tutorials"]}</b> הרצאות</span>
    <span><b>{m["n_assignments"]}</b> מטלות ({m["n_pending"]} ממתינות)</span>
    <span><b>{m["n_announcements"]}</b> הודעות</span>
    <span><b>{m["n_concepts"]}</b> מושגים</span>
    <span><b>{m["n_weak"]}</b> חולשות</span>
  </div>
</div>
        ''',
        unsafe_allow_html=True,
    )

    if st.button("⭕ הכנס למוח-העל", key="enter_meta",
                 use_container_width=True):
        st.session_state["active_brain"] = "__meta__"
        st.rerun()


# ─── focused brain view ───────────────────────────────────────────────────
def render_brain_view(course_key: str) -> None:
    s = vr.course_stats(course_key)
    short = course_short(course_key)
    emoji = course_emoji(course_key)

    top_l, top_r = st.columns([4, 1.4], vertical_alignment="center")
    with top_l:
        st.markdown(
            f'<h2 class="qc-section-title">{emoji} {short}</h2>',
            unsafe_allow_html=True,
        )
        st.caption(s["course_name"])
    with top_r:
        if st.button("→ חזרה לכל המוחות", key="back_from_brain",
                     use_container_width=True):
            st.session_state["active_brain"] = None
            st.rerun()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("הרצאות+תרגולים", s["n_lectures"] + s["n_tutorials"])
    m2.metric("מטלות פתוחות", s["n_pending"])
    m3.metric("חולשות", s["n_weak"])
    m4.metric("מושגים", s["n_concepts"])

    st.markdown('<hr class="qc-rule" />', unsafe_allow_html=True)

    col_l, col_r = st.columns(2)

    # ── recent lectures ──
    with col_l:
        st.markdown('<h3 class="qc-h3">📚 הרצאות אחרונות</h3>',
                    unsafe_allow_html=True)
        lectures = vr.list_summaries(course=course_key)
        lectures.sort(key=lambda n: n.get("date", ""), reverse=True)
        if not lectures:
            st.caption("אין סיכומים בכספת.")
        else:
            for n in lectures[:6]:
                date = n.get("date", "")
                title = n.get("title", n.get("stem", ""))
                with st.container():
                    c1, c2 = st.columns([4, 1])
                    c1.markdown(f"**{title}**  \n<span class='qc-meta'>{date}</span>",
                                unsafe_allow_html=True)
                    with c2:
                        _obsidian_open_button("פתח", n["path"],
                                              key=f"open_lec_{n['stem']}")

    # ── pending assignments ──
    with col_r:
        st.markdown('<h3 class="qc-h3">📋 מטלות פתוחות</h3>',
                    unsafe_allow_html=True)
        pending = [a for a in vr.list_assignments(course=course_key)
                   if (a.get("status") or "").lower() in ("pending", "ממתין", "")]
        pending.sort(key=lambda a: a.get("due_date") or "")
        if not pending:
            st.caption("אין מטלות פתוחות 🎉")
        else:
            today = datetime.now().date()
            for a in pending[:8]:
                title = a.get("title", a.get("stem", ""))
                due = a.get("due_date") or ""
                tag = "🟢"
                try:
                    if due:
                        d = datetime.fromisoformat(str(due)[:10]).date()
                        delta = (d - today).days
                        if delta < 0:
                            tag = "🔴"
                        elif delta <= 3:
                            tag = "🟡"
                except Exception:
                    pass
                c1, c2 = st.columns([4, 1])
                c1.markdown(f"{tag} **{title}**  \n<span class='qc-meta'>{due}</span>",
                            unsafe_allow_html=True)
                with c2:
                    _obsidian_open_button("פתח", a["path"],
                                          key=f"open_a_{a['stem']}")

    st.markdown('<hr class="qc-rule" />', unsafe_allow_html=True)

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown('<h3 class="qc-h3">📢 הודעות אחרונות</h3>',
                    unsafe_allow_html=True)
        anns = vr.list_announcements(course=course_key)[:5]
        if not anns:
            st.caption("אין הודעות.")
        for a in anns:
            st.markdown(f"- **{a.get('title','?')}** "
                        f"— <span class='qc-meta'>{a.get('date','')} · "
                        f"{a.get('sender','')}</span>",
                        unsafe_allow_html=True)

    with col_b:
        st.markdown('<h3 class="qc-h3">🧩 מושגים מובילים</h3>',
                    unsafe_allow_html=True)
        concepts = [c for c in vr.list_concepts()
                    if c.get("course_key") == course_key]
        if not concepts:
            st.caption("עדיין לא חולצו מושגים — הרץ סנכרון.")
        for c in concepts[:8]:
            st.markdown(f"- {c.get('title', c.get('stem',''))}")

    st.markdown('<hr class="qc-rule" />', unsafe_allow_html=True)

    # ── scoped RAG ──
    st.markdown(
        f'<h3 class="qc-h3">💬 שאל את המוח של {short}</h3>',
        unsafe_allow_html=True,
    )
    q = st.text_input(
        "שאלה:",
        key=f"rag_q_{course_key}",
        placeholder="למשל: הסבר על שיטת גאוס בתרגיל 3",
    )
    if st.button("שאל", key=f"rag_btn_{course_key}") and q.strip():
        from rag.chat import chat as _chat
        from learning.weakness_tracker import log_request as _log_req
        with st.spinner("מחפש..."):
            res = _chat(q, course_filter=course_key)
        st.markdown(res["answer"])
        if res.get("sources"):
            with st.expander("📚 מקורות"):
                for s in res["sources"]:
                    st.markdown(
                        f"- **{s['title']}** ({s['course_key']}) — "
                        f"דמיון `{s['score']}` — `[[{s['stem']}]]`"
                    )
        try:
            _log_req(course_key, q[:60], weight=0.05)
        except Exception:
            pass


# ─── meta-brain view ──────────────────────────────────────────────────────
def render_meta_view() -> None:
    top_l, top_r = st.columns([4, 1.4], vertical_alignment="center")
    with top_l:
        st.markdown(
            '<h2 class="qc-section-title">🌐 מוח-העל — קישור בין הקורסים</h2>',
            unsafe_allow_html=True,
        )
    with top_r:
        if st.button("→ חזרה לכל המוחות", key="back_from_meta",
                     use_container_width=True):
            st.session_state["active_brain"] = None
            st.rerun()

    m = vr.meta_stats()
    cols = st.columns(5)
    cols[0].metric("הרצאות", m["n_lectures"] + m["n_tutorials"])
    cols[1].metric("מטלות", f"{m['n_pending']}/{m['n_assignments']}")
    cols[2].metric("הודעות", m["n_announcements"])
    cols[3].metric("מושגים", m["n_concepts"])
    cols[4].metric("חולשות", m["n_weak"])

    st.markdown('<hr class="qc-rule" />', unsafe_allow_html=True)

    col_l, col_r = st.columns(2)

    # ── all upcoming deadlines (sorted) ──
    with col_l:
        st.markdown('<h3 class="qc-h3">⏳ דדליינים קרובים (כל המוחות)</h3>',
                    unsafe_allow_html=True)
        all_pending = []
        for ck in COURSE_ORDER:
            all_pending.extend([
                a for a in vr.list_assignments(course=ck)
                if (a.get("status") or "").lower() in ("pending", "ממתין", "")
                and a.get("due_date")
            ])
        all_pending.sort(key=lambda a: a.get("due_date") or "")
        if not all_pending:
            st.caption("אין דדליינים פתוחים.")
        today = datetime.now().date()
        for a in all_pending[:12]:
            try:
                d = datetime.fromisoformat(str(a["due_date"])[:10]).date()
                delta = (d - today).days
                tag = "🔴" if delta < 0 else ("🟡" if delta <= 3 else "🟢")
            except Exception:
                tag = "·"
            ck = a.get("course_key", "")
            st.markdown(
                f"{tag} {course_emoji(ck)} **{a.get('title','?')}** "
                f"— <span class='qc-meta'>{a['due_date']}</span>",
                unsafe_allow_html=True,
            )

    # ── top combined weaknesses ──
    with col_r:
        st.markdown('<h3 class="qc-h3">💪 חולשות מובילות (כל המוחות)</h3>',
                    unsafe_allow_html=True)
        try:
            from learning.weakness_tracker import get_top_weaknesses
            weak = get_top_weaknesses(n=10)
        except Exception:
            weak = []
        if not weak:
            st.caption("עדיין אין סיגנל. שאל שאלות והשיטה תזהה חולשות.")
        for label, score in weak:
            bar = "█" * int(score * 10) + "░" * (10 - int(score * 10))
            st.markdown(f"`{bar}` **{label}** — `{score:.2f}`")

    st.markdown('<hr class="qc-rule" />', unsafe_allow_html=True)

    # ── cross-course concepts ──
    st.markdown('<h3 class="qc-h3">🧩 מושגים נפוצים בין-קורסיים</h3>',
                unsafe_allow_html=True)
    concepts = vr.list_concepts()
    # rank by # backlinks (rel_path of every note that mentions concept stem)
    backlinks: dict[str, int] = {}
    all_notes = (vr.list_summaries() + vr.list_assignments() +
                 vr.list_announcements())
    for c in concepts:
        stem = c.get("stem", "")
        if not stem:
            continue
        cnt = 0
        for n in all_notes:
            body = n.get("body", "")
            if f"[[{stem}]]" in body or f"#{stem}" in body:
                cnt += 1
        backlinks[stem] = cnt

    ranked = sorted(concepts,
                    key=lambda c: backlinks.get(c.get("stem", ""), 0),
                    reverse=True)[:18]
    if not ranked:
        st.caption("אין מושגים בכספת. הרץ סנכרון.")
    else:
        cc = st.columns(3)
        for i, c in enumerate(ranked):
            with cc[i % 3]:
                bl = backlinks.get(c.get("stem", ""), 0)
                st.markdown(
                    f"- **{c.get('title', c.get('stem',''))}** "
                    f"<span class='qc-meta'>· {bl} קישורים</span>",
                    unsafe_allow_html=True,
                )

    st.markdown('<hr class="qc-rule" />', unsafe_allow_html=True)

    # ── meta RAG ──
    st.markdown('<h3 class="qc-h3">💬 שאל את כל המוחות</h3>',
                unsafe_allow_html=True)
    q = st.text_input("שאלה:", key="rag_q_meta",
                      placeholder="למשל: ההבדל בין מטריצה הפיכה למטריצה אורתוגונלית?")
    if st.button("שאל", key="rag_btn_meta") and q.strip():
        from rag.chat import chat as _chat
        from learning.weakness_tracker import log_request as _log_req
        with st.spinner("מחפש בכל המוחות..."):
            res = _chat(q)
        st.markdown(res["answer"])
        if res.get("sources"):
            with st.expander("📚 מקורות"):
                for s in res["sources"]:
                    st.markdown(
                        f"- {course_emoji(s.get('course_key','')) } "
                        f"**{s['title']}** ({s['course_key']}) — "
                        f"`[[{s['stem']}]]`"
                    )
        try:
            _log_req("", q[:60], weight=0.05)
        except Exception:
            pass


# ─── diff panel (what's new since last sync) ──────────────────────────────
def render_diff_panel() -> None:
    """Show new items since the previous sync_log entry."""
    import json
    from config.paths import DATA_DIR
    log_path = DATA_DIR / "sync_log.json"
    if not log_path.exists():
        st.caption("עדיין לא בוצע סנכרון.")
        return
    try:
        log = json.loads(log_path.read_text(encoding="utf-8"))
    except Exception:
        log = []
    if not log:
        st.caption("עדיין אין היסטוריית סנכרון.")
        return
    last = log[-1]
    n_new = last.get("n_new", 0)
    cost = last.get("total_cost_usd", 0)
    finished = last.get("finished_at", "")
    st.markdown(
        f'<div class="qc-diff-bar">'
        f'<b>סנכרון אחרון:</b> {finished[:16]} · '
        f'<b>{n_new}</b> פריטים חדשים · '
        f'<b>${cost:.3f}</b>'
        f'</div>',
        unsafe_allow_html=True,
    )
    if n_new <= 0:
        return
    with st.expander(f"🆕 פירוט: {n_new} פריטים חדשים", expanded=False):
        for s in last.get("steps", []):
            if "error" in s:
                continue
            items = s.get("items", 0)
            if items > 0:
                st.markdown(f"- **{s['name']}** — {items} פריטים")


# ─── persistent sidebar ask ───────────────────────────────────────────────
def render_sidebar_ask() -> None:
    """Quick-ask widget pinned to the sidebar — visible from every tab."""
    with st.sidebar:
        st.markdown('<h3 class="qc-h3">💬 שאל את המוח</h3>',
                    unsafe_allow_html=True)
        scope_label = st.selectbox(
            "היקף:",
            ["כל המוחות"] + [course_short(ck) for ck in COURSE_ORDER],
            key="sidebar_rag_scope",
        )
        scope_map = {"כל המוחות": None,
                     **{course_short(ck): ck for ck in COURSE_ORDER}}
        scope = scope_map.get(scope_label)

        q = st.text_input("", key="sidebar_rag_q",
                          placeholder="שאלה קצרה...",
                          label_visibility="collapsed")
        if st.button("שאל", key="sidebar_rag_btn",
                     use_container_width=True) and q.strip():
            from rag.chat import chat as _chat
            with st.spinner("מחפש..."):
                res = _chat(q, course_filter=scope)
            st.markdown(res["answer"])
            if res.get("sources"):
                with st.expander("מקורות"):
                    for s in res["sources"]:
                        st.markdown(
                            f"- {course_emoji(s.get('course_key',''))} "
                            f"`[[{s['stem']}]]`"
                        )
