"""
Homework checker — upload exercise + solution, get per-question feedback;
previous reports + correction uploads; targeted re-check of failed questions;
auto-generated practice exercise from weak spots. Available to admin and friend
(friend is limited to allowed courses). Logic unchanged from the professor tab.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import streamlit as st

from dashboard import core
from dashboard.ui.components import page_header, section, score_card, callout, esc
from dashboard.views.professor_common import COURSE_KEYS, score_is_good

_MODEL_OPTIONS = {
    "🤖 ברירת מחדל (מומלץ לפי קורס)":                         "auto",
    "⚡ Gemini 3.1 Flash-Lite  — הכי זול, מדמ\"ח":            ("gemini", "gemini-3.1-flash-lite-preview"),
    "🔥 Gemini 3.8 Flash  — זול, מתמטיקה פשוטה":               ("gemini", "gemini-3.8-flash"),
    "🧠 Gemini 3.1 Pro  — הכי חזק, מתמטיקה מורכבת":          ("gemini", "gemini-3.1-pro-preview"),
    "🟢 GPT-4o  — מאוזן, עברית טובה, יקר יחסית":            ("openai", "gpt-4o"),
    "🟢 GPT-4o-mini  — זול, קוד ותיאוריה פשוטה":             ("openai", "gpt-4o-mini"),
    # (Claude options removed — Gemini-only rule for LLM calls)
}

_PROF_RECOMMEND = {
    'intro_to_cs':      ("⚡ Gemini 3.1 Flash-Lite  — הכי זול, מדמ\"ח",
                         "קוד Python בלבד — Flash-Lite מספיק ועולה פחות מ-$0.01 לתרגיל. "
                         "GPT-4o-mini אלטרנטיבה טובה אם Gemini מתקשה."),
    'calculus_2':       ("🧠 Gemini 3.1 Pro  — הכי חזק, מתמטיקה מורכבת",
                         "אינטגרלים ונגזרות — Pro מבין LaTeX והוכחות. "
                         "GPT-4o אלטרנטיבה אם יש בעיות עם כתב יד."),
    'linear_algebra_2': ("🧠 Gemini 3.1 Pro  — הכי חזק, מתמטיקה מורכבת",
                         "מטריצות, מרחבים וקטוריים — Pro מומלץ להוכחות מבניות. "
                         "GPT-4o אלטרנטיבה אם הפתרון בכתב יד עברי צפוף."),
    'discrete_math_2':  ("🧠 Gemini 3.1 Pro  — הכי חזק, מתמטיקה מורכבת",
                         "הוכחות ולוגיקה פורמלית — Pro עם הבנת מתמטיקה פורמלית. "
                         "GPT-4o אלטרנטיבה דומה באיכות."),
    'microeconomics':   ("🔥 Gemini 3.8 Flash  — זול, מתמטיקה פשוטה",
                         "גרפים ומשוואות — Flash מספיק. GPT-4o-mini אלטרנטיבה טובה וזולה."),
    # ── סמסטר א' תשפ"ז ──
    'probability':      ("🧠 Gemini 3.1 Pro  — הכי חזק, מתמטיקה מורכבת",
                         "הסתברות וסטטיסטיקה — חישובים, התפלגויות והוכחות: Pro."),
    'software_1':       ("🔥 Gemini 3.8 Flash  — זול, מתמטיקה פשוטה",
                         "קוד Java ותיאוריה — Flash מספיק. הקוד מקומפל ומורץ באמת (javac + מבחני קצה) "
                         "בסביבה מבודדת."),
    'microeconomics_2': ("🔥 Gemini 3.8 Flash  — זול, מתמטיקה פשוטה",
                         "גרפים, אופטימיזציה ומשוואות — Flash מספיק."),
    'macroeconomics':   ("🔥 Gemini 3.8 Flash  — זול, מתמטיקה פשוטה",
                         "מודלים ומשוואות מאקרו — Flash מספיק."),
}


def _user_dir() -> str:
    return core.user_role() or 'admin'


# Courses whose code is compiled/run by the checker: course → (extensions, label, multiple files?)
_CODE_COURSES = {
    'intro_to_cs': (["py"], "🐍 קוד Python (.py)", False),
    'software_1':  (["java"], "☕ קוד Java (.java — אפשר כמה קבצים)", True),
}


def _code_uploader(course_key: str, key: str, label_prefix: str = ""):
    exts, label, multi = _CODE_COURSES[course_key]
    return st.file_uploader(label_prefix + label, type=exts, accept_multiple_files=multi, key=key)


def _save_code(upload, tmp: Path) -> tuple[Path | None, str]:
    """Save one upload, or a list of .java uploads, for check_homework.
    → (path: a file, or a folder of .java files; original name(s) for the format check)."""
    if not upload:
        return None, ''
    if isinstance(upload, list):
        d = tmp / "java_src"
        d.mkdir(parents=True, exist_ok=True)
        for u in upload:
            (d / Path(u.name).name).write_bytes(u.getbuffer())
        return d, ', '.join(u.name for u in upload)
    p = tmp / Path(upload.name).name
    p.write_bytes(upload.getbuffer())
    return p, upload.name


# ─── previous reports + correction upload ────────────────────────────────────

def _previous_reports() -> None:
    root = Path("data/homework") / _user_dir()
    past = []
    if root.exists():
        for course_dir in root.iterdir():
            if not course_dir.is_dir():
                continue
            if not core.can_see_course(course_dir.name):
                continue
            for f in sorted(course_dir.glob("*_feedback.md"), reverse=True):
                past.append((course_dir.name, f))
    if not past:
        return
    with st.expander(f"📂 דוחות בדיקה קודמים ({len(past)})", expanded=False):
        options = ['— בחר דוח —'] + [f"{COURSE_KEYS.get(ck, ck)} | {f.stem.replace('_feedback', '')}" for ck, f in past]
        sel = st.selectbox("צפה בדוח קודם", options, key="past_report_sel", label_visibility="collapsed")
        if not sel or sel == '— בחר דוח —':
            return
        ck, f = past[options.index(sel) - 1]
        content = f.read_text(encoding='utf-8', errors='replace')
        v1, v2 = st.columns([5, 1], vertical_alignment="center")
        with v1:
            st.caption(f"📄 {f.name}  ·  {f.stat().st_size // 1024}KB")
        with v2:
            st.download_button("⬇️ הורד", data=content.encode('utf-8'), file_name=f.name,
                               mime='text/markdown', key=f"dl_past_{f.name}", use_container_width=True)
        st.markdown("---")
        st.markdown(content)
        st.markdown("---")

        st.markdown("### 🔁 העלאת תיקון לדוח זה")
        st.caption("מעלים את הקוד / התיאוריה המתוקנים — הבודק ישווה לדוח שלמעלה ויפסוק האם תוקנו הבעיות.")
        c1, c2 = st.columns(2)
        with c1:
            fix_ex = st.file_uploader("📄 שאלות התרגיל (PDF) — אותו תרגיל מקורי", type=["pdf"], key=f"fix_ex_{f.name}")
        with c2:
            if ck in _CODE_COURSES:
                fix_code = _code_uploader(ck, f"fix_code_{f.name}", "מתוקן: ")
            else:
                fix_code = st.file_uploader("🐍 קוד מתוקן (.py)", type=["py"], key=f"fix_code_{f.name}")
        fix_theory = st.file_uploader("✏️ תיאוריה מתוקנת (PDF) — אופציונלי", type=["pdf"], key=f"fix_theory_{f.name}")
        if st.button("🔍 בדוק תיקון", type="primary", use_container_width=True, key=f"fix_check_{f.name}"):
            if not fix_ex:
                st.warning("⚠️ צריך להעלות גם את קובץ השאלות המקורי")
            elif not (fix_code or fix_theory):
                st.warning("⚠️ צריך להעלות לפחות קוד מתוקן או תיאוריה מתוקנת")
            else:
                from generators.homework_checker import HomeworkChecker
                tmp = Path(tempfile.mkdtemp(prefix='hw_fix_'))
                try:
                    fex = tmp / fix_ex.name
                    fex.write_bytes(fix_ex.getbuffer())
                    fth_p = None
                    fcode_p, fcode_name = _save_code(fix_code, tmp)
                    if fix_theory:
                        fth_p = tmp / fix_theory.name
                        fth_p.write_bytes(fix_theory.getbuffer())
                    with st.spinner("בודק תיקון מול הדוח הקודם..."):
                        checker = HomeworkChecker()
                        res = checker.check_homework(
                            exercise_pdf_path=str(fex),
                            solution_pdf_path=str(fth_p) if fth_p else None,
                            course_key=ck,
                            solution_code_path=str(fcode_p) if fcode_p else None,
                            code_filename=fcode_name,
                            theory_filename=fix_theory.name if fix_theory else '',
                            previous_report_md=content,
                        )
                    st.success(f"✅ בדיקה הסתיימה. ציון: {res.get('overall_score', '?')}")
                    fix_md = checker.format_feedback_md(res, COURSE_KEYS.get(ck, ck), f.stem.replace('_feedback', ''))
                    st.markdown(fix_md)
                    checker.save_feedback(res, ck, COURSE_KEYS.get(ck, ck),
                                          f"{f.stem.replace('_feedback', '')}_FIX", user_id=_user_dir())
                    st.caption("💾 הדוח החדש נשמר. תוכל לראות אותו ברשימה למעלה.")
                except Exception as fe:
                    st.error(f"❌ שגיאה: {fe}")
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)


# ─── main check ──────────────────────────────────────────────────────────────

def _render_result(result: dict, checker, hw_course: str, hw_course_key: str, hw_title: str) -> None:
    score = result.get('overall_score', '?')
    pre_issues = result.get('filename_issues', []) + result.get('static_issues', [])
    if pre_issues:
        st.error("🔴 **בעיות הגשה שנמצאו — יש לתקן לפני הגשה!**")
        for iss in pre_issues:
            st.markdown(f"- {iss}")
        st.markdown("---")
    score_card(str(score), result.get('summary', ''), footer=f"עלות: ${result.get('cost_usd', 0):.4f}",
               good=score_is_good(score))

    unauthorized = result.get('unauthorized_methods', [])
    if unauthorized and unauthorized != ['לא נמצאו שיטות לא מורשות']:
        st.error(f"⚠️ שיטות שלא נלמדו: {', '.join(unauthorized)}")

    per_q = result.get('per_question', [])
    if per_q:
        st.markdown("### פירוט לפי שאלה")
        for q in per_q:
            status = q.get('status', '')
            optional = q.get('optional', False)
            feedback = q.get('feedback', '').strip()
            score_pct = q.get('score_pct', '')
            if 'רשות' in status and 'לא הוגש' in status:
                icon = '⚪'
            elif status == 'נכון':
                icon = '✅'
            elif 'חלקי' in status:
                icon = '🔶'
            elif 'שגוי' in status or 'חסר' in status:
                icon = '❌'
            else:
                icon = '⬜'
            label = f"שאלה {q.get('q_num', '?')}"
            if optional:
                label += " (רשות)"
            label += f" — {status}"
            if score_pct:
                label += f" ({score_pct}%)"
            if status == 'נכון' and not feedback:
                st.markdown(f"{icon} **{label}**")
            elif 'רשות' in status and 'לא הוגש' in status:
                st.markdown(f"⚪ **שאלה {q.get('q_num', '?')} (רשות)** — לא הוגש ✓")
            else:
                with st.expander(f"{icon} {label}"):
                    if feedback:
                        st.markdown(feedback)

    mistakes = result.get('main_mistakes', [])
    if mistakes:
        st.markdown("### 🔴 טעויות עיקריות לתיקון")
        for i, m in enumerate(mistakes, 1):
            st.markdown(f"{i}. {m}")

    recommendations = result.get('recommendations', [])
    if recommendations:
        st.markdown("---")
        st.markdown("### 💡 המלצות לשיפור")
        st.caption("מבוסס על הנושאים שנלמדו בקורס עד כה")
        for i, r in enumerate(recommendations, 1):
            with st.expander(f"💡 המלצה {i}"):
                st.markdown(r)

    try:
        report_md = checker.format_feedback_md(result, hw_course, hw_title)
        checker.save_feedback(result, hw_course_key, hw_course, hw_title, user_id=_user_dir())
        st.download_button("⬇️ הורד דוח (Markdown)", data=report_md.encode('utf-8'),
                           file_name=f"feedback_{hw_title}.md", mime="text/markdown")
    except Exception:
        pass


def _checker() -> None:
    section("בדיקה חדשה", icon="🔍")
    with st.container(border=True, key="card_hw_form"):
        hw_col1, hw_col2 = st.columns(2)
        with hw_col1:
            course_opts = {k: v for k, v in COURSE_KEYS.items() if core.can_see_course(k)}
            if len(course_opts) == 1:
                hw_course = list(course_opts.values())[0]
                st.markdown(f"**קורס:** {hw_course}")
            else:
                hw_course = st.selectbox("קורס", list(course_opts.values()), key="hw_course")
            hw_title = st.text_input("שם התרגיל", placeholder="hw1 / תרגיל 2...", key="hw_title")
        with hw_col2:
            exercise_file = st.file_uploader("📄 שאלות התרגיל (PDF)", type=["pdf"], key="hw_exercise")

        hw_course_key = [k for k, v in COURSE_KEYS.items() if v == hw_course][0]
        is_cs_course = hw_course_key in _CODE_COURSES

        rec_model_label, rec_reason = _PROF_RECOMMEND.get(
            hw_course_key, ("🤖 ברירת מחדל (מומלץ לפי קורס)", "ברירת המחדל מותאמת לקורס זה"))
        with st.expander("🎛️ בחירת מודל AI לבדיקה", expanded=False):
            callout(f"👨‍🏫 <b>המלצת הפרופסור:</b> <b>{esc(rec_model_label.split('—')[0].strip())}</b><br>"
                    f"<span style='color:var(--text-2);font-size:13px'>{esc(rec_reason)}</span>", tone="success")
            st.radio("מודל לבדיקה:", list(_MODEL_OPTIONS.keys()), index=0, key="hw_model_select", horizontal=False)

        if is_cs_course:
            if hw_course_key == 'software_1':
                callout("📌 <b>תוכנה 1</b> — מעלים את קבצי ה-Java (אפשר כמה יחד) ואם יש — תיאוריה ב-PDF. "
                        "הקוד מקומפל ומורץ על מקרי קצה בסביבה מבודדת.", tone="info")
            else:
                callout("📌 <b>מדמ\"ח</b> — יש להגיש שני קבצים: קוד Python (.py) ותיאוריה (.pdf). "
                        "ניתן להעלות את שניהם, או רק אחד אם השאלות לא מחייבות שניהם.", tone="info")
            sol_col1, sol_col2 = st.columns(2)
            with sol_col1:
                code_file = _code_uploader(hw_course_key, "hw_code")
            with sol_col2:
                theory_file = st.file_uploader("✍️ תיאוריה (.pdf) — כתב יד / מוקלד", type=["pdf"], key="hw_theory")
            solution_file = None
        else:
            code_file = None
            theory_file = None
            solution_file = st.file_uploader("✏️ הפתרון שלך (PDF / .py)", type=["pdf", "py"], key="hw_solution")

        prev_report_text = None
        resub_dir = Path("data/homework") / _user_dir() / hw_course_key
        resub_reports = sorted(resub_dir.glob("*_feedback.md"), reverse=True) if resub_dir.exists() else []
        if resub_reports:
            resub_options = ['🆕 בדיקה חדשה'] + [f"🔁 תיקון לדוח: {f.stem.replace('_feedback', '')}"
                                                 for f in resub_reports[:10]]
            resub_choice = st.selectbox("סוג בדיקה", resub_options, key="hw_resub_choice",
                                        help="אם תבחר תיקון — הבודק ישווה לדוח הקודם ויפסוק האם תוקנו הבעיות.")
            if resub_choice != '🆕 בדיקה חדשה':
                resub_file = resub_reports[resub_options.index(resub_choice) - 1]
                try:
                    prev_report_text = resub_file.read_text(encoding='utf-8', errors='replace')
                    st.caption(f"📎 משווה ל: `{resub_file.name}` ({len(prev_report_text) // 1024}KB)")
                except Exception as e:
                    st.warning(f"לא הצלחתי לקרוא את הדוח הקודם: {e}")

        clicked = st.button("🔍 בדוק שיעורי בית", type="primary", use_container_width=True, key="hw_check")

    if not clicked:
        return
    has_solution = solution_file or code_file or theory_file
    if not exercise_file:
        st.warning("⚠️ יש להעלות את קובץ השאלות (PDF)")
        return
    if not has_solution:
        st.warning("⚠️ יש להעלות לפחות קובץ פתרון אחד")
        return
    if not hw_title:
        st.warning("⚠️ נא להזין שם לתרגיל")
        return

    tmp_dir = Path(tempfile.mkdtemp())
    ex_path = tmp_dir / "exercise.pdf"
    ex_path.write_bytes(exercise_file.read())
    orig_code_name = orig_theory_name = ''
    code_tmp_path = theory_tmp_path = None
    sol_path = None
    if code_file:
        code_tmp_path, orig_code_name = _save_code(code_file, tmp_dir)
    if theory_file:
        orig_theory_name = theory_file.name
        theory_tmp_path = tmp_dir / "solution_theory.pdf"
        theory_tmp_path.write_bytes(theory_file.read())
    if solution_file:
        sol_ext = "py" if solution_file.name.endswith(".py") else "pdf"
        sol_path = tmp_dir / f"solution.{sol_ext}"
        sol_path.write_bytes(solution_file.read())
        if sol_ext == 'py':
            orig_code_name = solution_file.name
            code_tmp_path = sol_path
        else:
            orig_theory_name = solution_file.name
            theory_tmp_path = sol_path

    with st.spinner("הפרופסור בודק... (עד דקה)"):
        try:
            from generators.homework_checker import HomeworkChecker
            checker = HomeworkChecker()
            override_val = _MODEL_OPTIONS.get(st.session_state.get('hw_model_select', 'auto'), 'auto')
            result = checker.check_homework(
                exercise_pdf_path=str(ex_path),
                solution_pdf_path=str(theory_tmp_path) if theory_tmp_path else None,
                course_key=hw_course_key,
                solution_code_path=str(code_tmp_path) if code_tmp_path else None,
                code_filename=orig_code_name,
                theory_filename=orig_theory_name,
                model_override=None if override_val == 'auto' else override_val,
                previous_report_md=prev_report_text,
            )
            _render_result(result, checker, hw_course, hw_course_key, hw_title)

            st.session_state["hw_last_result"] = result
            st.session_state["hw_last_course_key"] = hw_course_key
            st.session_state["hw_last_course_name"] = hw_course
            st.session_state["hw_last_title"] = hw_title
            st.session_state["hw_last_ex_bytes"] = open(ex_path, "rb").read() if ex_path.exists() else None
            st.session_state["hw_last_is_cs"] = is_cs_course

            # ── sync to NotebookLM automatically ──
            # Behaviour kept exactly: in CS mode (code + theory slots) the old
            # code hit an undefined `sol_path` and skipped the sync; do the same.
            try:
                if sol_path is None:
                    raise RuntimeError("אין קובץ פתרון יחיד")
                from generators.notebooklm_agent import NotebookLMAgent
                nlm = NotebookLMAgent()
                if nlm.is_authenticated():
                    hw_pdfs = []
                    if ex_path.exists():
                        hw_pdfs.append(str(ex_path))
                    sol_for_nb = sol_path if sol_path and sol_path.exists() else None
                    if sol_for_nb:
                        disc_dir = Path(tempfile.mkdtemp())
                        disc_path = disc_dir / f"[תשובת_תלמיד_לא_חומר_רשמי] {hw_title}_{sol_for_nb.suffix}"
                        shutil.copy2(sol_for_nb, disc_path)
                        hw_pdfs.append(str(disc_path))
                    if hw_pdfs:
                        with st.spinner("📓 מסנכרן ל-NotebookLM..."):
                            nlm_res = nlm.sync_sources(hw_course_key, "all", summary_pdfs=hw_pdfs)
                        st.caption(f"📓 NotebookLM: {len(nlm_res['added'])} קבצים נוספו למחברת"
                                   if nlm_res['added'] else "📓 NotebookLM: קבצים כבר קיימים במחברת")
            except Exception as e:
                st.caption(f"📓 NotebookLM: דילוג ({e})")
        except Exception as e:
            st.error(f"❌ שגיאה בבדיקה: {e}")
        finally:
            try:
                ex_path.unlink()
                if sol_path:
                    sol_path.unlink()
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass


# ─── targeted correction of failed questions ─────────────────────────────────

def _fix_failed() -> None:
    last = st.session_state.get("hw_last_result")
    if not last:
        return
    section("הגש תיקון לשאלות שנכשלו", icon="✏️")
    failed_qs = [
        q for q in last.get("per_question", [])
        if q.get("status") not in ("נכון", "רשות - לא הוגש", "לא נבדק (תיקון ממוקד)")
        and not (q.get("optional") and "לא הוגש" in q.get("status", ""))
    ]
    failed_nums = [q["q_num"] for q in failed_qs]
    if not failed_qs:
        st.success("🎉 אין שאלות שדורשות תיקון — כל השאלות תקינות!")
        return
    with st.container(border=True, key="card_hw_fix"):
        st.markdown("**שאלות שדורשות תיקון:** " + " | ".join(
            f"`שאלה {q['q_num']}` — {q['status']} ({q.get('score_pct', '?')}%)" for q in failed_qs))
        st.caption("העלה את כל קובץ התרגיל המתוקן — המערכת תבדוק **רק** את השאלות שנכשלו "
                   f"({', '.join(str(n) for n in failed_nums)}).")
        fix_is_cs = st.session_state.get("hw_last_is_cs", False)
        fix_ck = st.session_state.get("hw_last_course_key", "")
        fix_ex_bytes = st.session_state.get("hw_last_ex_bytes")
        c1, c2 = st.columns(2)
        with c1:
            if fix_is_cs and fix_ck in _CODE_COURSES:
                _code_uploader(fix_ck, "fix_code", "מתוקן: ")
            elif fix_is_cs:
                st.file_uploader("🐍 קוד Python מתוקן (.py)", type=["py"], key="fix_code")
                st.file_uploader("📄 תיאוריה מתוקנת (PDF, אם יש)", type=["pdf"], key="fix_theory")
            else:
                st.file_uploader("📄 הפתרון המתוקן (PDF)", type=["pdf"], key="fix_sol")
        with c2:
            st.info(f"🎯 המערכת תבדוק **רק** שאלות: **{', '.join(str(n) for n in failed_nums)}**\n\n"
                    "שאר השאלות לא ייבדקו מחדש.")
        clicked = st.button("✅ בדוק את התיקון", key="fix_check_btn", type="primary")

    if not clicked:
        return
    fix_sol_up = st.session_state.get("fix_sol") if not fix_is_cs else None
    fix_code_up = st.session_state.get("fix_code")
    fix_theory_up = st.session_state.get("fix_theory")
    if not (fix_sol_up or fix_code_up or fix_theory_up):
        st.warning("⚠️ יש להעלות לפחות קובץ אחד של הפתרון המתוקן.")
        return
    if not fix_ex_bytes:
        st.warning("⚠️ קובץ שאלות התרגיל המקורי לא נמצא. בצע בדיקה ראשונה שוב.")
        return
    tmp = Path(tempfile.mkdtemp())
    ex_path = tmp / "exercise.pdf"
    ex_path.write_bytes(fix_ex_bytes)
    theory_p = sol_p = None
    code_p, code_name = _save_code(fix_code_up, tmp)
    if fix_theory_up:
        theory_p = tmp / fix_theory_up.name
        theory_p.write_bytes(fix_theory_up.read())
    if fix_sol_up:
        sol_p = tmp / fix_sol_up.name
        sol_p.write_bytes(fix_sol_up.read())
    with st.spinner(f"בודק תיקון לשאלות {', '.join(str(n) for n in failed_nums)}..."):
        try:
            from generators.homework_checker import HomeworkChecker
            res = HomeworkChecker().check_homework(
                exercise_pdf_path=str(ex_path),
                solution_pdf_path=str(theory_p or sol_p) if (theory_p or sol_p) else None,
                course_key=st.session_state.get("hw_last_course_key", ""),
                solution_code_path=str(code_p) if code_p else None,
                code_filename=code_name,
                target_questions=failed_nums,
            )
            fscore = res.get("overall_score", "?")
            score_card(str(fscore), res.get("summary", ""), good=score_is_good(fscore), prefix="תיקון: ")
            checked = [q for q in res.get("per_question", []) if "לא נבדק" not in q.get("status", "")]
            if checked:
                st.markdown(f"**תוצאות שאלות {', '.join(str(n) for n in failed_nums)}:**")
                for fq in checked:
                    fst = fq.get("status", "")
                    ficon = "✅" if fst == "נכון" else ("🔶" if "חלקי" in fst else "❌")
                    pct = f"({fq.get('score_pct', '')}%)" if fq.get('score_pct') else ''
                    st.markdown(f"{ficon} **שאלה {fq['q_num']}** — {fst} {pct}")
                    if fq.get("feedback"):
                        with st.expander("פירוט"):
                            st.markdown(fq["feedback"])
            st.session_state["hw_last_result"] = res
        except Exception as fe:
            st.error(f"❌ שגיאה: {fe}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ─── practice exercise ───────────────────────────────────────────────────────

def _practice() -> None:
    section("תרגיל מתקן אוטומטי", "הפרופסור סורק את הבדיקות, מזהה נקודות חלשות ויוצר תרגיל ממוקד", icon="🔁")
    with st.container(border=True, key="card_hw_practice"):
        course_opts = {k: v for k, v in COURSE_KEYS.items() if core.can_see_course(k)}
        c1, _ = st.columns([2, 2])
        with c1:
            if len(course_opts) == 1:
                pr_course_name = list(course_opts.values())[0]
                pr_course_key = list(course_opts.keys())[0]
                st.markdown(f"**קורס:** {pr_course_name}")
            else:
                pr_course_name = st.selectbox("קורס לתרגיל מתקן", list(course_opts.values()), key="pr_course")
                pr_course_key = [k for k, v in course_opts.items() if v == pr_course_name][0]

        hw_dir = Path("data/homework") / _user_dir() / pr_course_key
        feedback_files = sorted(hw_dir.glob("*_feedback.md")) if hw_dir.exists() else []
        st.caption(f"נמצאו {len(feedback_files)} דוחות בדיקה קיימים עבור {pr_course_name}")
        if not feedback_files:
            st.info("💡 עדיין אין דוחות בדיקה לקורס זה. בדוק לפחות תרגיל אחד קודם.")
        else:
            with st.expander(f"📂 דוחות בדיקה קיימים ({len(feedback_files)})"):
                for f in feedback_files:
                    st.caption(f"• {f.name}")
            if st.button("🔍 נתח חולשות וצור תרגיל מתקן", key="pr_generate", type="primary"):
                with st.spinner(f"הפרופסור מנתח {len(feedback_files)} דוחות ויוצר תרגיל מתקן..."):
                    try:
                        from generators.homework_checker import HomeworkChecker
                        pr_result = HomeworkChecker().generate_practice_exercise(
                            course_key=pr_course_key, course_name=pr_course_name,
                            feedback_mds=[str(f) for f in feedback_files])
                        st.session_state["pr_result"] = pr_result
                        st.session_state["pr_course_key"] = pr_course_key
                        st.session_state["pr_course_name"] = pr_course_name
                    except Exception as pe:
                        st.error(f"❌ שגיאה: {pe}")

    pr_result = st.session_state.get("pr_result")
    if not pr_result:
        return
    pr_course_key = st.session_state.get("pr_course_key", "")
    weak = pr_result.get("weak_topics", [])
    if weak:
        st.markdown("### 🎯 נושאים לחיזוק שזוהו")
        for w in weak:
            st.markdown(f"- ⚠️ {w}")
    intro = pr_result.get("exercise_intro", "")
    if intro:
        st.info(f"💬 **מהפרופסור:** {intro}")
    ex_md = pr_result.get("exercise_md", "")
    if not ex_md:
        return
    st.markdown("### 📝 התרגיל המתקן")
    st.markdown(ex_md)
    st.markdown(f"*עלות ניתוח: ${pr_result.get('cost_usd', 0):.4f}*")
    st.download_button("⬇️ הורד תרגיל (Markdown)", data=ex_md.encode("utf-8"),
                       file_name=f"תרגיל_מתקן_{pr_course_key}.md", mime="text/markdown", key="pr_download")

    st.markdown("---")
    st.markdown("### 📤 הגש את הפתרון שלך לבדיקה אוטומטית")
    st.caption("הפרופסור יבדוק **רק** את השאלות בתרגיל המתקן הזה — לא את שאר הקורס.")
    pr_is_cs = pr_course_key in _CODE_COURSES
    pr_ex_file = st.file_uploader("📄 שאלות התרגיל המתקן (PDF — אפשר להוריד למעלה ולהדפיס)",
                                  type=["pdf"], key="pr_ex_file")
    if pr_is_cs:
        pr_sol_code = _code_uploader(pr_course_key, "pr_sol_code")
        pr_sol_theory = st.file_uploader("📄 חלק תיאורטי (PDF, אם יש)", type=["pdf"], key="pr_sol_theory")
    else:
        pr_sol_file = st.file_uploader("📄 הפתרון שלך (PDF)", type=["pdf"], key="pr_sol_file")
        pr_sol_code = None
        pr_sol_theory = pr_sol_file

    if st.button("✅ בדוק את הפתרון שלי", key="pr_check_btn", type="primary"):
        if not pr_ex_file:
            st.warning("⚠️ יש להעלות את קובץ שאלות התרגיל המתקן (PDF)")
            return
        tmp = Path(tempfile.mkdtemp())
        ex_path = tmp / pr_ex_file.name
        ex_path.write_bytes(pr_ex_file.read())
        theory_p = None
        code_p, code_name = _save_code(pr_sol_code, tmp)
        if pr_sol_theory:
            theory_p = tmp / pr_sol_theory.name
            theory_p.write_bytes(pr_sol_theory.read())
        with st.spinner("בודק את הפתרון המתקן..."):
            try:
                from generators.homework_checker import HomeworkChecker
                chk = HomeworkChecker().check_homework(
                    exercise_pdf_path=str(ex_path),
                    solution_pdf_path=str(theory_p) if theory_p else None,
                    course_key=pr_course_key,
                    solution_code_path=str(code_p) if code_p else None,
                    code_filename=code_name,
                    theory_filename=pr_sol_theory.name if pr_sol_theory else None,
                )
                sc = chk.get("overall_score", "?")
                score_card(str(sc), chk.get("summary", ""), good=score_is_good(sc))
                mistakes = chk.get("main_mistakes", [])
                if mistakes:
                    st.markdown("**טעויות שנותרו:**")
                    for m in mistakes:
                        st.markdown(f"- {m}")
                else:
                    st.success("🎉 מצוין! אין טעויות בתרגיל המתקן.")
            except Exception as pe2:
                st.error(f"❌ שגיאה: {pe2}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


def render() -> None:
    page_header("בדיקת שיעורי בית", "העלה תרגיל ופתרון — הפרופסור יבדוק נכונות, שיטות לא מותרות, "
                "תקינות הגשה ויתן פידבק מפורט לפי שאלה.", icon="📝", eyebrow="פרופסור AI")
    _previous_reports()
    _checker()
    core.mark("homework:checker")
    _fix_failed()
    _practice()
    core.mark("homework:render")
