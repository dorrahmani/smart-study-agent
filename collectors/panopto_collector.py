"""
Panopto Collector - מוריד תמלולים מ-Panopto של TAU
גישה: Playwright לlogin (SAML/SSO) + requests עם cookies לשאר
"""

import os
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass
import io
import json
import re
import time
import requests
from pathlib import Path

from typing import List, Dict, Optional
import yaml
from datetime import datetime, timedelta, timezone


# חייב להתאים ל-_classify_item_type ב-pipelines/sync_all.py
TUTORIAL_TITLE_KW = ('תרגול', 'tut', 'tirgul', 'recitation', 'תרגיל')


class PanoptoCollector:
    BASE_URL = "https://tau.cloud.panopto.eu"

    def __init__(self, config_path: str = "config/courses.yaml"):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        self.username = os.getenv('TAU_USERNAME')
        self.tau_id = os.getenv('TAU_ID', '')
        self.password = os.getenv('TAU_PASSWORD')
        self.output_dir = Path("data/panopto")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})
        self._current_course = None  # לתיוג התראות (pipeline_warn)

    # ─────────────────────────── login ───────────────────────────

    def login(self):
        """התחברות ל-Panopto דרך TAU SSO - Playwright לSSO, cookies לrequests"""
        print("מתחבר ל-Panopto...")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()

            # שלב 1: לך לדף הלוגין של Panopto
            page.goto(f"{self.BASE_URL}/Panopto/Pages/Auth/Login.aspx")
            page.wait_for_load_state('networkidle')

            # שלב 2: לחץ על כפתור SSO (Sign in)
            page.click('#PageContentPlaceholder_loginControl_externalLoginButton')
            page.wait_for_load_state('networkidle')

            # שלב 3: מלא פרטי ההתחברות של TAU
            if 'nidp.tau.ac.il' in page.url or 'tau.ac.il' in page.url:
                page.locator('#Ecom_User_ID').fill(self.username or '')
                if self.tau_id:
                    try:
                        page.locator('#Ecom_User_Pid').fill(self.tau_id)
                    except Exception:
                        pass
                page.locator('#Ecom_Password').fill(self.password or '')
                page.locator('#loginButton').click()
                # Wait for SAML redirect chain to complete
                page.wait_for_timeout(8000)
                page.wait_for_load_state('networkidle')

            # שלב 4: בדוק שהתחברנו ל-Panopto
            page.goto(f"{self.BASE_URL}/Panopto/Pages/Home.aspx")
            page.wait_for_load_state('networkidle')

            if 'login' in page.url.lower() or 'Sign In' in page.title():
                browser.close()
                raise Exception("ההתחברות נכשלה - בדוק TAU_USERNAME ו-TAU_PASSWORD")

            # שלב 5: העבר cookies ל-requests session
            for cookie in ctx.cookies():
                self.session.cookies.set(
                    cookie['name'],
                    cookie['value'],
                    domain=cookie.get('domain', '').lstrip('.')
                )

            browser.close()

        print("התחברות הצליחה!")

    # ─────────────────────────── search sessions ───────────────────────────

    @staticmethod
    def parse_panopto_date(raw: str) -> Optional[datetime]:
        """
        מפרסר תאריך Panopto — תומך בשני פורמטים:
          /Date(1776236400000)/  — .NET JSON timestamp (milliseconds)
          2026-04-14T10:00:00   — ISO 8601
        """
        import re as _re
        if not raw:
            return None
        m = _re.search(r'/Date\((-?\d+)\)/', raw)
        if m:
            return datetime.utcfromtimestamp(int(m.group(1)) / 1000)
        try:
            return datetime.fromisoformat(raw[:19])
        except Exception:
            return None

    def search_sessions(self, query: str, days_back: int = 2,
                        folder_id: str = None) -> List[Dict]:
        """
        חיפוש הרצאות לפי שם קורס ו/או folder_id.
        סינון לפי תאריך נעשה client-side (Panopto מחזיר /Date(ms)/ שלא עובד עם startDate API).
        """
        payload = {
            "queryParameters": {
                "query": query,
                "sortColumn": 1,        # מיון לפי תאריך
                "sortAscending": False,
                "maxResults": 100,
                "page": 0,
                "startDate": None,
                "endDate": None,
                "folderID": folder_id,
                "bookmarked": False,
                "sessionListOnly": True,
                "isSharedWithMe": False
            }
        }

        r = self.session.post(
            f"{self.BASE_URL}/Panopto/Services/Data.svc/GetSessions",
            json=payload
        )
        data = r.json()
        all_sessions = data.get('d', {}).get('Results', [])
        self._last_unfiltered = all_sessions        # 11/11: older ones tell what an older item of a week was

        # סינון client-side לפי days_back
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        filtered = []
        for s in all_sessions:
            dt = self.parse_panopto_date(s.get('StartTime', ''))
            if dt is None or dt >= cutoff:
                filtered.append(s)

        return filtered

    @staticmethod
    def _clean_title(raw: str) -> str:
        """
        מנקה כותרת Panopto:
        - מסיר HTML tags
        - מסיר קוד קורס בהתחלה (e.g. "1011210304 - Name" → "Name")
        - מסיר קוד קורס מוטבע + כל מה שאחריו
          (e.g. "Hebrew Title1011210304 - English Title" → "Hebrew Title")
        """
        import re as _re
        title = _re.sub(r'<[^>]+>', '', raw).strip()
        # הסר קוד קורס בהתחלה
        title = _re.sub(r'^\d{7,10}\s*-\s*', '', title).strip()
        # הסר קוד קורס מוטבע + כל מה שאחריו (e.g. "Title1011210304 - ...")
        title = _re.sub(r'\d{7,10}\s*-\s*.+$', '', title).strip()
        # נקה תווים מיותרים בסוף
        title = title.rstrip(' -').strip()
        return title

    CURRENT_YEAR_MARKERS = ["2025-2", "2026", "2025"]  # סמסטר ב' תשפ"ו

    def is_current_semester(self, session: Dict) -> bool:
        """בודק שהסרטון שייך לסמסטר הנוכחי (לא שנים קודמות)"""
        folder = session.get('FolderName', '')
        # אם יש folder_id - כבר מסוננים לתיקייה הנכונה, לא צריך לסנן
        if session.get('FolderID'):
            return True
        # אחרת - בדוק שהשם מכיל מזהה של השנה הנוכחית
        return any(marker in folder for marker in self.CURRENT_YEAR_MARKERS)

    # ── סינון לפי מערכת השעות האישית ────────────────────────────────────
    # ב-Panopto מוקלטות *כל* הקבוצות של הקורס. `my_sessions` בקונפיג (מיומן
    # Google של המשתמש) אומר באיזה יום+שעה נמצאת *הקבוצה שלו*, ולכן כל הקלטה
    # מחוץ לסלוטים האלה היא קבוצה אחרת ונזרקת. עמיד יותר משמות מרצים, ועובד
    # גם בשבוע הראשון של קורס חדש (אין היסטוריה להסתמך עליה).
    _DAY_TO_PYWD = {'sun': 6, 'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3,
                    'fri': 4, 'sat': 5}

    @staticmethod
    def _to_israel_local(dt):
        """parse_panopto_date מחזיר UTC; ממיר לשעון קיר ישראלי."""
        from datetime import timezone, timedelta
        try:
            from zoneinfo import ZoneInfo
            return (dt.replace(tzinfo=timezone.utc)
                      .astimezone(ZoneInfo('Asia/Jerusalem'))
                      .replace(tzinfo=None))
        except Exception as e:
            # קירוב רק אם אין tzdata. שעון קיץ בישראל: יום שישי שלפני
            # יום ראשון האחרון של מרץ → יום ראשון האחרון של אוקטובר.
            # (חשוב: 2026-10-25 נופל *בתוך* הסמסטר — קירוב "כל אוקטובר=+3"
            #  היה מזיז הרצאות של סוף אוקטובר בשעה ומפיל אותן מהסינון.)
            print(f"⚠️  tzdata לא זמין ({e}) — קירוב שעון ישראל", flush=True)
            import calendar as _cal
            last_sun = max(d for d in range(25, 32)
                           if _cal.weekday(dt.year, 10, d) == _cal.SUNDAY) \
                if dt.month == 10 else None
            if dt.month in (4, 5, 6, 7, 8, 9):
                off = 3
            elif dt.month == 10:
                off = 3 if dt.day < last_sun else 2
            elif dt.month == 3:
                off = 3 if dt.day >= 27 else 2
            else:
                off = 2
            return dt + timedelta(hours=off)

    @staticmethod
    def _collected_weeks(course_key: str, session_type: str | None, panopto: Path | None = None) -> set:
        """Sundays (00:00) of the weeks that already have a recording of this course and type in data/panopto."""
        import re as _re
        if not course_key or session_type not in ('lecture', 'tutorial'):
            return set()
        d = panopto or Path(__file__).resolve().parent.parent / 'data' / 'panopto'
        out = set()
        for srt in d.glob(f'{course_key}_*.srt') if d.exists() else []:
            rest = srt.stem[len(course_key) + 1:]
            m = _re.match(r'(\d{4}-\d{2}-\d{2})_(.*)$', rest)
            if not m:
                continue
            is_tut = bool(_re.search(r'תרגול|תרגיל|recitation|tirgul', m.group(2), _re.I))
            if is_tut != (session_type == 'tutorial'):
                continue
            day = datetime.fromisoformat(m.group(1))
            out.add((day - timedelta(days=(day.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0))
        return out

    def drop_foreign(self, sessions: List[Dict], course_key: str) -> List[Dict]:
        """26/10: Software 1 has no folder yet, so it's searched by name — Panopto's search returned micro B's
        lecture ("10112109 - מיקרו כלכלה ב'…"), the other-group fallback took it, and it was summarised and sent
        as "תוכנה 1". A recording whose title/folder names ANOTHER configured course (its number or name) and not
        this one is never this course's."""
        import re as _re

        def norm(t: str) -> str:
            return _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', '', t or '').replace('׳', "'").replace('"', '').replace('״', '')).strip()

        def marks(k: str) -> List[str]:
            c = self.config['courses'].get(k) or {}
            code = str(c.get('course_code') or '').lstrip('0')
            return [m for m in ([code] if len(code) >= 6 else []) + [norm(c.get('name', ''))] if m]
        mine = marks(course_key)
        others = [m for k in self.config.get('courses', {}) if k != course_key for m in marks(k)
                  if m not in mine and not any(m in x for x in mine)]
        kept = []
        for s in sessions:
            text = norm(f"{s.get('SessionName', '')} {s.get('FolderName', '')}")
            if not any(m in text for m in mine) and any(m in text for m in others):
                print(f"🚫 הקלטה של קורס אחר — לא שייכת ל-{course_key}: {s.get('SessionName', '')[:60]}", flush=True)
                continue
            kept.append(s)
        return kept

    @staticmethod
    def _wall(dt):
        """11/11: Panopto's /Date(ms)/ is ALREADY Israel wall-clock time (TAU's account timezone), not UTC — verified on
        the live folders: "Recitation - Omer - 1600-1800" → 16:15, "Igor 1200-1300" → 12:02, probability "תרגול 3 - שעה
        11" → 11:10, in summer and in winter time. Converting it again put every recording 2–3 hours late, so nothing
        matched the owner's slots and the other-group fallback took whatever was newest (probability's 13:00 tutorial
        instead of his 11:00 one)."""
        return dt

    @staticmethod
    def _tutor_tokens(slot: Dict) -> List[str]:
        """'מר ראובן אלמוני' → ['ראובן', 'אלמוני'] — matched against the recording's name."""
        import re as _re
        name = _re.sub(r'(פרופ|ד"ר|דר|גב|מר|Prof|Dr|Mr|Ms|Mrs)[\'׳.]*(?=\s)', ' ', f" {slot.get('lecturer') or ''} ")
        return [w for w in _re.split(r'[\s,()\-]+', name) if len(w) >= 2]

    def filter_by_schedule(self, sessions: List[Dict], my_sessions: List[Dict],
                           session_type: str = None,
                           tolerance_min: int = 45, now_local: datetime | None = None,
                           existing: List[Dict] | None = None, history: List[Dict] | None = None,
                           foreign_hosts: List[str] | None = None) -> List[Dict]:
        """משאיר רק הקלטות שנופלות בסלוטים של המשתמש (יום בשבוע + שעת התחלה).
        11/11 (combined folders — every group of the course in one folder): a slot matches within min(45, half its
        length) of its start; a recording much shorter than the slot (a 3-min false start, the lecturer's 10-min
        upload) is not the class itself; several recordings in one slot → one: the one naming his tutor, else the
        closest start.
        11/11 (The owner: "אין מקצוע שיש לי 2 הרצאות או 2 תרגולים בשבוע"): ONE per week (Sun–Sat) — his slot, else his
        lecturer/tutor on another day (a makeup), else a title saying makeup, else another group's (_other_group);
        a week already covered by his recording (data/panopto, data/summaries) gets nothing; a week covered only by
        another group's gets his, and the old one is moved aside (collectors/weekly_pick). `history` = the folder's
        older recordings (beyond the lookback): they decide what an older item of a week is and whether a recording
        is the previous week's makeup, but only recordings in `sessions` are collected. → the recordings to collect,
        each with _week / _match / _replaces."""
        from collectors import weekly_pick as wp
        if not session_type:
            types = {s.get('type') for s in (my_sessions or []) if s.get('type') in ('lecture', 'tutorial')}
            if not types:
                return sessions
            out = []
            for t in sorted(types):
                out += [s for s in self.filter_by_schedule(sessions, my_sessions, t, tolerance_min, now_local, existing,
                                                                history, foreign_hosts)
                        if s not in out]
            return out
        slots = [s for s in (my_sessions or []) if s.get('type') == session_type]
        if not slots:
            return sessions  # אין שיבוץ מוגדר — לא מסננים
        import re as _re
        now_wall = now_local or self._to_israel_local(datetime.utcnow())
        weekly = [s for s in slots if not s.get('date')]
        others = [s for s in (my_sessions or []) if s.get('type') != session_type]
        me, other = wp.person_for(weekly or slots), wp.person_for(others)
        group = next((str(s.get('group')) for s in weekly or slots if s.get('group')), '')
        try:
            from collectors.course_files import SEMESTER_START
        except Exception:
            SEMESTER_START = None

        def wall(raw):
            d = self.parse_panopto_date(raw)
            return self._wall(d) if d else None
        window = {id(s) for s in sessions}
        seen_v = {self._video_id(s) for s in sessions}
        older = [h for h in (history or []) if self._video_id(h) not in seen_v and wall(h.get('StartTime', ''))]
        p = wp.plan(sessions + older, slots, session_type, now_wall, wall, me, other, group, SEMESTER_START,
                    tolerance_min, foreign_hosts)
        learned = p['foreign'] - {wp.host_key(h) for h in foreign_hosts or ()}
        if learned:
            print(f"👥 קבוצה מקבילה באותה שעה ({session_type}): {', '.join(sorted(learned))} — ההקלטות שלה לא שלי",
                  flush=True)
        p['undated'] = [s for s in p['undated'] if id(s) in window]
        for s in p['undated']:
            # fail-open: בלי תאריך אי אפשר לשפוט — עדיף לשמור ולהתריע
            # מאשר להפיל הרצאה אמיתית בשקט (אין keywords כגיבוי כאן).
            print(f"⚠️  StartTime לא נפרס — שומר ליתר ביטחון: "
                  f"{s.get('SessionName', '?')[:60]}", flush=True)
        for r in p['recs'].values():
            if r['kind'] == 'short' and r['s'].get('Duration'):
                print(f"⏭️  קצרה מדי בשביל השיעור עצמו ({float(r['s']['Duration']) / 60:.0f} דק'): "
                      f"{wp.clean(r['s'].get('SessionName', ''))[:60]}", flush=True)
        his = [r['s'] for r in p['weeks'].values() if r['match'] in wp.MINE and id(r['s']) in window]

        # 11/11: "no recording matches my schedule" only when it's true for the last week: the course has recordings
        # of this type from the last 8 days (a tutorial-titled one isn't a lecture and vice versa; 0-min placeholder
        # items don't count), none of them is his, and the window reaches back a full week (not a folder's day one).
        dates = {id(s): self._wall(d) for s in sessions if (d := self.parse_panopto_date(s.get('StartTime', '')))}
        week_ago = now_wall - timedelta(days=8)
        other_rx = {'lecture': r'תרגול|תרגיל|recitation|tirgul', 'tutorial': r'הרצאה|lecture'}.get(session_type or '')

        def could_be(s: Dict) -> bool:
            name = _re.sub(r'<[^>]+>', '', s.get('SessionName', ''))
            dur = float(s.get('Duration') or 0)
            return not (other_rx and _re.search(other_rx, name, _re.I)) and not ('Duration' in s and dur < 15 * 60)
        recent = [s for s in sessions if id(s) in dates and dates[id(s)] >= week_ago and could_be(s)]
        # 14/11: software 1's tutorial (Zevulun, Tue 16:00) was recorded once in four weeks, probability's lectures are on
        # the lecturer's site: "check if the class moved" every night was false. Only a class that is usually recorded
        # (his in at least half the semester's past weeks) and is missing now is a warning; otherwise one quiet line.
        mine_w, past_w = wp.usual_weeks(p, now_wall, SEMESTER_START)
        usual = bool(past_w) and 2 * len(mine_w) >= len(past_w)
        if recent and min(dates.values()) <= week_ago and not any(
                id(s) in dates and dates[id(s)] >= week_ago for s in his):
            if usual:
                from utils.pipeline_health import pipeline_warn
                pipeline_warn(f"{self._current_course or ''}: אף הקלטה מהשבוע האחרון לא תואמת את המערכת שלי "
                              f"({session_type or 'כל הסוגים'}) מתוך {len(recent)} — בדוק אם השיעור זז ביומן/בקונפיג")
            else:
                print(f"ℹ️  ה{'תרגול' if session_type == 'tutorial' else 'הרצאה'} שלי כמעט לא מוקלט/ת בפנופטו "
                      f"({len(mine_w)} מתוך {len(past_w)} שבועות) — לא התראה", flush=True)
        elif sessions and not his and usual:
            print(f"⚠️  אף הקלטה חדשה לא תואמת את המערכת שלי ({session_type}) מתוך {len(sessions)}", flush=True)

        ck = self._current_course or ''
        if existing is None:
            existing = self._existing_items(ck, session_type)
        rec = wp.reconcile(p, existing, self._video_id, collectable=window)
        self.__dict__.setdefault('_week_actions', {})[(ck, session_type)] = rec
        why = {'slot': 'הסלוט שלי', 'my_tutor': 'המתרגל/המרצה שלי ביום/שעה אחרים', 'makeup': 'שיעור השלמה',
               'other_group': 'קבוצה אחרת'}
        picked = {}
        for r, replaces in rec['collect']:
            s = r['s']
            name = wp.clean(s.get('SessionName', ''))[:60]
            back = ' (השלמה של השבוע הקודם)' if r.get('moved_back') else ''
            print(f"🗓️  שבוע {r['assigned_week']:%d/%m}: {name} — {why[r['match']]}{back}"
                  + (f" — מחליף {', '.join(i['title'][:30] for i in replaces)}" if replaces else ''), flush=True)
            if r['match'] == 'other_group':
                print(f"↪️  אין הקלטה של הקבוצה שלי — לוקח את של קבוצה אחרת: {name}", flush=True)
            picked[id(s)] = {**s, '_week': r['assigned_week'].isoformat(), '_match': r['match'], '_replaces': replaces,
                             '_host': wp.host(s), '_start': r['dt'].isoformat(timespec='minutes'),
                             **({'_other_group': True} if r['match'] == 'other_group' else {})}
        for r, why_not in rec['skip']:
            print(f"⏭️  שבוע {r['assigned_week']:%d/%m} כבר מכוסה — לא אוסף "
                  f"{wp.clean(r['s'].get('SessionName', ''))[:50]}: {why_not}", flush=True)
        if rec['have']:            # 14/11: "15 → 0" with no word said why — the weeks whose pick is already in place
            print(f"✔️  כבר נאספו ({session_type}): "
                  + ', '.join(f"{r['assigned_week']:%d/%m} {wp.clean(r['s'].get('SessionName', ''))[:25]}"
                              + (' (קבוצה אחרת)' if r['match'] == 'other_group' else '')
                              for r in sorted(rec['have'], key=lambda r: r['assigned_week'])), flush=True)
        undated = {id(s) for s in p['undated']}
        return [picked.get(id(s), s) for s in sessions if id(s) in picked or id(s) in undated]   # input order

    def _history(self, window: List[Dict], course_key: str) -> List[Dict]:
        """The last search's recordings beyond the lookback (Panopto returns the folder's newest 100 anyway)."""
        import contextlib
        seen = {self._video_id(s) for s in window}
        older = [s for s in self.__dict__.get('_last_unfiltered') or [] if self._video_id(s) not in seen]
        with contextlib.redirect_stdout(io.StringIO()):
            return self.drop_foreign(older, course_key)

    @staticmethod
    def _video_id(s: Dict) -> str:
        return s.get('DeliveryID') or (s.get('IosVideoUrl') or '').split('/')[-1] or s.get('SessionID') or str(id(s))

    def _done_items(self, course_key: str) -> set:
        """Video ids already collected: in the metadata, not moved aside, and their SRT still in data/panopto."""
        out_dir = Path(getattr(self, 'output_dir', 'data/panopto'))
        try:
            meta = json.loads((out_dir / f'{course_key}_metadata.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return set()
        out = set()
        for m in meta if isinstance(meta, list) else []:
            srt = out_dir / Path(str(m.get('srt_path') or '').replace('\\', '/')).name
            if m.get('video_id') and m.get('srt_path') and not m.get('replaced_by') and srt.is_file() \
                    and srt.stat().st_size > 500:
                out.add(m['video_id'])
        return out

    def _existing_items(self, course_key: str, session_type: str) -> List[Dict]:
        """What this course already has for this type (metadata + SRTs + summaries); [] for a bare test collector."""
        from collectors import weekly_pick as wp
        out_dir = getattr(self, 'output_dir', None)
        if not course_key or out_dir is None:
            return []
        mp = Path(out_dir) / f'{course_key}_metadata.json'
        try:
            meta = json.loads(mp.read_text(encoding='utf-8')) if mp.exists() else []
        except (OSError, ValueError):
            meta = []
        return wp.coverage(course_key, session_type, Path(out_dir), Path(out_dir).parent / 'summaries', meta)

    def _calendar_slots(self, course_key: str, my_sessions: List[Dict], days_back: int, events_on=None) -> List[Dict]:
        """His group's classes of this course on another day/time in the studies calendar (a moved class) → dated
        one-off slots. Nothing (fail-open) without a calendar."""
        try:
            from collectors.weekly_pick import calendar_slots
            from config.paths import COURSE_NAMES
            if events_on is None:
                from integrations import gcal
                if not gcal.configured():
                    return []
                cache = self.__dict__.setdefault('_cal_cache', {})

                def events_on(d):
                    if d not in cache:
                        cache[d] = gcal.events_on(d)
                    return cache[d]
            today = datetime.now().date()
            days = [today - timedelta(days=i) for i in range(min(days_back, 30) + 1)]
            label = (COURSE_NAMES.get(course_key) or self.config['courses'][course_key].get('name', '')).split(' (')[0]
            out = []
            for typ in ('lecture', 'tutorial'):
                weekly = [s for s in my_sessions if s.get('type') == typ and not s.get('date')]
                if not weekly:
                    continue
                moved, cancelled = calendar_slots(label, typ, str(weekly[0].get('group') or ''), weekly, days, events_on)
                for m in moved:
                    print(f"📅 {course_key}: {typ} של הקבוצה שלי ביומן ב-{m['date']} {m['start']} (לא בסלוט הקבוע)",
                          flush=True)
                if cancelled:
                    print(f"📅 {course_key}: {typ} מבוטל ביומן: {', '.join(sorted(d.isoformat() for d in cancelled))}",
                          flush=True)
                out += moved
            return out
        except Exception as e:
            print(f"  ⚠️ calendar slots: {e}", flush=True)
            return []

    def _apply_week_actions(self, course_key: str, replaced: Dict[str, str], log=print, move_srt=None,
                            retire_md=None) -> None:
        """Relabel the week of a recording that turned out to be the previous week's makeup; move aside the extra
        recordings of a week that already has his (only when his is in place); record both in the metadata.
        replaced: video id of an item replaced in this run → the video id that replaced it."""
        from collectors import weekly_pick as wp
        relabel, retired = {}, dict(replaced)
        for (ck, _t), rec in (self.__dict__.get('_week_actions') or {}).items():
            if ck != course_key:
                continue
            relabel.update({v: w.isoformat() for v, w in rec['relabel'].items()})
            for item, keeper in rec['retire']:
                try:
                    wp.retire(item, log, move_srt, retire_md)
                    log(f"♻️  {ck}: {item['date']} {item['title'][:40]} — כבר יש את שלי לשבוע הזה, הועבר ל-replaced/")
                    if item.get('video_id'):
                        retired[item['video_id']] = self._video_id(keeper['s'])
                except Exception as e:
                    log(f"  ⚠️ retire {item.get('title')}: {e}")
        if not (relabel or retired):
            return
        mp = Path(self.output_dir) / f'{course_key}_metadata.json'
        try:
            meta = json.loads(mp.read_text(encoding='utf-8')) if mp.exists() else []
        except (OSError, ValueError):
            return
        for m in meta:
            if m.get('video_id') in relabel:
                m['week'] = relabel[m['video_id']]
            if m.get('video_id') in retired and not m.get('replaced_by'):
                m['replaced_by'] = retired[m['video_id']]
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

    def filter_sessions(self, sessions: List[Dict], keywords: List[str],
                        exclude_keywords: List[str] = None,
                        fallback_keywords: List[str] = None) -> List[Dict]:
        """
        מסנן הרצאות לפי keywords (שם מרצה / קבוצה).
        - מסנן קודם לפי סמסטר נוכחי
        - אם אין התאמה לקבוצה הרגילה - מחזיר fallback
        """
        import re

        # סנן קודם לסמסטר הנוכחי בלבד
        sessions = [s for s in sessions if self.is_current_semester(s)]

        if not keywords:
            # אין מילות מפתח (למשל כשמסננים לפי מערכת שעות) — עדיין להחיל exclude
            if exclude_keywords:
                sessions = [
                    s for s in sessions
                    if not any(ex.lower() in
                               f"{re.sub(r'<[^>]+>', '', s.get('SessionName', '')).lower()} "
                               f"{re.sub(r'<[^>]+>', '', s.get('FolderName', '')).lower()}"
                               for ex in exclude_keywords)
                ]
            return sessions

        matched = []
        fallback = []

        for s in sessions:
            title = re.sub(r'<[^>]+>', '', s.get('SessionName', '')).lower()
            folder = re.sub(r'<[^>]+>', '', s.get('FolderName', '')).lower()
            combined = f"{title} {folder}"

            is_excluded = exclude_keywords and any(
                ex.lower() in combined for ex in exclude_keywords
            )
            if is_excluded:
                continue

            is_mine = any(kw.lower() in combined for kw in keywords)
            if is_mine:
                matched.append(s)
            else:
                fallback.append(s)

        if matched:
            return matched

        # אם אין הרצאה של הקבוצה שלנו - קח חלופה
        if fallback and fallback_keywords:
            candidates = [
                s for s in fallback
                if any(kw.lower() in (re.sub(r'<[^>]+>', '', s.get('SessionName', '')).lower())
                       for kw in fallback_keywords)
            ]
            if candidates:
                print("⚠️  הקבוצה הרגילה לא הועלתה - משתמש בהרצאה חלופית")
                return candidates  # dedup_by_week יגביל לאחר מכן

        return []

    # ─────────────────────────── download SRT ───────────────────────────

    # Minimum characters for a transcript to be considered "good enough"
    SRT_MIN_CHARS = 500

    def _safe_stem(self, title: str, course_key: str,
                   date_str: Optional[str] = None) -> str:
        """
        בונה stem בטוח לקבצים. כולל תאריך (אם נתון) כדי למנוע התנגשויות
        בין הרצאות שונות עם אותו title (למשל "מרצה_א מרצה_ע" בכל שבוע).
        """
        safe_title = "".join(
            c for c in title if c.isalnum() or c in (' ', '-', '_')
        ).strip()
        if date_str:
            return f"{course_key}_{date_str}_{safe_title}"
        return f"{course_key}_{safe_title}"

    def download_srt(self, video_id: str, title: str,
                     course_key: str,
                     date_str: Optional[str] = None) -> Optional[str]:
        """הורדת קובץ SRT ישירות מה-endpoint של Panopto"""
        print(f"📝 מוריד כתוביות: {title}")

        r = self.session.get(
            f"{self.BASE_URL}/Panopto/Pages/Transcription/GenerateSRT.ashx",
            params={"id": video_id, "language": 0}
        )

        if r.status_code != 200 or len(r.text) < 10:
            print(f"⚠️  אין כתוביות עבור: {title}")
            return None

        # בדוק שזה SRT אמיתי (מכיל timestamps)
        if '-->' not in r.text:
            print(f"⚠️  תוכן לא תקין עבור: {title}")
            return None

        stem = self._safe_stem(title, course_key, date_str)
        output_path = self.output_dir / f"{stem}.srt"

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(r.text)

        print(f"✅ נשמר: {output_path}")
        return str(output_path)

    def _srt_is_good(self, srt_path: str) -> bool:
        """
        בדיקת איכות SRT:
        1. מינימום תוכן (500 תווים)
        2. אחוז עברית — לפחות 20% מהתווים האלפביתיים הם עבריים
        3. לא חוזרני מדי (אותו ביטוי >40% מהשורות)
        4. Gemini relevance check — בדיקה שהתוכן הוא הרצאה אקדמית
        אם נכשל → `get_transcript` יפנה ל-Whisper אוטומטית.
        """
        try:
            import re as _re
            text = Path(srt_path).read_text(encoding='utf-8')

            # Strip SRT numbering and timestamps, keep only speech lines
            clean = _re.sub(r'^\d+\s*$', '', text, flags=_re.MULTILINE)
            clean = _re.sub(r'^\d{2}:\d{2}:\d{2}[^\n]*$', '', clean, flags=_re.MULTILINE)
            lines = [l.strip() for l in clean.split('\n') if l.strip()]
            speech = ' '.join(lines)

            # Check 1: minimum length
            if len(speech) < self.SRT_MIN_CHARS:
                print(f"  ⚠️ SRT קצר מדי: {len(speech)} תווים → Whisper")
                return False

            # Check 2: Hebrew character ratio
            # Panopto auto-captions for Hebrew/Russian speakers often produce garbled English.
            # Real Hebrew lectures should have ≥20% Hebrew letters.
            hebrew_chars = sum(1 for c in speech if '\u05d0' <= c <= '\u05ea')
            alpha_chars  = sum(1 for c in speech if c.isalpha())
            if alpha_chars > 200:
                ratio = hebrew_chars / alpha_chars
                if ratio < 0.20:
                    print(f"  ⚠️ SRT כמעט ללא עברית ({ratio:.0%} תווים עבריים) → Whisper")
                    return False
                print(f"  ✓ עברית: {ratio:.0%} מהתווים האלפביתיים")

            # Check 3: repetitiveness — if one phrase appears in >40% of lines it's garbage
            if lines:
                from collections import Counter
                counts = Counter(lines)
                most_common_freq = counts.most_common(1)[0][1] / len(lines)
                if most_common_freq > 0.40:
                    print(f"  ⚠️ SRT חוזרני מדי: '{counts.most_common(1)[0][0]}' ב-{most_common_freq:.0%} מהשורות → Whisper")
                    return False

            # Check 4: Gemini relevance check — sample 1500 chars
            sample = speech[:1500]
            if not self._claude_transcript_is_relevant(sample):
                print(f"  ⚠️ Gemini: התמלול לא קשור לנושא הקורס → Whisper")
                return False

            return True

        except Exception:
            return False

    def _claude_transcript_is_relevant(self, transcript_sample: str) -> bool:
        """
        שולח דגימה מהתמלול ל-Gemini ובודק שני דברים:
        1. האם הטקסט הגיוני (לא זבל)?
        2. אם יש בו עברית — האם המילים העבריות הן מילים אמיתיות (לא אותיות אקראיות)?
        מחזיר False אם התמלול זבל, שפה לא נכונה, עברית שבורה, או לא קשור.
        """
        try:
            import google.genai as genai
            _gk = os.environ.pop('GOOGLE_API_KEY', None)
            client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or _gk)
            if _gk:
                os.environ['GOOGLE_API_KEY'] = _gk

            resp = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=f"""Analyze this text which should be an academic lecture transcript.
Answer GOOD or BAD.

Answer BAD if ANY of these are true:
- The text is random/garbled characters
- The text contains meaningless repetition
- Hebrew words present but are NOT real Hebrew words (e.g. random Hebrew letters strung together, phonetic transliterations of non-Hebrew speech into Hebrew letters, nonsense syllables)
- The text is clearly not a lecture (e.g. error messages, metadata only)

Answer GOOD if:
- The text contains coherent speech in Hebrew, English, or a mix
- Hebrew words present are actual Hebrew words (even if mixed with other languages)
- The content could plausibly be an academic lecture

Answer only: GOOD or BAD

Text sample:
{transcript_sample}""",
            )
            from utils.usage_tracker import log_gemini as _lg; _lg(resp, 'gemini-3.8-flash', op='panopto.transcript_check')
            answer = (resp.text or "").strip().upper()
            is_good = answer.startswith('GOOD')
            if not is_good:
                print(f"  ⚠️ Gemini: תמלול לא תקין ('{answer[:20]}') → Whisper")
            return is_good

        except Exception as e:
            print(f"  ⚠️ בדיקת Gemini נכשלה: {e} — מניח שהתמלול תקין")
            return True  # fail-safe

    def _get_video_mp4_url(self, video_id: str) -> Optional[str]:
        """
        מחלץ URL לוידאו מ-Panopto.
        מנסה קודם MP4 ישיר; אם אין — מחזיר HLS master URL (נתמך ע"י ffmpeg).
        """
        from xml.etree import ElementTree as ET
        import requests as _req

        r = self.session.get(
            f"{self.BASE_URL}/Panopto/Pages/Viewer/DeliveryInfo.aspx",
            params={"deliveryId": video_id, "isEmbed": "true"}
        )
        if r.status_code != 200:
            return None

        try:
            root = ET.fromstring(r.text)
            session_file_id = None
            stream_file_id = None
            hls_url = None

            for el in root.iter():
                tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag
                text = (el.text or '').strip()
                if tag == 'SessionFileId' and not session_file_id:
                    session_file_id = text
                elif tag == 'StreamFileId' and not stream_file_id:
                    stream_file_id = text
                elif tag == 'StreamUrl' and not hls_url and text.startswith('http'):
                    hls_url = text  # HLS master playlist URL

            # נסה MP4 ישיר קודם
            if session_file_id and stream_file_id:
                mp4_url = f'https://d2hpwsdp0ihr0w.cloudfront.net/sessions/{session_file_id}/{video_id}-{stream_file_id}.mp4'
                try:
                    head = _req.head(mp4_url, timeout=10, allow_redirects=True)
                    if head.status_code == 200:
                        print(f"  📹 נמצא MP4 ישיר")
                        return mp4_url
                except Exception:
                    pass

            # Fallback: HLS URL — ffmpeg תומך ב-HLS ישירות
            if hls_url:
                try:
                    head = _req.head(hls_url, timeout=10, allow_redirects=True)
                    if head.status_code == 200:
                        print(f"  📡 משתמש ב-HLS stream (ffmpeg תומך בו)")
                        return hls_url
                except Exception:
                    pass

        except Exception as e:
            print(f"⚠️  שגיאה בחילוץ URL: {e}")

        return None

    def download_and_transcribe(self, video_id: str, title: str,
                                 course_key: str,
                                 date_str: Optional[str] = None) -> Optional[str]:
        """
        מוריד וידאו מ-Panopto ומתמלל עם Whisper.
        מחזיר נתיב לקובץ SRT שנוצר.
        """
        from collectors.whisper_transcriber import transcribe_and_save

        stem = self._safe_stem(title, course_key, date_str)
        srt_path_check = str(self.output_dir / f"{stem}.srt")

        # אם SRT כבר קיים מ-Whisper — לא להוריד שוב
        if Path(srt_path_check).exists() and Path(srt_path_check).stat().st_size > 5000:
            print(f"  ✅ SRT קיים מ-Whisper — דולג על הורדה חוזרת")
            return srt_path_check

        mp4_url = self._get_video_mp4_url(video_id)
        if not mp4_url:
            print(f"❌ לא נמצא URL להורדה עבור: {title}")
            return None

        mp4_path = str(self.output_dir / f"{stem}_temp.mp4")
        srt_path = str(self.output_dir / f"{stem}.srt")
        is_hls = 'hls' in mp4_url.lower() or mp4_url.endswith('.panobf2')

        if is_hls:
            # HLS — download all segments via byte-range (using board_reader's parser)
            # then pipe combined fMP4 to ffmpeg for audio extraction
            import subprocess
            from collectors.board_reader import (
                FFMPEG_PATH, _parse_hls_variant_url, _parse_hls_segments, _download_range
            )
            audio_path = mp4_path.replace('_temp.mp4', '_audio.mp3')
            print(f"📡 HLS — מוריד כל ה-segments ומחלץ audio...")

            try:
                variant_url = _parse_hls_variant_url(mp4_url)
                if not variant_url:
                    print(f"❌ לא ניתן לפרסר HLS variant")
                    return None

                init_url, init_offset, init_len, segments = _parse_hls_segments(variant_url)
                if not segments:
                    print(f"❌ לא נמצאו segments ב-HLS")
                    return None

                frag_url = variant_url.rsplit('/', 1)[0] + '/fragmented.panobf1'
                total_dur = sum(d for _, _, d in segments)
                print(f"  {len(segments)} segments ({total_dur/60:.0f} דקות)")

                # הורד במקביל עם ThreadPoolExecutor
                from concurrent.futures import ThreadPoolExecutor, as_completed
                import requests as _req

                def dl_seg(args):
                    idx, off, ln = args
                    end = off + ln - 1
                    for attempt in range(3):
                        try:
                            r = _req.get(frag_url, headers={'Range': f'bytes={off}-{end}'}, timeout=30)
                            return idx, r.content
                        except Exception:
                            if attempt == 2:
                                return idx, b''
                    return idx, b''

                init_bytes = b''
                if init_len > 0:
                    init_bytes = _download_range(frag_url, init_offset, init_len)

                seg_args = [(i, off, ln) for i, (off, ln, _) in enumerate(segments)]
                results_map = {}
                with ThreadPoolExecutor(max_workers=12) as ex:
                    futures = {ex.submit(dl_seg, a): a[0] for a in seg_args}
                    done = 0
                    for fut in as_completed(futures):
                        idx, data = fut.result()
                        results_map[idx] = data
                        done += 1
                        if done % 50 == 0:
                            print(f"  {done}/{len(segments)} segments...", end='\r', flush=True)

                all_bytes = bytearray(init_bytes)
                for i in range(len(segments)):
                    all_bytes += results_map.get(i, b'')

                print(f"\n  הורד: {len(all_bytes)//1024//1024}MB")

                # חלץ audio עם ffmpeg מ-stdin
                cmd = [
                    FFMPEG_PATH, '-y',
                    '-f', 'mp4', '-i', 'pipe:0',
                    '-vn', '-acodec', 'mp3', '-q:a', '4',
                    audio_path
                ]
                result_proc = subprocess.run(cmd, input=bytes(all_bytes),
                                              capture_output=True, timeout=600,
                                              creationflags=(0x08000000 if os.name == 'nt' else 0))
                all_bytes = None  # free memory

                if result_proc.returncode != 0 or not Path(audio_path).exists():
                    print(f"❌ ffmpeg נכשל בחילוץ audio")
                    print(result_proc.stderr.decode('utf-8', errors='replace')[-500:])
                    return None

                size_mb = Path(audio_path).stat().st_size // 1024 // 1024
                print(f"  audio: {size_mb}MB")
                result = transcribe_and_save(audio_path, srt_path, language=None)
                try:
                    Path(audio_path).unlink()
                except Exception:
                    pass

            except Exception as e:
                print(f"❌ שגיאה בהורדת HLS: {e}")
                return None
        else:
            # MP4 ישיר — הורד ותמלל
            print(f"⬇️  מוריד וידאו: {title} (יכול לקחת מספר דקות)...")
            r = self.session.get(mp4_url, stream=True, timeout=60)
            total = int(r.headers.get('Content-Length', 0))
            downloaded = 0
            with open(mp4_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            print(f"  {downloaded/total*100:.0f}%", end='\r', flush=True)
            print(f"\n  הורד: {downloaded//1024//1024}MB")
            result = transcribe_and_save(mp4_path, srt_path, language=None)

        # Clean up MP4
        try:
            Path(mp4_path).unlink()
        except Exception:
            pass

        return result

    # 19/11 the owner: Panopto's free captions first, as much as possible — saving beats a summary 2 days later. They often
    # appear hours or days after the recording, so EVERY recording with no captions yet waits up to 48h for them
    # (each nightly run re-checks it — the collector looks 14 days back); only then the local model, OpenAI last.
    # One exception: an exam or quiz (בוחן) of that course within 2 days → processed right away.
    CAPTION_WAIT_HOURS = 48
    URGENT_EXAM_DAYS = 2

    @staticmethod
    def _exam_soon(course_key: str, days: int, today=None) -> bool:
        """An exam / quiz of this course within `days` (the exam detector's sources: Moodle cache + manual list)."""
        try:
            from scripts.exam_mode_detector import _find_upcoming_exams
            today = today or datetime.now().date()
            for ex in _find_upcoming_exams():
                if ex.get('course_key') == course_key:
                    d = datetime.strptime(ex['exam_date'][:10], '%Y-%m-%d').date()
                    if 0 <= (d - today).days <= days:
                        return True
        except Exception as e:
            print(f"  ⚠️ exam check: {e}")
        return False

    def wait_for_captions(self, course_key: str, kind: str, recorded_at: Optional[datetime],
                          now: Optional[datetime] = None) -> bool:
        """True → leave this recording for a later run: its Panopto captions may still come (free)."""
        if not recorded_at:
            return False
        now = now or datetime.utcnow()
        if recorded_at.tzinfo is not None:
            recorded_at = recorded_at.astimezone(timezone.utc).replace(tzinfo=None)
        if (now - recorded_at).total_seconds() >= self.CAPTION_WAIT_HOURS * 3600:
            return False
        return not self._exam_soon(course_key, self.URGENT_EXAM_DAYS)

    def get_transcript(self, video_id: str, title: str, course_key: str,
                       date_str: Optional[str] = None, recorded_at: Optional[datetime] = None,
                       kind: str = 'lecture') -> Optional[str]:
        """
        Order (19/11): Panopto's captions (free, when srt_is_good) → the VM's local model (free) → OpenAI (last).
        No captions yet on a fresh recording → None for now (wait_for_captions); a later run checks again.

        date_str נכלל בשם הקובץ כדי למנוע התנגשות בין הרצאות שונות עם אותו title.
        """
        openai_key = os.getenv('OPENAI_API_KEY', '')

        # 1. נסה SRT מ-Panopto
        srt_path = self.download_srt(video_id, title, course_key, date_str)

        if srt_path and self._srt_is_good(srt_path):
            return srt_path  # SRT טוב — משתמשים בו

        if not srt_path and self.wait_for_captions(course_key, kind, recorded_at):
            hrs = (datetime.utcnow() - recorded_at.replace(tzinfo=None)).total_seconds() / 3600
            print(f"⏳ אין עדיין כתוביות Panopto ({hrs:.0f} שעות מההקלטה) — מחכים להן עד "
                  f"{self.CAPTION_WAIT_HOURS} שעות, בודקים שוב בריצה הבאה: {title[:50]}")
            return None

        if srt_path:
            # Quarantine the bad SRT so it won't be reused on next run
            bad_bak = Path(srt_path).with_suffix('.srt.bad')
            try:
                Path(srt_path).rename(bad_bak)
                print(f"⚠️  SRT גרוע (קירילית/לא עברי) — הועבר ל-{bad_bak.name}")
            except Exception:
                print(f"⚠️  SRT גרוע — עובר ל-Whisper")
        else:
            print(f"ℹ️  אין SRT — עובר ל-Whisper")

        # 2. Transcription. 19/11: on the VM the lecture is transcribed locally first ($0) — no key or budget needed;
        # transcribe_and_save applies both gates itself before its paid fallback.
        from collectors import local_asr
        if local_asr.enabled():
            return self.download_and_transcribe(video_id, title, course_key, date_str)
        if not openai_key:
            print("⚠️  אין OPENAI_API_KEY — לא ניתן לתמלל עם Whisper. ה-SRT הגרוע הועבר לקובץ .bad")
            return None  # Never return a known-bad SRT

        # paid transcription must respect the monthly budget lock too
        from utils.budget import budget_lock_reason
        _lock = budget_lock_reason()
        if _lock:
            from utils.pipeline_health import pipeline_warn
            pipeline_warn(f"{course_key}: תמלול בתשלום נחסם — נעילת תקציב ({_lock}): {title[:50]}")
            return None

        return self.download_and_transcribe(video_id, title, course_key, date_str)

    def download_video(self, video_id: str, title: str, course_key: str,
                       date_str: Optional[str] = None) -> Optional[str]:
        """
        מחזיר URL/נתיב לוידאו לצורך קריאת לוח (board reading).
        - אם MP4 ישיר: מוריד לדיסק ומחזיר נתיב
        - אם HLS: מחזיר את ה-URL ישירות (ffmpeg מסתרם ממנו)
        """
        url = self._get_video_mp4_url(video_id)
        if not url:
            print(f"❌ לא נמצא URL לוידאו: {title}")
            return None

        # HLS — pass URL directly to ffmpeg (no download needed)
        if 'hls' in url.lower() or url.endswith('.panobf2'):
            print(f"📡 וידאו HLS — ffmpeg יקרא ישירות מ-URL")
            return url

        # MP4 ישיר — הורד לדיסק
        stem = self._safe_stem(title, course_key, date_str)
        mp4_path = str(self.output_dir / f"{stem}_board.mp4")

        if Path(mp4_path).exists():
            print(f"📽️  וידאו כבר קיים: {mp4_path}")
            return mp4_path

        print(f"⬇️  מוריד MP4 ללוח: {title}...")
        try:
            r = self.session.get(url, stream=True, timeout=60)
            total = int(r.headers.get('Content-Length', 0))
            downloaded = 0
            with open(mp4_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            print(f"  {downloaded/total*100:.0f}%", end='\r', flush=True)
            print(f"\n  הורד: {downloaded//1024//1024}MB → {mp4_path}")
            return mp4_path
        except Exception as e:
            print(f"❌ שגיאה בהורדת וידאו: {e}")
            return None

    def get_delivery_info(self, video_id: str) -> Dict:
        """מקבל מידע על הסרטון (כולל האם יש כתוביות)"""
        r = self.session.get(
            f"{self.BASE_URL}/Panopto/Pages/Viewer/DeliveryInfo.aspx",
            params={"deliveryId": video_id, "responseType": "json"}
        )
        return r.json() if r.status_code == 200 else {}

    # ─────────────────────────── collect course ───────────────────────────

    def collect_course(self, course_key: str,
                       days_back: int = 2) -> List[Dict]:
        """
        אוסף הרצאות חדשות לקורס.
        days_back: כמה ימים אחורה לחפש (ברירת מחדל 2 - כי הרצאה מועלית ~24 שעות אחרי)
        """
        course = self.config['courses'][course_key]
        self._current_course = course_key

        if not course['sources']['panopto']['enabled']:
            print(f"⏭️  Panopto לא מופעל עבור {course['name']}")
            return []

        panopto_cfg = course['sources']['panopto']
        search_query = panopto_cfg.get('search_query', course['name'])
        lecture_keywords = panopto_cfg.get('lecture_keywords', [])
        tutorial_keywords = panopto_cfg.get('tutorial_keywords', [])
        exclude_keywords = panopto_cfg.get('exclude_keywords', [])
        collect_tutorials = panopto_cfg.get('collect_tutorials', False)

        # מקסימום הרצאות/תרגולים שבועי
        weekly = course.get('weekly_schedule', {})
        max_lectures = weekly.get('max_lectures', 99)
        max_tutorials = weekly.get('max_tutorials', 99)

        print(f"\n{'='*50}")
        print(f"📚 אוסף: {course['name']}")
        print(f"{'='*50}")

        board_reading = course.get('board_reading', False)
        folder_id = panopto_cfg.get('folder_id') if panopto_cfg.get('folder_id') != 'TO_BE_FILLED' else None
        tutorial_folder_id = panopto_cfg.get('tutorial_folder_id')
        if not folder_id:                       # found automatically by course number + the owner's group (panopto_discovery)
            try:
                from collectors.panopto_discovery import folders_for
                auto_lec, auto_tut = folders_for(course_key)
                folder_id, tutorial_folder_id = auto_lec, tutorial_folder_id or auto_tut
                if folder_id:
                    print(f"🎯 תיקיית Panopto (נמצאה אוטומטית לפי הקבוצה שלי): {folder_id}")
            except Exception as e:
                print(f"  ⚠️ panopto folders: {e}")

        # מערכת השעות האישית (מהיומן) — לסינון "רק הקבוצה שלי"
        my_sessions = course.get('my_sessions') or []
        try:                                    # the site's current slots win over the planning snapshot (13/10)
            from collectors.panopto_discovery import portal_sessions
            my_sessions = portal_sessions(course_key) or my_sessions
        except Exception as e:
            print(f"  ⚠️ portal sessions: {e}")
        match_by_schedule = panopto_cfg.get('match_by_schedule', False) and bool(my_sessions)
        self.__dict__.pop('_week_actions', None)
        if match_by_schedule:                   # 11/11: his group's class moved to another day in the studies calendar
            my_sessions = list(my_sessions) + self._calendar_slots(course_key, my_sessions, days_back)

        lecture_search_query = '' if folder_id else search_query
        lecture_sessions = self.search_sessions(
            lecture_search_query, days_back=days_back, folder_id=folder_id
        )
        lecture_sessions = self.drop_foreign(lecture_sessions, course_key)
        lecture_history = self._history(lecture_sessions, course_key)
        print(f"🔍 נמצאו {len(lecture_sessions)} הרצאות בחיפוש")

        if match_by_schedule:
            _before = len(lecture_sessions)
            lecture_sessions = self.filter_by_schedule(
                lecture_sessions, my_sessions, 'lecture', history=lecture_history,
                foreign_hosts=panopto_cfg.get('other_group_hosts'))
            print(f"🗓️  סינון מערכת שעות (הרצאות): {_before} → {len(lecture_sessions)} — רק הקבוצה שלי")

        import re
        results = []

        def dedup_by_week(sessions: List[Dict], max_count: int) -> List[Dict]:
            """
            מסיר כפילויות: אם אותו מרצה מעביר כמה פעמים את אותה הרצאה בשבוע,
            לוקחים רק max_count (הכי חדשות).
            מכיוון שהתוצאות ממוינות לפי תאריך יורד, פשוט לוקחים את ה-max_count הראשונים.
            11/11: matched by schedule → already one per week (_week); max_count per WEEK, not per run (the cap of 1
            per run left every other week of the 14-day lookback without his recording).
            """
            if any(s.get('_week') for s in sessions):
                per, out = {}, []
                for s in sessions:
                    k = s.get('_week') or ''
                    if per.get(k, 0) < max_count:
                        per[k] = per.get(k, 0) + 1
                        out.append(s)
                return out
            return sessions[:max_count]

        replaced: Dict[str, str] = {}

        def retire_replaced(s: Dict, video_id: str) -> None:
            """His recording of the week is in place → the other group's one of that week moves aside (never deleted)."""
            from collectors import weekly_pick as wp
            for item in s.get('_replaces') or []:
                try:
                    wp.retire(item, print)
                    print(f"♻️  {course_key}: {item['date']} {item['title'][:40]} — הוחלף בהקלטה של הקבוצה שלי "
                          f"(הועבר ל-replaced/)")
                    if item.get('video_id'):
                        replaced[item['video_id']] = video_id
                except Exception as e:
                    print(f"  ⚠️ retire {item.get('title')}: {e}")

        def week_fields(s: Dict) -> Dict:
            return {**({'week': s['_week']} if s.get('_week') else {}),
                    **({'match': s['_match']} if s.get('_match') else {}),
                    **({'other_group': True} if s.get('_other_group') else {}),
                    **({'host': s['_host']} if s.get('_host') else {}),       # → the "from another group" line
                    **({'start': s['_start']} if s.get('_start') else {})}

        done = self._done_items(course_key)

        def finished(video_id: str, title: str) -> bool:
            """14/11: an item whose transcript is already here (metadata + its SRT) is never fetched again — the nightly
            re-download of Panopto's SRT cost a Gemini check each time and wrote over a paid transcript."""
            if video_id in done:
                print(f"✔️  כבר יש תמלול — לא מוריד שוב: {title[:60]}")
                return True
            return False

        # הרצאות
        fallback_lecture_kw = panopto_cfg.get('fallback_lecture_keywords', [])
        lectures = self.filter_sessions(
            lecture_sessions, lecture_keywords, exclude_keywords,
            fallback_keywords=fallback_lecture_kw
        )
        lectures = dedup_by_week(lectures, max_lectures)
        print(f"📖 הרצאות רלוונטיות: {len(lectures)} (מקסימום {max_lectures})")

        seen_titles = {}
        for s in lectures:
            video_id = s.get('DeliveryID') or s.get('IosVideoUrl', '').split('/')[-1]
            title = self._clean_title(s.get('SessionName', 'unknown'))
            dt = self.parse_panopto_date(s.get('StartTime', ''))
            date_str = dt.strftime('%Y-%m-%d') if dt else ''

            # אם שתי הרצאות באותו שם — הוסף מספר לשנייה
            if title in seen_titles:
                seen_titles[title] += 1
                title = f"{title} ({seen_titles[title]})"
            else:
                seen_titles[title] = 1

            if finished(video_id, title):
                continue
            srt_path = self.get_transcript(video_id, title, course_key, date_str, recorded_at=dt, kind='lecture')
            if srt_path:
                video_path = None
                if board_reading:
                    try:
                        video_path = self.download_video(video_id, title, course_key, date_str)
                    except Exception as e:
                        print(f"⚠️ שגיאה בהורדת וידאו ל-board reading: {e}")
                        video_path = None
                results.append({
                    'course': course_key,
                    'type': 'lecture',
                    'video_id': video_id,
                    'title': title,
                    'date': date_str,
                    'srt_path': srt_path,
                    'video_path': video_path,
                    **week_fields(s),
                })
                retire_replaced(s, video_id)
            time.sleep(0.5)

        # תרגולים (אם מופעל)
        # מילות מפתח *או* שיבוץ מהיומן מספיקים כדי לאסוף תרגולים
        if collect_tutorials and (tutorial_keywords or match_by_schedule):
            tut_folder = tutorial_folder_id or folder_id
            tutorial_sessions = self.search_sessions(
                '' if tut_folder else search_query, days_back=days_back,
                folder_id=tut_folder
            )
            tutorial_sessions = self.drop_foreign(tutorial_sessions, course_key)
            tutorial_history = self._history(tutorial_sessions, course_key)

            if match_by_schedule:
                _before = len(tutorial_sessions)
                tutorial_sessions = self.filter_by_schedule(
                    tutorial_sessions, my_sessions, 'tutorial', history=tutorial_history,
                    foreign_hosts=panopto_cfg.get('other_group_hosts'))
                print(f"🗓️  סינון מערכת שעות (תרגולים): {_before} → {len(tutorial_sessions)} — רק הקבוצה שלי")

            fallback_tutorial_kw = panopto_cfg.get('fallback_tutorial_keywords', [])
            tutorials = self.filter_sessions(
                tutorial_sessions, tutorial_keywords, exclude_keywords,
                fallback_keywords=fallback_tutorial_kw
            )
            tutorials = dedup_by_week(tutorials, max_tutorials)
            print(f"✏️  תרגולים רלוונטיים: {len(tutorials)} (מקסימום {max_tutorials})")

            lecture_ids = {r['video_id'] for r in results}
            for s in tutorials:
                video_id = s.get('DeliveryID') or s.get('IosVideoUrl', '').split('/')[-1]
                title = self._clean_title(s.get('SessionName', 'unknown'))
                # 23/10: Software 1's "Lecture 1" was also collected as "תרגול - Lecture 1" → a second summary of
                # the same recording. A lecture-titled recording is never a tutorial, and one recording is one item.
                if video_id in lecture_ids or (re.search(r'הרצאה|lecture', title, re.I)
                                               and not re.search(r'תרגול|תרגיל|recitation|tirgul', title, re.I)):
                    print(f"⏭️  לא תרגול (הרצאה / כבר נאסף): {title[:60]}")
                    continue
                # תרגול שנתפס לפי המערכת (בלי מילת מפתח בכותרת) היה מסווג
                # כהרצאה בשלב הסיכום (sync_all מסווג לפי הכותרת) — מסמנים אותו.
                if not any(kw in title.lower() for kw in TUTORIAL_TITLE_KW):
                    title = f"תרגול - {title}"
                dt = self.parse_panopto_date(s.get('StartTime', ''))
                date_str = dt.strftime('%Y-%m-%d') if dt else ''

                if finished(video_id, title):
                    continue
                srt_path = self.get_transcript(video_id, title, course_key, date_str, recorded_at=dt, kind='tutorial')
                if srt_path:
                    results.append({
                        'course': course_key,
                        'type': 'tutorial',
                        'video_id': video_id,
                        'title': title,
                        'date': date_str,
                        'srt_path': srt_path,
                        **week_fields(s),
                    })
                    retire_replaced(s, video_id)
                time.sleep(0.5)

        # שמור metadata
        if results:
            meta_path = self.output_dir / f"{course_key}_metadata.json"
            existing = []
            if meta_path.exists():
                with open(meta_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)

            # הוסף רק חדשים (לפי video_id)
            existing_ids = {r['video_id'] for r in existing}
            new_results = [r for r in results if r['video_id'] not in existing_ids]
            existing.extend(new_results)

            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)

            print(f"✅ נוספו {len(new_results)} הרצאות חדשות")
        else:
            print("ℹ️  אין הרצאות חדשות")
        if match_by_schedule:
            try:                       # 11/11: a makeup's week relabelled; a week's extra moved aside (his is in place)
                self._apply_week_actions(course_key, replaced)
            except Exception as e:
                print(f"  ⚠️ week actions: {e}")

        return results

    def _discover_folders(self, keys: List[str], days_back: int = 90) -> None:
        """Missing folder_id → one search per course (we're logged in anyway), matched to the owner's group. No LLM."""
        try:
            from collectors import panopto_discovery as pd
            missing = {}
            for k in keys:
                pan = (self.config['courses'][k].get('sources') or {}).get('panopto') or {}
                if pan.get('folder_id') in (None, '', 'TO_BE_FILLED') and not pd.folders_for(k)[0]:
                    from config.paths import COURSE_ALT_NAMES
                    missing[k] = [pan.get('search_query', self.config['courses'][k]['name'])] + COURSE_ALT_NAMES.get(k, [])
            if not missing:
                return
            found = pd.discover(lambda q: self.search_sessions(q, days_back=days_back), missing)
            for k, rec in found.items():
                name = self.config['courses'][k]['name']
                print(f"🎯 {name}: נמצאה תיקיית Panopto — {rec['folder_name']}")
                try:
                    from utils.notify import notify
                    if rec['group'] == 'combined':
                        text = (f"🎥 מצאתי את תיקיית ההקלטות של *{name}* — תיקייה אחת לכל הקבוצות, "
                                'אז נלקחות ממנה רק ההקלטות ביום ובשעה של הקבוצה שלך.')
                    else:
                        text = (f"🎥 מצאתי את תיקיית ההקלטות של *{name}* (הקבוצה שלך, {rec['group']})"
                                + (' + התרגול' if rec.get('tutorial_folder_id') else '')
                                + ' — הסיכומים יגיעו מכאן, בלי הקלטות של קבוצות אחרות.')
                    notify(text, key=f'panopto-folder:{k}')
                except Exception:
                    pass
        except Exception as e:
            print(f"  ⚠️ panopto folder discovery: {e}")

    def collect_all(self, days_back: int = 2) -> Dict[str, List]:
        """אוסף את כל הקורסים"""
        from utils.pipeline_health import pipeline_warn
        self.login()
        all_results = {}
        # רק קורסי הסמסטר הנוכחי (הישנים נשארים בקונפיג להיסטוריה)
        keys = list(self.config['courses'].keys())
        try:
            from config.paths import ACTIVE_COURSES
            keys = [k for k in keys if k in ACTIVE_COURSES] or keys
        except ImportError:
            pass
        self._discover_folders(keys, days_back=max(days_back, 90))
        for course_key in keys:
            pan = (self.config['courses'][course_key].get('sources') or {}).get('panopto') or {}
            from collectors.panopto_discovery import folders_for
            if pan.get('folder_id') in (None, '', 'TO_BE_FILLED') and not folders_for(course_key)[0]:
                pipeline_warn(f"{course_key}: folder_id של Panopto עדיין לא מולא — "
                              f"מחפש לפי שם הקורס, עלול לתפוס קבוצות/קורסים אחרים")
            try:
                all_results[course_key] = self.collect_course(course_key, days_back=days_back)
            except Exception as e:
                # קורס אחד שנופל לא מפיל את השאר — אבל מתריע
                pipeline_warn(f"{course_key}: איסוף Panopto נכשל — {str(e)[:150]}")
                all_results[course_key] = []
        return all_results


if __name__ == "__main__":
    collector = PanoptoCollector()
    collector.collect_all(days_back=2)
