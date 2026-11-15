"""
assignment_tracker.py — מושך מטלות + דדליינים מ-Moodle ומ-Wikidot.

מחזיר:
  [
    {
      course_key, course_name,
      title,         ← שם המטלה
      due_date,      ← datetime | None
      days_left,     ← int | None
      local_path,    ← str | None  ← קובץ שהורד
      source,        ← "moodle" | "wikidot"
      url,           ← קישור מקורי
    },
    ...
  ]

ריצה:
    tracker = AssignmentTracker()
    assignments = tracker.fetch_all()   ← מושך ומוריד הכל
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / '.env', override=True)
except ImportError:
    pass

import yaml

ROOT = Path(__file__).parent.parent
ASSIGNMENTS_DIR = ROOT / 'data' / 'assignments'
ASSIGNMENTS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_PATH = ROOT / 'data' / 'assignments' / '_cache.json'


# ─────────────────────────────────────────────────────────────────────────────
# Cache helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def _save_cache(data: dict):
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


# ─────────────────────────────────────────────────────────────────────────────
# Moodle assignments via Playwright
# ─────────────────────────────────────────────────────────────────────────────

def _parse_due_date(text: str) -> Optional[datetime]:
    """מנסה לחלץ תאריך הגשה מטקסט."""
    if not text:
        return None
    text = text.strip()

    # פורמטים נפוצים ב-Moodle: "יום שני, 4 במאי 2026, 23:59"
    hebrew_months = {
        'בינואר': 1, 'בפברואר': 2, 'במרץ': 3, 'באפריל': 4,
        'במאי': 5, 'ביוני': 6, 'ביולי': 7, 'באוגוסט': 8,
        'בספטמבר': 9, 'באוקטובר': 10, 'בנובמבר': 11, 'בדצמבר': 12,
        'ינואר': 1, 'פברואר': 2, 'מרץ': 3, 'אפריל': 4,
        'מאי': 5, 'יוני': 6, 'יולי': 7, 'אוגוסט': 8,
        'ספטמבר': 9, 'אוקטובר': 10, 'נובמבר': 11, 'דצמבר': 12,
    }

    # "4 במאי 2026"
    for month_he, month_num in hebrew_months.items():
        m = re.search(rf'(\d{{1,2}})\s+{re.escape(month_he)}\s+(\d{{4}})', text)
        if m:
            try:
                day, year = int(m.group(1)), int(m.group(2))
                # זמן: חפש HH:MM
                t = re.search(r'(\d{1,2}):(\d{2})', text)
                hour, minute = (int(t.group(1)), int(t.group(2))) if t else (23, 59)
                return datetime(year, month_num, day, hour, minute)
            except Exception:
                pass

    # ISO / DD/MM/YYYY / YYYY-MM-DD
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d', '%d/%m/%Y %H:%M', '%d/%m/%Y'):
        try:
            return datetime.strptime(text[:len(fmt) + 2].strip(), fmt)
        except Exception:
            pass

    return None


def _days_left(due: Optional[datetime]) -> Optional[int]:
    if due is None:
        return None
    now = datetime.now()
    return (due.replace(tzinfo=None) - now).days


def _fetch_moodle_assignments(course_id: str, course_key: str, course_dir: Path) -> list[dict]:
    """
    מושך מטלות (mod/assign) מ-Moodle עם דדליינים דרך Playwright.
    """
    import requests as _req
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    username = os.getenv('TAU_USERNAME', '')
    password = os.getenv('TAU_PASSWORD', '')
    base_url = 'https://moodle.tau.ac.il'

    if not username or not password:
        print('  ⚠️  TAU_USERNAME/TAU_PASSWORD לא מוגדרים')
        return []

    assignments = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()

            # Login
            page.goto(f'{base_url}/login/index.php')
            page.wait_for_load_state('networkidle', timeout=20000)

            sso_selectors = [
                'a[href*="shibboleth"]', 'a[href*="saml"]',
                'text=כניסה דרך שירות מרכזי', 'text=SSO',
            ]
            for sel in sso_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        page.click(sel)
                        break
                except Exception:
                    pass

            page.wait_for_load_state('networkidle', timeout=20000)

            # Fill credentials
            for u_sel in ['#username', 'input[name="username"]', 'input[type="text"]']:
                try:
                    if page.locator(u_sel).count() > 0:
                        page.fill(u_sel, username)
                        break
                except Exception:
                    pass
            for p_sel in ['#password', 'input[name="password"]', 'input[type="password"]']:
                try:
                    if page.locator(p_sel).count() > 0:
                        page.fill(p_sel, password)
                        break
                except Exception:
                    pass
            for btn in ['button[type="submit"]', 'input[type="submit"]', 'text=כניסה', 'text=Login']:
                try:
                    if page.locator(btn).count() > 0:
                        page.click(btn)
                        break
                except Exception:
                    pass

            page.wait_for_load_state('networkidle', timeout=30000)

            # Navigate to course page
            page.goto(f'{base_url}/course/view.php?id={course_id}')
            page.wait_for_load_state('networkidle', timeout=20000)

            # מצא קישורי מטלות (mod/assign)
            links = page.eval_on_selector_all(
                'a[href*="/mod/assign/view.php"]',
                'els => els.map(e => ({href: e.href, text: e.innerText.trim()}))'
            )

            for link in links:
                href = link.get('href', '')
                title = link.get('text', '').strip()
                if not title or not href:
                    continue

                # כנס לעמוד המטלה לחץ דדליין
                try:
                    page.goto(href, timeout=15000)
                    page.wait_for_load_state('networkidle', timeout=15000)
                    content = page.content()

                    # חפש תאריך הגשה
                    due_date = None
                    for pattern in [
                        r'מועד הגשה[^\d]*([^\n<]{5,40})',
                        r'Due date[^\d]*([^\n<]{5,40})',
                        r'תאריך הגשה[^\d]*([^\n<]{5,40})',
                    ]:
                        m = re.search(pattern, content, re.IGNORECASE)
                        if m:
                            due_date = _parse_due_date(m.group(1))
                            if due_date:
                                break

                    # הורד את קובץ המטלה אם יש
                    local_path = None
                    pdf_links = re.findall(r'href="([^"]+pluginfile[^"]+\.pdf)"', content)
                    if pdf_links:
                        try:
                            cookies = {c['name']: c['value'] for c in ctx.cookies()}
                            s = _req.Session()
                            s.cookies.update(cookies)
                            r = s.get(pdf_links[0], timeout=20)
                            if r.status_code == 200 and len(r.content) > 500:
                                safe = re.sub(r'[^\w\s\-\u0590-\u05FF]', '', title).strip()
                                safe = re.sub(r'\s+', '_', safe) or f'assign_{int(time.time())}'
                                out = course_dir / f"{safe}.pdf"
                                out.write_bytes(r.content)
                                local_path = str(out)
                                print(f'  ✅ הורד: {title} → {out.name}')
                        except Exception as e:
                            print(f'  ⚠️ שגיאה בהורדת {title}: {e}')

                    # זהה סטטוס הגשה
                    submitted = False
                    submission_markers = [
                        'הגשתם', 'הוגש', 'הוגשה', 'submitted', 'submission submitted',
                        'סטטוס הגשה: הוגשה', 'Your submission', 'הגשתי',
                    ]
                    for marker in submission_markers:
                        if marker.lower() in content.lower():
                            submitted = True
                            break

                    assignments.append({
                        'title': title,
                        'url': href,
                        'due_date': due_date.isoformat() if due_date else None,
                        'days_left': _days_left(due_date),
                        'local_path': local_path,
                        'source': 'moodle',
                        'submitted': submitted,
                    })

                except Exception as e:
                    print(f'  ⚠️ לא נגיש: {title}: {e}')
                    assignments.append({
                        'title': title,
                        'url': href,
                        'due_date': None,
                        'days_left': None,
                        'local_path': None,
                        'source': 'moodle',
                        'submitted': False,
                    })

            browser.close()

    except Exception as e:
        print(f'  ❌ שגיאה ב-Moodle assignments ({course_id}): {e}')

    return assignments


# ─────────────────────────────────────────────────────────────────────────────
# Wikidot assignments (מדמ"ח)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_wikidot_assignments(course_key: str, recitations_url: str, course_dir: Path) -> list[dict]:
    """
    מושך מטלות (קבצי תרגיל) מ-Wikidot של מדמ"ח.
    מחפש קישורי PDF/PY בטבלת תרגולים + חולץ תאריכי הגשה מהטקסט.
    """
    import requests as _req
    from bs4 import BeautifulSoup

    assignments = []

    try:
        r = _req.get(recitations_url, timeout=20)
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')

        base = recitations_url.split('/')[0] + '//' + recitations_url.split('/')[2]

        # חפש שורות טבלה עם קישורים לקבצים
        rows = soup.find_all('tr')
        for row in rows:
            cells = row.find_all('td')
            if not cells:
                continue

            row_text = row.get_text(' ', strip=True)

            # חפש קישורים לקבצים
            for a in row.find_all('a', href=True):
                href = a['href'].strip()
                if href.startswith('/local--files/'):
                    href = base + href
                elif href.startswith('//'):
                    href = 'http:' + href
                elif not href.startswith('http'):
                    continue

                path_lower = href.split('?')[0].lower()
                if not any(path_lower.endswith(ext) for ext in ['.pdf', '.py', '.zip', '.ipynb']):
                    continue

                title = a.get_text(strip=True) or Path(path_lower).stem
                if title.lower() in ('pdf', 'py', 'zip', ''):
                    title = Path(path_lower).stem

                # חלץ תאריך הגשה מטקסט השורה
                due_date = None
                for pattern in [
                    r'הגשה[:\s]+([^\n,]{5,35})',
                    r'due[:\s]+([^\n,]{5,35})',
                    r'(\d{1,2}[./]\d{1,2}[./]\d{4})',
                    r'(\d{1,2}[./]\d{1,2})',
                ]:
                    m = re.search(pattern, row_text, re.IGNORECASE)
                    if m:
                        due_date = _parse_due_date(m.group(1))
                        if due_date:
                            break

                # הורד קובץ
                local_path = None
                try:
                    dr = _req.get(href, timeout=20)
                    if dr.status_code == 200 and len(dr.content) > 200:
                        ext = Path(path_lower).suffix or '.pdf'
                        safe = re.sub(r'[^\w\s\-\u0590-\u05FF]', '', title).strip()
                        safe = re.sub(r'\s+', '_', safe) or f'wikidot_{int(time.time())}'
                        out = course_dir / f"{safe}{ext}"
                        out.write_bytes(dr.content)
                        local_path = str(out)
                        print(f'  ✅ ויקידוט: {title} → {out.name}')
                except Exception as e:
                    print(f'  ⚠️ שגיאה בהורדה ({title}): {e}')

                assignments.append({
                    'title': title,
                    'url': href,
                    'due_date': due_date.isoformat() if due_date else None,
                    'days_left': _days_left(due_date),
                    'local_path': local_path,
                    'source': 'wikidot',
                    'submitted': False,   # Wikidot אין מנגנון הגשה — תמיד False
                })

    except Exception as e:
        print(f'  ❌ שגיאה ב-Wikidot ({recitations_url}): {e}')

    return assignments


# ─────────────────────────────────────────────────────────────────────────────
# Main tracker
# ─────────────────────────────────────────────────────────────────────────────

class AssignmentTracker:

    def __init__(self, config_path: str = 'config/courses.yaml'):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        self.courses = self.config.get('courses', {})

    def fetch_all(self, force: bool = False) -> list[dict]:
        """
        מושך מטלות מכל הקורסים.
        משתמש ב-cache לשעה אחת (אלא אם force=True).
        """
        cache = _load_cache()
        now_ts = time.time()

        # Cache TTL: שעה אחת
        if not force and cache.get('_ts') and (now_ts - cache['_ts']) < 3600:
            print('📋 מטלות נטענו מ-cache')
            return cache.get('assignments', [])

        all_assignments = []

        for course_key, course_conf in self.courses.items():
            course_name = course_conf.get('name', course_key)
            course_dir  = ASSIGNMENTS_DIR / course_key
            course_dir.mkdir(parents=True, exist_ok=True)

            print(f'\n📚 מושך מטלות: {course_name}')

            sources = course_conf.get('sources', {})

            # Moodle assignments
            moodle = sources.get('moodle', {})
            if moodle.get('enabled') and moodle.get('course_id'):
                items = _fetch_moodle_assignments(
                    str(moodle['course_id']), course_key, course_dir
                )
                for item in items:
                    item['course_key']  = course_key
                    item['course_name'] = course_name
                all_assignments.extend(items)

            # Wikidot (מדמ"ח)
            wikidot = sources.get('wikidot', {})
            if wikidot.get('enabled') and wikidot.get('recitations_url'):
                items = _fetch_wikidot_assignments(
                    course_key,
                    wikidot['recitations_url'],
                    course_dir,
                )
                for item in items:
                    item['course_key']  = course_key
                    item['course_name'] = course_name
                all_assignments.extend(items)

        # מיין לפי דדליין
        def _sort_key(a):
            if a.get('due_date'):
                return a['due_date']
            return '9999-99-99'

        all_assignments.sort(key=_sort_key)

        # שמור cache
        _save_cache({'_ts': now_ts, 'assignments': all_assignments})
        print(f'\n✅ סה"כ {len(all_assignments)} מטלות נמצאו')
        return all_assignments

    def fetch_cached(self) -> list[dict]:
        """מחזיר cache קיים ללא בקשת רשת."""
        cache = _load_cache()
        return cache.get('assignments', [])
