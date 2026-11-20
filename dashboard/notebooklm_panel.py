"""
dashboard/notebooklm_panel.py — NotebookLM tab body.

Look (dashboard/ui/theme.py):
- only CSS variables (var(--qc-*) / var(--text) / var(--border) ...)
- no hardcoded hex
- Hebrew RTL UI

Public entry point:
    render(vault_root: Path, courses: dict[str, str]) -> None

Side-effects identical to the original tab body in app.py:
- writes generated artifacts via exporters.notebooklm_obsidian.export_artifact
- session_state keys reused: nb_course, nb_scope, nb_quiz_data, nb_pod_path,
  nb_vid_path, nb_cards_data, nb_guide_path
- triggers Windows toast on success when plyer is available
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import streamlit as st


# ─── CSS injected once per session ────────────────────────

_PANEL_CSS = """
<style>
.nb-strip{
  display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;
  padding:10px 14px;direction:rtl;
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  font-size:13.5px;color:var(--text);
  box-shadow:var(--shadow-sm);
}
.nb-strip .nb-status{display:flex;gap:10px;align-items:center;font-weight:500}
.nb-strip .nb-dot{
  display:inline-block;width:9px;height:9px;border-radius:50%;
  background:var(--danger);box-shadow:0 0 0 3px var(--danger-soft);
}
.nb-strip .nb-dot.ok{background:var(--success);box-shadow:0 0 0 3px var(--success-soft)}
.nb-strip .nb-hint{color:var(--muted);font-size:12.5px}
.nb-col-title{
  font-size:15px;font-weight:600;color:var(--text);
  padding-bottom:8px;margin:6px 0 10px;direction:rtl;
  border-bottom:1px solid var(--border);
}
.nb-art-row{
  display:flex;justify-content:space-between;align-items:flex-start;
  padding:10px 12px;direction:rtl;
  border:1px solid var(--border);
  border-radius:var(--radius);
  background:var(--surface);
  margin-bottom:6px;
}
.nb-art-row .nb-art-title{font-size:14px;color:var(--text);font-weight:500;}
.nb-art-row .nb-art-meta{font-size:12px;color:var(--muted);margin-top:2px}
.nb-open{
  display:block;padding:10px;text-align:center;border-radius:var(--radius);
  border:1px solid var(--border-strong);background:var(--surface);color:var(--text) !important;
  font-size:13px;text-decoration:none !important;direction:rtl;
}
.nb-open:hover{border-color:var(--primary);color:var(--primary) !important}
.nb-empty{
  padding:18px;direction:rtl;text-align:center;
  color:var(--muted);font-size:13.5px;background:var(--surface);
  border:1px dashed var(--border-strong);border-radius:var(--radius);
}
.nb-msg{display:flex;margin:6px 0}
/* RTL: the user's own messages sit on the left (flex-end), answers on the right */
.nb-msg{direction:rtl}
.nb-msg.user{justify-content:flex-end}
.nb-msg.bot{justify-content:flex-start}
.nb-bubble{max-width:78%;padding:10px 13px;border-radius:16px;font-size:14px;line-height:1.6;
  white-space:pre-wrap;direction:rtl;border:1px solid var(--border)}
