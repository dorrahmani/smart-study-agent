"""
dashboard/core.py — shared, UI-agnostic helpers for the dashboard views.

Moved out of the old single-file app.py WITHOUT changing behaviour: roles and
the fail-closed password gate, data loading, recipients, approvals, e-mail
sending, background runs, alerts. New read-only status helpers for the home
page (pipeline health, monthly budget, semester) live at the bottom.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.parser import (  # noqa: E402
    get_all_summaries, get_totals, load_last_run,
    load_json, save_json, COURSE_NAMES, LAST_RUN_PATH,
)


# ─── profiling (DASH_PROFILE=1) ──────────────────────────────────────────────
# Section timers: each mark() records the time since the previous mark. With
# DASH_PROFILE=1 the marks are appended to <tmp>/dash_marks.txt (on the VM that
# is /tmp/dash_marks.txt) and shown to the admin at the bottom of the page.

PROFILE = os.environ.get("DASH_PROFILE") == "1"


def mark_reset() -> None:
    st.session_state["_dash_marks"] = [("start", time.perf_counter())]


def mark(label: str) -> None:
    if not PROFILE:
        return
    marks = st.session_state.setdefault("_dash_marks", [("start", time.perf_counter())])
    marks.append((label, time.perf_counter()))


def marks_report() -> list[tuple[str, float]]:
    marks = st.session_state.get("_dash_marks") or []
    return [(marks[i][0], marks[i][1] - marks[i - 1][1]) for i in range(1, len(marks))]


def marks_flush(page: str) -> None:
    if not PROFILE:
        return
    import tempfile
    rep = marks_report()
    total = sum(d for _, d in rep)
    line = (f"{datetime.now().isoformat(timespec='seconds')} page={page} total={total:.2f}s "
            + " ".join(f"{k}={d:.2f}" for k, d in rep))
    try:
        with open(Path(tempfile.gettempdir()) / "dash_marks.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── roles + password gate ───────────────────────────────────────────────────
# Two roles supported:
#   'admin'  — full access (env: DASHBOARD_PASSWORD)
#   'friend' — read-only summaries for intro_to_cs + homework checker
#              (env: DASHBOARD_FRIEND_PASSWORD)

ROLE_PERMISSIONS = {
    'admin':  {'courses': None, 'tabs': None},  # None = all
    'friend': {
        'courses': ['intro_to_cs'],
        'tabs': ['summaries', 'professor'],  # professor tab holds the HW checker
    },
}


def _users() -> dict:
    """Password → role, read from the environment on every run."""
    users = {}
    if os.environ.get('DASHBOARD_PASSWORD'):
        users[os.environ['DASHBOARD_PASSWORD']] = 'admin'
    if os.environ.get('DASHBOARD_FRIEND_PASSWORD'):
        users[os.environ['DASHBOARD_FRIEND_PASSWORD']] = 'friend'
    return users


def user_role() -> str:
    """Returns the current user's role ('admin' / 'friend'). Empty if not logged in."""
    return st.session_state.get('role', '')


def can_see_course(course_key: str) -> bool:
    perms = ROLE_PERMISSIONS.get(user_role(), {})
    allowed = perms.get('courses')
    return allowed is None or course_key in allowed


def can_see_tab(tab_key: str) -> bool:
    perms = ROLE_PERMISSIONS.get(user_role(), {})
    allowed = perms.get('tabs')
    return allowed is None or tab_key in allowed


# 07/11 audit A13: a guess cost nothing. Counted in the process and not the session, which a new tab replaces. One count for
# everyone: behind the proxy every visitor is 127.0.0.1, and the only way in is the tailnet, so locking a guesser out is cheap.
FREE_TRIES = 3
MAX_WAIT = 300
_logins = {'fails': 0, 'last': 0.0}
_logins_lock = threading.Lock()


def login_wait(now: float | None = None) -> int:
    """Seconds until the next try counts. 0 = go."""
    with _logins_lock:
        n, last = _logins['fails'], _logins['last']
    if n < FREE_TRIES:
        return 0
    return max(0, math.ceil(last + min(MAX_WAIT, 2 ** (n - FREE_TRIES + 1)) - (now if now is not None else time.time())))


