"""Assignments — deadline calendar (Moodle/Wikidot), NotebookLM podcast, generated quizzes."""
from __future__ import annotations

import datetime as _dt_mod
import os
import sys
from pathlib import Path

import streamlit as st

from config.paths import COURSE_NAMES
from dashboard import core
from dashboard.ui.components import esc, html, page_header, section, empty_state, grid, stat_card

ROOT = core.ROOT


@st.cache_data(ttl=600, show_spinner=False)
def _get_assignments_cached():
    """15/11: this called the old Playwright AssignmentTracker.fetch_all(), which logs in and REWRITES
    data/assignments/_cache.json in its older format (no quiz state, grade, feedback, opens / cut-off). The cache belongs
    to collectors/moodle_assignment_scraper (nightly) and scripts/moodle_light_check; the dashboard only reads it."""
    try:
        import json
        d = json.loads((ROOT / 'data' / 'assignments' / '_cache.json').read_text(encoding='utf-8'))
        return [a for a in d.get('assignments') or [] if isinstance(a, dict)]
    except (OSError, ValueError, AttributeError):
        return []


def _urgency_tone(days):
    if days is None:
        return ''
    if days <= 2:
        return 'danger'
    if days <= 6:
        return 'warning'
    return 'success'


def _urgency_label(days):
    if days is None:
        return 'ללא תאריך'
    if days < 0:
        return f'איחור {abs(days)} ימים 🔴'
    if days == 0:
        return 'היום! 🔴'
    if days == 1:
        return 'מחר 🟠'
    if days <= 6:
        return f'בעוד {days} ימים 🟡'
    return f'בעוד {days} ימים 🟢'


def _render_assignment_card(a: dict):
    days = a.get('_days') or a.get('days_left')
    due_obj = a.get('_due_obj')
    tone = _urgency_tone(days)
    color = f"var(--{tone})" if tone else "var(--muted)"
    due_str = due_obj.strftime('%d/%m %H:%M') if due_obj else '—'
    has_file = bool(a.get('local_path')) and Path(a['local_path']).exists()
    html(
        f'<div class="ssa-card" style="border-inline-start:4px solid {color};padding:13px 16px;margin-bottom:8px">'
        '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap">'
        f'<div style="min-width:0"><div class="ssa-row-title" style="white-space:normal">{esc(a.get("title", ""))}</div>'
        f'<div class="ssa-row-sub">{esc(a.get("course_name", ""))} · {esc(a.get("source", ""))}</div></div>'
        f'<div style="text-align:left;flex-shrink:0"><div style="font-size:13px;color:{color};font-weight:600">{esc(_urgency_label(days))}</div>'
        f'<div class="ssa-row-date" style="text-align:left">{esc(due_str)}</div></div>'
        '</div></div>'
    )
    if has_file:
        with open(a['local_path'], 'rb') as f:
            st.download_button(f"⬇️ {Path(a['local_path']).name}", data=f.read(),
                               file_name=Path(a['local_path']).name,
                               key=f"dl_{a['title']}_{a.get('course_key', '')}",
                               use_container_width=False)


def _hw_status(r: dict, today) -> tuple[str, str]:
    """(label, tone) for a homework row."""
    if r['submitted']:
        return '✅ הוגש', 'success'
    if r['due'] is None:
        return 'ללא תאריך הגשה', ''
    days = (r['due'] - today).total_seconds() / 86400
    if days < 0:
        return 'עבר המועד 🔴', 'danger'
    if days < 1:
        return 'היום! 🔴', 'danger'
    if days < 2:
        return 'מחר 🟠', 'warning'
    if days <= 6:
        return f'בעוד {int(days)} ימים 🟡', 'warning'
    return f'בעוד {int(days)} ימים', 'success'


