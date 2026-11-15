"""
dashboard/smart_chat.py — A persistent, multi-intent chat surface.

Four intents the chat understands (detected from the question):

  1. DEADLINE   — "מתי להגיש את התרגיל"  →  reads vault assignments, returns due dates
  2. RAG        — general study questions →  rag.chat.chat() over summaries
  3. HOMEWORK   — uploaded PDF + "האם זה בסדר?" → HomeworkChecker.check_homework()
  4. NOTEBOOKLM — "תיצור חידון/סרטון/כרטיסיות" → notebooklm_agent.generate_*

State lives in `data/chat_history.json` (last 200 turns kept). Per-session UI
state in `st.session_state` so file uploads don't lose context across reruns.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, date as _date
from pathlib import Path
from typing import Any, Optional

import streamlit as st

from config.paths import (
    COURSE_NAMES, COURSE_ORDER, DATA_DIR, ASSIGNMENTS_DIR,
)
from dashboard import vault_reader as vr


CHAT_HISTORY_PATH = DATA_DIR / "chat_history.json"
MAX_HISTORY = 200          # keep last 200 turns on disk
SHOW_LAST = 12             # render last 12 turns in the UI


# ─── intent detection ─────────────────────────────────────────────────────

# Hebrew + English keyword sets — the chat detects intent before deciding
# which backend to call. Order matters: the first match wins.

_DEADLINE_KW = (
    "מתי להגיש", "מתי הדדליין", "תאריך הגשה", "מועד הגשה", "deadline",
    "due date", "מתי צריך להגיש", "כמה זמן יש לי", "מתי זה לתאריך",
    "מתי המטלה", "תרגיל הבא", "המטלה הבאה",
)

_NOTEBOOKLM_KW = (
    ("video",      ("תיצור סרטון", "תכין סרטון", "סרטון חידון", "וידאו",
                    "video", "video overview", "סרטון הסבר")),
    ("quiz",       ("תיצור חידון", "תכין חידון", "מבחן תרגול", "חידון",
                    "quiz", "מבחן עצמי")),
    ("flashcards", ("כרטיסיות", "flashcards", "פלאשקארדס", "כרטיסיית זיכרון")),
    ("study_guide",("מדריך לימוד", "מדריך הכנה", "study guide", "מדריך")),
    ("podcast",    ("פודקאסט", "podcast", "אודיו")),
)

_HOMEWORK_KW = (
    "השיעורי בית האלה בסדר", "השיעורי בית בסדר", "תבדוק לי את התרגיל",
    "תבדוק שיעורי בית", "האם הפתרון נכון", "תבדוק את הפתרון",
    "homework", "האם הפתרון", "התרגיל הזה נכון", "האם הקוד נכון",
    "תבדוק לי",
)


def detect_intent(question: str, has_attachment: bool = False) -> dict[str, Any]:
    """
    Returns {'kind': str, 'meta': dict}. `kind` is one of:
      - 'deadline'    (assignments lookup)
      - 'homework'    (review uploaded PDF — also forced when has_attachment=True)
      - 'notebooklm'  (with meta.action ∈ {'video','quiz','flashcards','study_guide','podcast'})
      - 'rag'         (default — RAG question)
    """
    q = question.strip()
    qlow = q.lower()

    # Force homework mode if a file is attached — the upload is the signal
    if has_attachment:
        return {"kind": "homework", "meta": {}}

    for kw in _DEADLINE_KW:
        if kw in q or kw in qlow:
            return {"kind": "deadline", "meta": {}}

    for action, kws in _NOTEBOOKLM_KW:
        if any(kw in q or kw in qlow for kw in kws):
            return {"kind": "notebooklm", "meta": {"action": action}}

    for kw in _HOMEWORK_KW:
        if kw in q:
            return {"kind": "homework", "meta": {}}

    return {"kind": "rag", "meta": {}}


# ─── persistence ──────────────────────────────────────────────────────────

def load_history() -> list[dict]:
    if not CHAT_HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(CHAT_HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(turns: list[dict]) -> None:
    CHAT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    trimmed = turns[-MAX_HISTORY:]
    CHAT_HISTORY_PATH.write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_turn(role: str, text: str, **extras) -> None:
    hist = load_history()
    hist.append({
        "role": role,
        "text": text,
        "t": datetime.now().isoformat(timespec="seconds"),
        **extras,
    })
    save_history(hist)


# ─── handlers — one per intent ────────────────────────────────────────────

def handle_deadline(question: str, course_filter: Optional[str]) -> dict:
    """Lookup pending assignments. course_filter limits to one course."""
    if course_filter:
        pending = [
            a for a in vr.list_assignments(course=course_filter)
            if (a.get("status") or "").lower() in ("pending", "ממתין", "")
            and a.get("due_date")
        ]
    else:
        pending = []
        for ck in COURSE_ORDER:
            pending.extend([
                a for a in vr.list_assignments(course=ck)
                if (a.get("status") or "").lower() in ("pending", "ממתין", "")
                and a.get("due_date")
            ])
    pending.sort(key=lambda a: a.get("due_date") or "")

    if not pending:
        scope = COURSE_NAMES.get(course_filter, "בקורסים שלך") if course_filter else "בכל הקורסים"
        return {"answer": f"אין דדליינים פתוחים {scope} 🎉", "items": []}

    today = _date.today()
    lines = []
    for a in pending[:10]:
        due_raw = str(a.get("due_date", ""))[:10]
        try:
            d = datetime.fromisoformat(due_raw).date()
            delta = (d - today).days
            if delta < 0:
                tag = f"🔴 איחור של {-delta} ימים"
            elif delta == 0:
                tag = "🔴 היום!"
            elif delta <= 3:
                tag = f"🟡 בעוד {delta} ימים"
            else:
                tag = f"🟢 בעוד {delta} ימים"
        except Exception:
            tag = "·"
        ck = a.get("course_key", "?")
        course_short = COURSE_NAMES.get(ck, ck)
        title = a.get("title", "?")
        lines.append(f"- **{title}** — {course_short}  \n  {due_raw} · {tag}")

    answer = "📅 דדליינים קרובים:\n\n" + "\n".join(lines)
    return {"answer": answer, "items": pending[:10]}


def handle_rag(question: str, course_filter: Optional[str]) -> dict:
    """Standard RAG over summary vault."""
    from rag.chat import chat as _chat
    res = _chat(question, course_filter=course_filter)
    sources_md = ""
    if res.get("sources"):
        srcs = []
        for s in res["sources"][:5]:
            srcs.append(f"  - `[[{s['stem']}]]` ({s['course_key']})")
        sources_md = "\n\n📚 **מקורות:**\n" + "\n".join(srcs)
    return {
        "answer": res["answer"] + sources_md,
        "sources": res.get("sources", []),
        "used_api": res.get("used_api", False),
    }


def handle_homework(uploaded_files: list, course_filter: Optional[str],
                    title: str = "תרגיל מהצ'אט") -> dict:
    """Save uploaded PDF(s) to disk → invoke HomeworkChecker → return verdict."""
    if not course_filter:
        return {"answer": "כדי לבדוק שיעורי בית אני צריך שתבחר קורס בסרגל למעלה.", "items": []}
    if not uploaded_files:
        return {"answer": "צרף את ה-PDF של התרגיל (או ה-PDF + קוד .py למדמ\"ח).", "items": []}

    # Categorize uploads: exercise PDF / solution PDF / .py code
    exercise_pdf = solution_pdf = solution_code = None
    code_filename = ""
    theory_filename = ""
    for f in uploaded_files:
        fn = (f.name or "").lower()
        suffix = Path(fn).suffix.lower()
        if suffix == ".py":
            solution_code = f
            code_filename = f.name
        elif suffix == ".pdf":
            # First PDF = solution; "exercise" / "תרגיל" / "questions" in name = exercise
            if any(kw in fn for kw in ("exercise", "תרגיל", "questions", "assignment")):
                exercise_pdf = f
            elif solution_pdf is None:
                solution_pdf = f
                theory_filename = f.name
            else:
                exercise_pdf = exercise_pdf or f

    if exercise_pdf is None and solution_pdf is None and solution_code is None:
        return {"answer": "לא זיהיתי PDF/PY. נסה שוב.", "items": []}

    # If only one PDF, treat it as the solution and ask user later for exercise
    if exercise_pdf is None:
        return {"answer": ("נראה שצרפת רק קובץ אחד. כדי לבדוק תרגיל אני צריך גם את "
                           "**שאלות התרגיל** וגם את **הפתרון שלך**. צרף את שניהם ושאל שוב."),
                "items": []}

    # Persist uploads to disk under data/homework/{course}/_chat/
    target_dir = DATA_DIR / "homework" / course_filter / "_chat"
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _save(uf, suffix: str) -> Path:
        out = target_dir / f"{ts}_{uf.name}"
        out.write_bytes(uf.getbuffer())
        return out

    ex_path  = _save(exercise_pdf, ".pdf") if exercise_pdf else None
    sol_path = _save(solution_pdf, ".pdf") if solution_pdf else None
    py_path  = _save(solution_code, ".py") if solution_code else None

    # Run the checker
    from generators.homework_checker import HomeworkChecker
    checker = HomeworkChecker()
    try:
        result = checker.check_homework(
            exercise_pdf_path=str(ex_path),
            solution_pdf_path=str(sol_path) if sol_path else None,
            course_key=course_filter,
            solution_code_path=str(py_path) if py_path else None,
            code_filename=code_filename,
            theory_filename=theory_filename,
        )
    except Exception as e:
        return {"answer": f"❌ שגיאה בבדיקה: {e}", "items": []}

    # Format compact verdict
    score = result.get("overall_score", "—")
    summary = result.get("summary", "")
    mistakes = result.get("main_mistakes", [])
    unauthorized = [m for m in result.get("unauthorized_methods", [])
                    if m and "לא נמצאו" not in m]

    parts = [f"### 📝 תוצאת בדיקה — ציון `{score}`", "", summary]
    if unauthorized:
        parts.append("\n**⚠️ שיטות שלא נלמדו:**")
        parts.extend(f"- {u}" for u in unauthorized[:5])
    if mistakes:
        parts.append("\n**🔴 טעויות עיקריות:**")
        parts.extend(f"- {m}" for m in mistakes[:5])
    parts.append(f"\n_עלות בדיקה: ${result.get('cost_usd', 0):.4f}_")
    parts.append("\n💾 דוח מלא נשמר ב-`data/homework/`")

    # Save the full markdown report so the user can open it in Obsidian
    try:
        course_name = COURSE_NAMES.get(course_filter, course_filter)
        saved = checker.save_feedback(result, course_filter, course_name, title)
        parts.append(f"\n📄 [פתח דוח]({saved})")
    except Exception:
        pass

    return {"answer": "\n".join(parts), "items": [result]}


def handle_notebooklm(question: str, course_filter: Optional[str],
                      action: str) -> dict:
    """Trigger NotebookLM artifact generation. Free (Gemini quota)."""
    if not course_filter:
        return {"answer": "כדי לייצר חידון/סרטון/כרטיסיות — בחר קורס בסרגל למעלה.",
                "items": []}

    try:
        from generators.notebooklm_agent import NotebookLMAgent
    except Exception as e:
        return {"answer": f"❌ NotebookLM לא זמין: {e}", "items": []}

    if not NotebookLMAgent.is_authenticated():
        return {"answer": ("❌ NotebookLM לא מחובר. עבור ללשונית **🎓 NotebookLM** "
                           "ולחץ על 'התחבר/חדש'."),
                "items": []}

    # Default scope — "all" means whole course brain
    scope = "all"

    agent = NotebookLMAgent()
    label = {
        "video":       "סרטון הסבר",
        "quiz":        "חידון תרגול",
        "flashcards":  "כרטיסיות זיכרון",
        "study_guide": "מדריך לימוד",
        "podcast":     "פודקאסט",
    }.get(action, action)

    try:
        if action == "video":
            path = agent.generate_video(course_filter, scope, instructions=question)
            return {"answer": f"🎬 {label} נוצר: `{path}`\n\nנשמר ב-vault תחת **🎓 NotebookLM**.",
                    "items": [{"action": action, "path": path}]}
        if action == "quiz":
            data = agent.generate_quiz(course_filter, scope, topic_hint=question)
            n = len(data.get("questions") or [])
            return {"answer": f"❓ {label} נוצר ({n} שאלות). פתח את הקובץ ב-vault.",
                    "items": [{"action": action, "data": data}]}
        if action == "flashcards":
            data = agent.generate_flashcards(course_filter, scope)
            n = len(data.get("cards") or [])
            return {"answer": f"🃏 {label} נוצרו ({n} כרטיסיות).",
                    "items": [{"action": action, "data": data}]}
        if action == "study_guide":
            path = agent.generate_study_guide(course_filter, scope, extra_instructions=question)
            return {"answer": f"📘 {label} נוצר: `{path}`",
                    "items": [{"action": action, "path": path}]}
        if action == "podcast":
            path = agent.generate_podcast(course_filter, scope, instructions=question)
            return {"answer": f"🎧 {label} נוצר: `{path}`",
                    "items": [{"action": action, "path": path}]}
    except Exception as e:
        return {"answer": f"❌ שגיאה ביצירת {label}: {e}", "items": []}

    return {"answer": f"לא תמכתי בפעולה: {action}", "items": []}


# ─── dispatcher ───────────────────────────────────────────────────────────

def respond(question: str, course_filter: Optional[str],
            uploaded_files: Optional[list] = None) -> dict:
    """
    Single entry point. Detects intent, routes to the right handler,
    returns {'answer', 'kind', ...handler-specific keys}.
    """
    has_attachment = bool(uploaded_files)
    intent = detect_intent(question, has_attachment=has_attachment)
    kind = intent["kind"]

    if kind == "deadline":
        out = handle_deadline(question, course_filter)
    elif kind == "homework":
        out = handle_homework(uploaded_files or [], course_filter)
    elif kind == "notebooklm":
        out = handle_notebooklm(question, course_filter, intent["meta"]["action"])
    else:
        out = handle_rag(question, course_filter)

    out["kind"] = kind
    return out


# ─── Streamlit UI ─────────────────────────────────────────────────────────

# WhatsApp-style bubbles in the theme palette: page background,
# the accent for the user, a plain tile for the assistant.
# The shell uses column-reverse (turns emitted newest-first) so it opens
# scrolled to the latest message.
_CHAT_CSS = """
<style>
.qc-chat-shell{
  display:flex; flex-direction:column-reverse; gap:14px;
  padding:14px 18px; background:var(--bg);
  border:1px solid var(--border); border-radius:var(--radius);
  min-height:340px; max-height:62vh; overflow-y:auto; direction:rtl;
  font-family:var(--font);
}
.qc-msg{ display:flex; flex-direction:column; max-width:78%; }
.qc-msg.user{ align-self:flex-end; align-items:flex-end; }
.qc-msg.bot{  align-self:flex-start; align-items:flex-start; }
.qc-bubble{
  padding:10px 14px; border-radius:var(--radius);
  border:1px solid var(--border2);
  font-size:14px; line-height:1.55;
  word-break:break-word; direction:rtl; text-align:right;
}
.qc-msg.user .qc-bubble{
  background:var(--amber); color:var(--on-primary); border-color:var(--amber);
}
.qc-msg.bot .qc-bubble{ background:var(--surface); color:var(--text); }
.qc-meta-line{
  font-family:var(--mono); font-size:10px;
  color:var(--muted); margin-top:4px; letter-spacing:.4px;
  display:flex; gap:6px; align-items:center;
}
.qc-intent-pill{
  display:inline-block; padding:1px 6px; margin-left:4px;
  border:1px solid var(--border2); border-radius:var(--radius);
  font-family:var(--mono); font-size:9px;
  text-transform:uppercase; color:var(--text-2);
}
.qc-empty{
  margin:auto; color:var(--muted); text-align:center; padding:32px 12px;
  font-style:italic; font-size:13px;
}
.qc-suggestions{ display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
</style>
"""


def _render_history(turns: list[dict]) -> None:
    if not turns:
        st.markdown(
            '<div class="qc-chat-shell"><div class="qc-empty">'
            "התחילו שיחה — שאלו מתי להגיש תרגיל, על החומר, או צרפו PDF לבדיקת שיעורי בית."
            '</div></div>',
            unsafe_allow_html=True,
        )
        return

    kind_he = {"rag": "חומר", "deadline": "דדליין", "homework": "שיעורי בית",
               "notebooklm": "NotebookLM", "error": "שגיאה"}
    bubbles_html = ['<div class="qc-chat-shell">']
    # newest first: the shell is column-reverse so it opens at the bottom
    for turn in reversed(turns[-SHOW_LAST:]):
        role = "user" if turn.get("role") == "user" else "bot"
        text = (turn.get("text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # <br>, not raw newlines: a blank line would end markdown's HTML block
        text = text.replace("\r\n", "\n").replace("\n", "<br>")
        kind = turn.get("kind", "")
        kind_pill = f'<span class="qc-intent-pill">{kind_he.get(kind, kind)}</span>' if kind else ""
        when = (turn.get("t") or "")[11:16]
        bubbles_html.append(
            f'<div class="qc-msg {role}">'
            f'<div class="qc-bubble">{text}</div>'
            f'<div class="qc-meta-line"><span style="direction:ltr">{when}</span>{kind_pill}</div>'
            f'</div>'
        )
    bubbles_html.append("</div>")
    st.markdown("".join(bubbles_html), unsafe_allow_html=True)


_QUICK_PROMPTS = [
    ("📅 מה הדדליינים שלי?",      "מה הדדליינים הקרובים שלי?"),
    ("❓ תיצור חידון",             "תיצור חידון על החומר האחרון"),
    ("🎬 תיצור סרטון הסבר",       "תיצור סרטון הסבר על החומר"),
    ("📘 מדריך לימוד",            "תיצור מדריך לימוד מקיף"),
]


def render_smart_chat() -> None:
    """Main entry — full-tab chat surface with all 4 capabilities."""
    st.markdown(_CHAT_CSS, unsafe_allow_html=True)

    st.markdown(
        '<h2 class="qc-section-title">💬 צ\'אט חכם — שאלות, דדליינים, בדיקות, יצירה</h2>',
        unsafe_allow_html=True,
    )
    st.caption(
        "שאל שאלה כללית · שאל מתי להגיש תרגיל · צרף PDF לבדיקת שיעורי בית · "
        "בקש חידון/סרטון/כרטיסיות — והכל מהמקום הזה."
    )

    # ── scope selector ─────────────────────────────────────────────────
    top_l, top_r = st.columns([3, 1], vertical_alignment="bottom")
    with top_l:
        scope_options = ["כל הקורסים"] + [COURSE_NAMES[ck] for ck in COURSE_ORDER]
        scope_label = st.selectbox("היקף השאלה:", scope_options,
                                    key="smart_chat_scope")
    with top_r:
        if st.button("🗑 נקה היסטוריה", key="smart_chat_clear",
                     use_container_width=True):
            save_history([])
            st.rerun()

    # Resolve course_filter
    if scope_label == "כל הקורסים":
        course_filter = None
    else:
        course_filter = next(
            (ck for ck in COURSE_ORDER if COURSE_NAMES[ck] == scope_label),
            None,
        )

    # ── history ────────────────────────────────────────────────────────
    history = load_history()
    _render_history(history)

    # ── quick prompts ──────────────────────────────────────────────────
    qp_cols = st.columns(len(_QUICK_PROMPTS))
    for col, (label, prompt) in zip(qp_cols, _QUICK_PROMPTS):
        with col:
            if st.button(label, key=f"qp_{label}", use_container_width=True):
                st.session_state["smart_chat_pending"] = prompt
                st.rerun()

    # ── attachment uploader ────────────────────────────────────────────
    with st.expander("📎 צרף קבצים (לבדיקת שיעורי בית)", expanded=False):
        uploads = st.file_uploader(
            "PDF של תרגיל + פתרון, או PDF + קובץ .py (מדמ\"ח)",
            type=["pdf", "py"],
            accept_multiple_files=True,
            key="smart_chat_uploads",
        )
        if uploads:
            names = ", ".join(f.name for f in uploads)
            st.caption(f"מצורף: {names}")

    # ── input ──────────────────────────────────────────────────────────
    pending = st.session_state.pop("smart_chat_pending", None)
    user_q = pending or st.chat_input(
        "שאל אותי משהו... (לדוגמה: מתי להגיש את התרגיל הבא? / הסבר על דטרמיננטה / תיצור חידון)"
    )

    if user_q:
        # Append user turn
        append_turn("user", user_q,
                    course=course_filter or "all",
                    has_files=bool(uploads))

        with st.spinner("חושב..."):
            try:
                result = respond(user_q, course_filter, uploaded_files=uploads or None)
            except Exception as e:
                result = {"answer": f"❌ שגיאה: {e}", "kind": "error"}

        append_turn("bot", result["answer"],
                    kind=result.get("kind", ""),
                    course=course_filter or "all")

        # Clear uploads after use so the next message doesn't re-trigger HW check
        if "smart_chat_uploads" in st.session_state:
            try:
                del st.session_state["smart_chat_uploads"]
            except Exception:
                pass
        st.rerun()