def login_result(ok: bool, now: float | None = None) -> None:
    with _logins_lock:
        _logins.update(fails=0 if ok else _logins['fails'] + 1, last=now if now is not None else time.time())


def check_password() -> bool:
    """Show login form; return True only after correct password entered.

    FAIL-CLOSED: with no password configured nobody gets in (st.stop()).
    """
    from dashboard.ui.components import html, esc, BRAND_SVG
    users = _users()
    if not users:
        # fail CLOSED. קודם זה נתן admin לכל אחד כשמשתני הסביבה חסרים —
        # מסוכן אם הדשבורד נחשף אי פעם לרשת (קרה: 8501 היה פתוח לאינטרנט).
        st.error(
            '🔒 אין סיסמאות מוגדרות — הגישה חסומה.\n\n'
            'הגדר `DASHBOARD_PASSWORD` (ואופציונלי `DASHBOARD_FRIEND_PASSWORD`) '
            'ב-`.env` והפעל מחדש: `sudo systemctl restart dashboard`'
        )
        st.stop()
    if st.session_state.get('auth_ok') and st.session_state.get('role') in ROLE_PERMISSIONS:
        return True
    # Login screen
    html(
        '<div style="max-width:420px;margin:9vh auto 0;text-align:center">'
        f'<div class="ssa-brand-mark" style="width:56px;height:56px;border-radius:16px;margin:0 auto 16px">{BRAND_SVG}</div>'
        '<div class="ssa-ph-title" role="heading" aria-level="1">Smart Study Agent</div>'
        f'<div class="ssa-ph-sub">{esc("דשבורד פרטי — נדרשת סיסמה")}</div></div>'
    )
    _, col, _ = st.columns([1, 1.4, 1])
    with col:
        with st.container(border=True, key="card_login"):
            with st.form("login_form", border=False):
                pw = st.text_input('סיסמה', type='password', key='_pw_in', placeholder='הקלד סיסמה')
                ok = st.form_submit_button('כניסה', use_container_width=True, type='primary')
            if ok:
                wait = login_wait()
                role = None if wait else users.get(pw)         # while it waits, not even a wrong/right answer
                if wait:
                    st.error(f'יותר מדי ניסיונות. אפשר לנסות שוב בעוד {wait} שניות.')
                elif role:
                    login_result(True)
                    st.session_state['auth_ok'] = True
                    st.session_state['role'] = role
                    st.rerun()
                else:
                    login_result(False)
                    st.error('סיסמה שגויה.')
    return False


def logout() -> None:
    for k in ('auth_ok', 'role', '_pw_in'):
        st.session_state.pop(k, None)
    st.rerun()


# ─── data loading ────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_data():
    summaries = get_all_summaries()
    totals = get_totals(summaries)
    return summaries, totals


def refresh():
    st.cache_data.clear()
    st.rerun()


# ─── recipients / budget file helpers ────────────────────────────────────────

RECIPIENTS_PATH = Path("config/recipients.json")
BUDGET_PATH     = Path("config/api_budget.json")


def load_recipients() -> dict:
    return load_json(RECIPIENTS_PATH, {k: [] for k in COURSE_NAMES} | {"global": []})


def save_recipients(r: dict):
    save_json(RECIPIENTS_PATH, r)


def load_budget() -> dict:
    return load_json(BUDGET_PATH, {"claude_total_loaded": 0.0, "openai_total_loaded": 0.0})


def save_budget(b: dict):
    save_json(BUDGET_PATH, b)