.nb-msg.user .nb-bubble{background:var(--primary);color:var(--on-primary);border-color:transparent;border-start-end-radius:6px}
.nb-msg.bot .nb-bubble{background:var(--surface);color:var(--text);border-start-start-radius:6px}
</style>
"""

# Artifact-type metadata (emoji + Hebrew name + filesystem subdir + session key)
_KIND_HE = {
    "quiz":        ("🧠", "חידון"),
    "podcast":     ("🎙️", "פודקאסט"),
    "video":       ("🎬", "סרטון"),
    "flashcard":   ("🃏", "כרטיסיות"),
    "study_guide": ("📖", "מדריך לימוד"),
    # legacy aliases possible in older frontmatter
    "audio":       ("🎙️", "פודקאסט"),
    "flashcards":  ("🃏", "כרטיסיות"),
    "report":      ("📖", "מדריך לימוד"),
    "cards":       ("🃏", "כרטיסיות"),
}


# ─── small utilities ─────────────────────────────────────────────────────────

def _is_auth_err(e: Exception) -> bool:
    s = str(e).lower()
    return "authentication" in s or "expired" in s


def _win_notify(title: str, msg: str) -> None:
    try:
        from plyer import notification
        notification.notify(
            title=title, message=msg,
            app_name="Smart Study Agent", timeout=8,
        )
    except Exception:
        pass


def _obsidian_uri(md_path: Path, vault_root: Path) -> str:
    """Build obsidian://open URI relative to the vault."""
    try:
        rel = md_path.relative_to(vault_root)
    except ValueError:
        rel = md_path
    return (
        f"obsidian://open?vault={quote(vault_root.name)}"
        f"&file={quote(str(rel).replace(chr(92), '/'))}"
    )


def _parse_frontmatter(md: str) -> dict:
    if not md.startswith("---"):
        return {}
    try:
        end = md.index("\n---", 3)
    except ValueError:
        return {}
    raw = md[3:end].strip()
    out: dict[str, Any] = {}
    try:
        import yaml
        parsed = yaml.safe_load(raw) or {}
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # tiny key:value fallback
    for line in raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _list_vault_artifacts(course_key: str, vault_root: Path) -> list[dict]:
    """
    Read existing NotebookLM artifacts for the given course from
    {vault_root}/Smart Study Agent/🎓 NotebookLM/{course_key}/*.md.

    Returns list of dicts sorted date-desc:
        {path, title, date, artifact_type, course_key}
    """
    base = vault_root / "Smart Study Agent" / "🎓 NotebookLM" / course_key
    if not base.exists():
        return []
    results: list[dict] = []
    for p in base.glob("*.md"):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm = _parse_frontmatter(txt)
        results.append({
            "path":          p,
            "title":         fm.get("title") or p.stem,
            "date":          (fm.get("date") or "")[:10],
            "artifact_type": fm.get("artifact_type") or fm.get("type") or "artifact",
            "course_key":    fm.get("course_key") or course_key,
        })
    results.sort(key=lambda r: r["date"], reverse=True)
    return results


# ─── auth handling (preserved from original) ────────────────────────────────

def _reauth() -> None:
    proj = Path(__file__).resolve().parent.parent
    with st.spinner("פותח דפדפן — אם נדרשת כניסה לגוגל, התחברי בדפדפן שנפתח (עד 3 דקות)..."):
        try:
            env = os.environ.copy(); env["PYTHONUTF8"] = "1"
            r = subprocess.run(
                [sys.executable, str(proj / "fix_notebooklm_auth.py")],
                capture_output=True, text=True, timeout=120,
                cwd=str(proj), encoding="utf-8", errors="replace",
                env=env,
            )
            out = r.stdout or ""
            if "נשמר ב" in out or "storage_state.json נשמר" in out or "storage_state" in out:
                st.success("✅ auth חודש — ממשיך...")
                st.rerun()
            else:
                st.error(f"שגיאה: {r.stderr or out or 'לא ידוע'}")
        except subprocess.TimeoutExpired:
            st.error("Timeout — נסה שוב")
        except Exception as e:
            st.error(f"שגיאה: {e}")


# ─── render helpers ─────────────────────────────────────────────────────────

