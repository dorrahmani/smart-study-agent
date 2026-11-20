"""Run — scheduled tasks, run now / stop, send summaries, per-course update,
run log with phases, Obsidian sync, refresh. Logic unchanged from "▶️ הרץ עכשיו"."""
from __future__ import annotations

import json
import os
import subprocess
import time

import streamlit as st

from config.paths import COURSE_NAMES
from dashboard import core
from dashboard.parser import get_all_summaries, load_last_run, LAST_RUN_PATH
from dashboard.ui.components import page_header, section, html

ROOT = core.ROOT


def _scheduler() -> None:
    section("משימות מתוזמנות (פעמיים ביום)", "Windows Task Scheduler — במחשב המקומי", icon="🕐")
    try:
        from scheduler.install_task import (
            is_installed as _sched_is_installed,
            install_daily_times as _sched_install_daily,
            list_daily_tasks as _sched_list_daily,
            uninstall as _sched_uninstall,
        )
        sched_known = _sched_is_installed()
        daily_names = _sched_list_daily()
    except Exception as se:
        sched_known = None
        daily_names = []
        st.warning(f"מודול scheduler לא זמין: {se}")

    if sched_known is None:
        return
    with st.container(border=True, key="card_sched"):
        scol1, scol2 = st.columns([3, 1], vertical_alignment="center")
        with scol1:
            if daily_names:
                times_str = ", ".join(
                    n.replace('SmartStudyAgent_Sync_', '')[:2] + ':' + n.replace('SmartStudyAgent_Sync_', '')[2:]
                    for n in daily_names)
                st.success(f"✅ {len(daily_names)} משימות מותקנות — שעות הרצה: **{times_str}**")
            elif sched_known:
                st.info("ℹ️ משימה ישנה (HOURLY) מותקנת — מומלץ לעבור ל-DAILY (08:00 + 18:00)")
            else:
                st.info("ℹ️ אין משימות מתוזמנות — הסנכרון רק ידני")
        with scol2:
            if st.button("הסר הכל", key='sched_uninstall_all', use_container_width=True, disabled=not sched_known):
                try:
                    r = _sched_uninstall()
                    if r['uninstalled']:
                        st.success(f"הוסרו {len(r['deleted'])} משימות")
                    else:
                        st.error(f"כשל: {r['failed']}")
                    st.rerun()
                except Exception as e:
                    st.error(f"כשל: {e}")

        ic1, ic2, ic3 = st.columns([2, 2, 1], vertical_alignment="bottom")
        with ic1:
            t1 = st.text_input("שעה ראשונה", value="08:00", key='sched_t1', help="פורמט HH:MM (24h)")
        with ic2:
            t2 = st.text_input("שעה שנייה", value="18:00", key='sched_t2', help="ריק = פעם אחת ביום")
        with ic3:
            if st.button("📅 התקן", key='sched_install_daily', use_container_width=True, type="primary"):
                times = [t for t in (t1.strip(), t2.strip()) if t]
                if not times:
                    st.error("לפחות שעה אחת נדרשת")
                else:
                    try:
                        r = _sched_install_daily(times)
                        if r['installed']:
                            st.success(f"הותקנו {len(r['tasks'])} משימות: " + ", ".join(r['times']))
                        else:
                            err_lines = [f"{t['name']}: {t['stderr'][:120]}" for t in r['tasks'] if not t['ok']]
                            st.error("חלק נכשלו:\n" + "\n".join(err_lines))
                        st.rerun()
                    except Exception as e:
                        st.error(f"כשל: {e}")

        try:
            from config.paths import DATA_DIR
            slog = DATA_DIR / 'sync_log.json'
            if slog.exists():
                entries = json.loads(slog.read_text(encoding='utf-8'))
                if entries:
                    last = entries[-1]
                    st.caption(f"סנכרון אחרון: {last.get('finished_at', '—')[:16]} · "
                               f"{last.get('n_new', 0)} פריטים · ${last.get('total_cost_usd', 0):.3f}")
        except Exception:
            pass