def source_badges(sources: dict) -> str:
    parts = []
    t = sources.get('transcript')
    if t == 'Panopto':
        parts.append('<span class="badge badge-panopto" title="תמלול אוטומטי מ-Panopto">🎙 Panopto</span>')
    elif t == 'Whisper':
        parts.append('<span class="badge badge-whisper" title="תמלול בינה מלאכותית (OpenAI Whisper)">🤖 Whisper</span>')

    p = sources.get('pdf')
    if p == 'Moodle':
        parts.append('<span class="badge badge-moodle" title="PDF מ-Moodle">📄 Moodle</span>')
    elif p == 'Wikidot':
        parts.append('<span class="badge badge-wikidot" title="PDF מ-Wikidot">🌐 Wikidot</span>')
    elif p == 'ספר':
        parts.append('<span class="badge badge-book" title="ספר קורס סטטי">📖 ספר</span>')
    elif p:
        parts.append(f'<span class="badge badge-moodle">📄 {p}</span>')

    bf = sources.get('board_frames')
    if bf:
        if bf <= 1:
            parts.append('<span class="badge badge-board" title="צילומי לוח נותחו">📷 לוח</span>')
        else:
            parts.append(f'<span class="badge badge-board" title="{bf} פריימים של הלוח נותחו">📷 {bf} פריימים</span>')

    return " ".join(parts)


def parse_email_list(raw: str) -> list:
    """מחזיר רשימת כתובות מייל תקינות מטקסט (מופרדות בפסיק, נקודה-פסיק, או שורה)"""
    addrs = [a.strip() for a in re.split(r'[,;\n]', raw or '') if a.strip()]
    return [a for a in addrs if re.match(r'[^@\s]+@[^@\s]+\.[^@\s]+', a)]


# ─── background runs ─────────────────────────────────────────────────────────

PYTHON314 = r"C:\Python314\python.exe"
LOGS_DIR  = ROOT / "logs"
LAST_LOG_PATH = LOGS_DIR / ".last_log"