def _render_auth_strip(agent) -> None:
    """Thin header strip showing connection status + a "רענן" (re-auth) button."""
    age = agent.auth_age_hours()
    ok = age <= 4
    label = "מחובר" if ok else "פג"
    age_txt = f"{age:.0f}ש׳" if age < 99 else "—"
    dot_class = "nb-dot ok" if ok else "nb-dot"

    c_left, c_right = st.columns([5, 1.2], vertical_alignment="center")
    with c_left:
        st.markdown(
            f'<div class="nb-strip">'
            f'<div class="nb-status">'
            f'<span class="{dot_class}"></span>'
            f'<span>NotebookLM · {label} · {age_txt}</span>'
            f'</div>'
            f'<div class="nb-hint">'
            f'חינמי דרך חשבון Google'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with c_right:
        if st.button("רענן auth", key="nb_reauth_btn", use_container_width=True):
            _reauth()
    if not ok:
        st.warning(f"⚠️ ה-auth לא חודש כבר {age:.0f} שעות — לחצי על 'רענן auth'")


def _render_selectors(courses: dict[str, str]) -> tuple[str, str, str]:
    """Course + scope selectors. Returns (course_key, course_name, scope)."""
    c1, c2 = st.columns([2, 2])
    with c1:
        course_name = st.selectbox(
            "קורס", list(courses.values()), key="nb_course",
        )
        # reverse-lookup the key
        course_key = next(
            (k for k, v in courses.items() if v == course_name),
            list(courses.keys())[0],
        )
    with c2:
        scope = st.selectbox(
            "היקף", ["all", "lectures_only", "tutorials_only"],
            format_func=lambda s: {
                "all": "כל החומר",
                "lectures_only": "רק הרצאות",
                "tutorials_only": "רק תרגולים",
            }.get(s, s),
            key="nb_scope",
        )
    return course_key, course_name, scope


def _export_through_vault(
    *, course_key: str, course_name: str,
    artifact_type: str, title: str,
    data: Any = None, markdown: str | None = None,
    file_path: str | None = None,
) -> Path | None:
    """Persist any generated artifact through exporters.notebooklm_obsidian."""
    try:
        from exporters.notebooklm_obsidian import export_artifact
        return export_artifact({
            "course_key":   course_key,
            "course_name":  course_name,
            "artifact_type": artifact_type,
            "title":         title,
            "date":          datetime.now().strftime("%Y-%m-%d"),
            "data":          data,
            "markdown":      markdown,
            "file_path":     file_path,
        })
    except Exception as e:
        st.error(f"שגיאת ייצוא ל-vault: {e}")
        return None


def _render_generate_column(
    agent, *, course_key: str, course_name: str, scope: str,
) -> None:
    """Five primary generate buttons (quiz, podcast, video, cards, guide)."""
    st.markdown('<div class="nb-col-title">צור חדש</div>', unsafe_allow_html=True)

    # ── Quiz ──
    if st.button("🧠 צור חידון", key="nb_gen_quiz", type="primary",
                 use_container_width=True):
        with st.spinner("יוצר חידון (60-90 שניות)..."):
            try:
                quiz = agent.generate_quiz(
                    course_key, scope,
                    topic_hint="", quantity="ten_questions", difficulty="medium",
                )
                st.session_state["nb_quiz_data"] = quiz
                st.session_state["nb_quiz_idx"] = 0
                st.session_state["nb_quiz_answers"] = {}
                title = quiz.get("title") or f"חידון {course_name}"
                vault_path = _export_through_vault(
                    course_key=course_key, course_name=course_name,
                    artifact_type="quiz", title=title,
                    data=quiz.get("questions") or quiz,
                )
                if vault_path:
                    st.toast(f"✅ נשמר ב-vault: {vault_path.name}", icon="🧠")
                _win_notify("🧠 חידון מוכן!", f"{course_name}")
                st.rerun()
            except Exception as e:
                if _is_auth_err(e):
                    st.error("🔐 שגיאת Authentication — לחצי על 'רענן auth'")
                else:
                    st.error(f"שגיאה: {str(e)[:400]}")

    # ── Podcast ──
    if st.button("🎙️ צור פודקאסט", key="nb_gen_pod", type="primary",
                 use_container_width=True):
        with st.spinner("יוצר פודקאסט (2-5 דקות)..."):
            try:
                path = agent.generate_podcast(
                    course_key, scope, language="he", instructions="",
                )
                st.session_state["nb_pod_path"] = path
                vault_path = _export_through_vault(
                    course_key=course_key, course_name=course_name,
                    artifact_type="podcast",
                    title=f"פודקאסט {course_name}",
                    file_path=path,
                )
                if vault_path:
                    st.toast(f"✅ נשמר ב-vault: {vault_path.name}", icon="🎙️")
                _win_notify("🎙️ פודקאסט מוכן!", f"{course_name}")
                st.rerun()
            except Exception as e:
                if _is_auth_err(e):
                    st.error("🔐 שגיאת Authentication — לחצי על 'רענן auth'")
                else:
                    st.error(f"שגיאה: {str(e)[:400]}")

    # ── Video ──
    if st.button("🎬 צור סרטון", key="nb_gen_vid", type="primary",
                 use_container_width=True):
        with st.spinner("יוצר סרטון (3-15 דקות)..."):
            try:
                path = agent.generate_video(
                    course_key, scope, language="he", instructions="",
                )
                st.session_state["nb_vid_path"] = path
                vault_path = _export_through_vault(
                    course_key=course_key, course_name=course_name,
                    artifact_type="video",
                    title=f"סרטון {course_name}",
                    file_path=path,
                )
                if vault_path:
                    st.toast(f"✅ נשמר ב-vault: {vault_path.name}", icon="🎬")
                _win_notify("🎬 סרטון מוכן!", f"{course_name}")
                st.rerun()
            except Exception as e:
                if _is_auth_err(e):
                    st.error("🔐 שגיאת Authentication — לחצי על 'רענן auth'")
                else:
                    st.error(f"שגיאה: {str(e)[:400]}")

    # ── Flashcards ──
    if st.button("🃏 צור כרטיסיות", key="nb_gen_cards", type="primary",
                 use_container_width=True):
        with st.spinner("יוצר כרטיסיות (30-60 שניות)..."):
            try:
                cards = agent.generate_flashcards(course_key, scope)
                st.session_state["nb_cards_data"] = cards
                st.session_state["nb_cards_idx"] = 0
                st.session_state["nb_cards_show_back"] = False
                title = cards.get("title") or f"כרטיסיות {course_name}"
                vault_path = _export_through_vault(
                    course_key=course_key, course_name=course_name,
                    artifact_type="flashcard", title=title,
                    data=cards.get("cards") or [],
                )
                if vault_path:
                    st.toast(f"✅ נשמר ב-vault: {vault_path.name}", icon="🃏")
                _win_notify("🃏 כרטיסיות מוכנות!", f"{course_name}")
                st.rerun()
            except Exception as e:
                if _is_auth_err(e):
                    st.error("🔐 שגיאת Authentication — לחצי על 'רענן auth'")
                else:
                    st.error(f"שגיאה: {str(e)[:400]}")

    # ── Study Guide ──
    if st.button("📖 צור מדריך לימוד", key="nb_gen_guide", type="primary",
                 use_container_width=True):
        with st.spinner("יוצר מדריך לימוד (1-5 דקות)..."):
            try:
                path = agent.generate_study_guide(
                    course_key, scope, language="he", extra_instructions="",
                )
                st.session_state["nb_guide_path"] = path
                md_text = ""
                try:
                    md_text = Path(path).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
                vault_path = _export_through_vault(
                    course_key=course_key, course_name=course_name,
                    artifact_type="study_guide",
                    title=f"מדריך לימוד {course_name}",
                    markdown=md_text or None,
                    file_path=path if not md_text else None,
                )
                if vault_path:
                    st.toast(f"✅ נשמר ב-vault: {vault_path.name}", icon="📖")
                _win_notify("📖 מדריך מוכן!", f"{course_name}")
                st.rerun()
            except Exception as e:
                if _is_auth_err(e):
                    st.error("🔐 שגיאת Authentication — לחצי על 'רענן auth'")
                else:
                    st.error(f"שגיאה: {str(e)[:400]}")


def _render_artifacts_column(course_key: str, vault_root: Path) -> None:
    """Right column — list existing artifacts in the vault for this course."""
    st.markdown('<div class="nb-col-title">כבר יש בכספת</div>', unsafe_allow_html=True)

    items = _list_vault_artifacts(course_key, vault_root)
    if not items:
        st.markdown(
            '<div class="nb-empty">אין artifacts עדיין בקורס זה.<br>'
            'צרי משהו חדש בעמודה השמאלית.</div>',
            unsafe_allow_html=True,
        )
        return

    for art in items:
        emoji, kind_he = _KIND_HE.get(art["artifact_type"], ("📄", art["artifact_type"]))
        date_txt = art["date"] or "—"
        title    = art["title"]
        uri      = _obsidian_uri(art["path"], vault_root)

        import html as _h
        c1, c2 = st.columns([5, 2])
        with c1:
            st.markdown(
                f'<div class="nb-art-row">'
                f'<div>'
                f'<div class="nb-art-title">{emoji} {_h.escape(str(title))}</div>'
                f'<div class="nb-art-meta">{date_txt} · {kind_he}</div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with c2:
            st.markdown(
                f'<a class="nb-open" href="{uri}" target="_blank">פתח ב-Obsidian</a>',
                unsafe_allow_html=True,
            )


# ─── Q&A surface — chat directly with NotebookLM (multi-turn) ────────────────

def _render_ask_surface(agent, *, course_key: str, scope: str,
                        course_name: str) -> None:
    """Multi-turn chat panel that calls NotebookLMAgent.ask() with conversation_id."""
    conv_key = f"nb_conv_{course_key}_{scope}"
    log_key  = f"nb_chatlog_{course_key}_{scope}"

    log: list[dict] = st.session_state.get(log_key, [])

    # render history (limit 8 visible)
    if log:
        for turn in log[-8:]:
            role = "user" if turn.get("role") == "user" else "bot"
            txt  = (turn.get("text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            st.markdown(
                f'<div class="nb-msg {role}"><div class="nb-bubble">{txt}</div></div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            '<div class="nb-empty">'
            'שאלו שאלה ישירות על המקורות בנוטבוק. NotebookLM יענה רק ממה שיש בו, עם ציטוטים.'
            '</div>',
            unsafe_allow_html=True,
        )

    c1, c2 = st.columns([5, 1])
    with c1:
        q = st.text_input(
            "שאלה:",
            key=f"nb_ask_q_{course_key}_{scope}",
            placeholder=f"למשל: מהן ההגדרות העיקריות בפרק האחרון של {course_name}?",
            label_visibility="collapsed",
        )
    with c2:
        send = st.button("שאל", key=f"nb_ask_btn_{course_key}_{scope}",
                          use_container_width=True)

    cclear, _ = st.columns([1, 5])
    with cclear:
        if st.button("🗑 נקה", key=f"nb_ask_clear_{course_key}_{scope}"):
            st.session_state.pop(conv_key, None)
            st.session_state.pop(log_key, None)
            st.rerun()

    if send and q.strip():
        log.append({"role": "user", "text": q})
        with st.spinner("NotebookLM חושב..."):
            try:
                res = agent.ask(course_key, scope, q,
                                conversation_id=st.session_state.get(conv_key))
                st.session_state[conv_key] = res.get("conversation_id")
                answer = res.get("answer") or "לא קיבלתי תשובה."
                refs = res.get("references", [])
                if refs:
                    ref_lines = []
                    for r in refs[:4]:
                        title = r.get("title") or ""
                        snippet = (r.get("snippet") or "")[:120]
                        if title:
                            ref_lines.append(f"📚 {title}: {snippet}…")
                    if ref_lines:
                        answer += "\n\n" + "\n".join(ref_lines)
                log.append({"role": "bot", "text": answer})
            except Exception as e:
                log.append({"role": "bot", "text": f"❌ שגיאה: {e}"})

        st.session_state[log_key] = log
        st.rerun()


# ─── public entry point ──────────────────────────────────────────────────────

def render(vault_root: Path, courses: dict[str, str]) -> None:
    """
    Render the full NotebookLM tab body.

    Parameters
    ----------
    vault_root : Path
        Root of the Obsidian vault (e.g. C:\\Users\\you\\Documents\\Obsidian Vault).
        Artifacts are read from {vault_root}/Smart Study Agent/🎓 NotebookLM/{course_key}/.
    courses : dict[str, str]
        Mapping course_key -> Hebrew display name.
    """
    st.markdown(_PANEL_CSS, unsafe_allow_html=True)
    from dashboard.ui.components import page_header
    page_header("NotebookLM", "חידונים, פודקאסטים, סרטונים, כרטיסיות ומדריכי לימוד מהחומר של הקורס",
                icon="🎓", eyebrow="כלי לימוד")

    # ── load agent ─────────────────────────────────────────────────────────
    try:
        from generators.notebooklm_agent import NotebookLMAgent
        agent = NotebookLMAgent()
    except Exception as e:
        st.error(f"לא הצלחתי לטעון את notebooklm-py: {e}")
        st.info("הרץ בטרמינל: `pip install \"notebooklm-py[browser]\"`")
        return

    # ── auth gate ──────────────────────────────────────────────────────────
    if not agent.is_authenticated():
        # Detect if we're running on a headless server (no display)
        import os as _os
        has_display = bool(_os.environ.get('DISPLAY')) or _os.name == 'nt'
        st.warning("🔐 NotebookLM לא מחובר.")
        if not has_display:
            st.info(
                "💡 **NotebookLM נדרש דפדפן חזותי לחיבור ראשוני** — לא ניתן ב-VM ללא מסך.\n\n"
                "**איך לחבר:**\n"
                "1. הרץ פעם אחת על המחשב המקומי שלך:\n"
                "   ```bash\n"
                "   python fix_notebooklm_auth.py\n"
                "   ```\n"
                "2. דפדפן ייפתח — היכנס ל-Google.\n"
                "3. תיקיית `~/.notebooklm/browser_profile/` תשמור את ה-cookies.\n"
                "4. העלה את התיקייה ל-VM:\n"
                "   ```bash\n"
                "   scp -r ~/.notebooklm ubuntu@VM:/home/ubuntu/.notebooklm\n"
                "   ```\n"
                "5. רענן את הדאשבורד — תהיה מחובר."
            )
        else:
            if st.button("🔐 התחבר ל-NotebookLM", type="primary",
                         key="nb_login_btn"):
                _reauth()
        return

    # ── thin auth strip ────────────────────────────────────────────────────
    _render_auth_strip(agent)

    # ── course + scope selectors ───────────────────────────────────────────
    course_key, course_name, scope = _render_selectors(courses)

    st.markdown("")  # small breathing room

    # ── two-column layout: Generate | Existing ────────────────────────────
    col_gen, col_arts = st.columns([1, 1], gap="large")
    with col_gen:
        _render_generate_column(
            agent,
            course_key=course_key,
            course_name=course_name,
            scope=scope,
        )
    with col_arts:
        _render_artifacts_column(course_key, vault_root)

    # ── Q&A surface — direct chat with the NotebookLM brain ───────────────
    st.markdown("")
    st.markdown(
        f'<div class="nb-col-title">💬 שאל את {course_name} (NotebookLM)</div>',
        unsafe_allow_html=True,
    )
    _render_ask_surface(agent, course_key=course_key, scope=scope,
                       course_name=course_name)