def _run_now() -> None:
    section("הרץ עכשיו", "הריצה תפעל ברקע — עקוב אחר ההתקדמות ביומן למטה", icon="▶️")
    run_col, stop_col = st.columns([4, 1])
    with run_col:
        if st.button("▶️ הרץ עדכון שבועי", type="primary", use_container_width=True):
            LAST_RUN_PATH.write_text(str(time.time()))
            core.launch_run(['scripts/run_now.py'])
            st.success("✅ הריצה הושקה ברקע — עקוב ב-log למטה")
            st.rerun()
    with stop_col:
        if st.button("⏹ עצור", use_container_width=True, help="הורג כל תהליך pipeline פעיל"):
            killed = []
            try:
                # Windows: taskkill every python running main.py / run_now.py / scripts/
                result = subprocess.run(
                    ['powershell', '-Command',
                     'Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like "*main.py*" -or $_.CommandLine -like "*run_now*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }'],
                    capture_output=True, text=True, timeout=10)
                killed = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
            except Exception as ke:
                st.error(f"שגיאה בעצירה: {ke}")
            if killed:
                st.warning(f"⏹ הופסקו תהליכים: {', '.join(killed)}")
            else:
                st.info("אין ריצה פעילה לעצור")
            st.rerun()


def _send() -> None:
    section("שלח סיכומים", "שלח סיכומים שנוצרו ולא נשלחו עדיין", icon="📤")
    with st.container(border=True, key="card_run_send"):
        send_course_sel = st.selectbox("קורס לשליחה:", ["כל הקורסים"] + list(COURSE_NAMES.values()),
                                       key="send_course_sel")
        course_key_map = {v: k for k, v in COURSE_NAMES.items()}
        keys_to_send = (list(COURSE_NAMES.keys()) if send_course_sel == "כל הקורסים"
                        else [course_key_map.get(send_course_sel, '')])
        c1, c2 = st.columns(2)
        with c1:
            if st.button("📨 שלח רק אליי", key="run_send_me", use_container_width=True,
                         help=f"שולח לכתובת {core.get_reviewer_email()} בלבד"):
                reviewer = core.get_reviewer_email()
                sent_n, fail_n = 0, 0
                for s in get_all_summaries():
                    if s.get('course') not in keys_to_send:
                        continue
                    if core.send_summary_to(s, reviewer):
                        sent_n += 1
                    else:
                        fail_n += 1
                if sent_n:
                    st.success(f"✅ נשלחו {sent_n} סיכומים אליך ({reviewer})")
                elif fail_n == 0:
                    st.info("אין סיכומים לשליחה")
                if fail_n:
                    st.error(f"❌ {fail_n} נכשלו")
        with c2:
            if st.button("📢 שלח לכולם", key="run_send_all", use_container_width=True, type="primary",
                         help="שולח לכל הנמענים ברשימה (recipients_full.json)"):
                sent_n, fail_n = 0, 0
                for s in get_all_summaries():
                    if s.get('course') not in keys_to_send:
                        continue
                    for addr in core.get_all_emails_for_course(s.get('course', '')):
                        if core.send_summary_to(s, addr):
                            sent_n += 1
                        else:
                            fail_n += 1
                if sent_n:
                    st.success(f"✅ נשלחו {sent_n} מיילים לכל הנמענים")
                elif fail_n == 0:
                    st.info("אין סיכומים לשליחה")
                if fail_n:
                    st.error(f"❌ {fail_n} נכשלו")


def _per_course() -> None:
    section("עדכן קורס ספציפי", icon="🎯")
    keys = list(COURSE_NAMES.items())
    per_row = 3
    for i in range(0, len(keys), per_row):
        cols = st.columns(per_row)
        for col, (course_key, course_name) in zip(cols, keys[i:i + per_row]):
            with col:
                if st.button(f"▶ עדכן {course_name}", key=f"run_{course_key}", use_container_width=True):
                    LAST_RUN_PATH.write_text(str(time.time()))
                    core.launch_run(['main.py', '--course', course_key])
                    st.success(f"הושק: {course_name}")
                    st.rerun()