def _hw_check(r: dict, k: str) -> None:
    """Send an answer for this homework to the professor (generators/homework_checker) — the exercise file is known."""
    up = st.file_uploader("✏️ הפתרון שלך (PDF / צילום סרוק / .py / .java)", type=["pdf", "py", "java"],
                          accept_multiple_files=False, key=f"hwsol_{k}")
    ex_up = None
    if not r['file']:
        ex_up = st.file_uploader("📄 קובץ התרגיל (לא נמצא אצלי — העלה אותו)", type=["pdf"], key=f"hwex_{k}")
    if not st.button("🔍 שלח לבדיקה אצל הפרופסור", key=f"hwgo_{k}", type="primary", disabled=up is None):
        return
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        if r['file']:
            ex = Path(r['file'])
            if ex.suffix.lower() != '.pdf':
                st.warning("קובץ התרגיל אינו PDF — העלה גרסת PDF שלו.")
                return
        elif ex_up is not None:
            ex = tmp / 'exercise.pdf'
            ex.write_bytes(ex_up.read())
        else:
            st.warning("צריך את קובץ התרגיל כדי לבדוק.")
            return
        sol = tmp / f"solution{Path(up.name).suffix.lower()}"
        sol.write_bytes(up.read())
        is_code = sol.suffix in ('.py', '.java')
        with st.spinner("הפרופסור בודק... (עד דקה)"):
            from generators.homework_checker import HomeworkChecker
            from dashboard.views.homework import _render_result
            checker = HomeworkChecker()
            result = checker.check_homework(
                exercise_pdf_path=str(ex), solution_pdf_path=None if is_code else str(sol), course_key=r['course_key'],
                solution_code_path=str(sol) if is_code else None, code_filename=up.name if is_code else '',
                theory_filename='' if is_code else up.name)
            _render_result(result, checker, r['course_name'], r['course_key'], r['title'])
    except Exception as e:
        st.error(f"❌ הבדיקה נכשלה: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _hw_card(r: dict, today, i: int) -> None:
    label, tone = _hw_status(r, today)
    color = f"var(--{tone})" if tone else "var(--muted)"
    due = r['due'].strftime('%a %d/%m %H:%M').replace('Sun', 'א׳').replace('Mon', 'ב׳').replace('Tue', 'ג׳') \
        .replace('Wed', 'ד׳').replace('Thu', 'ה׳').replace('Fri', 'ו׳').replace('Sat', 'ש׳') if r['due'] else '—'
    kind = {'quiz': '📝 בוחן', 'assign': '📄 מטלה', 'resource': '📄 תרגיל'}.get(r['kind'], '📄')
    html(
        f'<div class="ssa-card" style="border-inline-start:4px solid {color};padding:13px 16px;margin-bottom:6px">'
        '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap">'
        f'<div style="min-width:0"><div class="ssa-row-title" style="white-space:normal">{esc(r["title"])}</div>'
        f'<div class="ssa-row-sub">{esc(r["course_name"])} · {kind} · {esc(r["source"])}'
        f'{" · 📗 יש פתרון" if r["solution"] else ""}</div></div>'
        f'<div style="text-align:left;flex-shrink:0"><div style="font-size:13px;color:{color};font-weight:600">{esc(label)}</div>'
        f'<div class="ssa-row-date" style="text-align:left">{esc(due)}</div></div></div></div>')
    k = f"{r['course_key']}_{r['number'] or i}_{i}"
    cols = st.columns(4)
    if r['file']:
        with open(r['file'], 'rb') as f:
            cols[0].download_button("⬇️ הורד את המטלה", data=f.read(), file_name=Path(r['file']).name, key=f"hwdl_{k}",
                                    use_container_width=True)
    elif r['file_url']:
        cols[0].link_button("⬇️ פתח את המטלה", r['file_url'], use_container_width=True)
    sol = r['solution']
    if sol and Path(sol).exists():
        with open(sol, 'rb') as f:
            cols[1].download_button("📗 פתרון", data=f.read(), file_name=Path(sol).name, key=f"hwsl_{k}",
                                    use_container_width=True)
    elif sol.startswith('http'):
        cols[1].link_button("📗 פתרון", sol, use_container_width=True)
    if r['moodle_url']:
        cols[2].link_button("🔗 להגשה במודל", r['moodle_url'], use_container_width=True)
    if r['kind'] != 'quiz':
        with st.expander("📤 שלח תשובה לבדיקה אצל הפרופסור", expanded=False):
            _hw_check(r, k)


def _calendar() -> None:
    """19/10 rewrite (The owner's screenshot): one list from the bot's own sources — this semester only, solutions attached to
    their exercise, due dates and "submitted", download / solution / Moodle / professor check on every item, filters."""
    from dashboard import hw_data
    section("לוח מטלות", "מהמודל ומהאתרים של המרצים — כמו שהבוט רואה (מתעדכן פעמיים ביום ובלילה)", icon="📅")
    rows = hw_data.unified()
    if not rows:
        empty_state("📭", "אין מטלות", "עדיין לא נמצאו מטלות בקורסים של הסמסטר")
        return
    today = _dt_mod.datetime.now()
    courses = sorted({r['course_name'] for r in rows})
    f1, f2, f3, f4 = st.columns([3, 2, 2, 2])
    pick = f1.multiselect("קורס", courses, default=courses, key="hw_f_course")
    status = f2.selectbox("מצב", ["פתוחות", "הוגשו", "הכול"], key="hw_f_status")
    window = f3.selectbox("מועד הגשה", ["השבוע", "שבועיים", "החודש", "הכול", "ללא תאריך"], index=3, key="hw_f_window")
    order = f4.selectbox("מיון", ["לפי מועד הגשה", "לפי קורס", "לפי מספר תרגיל"], key="hw_f_order")
    horizon = {"השבוע": 7, "שבועיים": 14, "החודש": 31}.get(window)
    shown = []
    for r in rows:
        if r['course_name'] not in pick:
            continue
        if status == "פתוחות" and r['submitted'] or status == "הוגשו" and not r['submitted']:
            continue
        if window == "ללא תאריך" and r['due'] is not None:
            continue
        if horizon and (r['due'] is None or (r['due'] - today).days > horizon):
            continue
        shown.append(r)
    if order == "לפי קורס":
        shown.sort(key=lambda r: (r['course_name'], r['due'] or _dt_mod.datetime.max))
    elif order == "לפי מספר תרגיל":
        shown.sort(key=lambda r: (r['course_name'], int(r['number'] or 99)))
    open_ = [r for r in rows if not r['submitted'] and r['due'] and r['due'] >= today]
    urgent = [r for r in open_ if (r['due'] - today).days <= 3]
    grid([stat_card("פתוחות עם מועד", str(len(open_)), icon="📋", tone='primary'),
          stat_card("דחופות (≤3 ימים)", str(len(urgent)), icon="⏰", tone='warning' if urgent else 'success'),
          stat_card("הוגשו", str(sum(r['submitted'] for r in rows)), icon="✅", tone='success')], min_width=170)
    if not shown:
        st.info("אין מטלות שמתאימות לסינון.")
        return
    dated = [r for r in shown if r['due'] is not None]
    undated = [r for r in shown if r['due'] is None]
    for i, r in enumerate(dated):
        _hw_card(r, today, i)
    if undated:
        with st.expander(f"📂 תרגילים בלי מועד הגשה ({len(undated)}) — פורסמו מראש / לתרגול", expanded=window == "ללא תאריך"):
            for i, r in enumerate(undated, len(dated)):
                _hw_card(r, today, i)


def _podcast() -> None:
    section("פודקאסט NotebookLM", "שני קולות מסכמים את ההרצאה — מושלם להאזנה בדרך", icon="🎙️")
    with st.container(border=True, key="card_podcast"):
        c1, c2 = st.columns([2, 2], vertical_alignment="bottom")
        with c1:
            pod_course = st.selectbox("קורס:", list(COURSE_NAMES.keys()),
                                      format_func=lambda k: COURSE_NAMES[k], key="pod_course")
        with c2:
            pod_scope = st.radio("היקף:", ["latest", "all"],
                                 format_func=lambda s: "הרצאה אחרונה" if s == "latest" else "כל הקורס",
                                 key="pod_scope", horizontal=True)

        if st.button("🎙️ צור פודקאסט", key="gen_podcast", type="primary"):
            with st.spinner("יוצר פודקאסט... (2-5 דקות)"):
                try:
                    sys.path.insert(0, str(ROOT))
                    from generators.notebooklm_agent import NotebookLMAgent
                    orig = os.getcwd()
                    os.chdir(str(ROOT))
                    try:
                        agent = NotebookLMAgent()
                        mp3 = agent.generate_podcast(
                            course_key=pod_course, scope=pod_scope, language='he',
                            instructions='סיכום קצר ובהיר של עיקרי החומר — 10-15 דקות',
                        )
                    finally:
                        os.chdir(orig)

                    mp3_path = Path(mp3)
                    if mp3_path.exists():
                        st.success(f"✅ פודקאסט נוצר: {mp3_path.name}")
                        with open(mp3_path, 'rb') as f:
                            st.audio(f.read(), format='audio/mp3')
                        with open(mp3_path, 'rb') as f:
                            st.download_button("⬇️ הורד MP3", data=f.read(),
                                               file_name=mp3_path.name, mime='audio/mpeg')
                        if st.button("📧 שלח פודקאסט במייל"):
                            reviewer = core.get_reviewer_email()
                            from generators.gmail_sender import GmailSender
                            os.chdir(str(ROOT))
                            try:
                                GmailSender().send_email(
                                    to=reviewer,
                                    subject=f"🎙️ פודקאסט {COURSE_NAMES[pod_course]}",
                                    body_html=f"<p>מצורף פודקאסט NotebookLM עבור {COURSE_NAMES[pod_course]}</p>",
                                    attachment_path=str(mp3_path),
                                )
                                st.success(f"נשלח ל-{reviewer}")
                            finally:
                                os.chdir(orig)
                    else:
                        st.error("הפודקאסט לא נוצר — בדוק NotebookLM auth")
                except Exception as e:
                    st.error(f"שגיאה: {e}")


def _quizzes() -> None:
    section("חידונים שנוצרו", "נוצרים אוטומטית אחרי כל סיכום", icon="✏️")
    quiz_dir = ROOT / 'data' / 'quizzes'
    quiz_files = sorted(quiz_dir.rglob('*_quiz_questions.pdf'), reverse=True) if quiz_dir.exists() else []
    if not quiz_files:
        empty_state("✏️", "עדיין לא נוצרו חידונים", "יווצרו אוטומטית אחרי כל סיכום")
        return
    for qf in quiz_files[:10]:
        with st.container(border=True, key=f"card_quiz_{qf.name}"):
            qc1, qc2, qc3 = st.columns([4, 1.2, 1.2], vertical_alignment="center")
            with qc1:
                html(f"<div class='ssa-row-title' style='white-space:normal'>{esc(qf.stem.replace('_quiz_questions', ''))}</div>")
            with qc2:
                with open(qf, 'rb') as f:
                    st.download_button("⬇️ שאלות", data=f.read(), file_name=qf.name,
                                       key=f"q_{qf.name}", use_container_width=True)
            ans = qf.parent / qf.name.replace('_questions', '_answers')
            with qc3:
                if ans.exists():
                    with open(ans, 'rb') as f:
                        st.download_button("📋 תשובות", data=f.read(), file_name=ans.name,
                                           key=f"a_{ans.name}", use_container_width=True)


def obligation_rows(now=None) -> list[dict]:
    """14/11 what the bot nags about (agent/obligations), as table rows: still open first, then finished quizzes with
    their wrong-answer count. Read only — the dashboard never writes the store."""
    from agent import obligations as ob
    now = now or _dt_mod.datetime.now()
    store = ob.load()
    try:
        ob.sync(ob.collect(), store, now)             # fresh from the files, in memory only
    except Exception:
        pass
    rows = []
    cw = {ck: ob.coursework(ck) for ck in {r['course'] for r in store['items'].values()}}
    for r in ob.open_items(store, now):
        due = ob._dt(r.get('due'))
        sol = r['kind'] != 'quiz' and bool((cw.get(r['course'], {}).get(ob.hw_num(r['title'])) or {}).get('solution'))
        rows.append({'course': COURSE_NAMES.get(r['course'], r['course']), 'title': r['title'],
                     'kind': 'בוחן' if r['kind'] == 'quiz' else 'מטלה', 'due': f"{due:%d/%m %H:%M}" if due else '—',
                     'state': {'late': '⌛ באיחור', 'open': '⏳ פתוח'}.get(ob.status(r, now), '')
                              + (' · באמצע' if r.get('state') == 'in_progress' else ''),
                     'priority': ob.is_priority(r), 'wrong': '📗 יש פתרון' if sol else '', 'url': r.get('url', '')})
    for ck in ob.PRIORITY:
        wc = ob.wrong_counts(ck)
        for r in sorted((r for r in store['items'].values() if r['course'] == ck and r['kind'] == 'quiz'
                         and ob.status(r, now) == 'closed'), key=lambda r: ob._quiz_order(r['title'])):
            w = wc.get(r['id'])
            rows.append({'course': COURSE_NAMES.get(ck, ck), 'title': r['title'], 'kind': 'בוחן', 'due': '—',
                         'state': '✅ הושלם', 'priority': True,
                         'wrong': (f'{w[0]} / {w[1]}' if w else 'אין סקירה עדיין'), 'url': r.get('url', '')})
    return rows


def _obligations() -> None:
    section("מה עוד פתוח לי", "מה שהבוט מזכיר לך — בחנים שלא עשית ומטלות שלא הגשת. ⭐ = בחני תוכנה 1 (שאלה מהם במבחן)",
            icon="📋")
    try:
        rows = obligation_rows()
    except Exception as e:
        empty_state("⚠️", "לא הצלחתי לקרוא", str(e)[:120])
        return
    if not rows:
        empty_state("✅", "הכול סגור", "אין בחן או מטלה פתוחים במודל")
        return
    head = ''.join(f'<th style="text-align:right;padding:4px 8px">{h}</th>'
                   for h in ('קורס', 'פריט', 'סוג', 'נסגר', 'מצב', 'טעויות / פתרון'))
    body = ''
    for r in rows:
        title = esc(('⭐ ' if r['priority'] else '') + r['title'])
        if r['url']:
            title = f'<a href="{esc(r["url"])}" target="_blank" rel="noopener">{title}</a>'
        body += ('<tr>' + ''.join(f'<td style="padding:4px 8px">{c}</td>' for c in
                 (esc(r['course']), title, esc(r['kind']), esc(r['due']), esc(r['state']), esc(r['wrong']))) + '</tr>')
    html(f'<div dir="rtl" style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">'
         f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def render() -> None:
    page_header("מטלות", "דדליינים לפי שבוע, פודקאסטים וחידונים", icon="📅", eyebrow="לימוד")
    _obligations()
    _calendar()
    _podcast()
    _quizzes()
    core.mark("assignments:render")
