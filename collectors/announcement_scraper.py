"""
collectors/announcement_scraper.py — Scrape Moodle news/announcements forum.

For each course:
  1. Login to Moodle SSO (reuses TAU_USERNAME / TAU_PASSWORD from .env).
  2. Navigate to course page → find the "news"/"announcements"/"חדשות" forum.
  3. List discussion threads + extract: title, sender, date, body, url.
  4. Compare with previously-seen announcements (by hash of title+date).
  5. Return only NEW ones.

Result is consumed by moodle_watcher.sync() → exporters/announcement_obsidian.

The pipeline is best-effort. If login fails or selectors change, returns [].
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=True)
except Exception:
    pass

from config.paths import DATA_DIR

SEEN_PATH = DATA_DIR / 'announcements_seen.json'
COURSES_YAML = Path(__file__).resolve().parent.parent / 'config' / 'courses.yaml'
MOODLE_BASE = 'https://moodle.tau.ac.il'


def _load_seen() -> dict:
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def _save_seen(d: dict) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]


def _course_map() -> dict[str, tuple[str, str]]:
    """course_key -> (course_id, course_name)"""
    out = {}
    if not COURSES_YAML.exists():
        return out
    cfg = yaml.safe_load(COURSES_YAML.read_text(encoding='utf-8')) or {}
    for k, v in (cfg.get('courses') or {}).items():
        moodle = (v.get('sources') or {}).get('moodle') or {}
        cid = moodle.get('course_id')
        if cid and moodle.get('enabled', True):
            out[k] = (str(cid), v.get('name', k))
    return out


DISCOVERED = DATA_DIR / 'moodle_course_ids.json'


def _norm_name(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '').replace('׳', "'").replace('״', '"')).strip()


def course_numbers() -> dict[str, str]:
    """{university course number without leading zeros: active course key} — from the my.tau schedule cache
    (scripts/tau_watch.py), matched to config.paths names ('מיקרו כלכלה ב׳' ⊂ "מיקרו כלכלה ב' - כלכלת רווחה…")."""
    from config.paths import ACTIVE_COURSES, COURSE_NAMES, COURSE_ALT_NAMES
    out = {}
    try:                                   # course_code in courses.yaml first — it survives a course rename (12/10)
        cfg = yaml.safe_load(COURSES_YAML.read_text(encoding='utf-8')) or {}
        for k in ACTIVE_COURSES:
            code = str(((cfg.get('courses') or {}).get(k) or {}).get('course_code') or '')
            if code.isdigit():
                out[code.lstrip('0')] = k
    except (OSError, ValueError):
        pass
    try:
        events = json.loads((DATA_DIR / 'tau_calendar.json').read_text(encoding='utf-8'))['events']
    except (OSError, ValueError, KeyError):
        return out
    for e in events:
        num = (e.get('course_number') or '').lstrip('0')
        if not num:
            continue
        for k in ACTIVE_COURSES:
            names = [COURSE_NAMES.get(k, '')] + COURSE_ALT_NAMES.get(k, [])
            if num not in out and any(_norm_name(n) and _norm_name(n) in _norm_name(e.get('course', '')) for n in names):
                out[num] = k
    return out


def discover(page, numbers: dict[str, str] | None = None) -> dict[str, list[str]]:
    """10/10: the new semester's Moodle ids were 'TO_BE_FILLED'. Read the owner's enrolled courses from /my/ and map each id
    (course number + 2-digit group, e.g. 3682157 + 01) to a course key. Merged into data/moodle_course_ids.json."""
    numbers = numbers if numbers is not None else course_numbers()
    try:
        cur = json.loads(DISCOVERED.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cur = {}
    if not numbers:
        return cur
    try:
        page.goto(f'{MOODLE_BASE}/my/', timeout=30000)
        page.wait_for_load_state('networkidle', timeout=30000)
        page.wait_for_timeout(3000)
        hrefs = page.eval_on_selector_all('a[href*="course/view.php?id="]', 'els => els.map(e => e.href)')
    except Exception as e:
        print(f'  ⚠️ moodle discover failed: {e}', file=sys.stderr)
        return cur
    for h in hrefs:
        m = re.search(r'id=(\d+)', h or '')
        if not m:
            continue
        cid = m.group(1)
        for num, ck in numbers.items():
            if cid.startswith(num) and len(cid) - len(num) == 2 and cid not in cur.get(ck, []):
                cur.setdefault(ck, []).append(cid)
    DISCOVERED.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding='utf-8')
    return cur


def moodle_ids(key: str, configured=None, discovered: dict | None = None) -> list[str]:
    """15/10: the Moodle ids to read for one course — the yaml id if filled by hand, else every discovered group id
    (homework and slides skipped the new semester's courses while the yaml said 'TO_BE_FILLED')."""
    if str(configured or '').isdigit():
        return [str(configured)]
    try:
        disc = discovered if discovered is not None else json.loads(DISCOVERED.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        disc = {}
    return [str(c) for c in disc.get(key, []) if str(c).isdigit()]


def course_targets(discovered: dict | None = None) -> list[tuple[str, str, str]]:
    """(course key, moodle id, name): the configured ids (courses.yaml) + every discovered group id."""
    from config.paths import COURSE_NAMES
    out = [(k, cid, name) for k, (cid, name) in _course_map().items() if str(cid).isdigit()]
    try:
        disc = discovered if discovered is not None else json.loads(DISCOVERED.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        disc = {}
    for k, ids in disc.items():
        out += [(k, cid, COURSE_NAMES.get(k, k)) for cid in ids if (k, cid) not in {(a, b) for a, b, _ in out}]
    return out


def _login(page) -> bool:
    """SSO login via TAU's NIDP IdP. Returns True iff we land back on Moodle.

    Uses the same field IDs as collectors/moodle_collector.py (the proven
    pipeline): #Ecom_User_ID, #Ecom_User_Pid (TZ), #Ecom_Password, #loginButton.
    """
    username = os.getenv('TAU_USERNAME', '')
    password = os.getenv('TAU_PASSWORD', '')
    tau_id   = os.getenv('TAU_ID', '')
    if not username or not password:
        print('  ⚠️ TAU_USERNAME/TAU_PASSWORD missing', file=sys.stderr)
        return False

    page.goto(f'{MOODLE_BASE}/login/index.php')
    page.wait_for_load_state('networkidle', timeout=20000)

    # Trigger SSO redirect if the Moodle page shows a chooser
    for sel in ['a[href*="shibboleth"]', 'a[href*="saml"]',
                'a[href*="nidp"]', 'text=SSO',
                'a[href*="auth/shibboleth"]']:
        try:
            page.click(sel, timeout=2500)
            break
        except Exception:
            continue
    page.wait_for_load_state('networkidle', timeout=20000)

    # On NIDP IdP — three fields + login button
    if 'nidp.tau.ac.il' in page.url or 'tau.ac.il' in page.url:
        try:
            page.fill('#Ecom_User_ID', username)
            if tau_id:
                page.fill('#Ecom_User_Pid', tau_id)
            page.fill('#Ecom_Password', password)
            page.click('#loginButton')
            page.wait_for_timeout(8000)
            page.wait_for_load_state('networkidle', timeout=30000)
        except Exception as e:
            print(f'  ⚠️ NIDP form fill failed: {e}', file=sys.stderr)
            return False

    # Authenticated iff we're back on the Moodle host
    final_url = (page.url or '').lower()
    return 'moodle.tau.ac.il' in final_url and 'login' not in final_url


def _scrape_course_announcements(page, course_id: str, course_key: str,
                                 course_name: str) -> list[dict]:
    """Find news forum on course page and extract threads."""
    course_url = f'{MOODLE_BASE}/course/view.php?id={course_id}'
    try:
        page.goto(course_url, timeout=20000)
        page.wait_for_load_state('networkidle', timeout=15000)
    except Exception as e:
        print(f'    ⚠️ {course_key}: cannot open course page: {e}', file=sys.stderr)
        return []

    # Find the news/announcements forum
    forums = page.eval_on_selector_all(
        'a[href*="/mod/forum/view.php"]',
        'els => els.map(e => ({href: e.href, text: (e.innerText||"").trim()}))'
    )
    keywords = ('news', 'announcements', 'חדשות', 'הודעות', 'announcement')
    ordered = [f for f in forums if any(k in (f.get('text') or '').lower() for k in keywords)] + \
              [f for f in forums if not any(k in (f.get('text') or '').lower() for k in keywords)]
    # 20/10: the first forum link on a course page was another course's (or a shared) forum — the same
    # discussion (d=3054 "תרגולים יום רביעי") was saved under macro, Software 1 and probability. A forum counts only
    # if its own page leads back to THIS course.
    news_forum = None
    for f in ordered[:6]:
        try:
            page.goto(f['href'], timeout=15000)
            page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            continue
        # Moodle marks every page with its own course: <body class="… course-<id> …"> (the menus link every course)
        body_cls = page.evaluate('document.body.className') or ''
        if re.search(rf'\bcourse-{course_id}\b', body_cls):
            news_forum = f
            break
    if not news_forum:
        return []

    # Each thread row
    threads = page.eval_on_selector_all(
        'a[href*="/mod/forum/discuss.php"]',
        'els => els.map(e => ({href: e.href, text: (e.innerText||"").trim()}))'
    )

    out = []
    done_d: set[str] = set()
    for t in threads[:20]:  # cap at 20 most recent
        href = t.get('href', '')
        title = (t.get('text') or '').strip()
        if not href or not title or re.fullmatch(r'(?:המשך|More|Read more|קרא עוד)\W*', title, re.I):
            continue                                   # 20/10: "המשך..." / "More..." links were saved as announcements
        d = (re.search(r'discuss\.php\?d=(\d+)', href) or [None, ''])[1]
        if d and d in done_d:
            continue
        done_d.add(d)
        try:
            page.goto(href, timeout=15000)
            page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            continue
        # 20/10: a side block on the forum page lists other courses' posts (Software 1's appeared under every course)
        if not re.search(rf'\bcourse-{course_id}\b', page.evaluate('document.body.className') or ''):
            continue

        # Extract first post body + author + date
        try:
            body = page.eval_on_selector(
                '.forumpost .post-content-container, .forumpost .posting, [data-region="post-content-container"]',
                'el => el.innerText'
            ) or ''
        except Exception:
            body = ''
        try:
            author = page.eval_on_selector(
                '.forumpost .author a, .forumpost .author',
                'el => el.innerText'
            ) or ''
        except Exception:
            author = ''
        try:
            date_text = page.eval_on_selector(
                '.forumpost time, .forumpost .author + .text-muted',
                'el => el.getAttribute("datetime") || el.innerText'
            ) or ''
        except Exception:
            date_text = ''

        date_iso = _normalize_date(date_text)
        out.append({
            'course_key':  course_key,
            'course_name': course_name,
            'title':       title,
            'sender':      author.strip() or 'Moodle',
            'date':        date_iso,
            'body':        body.strip()[:5000],
            'moodle_url':  href,
        })
    return out


_MONTHS = {
    'ינואר': '01', 'פברואר': '02', 'מרץ': '03', 'אפריל': '04',
    'מאי': '05', 'יוני': '06', 'יולי': '07', 'אוגוסט': '08',
    'ספטמבר': '09', 'אוקטובר': '10', 'נובמבר': '11', 'דצמבר': '12',
}


def _normalize_date(s: str) -> str:
    if not s:
        return datetime.now().strftime('%Y-%m-%d')
    # ISO
    m = re.search(r'(\d{4}-\d{2}-\d{2})', s)
    if m:
        return m.group(1)
    # "12 בנובמבר 2025"
    m = re.search(r'(\d{1,2})\s*ב?(' + '|'.join(_MONTHS) + r')\s*(\d{4})', s)
    if m:
        d, mo, y = m.group(1), _MONTHS[m.group(2)], m.group(3)
        return f'{y}-{mo}-{d.zfill(2)}'
    # "12/11/2025"
    m = re.search(r'(\d{1,2})[./](\d{1,2})[./](\d{4})', s)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        return f'{y}-{mo.zfill(2)}-{d.zfill(2)}'
    return datetime.now().strftime('%Y-%m-%d')


def fetch_new_announcements(headless: bool = True) -> list[dict]:
    """Scrape all enabled courses for new announcements. Returns NEW ones only."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('  ⚠️ playwright not installed', file=sys.stderr)
        return []

    seen = _load_seen()
    baselined = set(seen.get('__baseline_ids__', {}).get('ids', []))

    new_items: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context()
        page = ctx.new_page()

        if not _login(page):
            browser.close()
            return []

        targets = course_targets(discover(page))
        taken: dict[str, str] = {}                     # discussion id → the course it was saved under (this run)
        for ck, cid, cname in targets:
            try:
                items = _scrape_course_announcements(page, cid, ck, cname)
            except Exception as e:
                print(f'    ⚠️ {ck}: {e}', file=sys.stderr)
                continue
            first_time = cid not in baselined          # a newly opened course: its old posts aren't news
            for it in items:
                did = (re.search(r'discuss\.php\?d=(\d+)', it.get('moodle_url', '') or '') or [None, ''])[1]
                if did and taken.get(did, ck) != ck:   # one discussion = one course, never copied to others
                    continue
                if did:
                    taken[did] = ck
                key = _hash(f"{ck}::{it['title']}::{it['date']}")
                if key in seen:
                    continue
                seen[key] = {
                    'course_key': ck,
                    'title':      it['title'],
                    'date':       it['date'],
                    'first_seen': datetime.now().isoformat(),
                }
                if not first_time:
                    new_items.append(it)
            if items or first_time:
                baselined.add(cid)
        seen['__baseline_ids__'] = {'ids': sorted(baselined)}

        browser.close()

    _save_seen(seen)
    return new_items


if __name__ == '__main__':
    items = fetch_new_announcements(headless='--head' not in sys.argv)
    print(f'Found {len(items)} new announcements')
    for a in items[:5]:
        print(f"  - [{a['course_key']}] {a['date']} | {a['title']} (by {a['sender']})")