def _log() -> None:
    section("יומן ריצה", icon="📜")
    last_log = core.get_last_log_path()
    if not last_log:
        st.caption("אין ריצה קודמת — לחץ על אחד מהכפתורים למעלה כדי להתחיל.")
        return
    active = core.is_run_active(last_log)
    status_col, refresh_col = st.columns([4, 1], vertical_alignment="center")
    with status_col:
        if active:
            st.markdown(f"🟢 **רץ כעת** — `{last_log.name}`")
        else:
            try:
                lc = last_log.read_text(encoding='utf-8', errors='replace')
                if '❌ שגיאה קריטית' in lc:
                    st.markdown(f"🔴 **נכשל** — `{last_log.name}`")
                else:
                    st.markdown(f"✅ **הושלם** — `{last_log.name}`")
            except Exception:
                st.markdown(f"⚪ **הסתיים** — `{last_log.name}`")
    with refresh_col:
        if st.button("🔄 רענן log", key="refresh_log", use_container_width=True):
            st.rerun()
    try:
        log_content = last_log.read_text(encoding='utf-8', errors='replace')

        def _phase_class(lc: str, start_kw: str, done_kw: str, err_kw: str = '❌') -> str:
            if err_kw and err_kw in lc:
                return 'phase-error'
            if done_kw in lc:
                return 'phase-done'
            if start_kw in lc:
                return 'phase-active'
            return 'phase-idle'

        p1 = _phase_class(log_content, 'שלב 1', 'שלב 2')
        p2 = _phase_class(log_content, 'שלב 2', 'שלב 3')
        p3 = _phase_class(log_content, 'שלב 3', '✅ נשלחו')
        icons = {'phase-idle': '⬜', 'phase-active': '🔵', 'phase-done': '✅', 'phase-error': '🔴'}
        labels = {'phase-idle': 'ממתין', 'phase-active': 'פועל...', 'phase-done': 'הושלם', 'phase-error': 'נכשל'}
        html(
            '<div class="phase-bar">'
            f'<div class="phase-item {p1}">{icons[p1]} איסוף חומרים<br><small>{labels[p1]}</small></div>'
            f'<div class="phase-item {p2}">{icons[p2]} עיבוד וסיכום<br><small>{labels[p2]}</small></div>'
            f'<div class="phase-item {p3}">{icons[p3]} שליחה במייל<br><small>{labels[p3]}</small></div>'
            '</div>'
        )
        lines = log_content.splitlines()
        tail = "\n".join(lines[-200:]) if len(lines) > 200 else log_content
        st.code(tail or "(ריק)", language="text")
    except Exception as e:
        st.error(f"שגיאה בקריאת log: {e}")
    if active:
        st.caption("💡 הריצה פועלת ברקע. הדף לא מתעדכן אוטומטית — לחץ 🔄 רענן log לעדכון.")


def _obsidian() -> None:
    section("Obsidian Sync", "מייצא סיכומים, חידונים ומטלות ל-Obsidian Vault", icon="📓")
    c1, c2 = st.columns([3, 1], vertical_alignment="center")
    with c2:
        if st.button("🔄 סנכרן עכשיו", key="obs_sync", use_container_width=True):
            with st.spinner("מסנכרן ל-Obsidian..."):
                try:
                    orig = os.getcwd()
                    os.chdir(str(ROOT))
                    try:
                        from exporters.obsidian_exporter import sync_all
                        res = sync_all(force=True)
                    finally:
                        os.chdir(orig)
                    st.success(f"✅ {res['summaries']} סיכומים, {res['quizzes']} חידונים — Vault מעודכן")
                except Exception as e:
                    st.error(f"❌ {e}")
    with c1:
        st.caption(f"עדכון אחרון: {load_last_run()}")


def render() -> None:
    page_header("הרצה", "הפעלה ידנית של הצינור, שליחה, יומן ריצה וסנכרון Obsidian",
                icon="▶️", eyebrow="מערכת")
    _run_now()
    _log()
    _send()
    _per_course()
    _obsidian()
    _scheduler()
    st.markdown("---")
    if st.button("🔄 רענן דשבורד", use_container_width=False):
        core.refresh()
    core.mark("run:render")