def launch_run(args: list):
    """מריץ את הפייפליין ב-background בלי חלון. Log נכתב לקובץ שהדשבורד מציג."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOGS_DIR / f"run_{ts}.log"
    LAST_LOG_PATH.write_text(str(log_path), encoding='utf-8')

    cmd = [PYTHON314, "-u"] + args  # -u = unbuffered

    # Windows: CREATE_NO_WINDOW + DETACHED_PROCESS → תהליך רץ ברקע בלי חלון ונשאר חי
    creationflags = 0
    if os.name == 'nt':
        creationflags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS

    log_file = open(log_path, 'w', encoding='utf-8', errors='replace', buffering=1)
    log_file.write(f"$ {' '.join(cmd)}\n\n")
    log_file.flush()

    env = os.environ.copy()
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'

    subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        env=env,
        close_fds=True,
    )
    return str(log_path)


def get_last_log_path() -> Path | None:
    if LAST_LOG_PATH.exists():
        p = Path(LAST_LOG_PATH.read_text(encoding='utf-8').strip())
        if p.exists():
            return p
    return None


def is_run_active(log_path: Path) -> bool:
    """מזהה אם הריצה עדיין פעילה לפי זה שה-log קיים ולא הסתיים במילת סיום."""
    try:
        content = log_path.read_text(encoding='utf-8', errors='replace')
        if '✅ הושלם!' in content or '❌ שגיאה קריטית' in content:
            return False
        # נחשב פעיל אם השתנה ב-5 דקות האחרונות
        age = time.time() - log_path.stat().st_mtime
        return age < 300
    except Exception:
        return False


# ─── recipients (full list) ──────────────────────────────────────────────────

def load_all_recipients() -> dict:
    """טוען recipients_full.json (הרשימה המלאה עם כל הנמענים)"""
    full_path = ROOT / 'config' / 'recipients_full.json'
    base_path = ROOT / 'config' / 'recipients.json'
    for p in [full_path, base_path]:
        if p.exists():
            try:
                return json.load(open(p, encoding='utf-8'))
            except Exception:
                pass
    return {}


def get_reviewer_email() -> str:
    rec = load_all_recipients()
    from config.paths import owner_email
    return rec.get('reviewer') or owner_email()


def get_all_emails_for_course(course_key: str) -> list[str]:
    """מחזיר רשימת כל הנמענים לקורס (global + course-specific), ללא כפילויות"""
    rec = load_all_recipients()
    emails = list(rec.get('global', []))
    emails += rec.get(course_key, [])
    seen, result = set(), []
    for e in emails:
        if e and e not in seen:
            seen.add(e)
            result.append(e)
    return result


# ─── approval log ────────────────────────────────────────────────────────────

APPROVAL_LOG_PATH = ROOT / 'config' / 'approval_log.json'


def load_approval_log() -> dict:
    if APPROVAL_LOG_PATH.exists():
        try:
            return json.load(open(APPROVAL_LOG_PATH, encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_approval_log(log: dict):
    APPROVAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(APPROVAL_LOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def pending_approvals() -> list[dict]:
    """מחזיר רשימת סיכומים שנשלחו ל-reviewer אבל טרם אושרו לשליחה לכולם"""
    log = load_approval_log()
    return [
        {'key': k, **v}
        for k, v in log.items()
        if v.get('status') == 'pending'
    ]


def approve_and_send_all(entry: dict) -> tuple[int, int]:
    """מאשר סיכום ושולח לכל הנמענים. מחזיר (sent, failed)."""
    course_key = entry.get('course_key', '')
    md_path    = ROOT / entry.get('md_path', '')
    pdf_path   = ROOT / entry.get('pdf_path', '') if entry.get('pdf_path') else None
    course_name = entry.get('course_name', '')
    title       = entry.get('title', '')
    recipients  = get_all_emails_for_course(course_key)

    sent, failed = 0, 0
    try:
        from generators.gmail_sender import GmailSender
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=ROOT / '.env', override=True)
        orig = os.getcwd()
        os.chdir(str(ROOT))
        try:
            mailer = GmailSender()
            ok = mailer.send_summary(
                course_name=course_name,
                lecture_title=title,
                summary_md_path=str(md_path),
                summary_pdf_path=str(pdf_path) if pdf_path and pdf_path.exists() else None,
                recipients=recipients,
            )
            if ok:
                sent = len(recipients)
            else:
                failed = len(recipients)
        finally:
            os.chdir(orig)
    except Exception as e:
        st.error(f"שגיאה בשליחה: {e}")
        failed = len(recipients)

    if sent > 0:
        # עדכן approval_log → approved
        log = load_approval_log()
        if entry['key'] in log:
            log[entry['key']]['status'] = 'approved'
            log[entry['key']]['sent_to_all'] = datetime.now().isoformat()
            save_approval_log(log)

    return sent, failed


def skip_approval(entry: dict) -> None:
    """סמן כ'אושר' ללא שליחה (status=skipped)."""
    alog = load_approval_log()
    if entry['key'] in alog:
        alog[entry['key']]['status'] = 'skipped'
        alog[entry['key']]['sent_to_all'] = datetime.now().isoformat()
        save_approval_log(alog)


def send_summary_to(summary: dict, recipient: str) -> bool:
    """שולח סיכום ספציפי לנמען"""
    try:
        from generators.gmail_sender import GmailSender
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=ROOT / '.env', override=True)

        # resolve paths relative to project root
        md_path = summary['path']
        md_abs = ROOT / md_path if not Path(md_path).is_absolute() else Path(md_path)
        pdf_abs = Path(str(md_abs).replace('.md', '.pdf'))

        if not md_abs.exists():
            st.error(f"קובץ לא נמצא: {md_abs}")
            return False

        # run GmailSender from project root so token/credentials paths work
        orig_cwd = os.getcwd()
        os.chdir(str(ROOT))
        try:
            mailer = GmailSender()
            return mailer.send_summary(
                course_name=summary['course_name'],
                lecture_title=summary['title'],
                summary_md_path=str(md_abs),
                summary_pdf_path=str(pdf_abs) if pdf_abs.exists() else None,
                recipient=recipient
            )
        finally:
            os.chdir(orig_cwd)
    except Exception as e:
        st.error(f"שגיאה בשליחה: {e}")
        return False


# ─── alerts (old "stale run" banner) ─────────────────────────────────────────

def days_since_last_run() -> float:
    if not LAST_RUN_PATH.exists():
        return 999
    try:
        from datetime import timezone
        ts = float(LAST_RUN_PATH.read_text().strip())
        return (datetime.now(tz=timezone.utc).timestamp() - ts) / 86400
    except Exception:
        return 999


def collect_alerts() -> list:
    alerts = []
    try:
        days_idle = days_since_last_run()
        if days_idle >= 999:  # sentinel: no data/.last_run at all
            alerts.append(('warning', "אין רישום של ריצה קודמת — לחץ על ▶️ הרץ עדכון שבועי"))
        elif days_idle >= 5:
            alerts.append(('warning', f"לא עודכן מזה **{days_idle:.0f} ימים** — לחץ על ▶️ הרץ עדכון שבועי"))

        summaries_snap, _ = load_data()

        missing_board = [s for s in summaries_snap
                         if s['type_folder'] == 'lectures' and not s['sources'].get('board_frames')]
        if missing_board:
            titles = ", ".join(s['title'] for s in missing_board[:3])
            more = f" ועוד {len(missing_board)-3}" if len(missing_board) > 3 else ""
            alerts.append(('info', f"{len(missing_board)} הרצאות ללא צילומי לוח: {titles}{more}"))
    except Exception:
        pass
    return alerts


# ─── NEW read-only status helpers (home page) ────────────────────────────────

HEB_DAYS = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
HEB_MONTHS = ['ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני', 'יולי',
              'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר']


def hebrew_date(d: datetime | date | None = None) -> str:
    d = d or datetime.now()
    return f"יום {HEB_DAYS[d.weekday()]}, {d.day} ב{HEB_MONTHS[d.month - 1]} {d.year}"


def greeting(now: datetime | None = None) -> str:
    h = (now or datetime.now()).hour
    if 5 <= h < 12:
        return "בוקר טוב"
    if 12 <= h < 17:
        return "צהריים טובים"
    if 17 <= h < 22:
        return "ערב טוב"
    return "לילה טוב"


@st.cache_data(ttl=600, show_spinner=False)
def courses_config() -> dict:
    try:
        from utils.semester import load_config
        return load_config(ROOT)
    except Exception:
        return {}


def semester_info(today: date | None = None) -> dict:
    """{'label', 'tone', 'detail'} derived from settings.schedule in courses.yaml."""
    today = today or date.today()
    sched = ((courses_config().get('settings') or {}).get('schedule') or {})

    def _d(v):
        try:
            return datetime.strptime(str(v), '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return None

    start, end = _d(sched.get('semester_start')), _d(sched.get('semester_end'))
    if start and today < start:
        n = (start - today).days
        return {'label': f"הסמסטר מתחיל בעוד {n} ימים", 'tone': 'primary',
                'detail': f"פתיחה {start.strftime('%d/%m/%Y')}"}
    if start and end and start <= today <= end:
        week = (today - start).days // 7 + 1
        total = (end - start).days // 7 + 1
        return {'label': f"שבוע {week} מתוך {total}", 'tone': 'success',
                'detail': f"סיום {end.strftime('%d/%m/%Y')}"}
    if end and today > end:
        return {'label': "תקופת מבחנים", 'tone': 'warning',
                'detail': f"הסמסטר הסתיים {end.strftime('%d/%m/%Y')}"}
    return {'label': "סמסטר", 'tone': '', 'detail': ''}


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def pipeline_status() -> dict:
    """Pipeline health from the files the VM already writes (read-only).

    tone: success / warning / danger / '' (unknown)
    """
    from config.paths import DATA_DIR
    out = {'tone': '', 'title': 'לא ידוע', 'detail': '', 'finished': '', 'failed': [],
           'last_run': load_last_run(), 'warning': None}
    rec = _read_json(DATA_DIR / 'last_pipeline_run.json') or {}
    fin = rec.get('finished') or ''
    out['finished'] = fin
    out['failed'] = rec.get('failed_steps') or []
    try:
        from utils.pipeline_health import missed_run_warning
        out['warning'] = missed_run_warning(DATA_DIR, courses_config())
    except Exception:
        out['warning'] = None

    if out['warning']:
        out['tone'] = 'danger' if out['warning'].startswith('🚨') else 'warning'
        out['title'] = 'דורש תשומת לב'
        out['detail'] = out['warning'].lstrip('🚨⚠️ ').strip()
    elif fin:
        out['tone'] = 'warning' if out['failed'] else 'success'
        out['title'] = 'תקין' if not out['failed'] else f"{len(out['failed'])} שלבים נכשלו"
        try:
            out['detail'] = f"ריצה אחרונה {datetime.fromisoformat(fin).strftime('%d/%m %H:%M')}"
        except ValueError:
            out['detail'] = f"ריצה אחרונה {fin[:16]}"
    else:
        days = days_since_last_run()
        if days < 999:
            out['tone'] = 'success' if days < 2 else ('warning' if days < 5 else 'danger')
            out['title'] = 'תקין' if days < 2 else f"לא רץ {days:.0f} ימים"
        out['detail'] = f"עדכון אחרון {out['last_run']}"
    return out


def last_sync() -> dict | None:
    from config.paths import DATA_DIR
    log = _read_json(DATA_DIR / 'sync_log.json')
    if isinstance(log, list) and log:
        return log[-1]
    return None


def _budget_limits() -> tuple[float, float]:
    """(soft, hard) read from scripts/budget_check.py WITHOUT importing it
    (importing that script runs the check and may send WhatsApp alerts)."""
    soft, hard = 20.0, 50.0
    try:
        src = (ROOT / 'scripts' / 'budget_check.py').read_text(encoding='utf-8')
        m = re.search(r'^HARD_LIMIT_USD\s*=\s*([0-9.]+)', src, re.M)
        if m:
            hard = float(m.group(1))
        m = re.search(r'^SOFT_LIMIT_USD\s*=\s*([0-9.]+)', src, re.M)
        if m:
            soft = float(m.group(1))
    except Exception:
        pass
    return soft, hard


@st.cache_data(ttl=120, show_spinner=False)
def _usage_month() -> dict:
    try:
        from utils.usage_tracker import summary
        return summary('month')
    except Exception:
        return {}


@st.cache_data(ttl=120, show_spinner=False)
def usage_daily_this_month() -> dict[str, float]:
    """{YYYY-MM-DD: usd} from data/usage.jsonl (same ledger budget_check sums)."""
    out: dict[str, float] = {}
    try:
        from utils.usage_tracker import _iter_records, _parse_since
        for r in _iter_records(_parse_since('month')):
            ts = str(r.get('ts') or r.get('t') or r.get('time') or '')[:10]
            if len(ts) == 10:
                out[ts] = out.get(ts, 0.0) + float(r.get('cost_usd', 0) or 0)
    except Exception:
        pass
    return dict(sorted(out.items()))


def budget_status() -> dict:
    soft, hard = _budget_limits()
    s = _usage_month()
    total = float(s.get('total_usd', 0) or 0)
    lock = _read_json(ROOT / 'data' / 'budget_lock.json')
    month_key = datetime.utcnow().strftime('%Y-%m')
    locked = bool(lock) and (lock.get('month') in (None, month_key))
    stale_lock = bool(lock) and not locked
    frac = total / hard if hard else 0
    tone = 'danger' if (locked or total >= hard) else ('warning' if total >= soft else 'success')
    return {
        'total': total, 'soft': soft, 'hard': hard, 'frac': frac, 'tone': tone,
        'locked': locked, 'stale_lock': stale_lock, 'lock': lock or {},
        'n_calls': int(s.get('n_calls', 0) or 0),
        'by_service': s.get('by_service') or {}, 'by_model': s.get('by_model') or {},
        'available': bool(s),
    }


def upcoming_deadlines(limit: int = 6, courses: list[str] | None = None) -> list[dict]:
    """Open vault assignments with a due date, soonest first (read-only)."""
    from config.paths import COURSE_ORDER
    try:
        from dashboard import vault_reader as vr
    except Exception:
        return []
    out = []
    for ck in (courses or COURSE_ORDER):
        if not can_see_course(ck):
            continue
        try:
            items = vr.list_assignments(course=ck)
        except Exception:
            items = []
        for a in items:
            if (a.get("status") or "").lower() in ("pending", "ממתין", "") and a.get("due_date"):
                out.append(a)
    today = date.today()
    res = []
    for a in sorted(out, key=lambda a: str(a.get("due_date") or "")):
        try:
            d = datetime.fromisoformat(str(a["due_date"])[:10]).date()
            delta = (d - today).days
        except Exception:
            delta = None
        if delta is not None and delta < -7:
            continue
        res.append({**a, '_days': delta})
    return res[:limit]
