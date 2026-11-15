"""
fetch_assignments_now.py — מוריד את כל קבצי המטלות וההגשות ממודל ומ-Wikidot,
ובונה cache מלא ל-Obsidian.

הרץ: python collectors/fetch_assignments_now.py
"""
from __future__ import annotations
import json, os, re, sys, time
from datetime import datetime
from pathlib import Path

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

ROOT = Path(__file__).parent.parent
ASSIGNMENTS_DIR = ROOT / 'data' / 'assignments'
ASSIGNMENTS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_PATH = ASSIGNMENTS_DIR / '_cache.json'

MOODLE_BASE = 'https://moodle.tau.ac.il'
USERNAME = os.getenv('TAU_USERNAME', '')
PASSWORD = os.getenv('TAU_PASSWORD', '')

# ─── נתוני המטלות שנאספו ידנית מהדפדפן ──────────────────────────────────────
TODAY = datetime(2026, 4, 27)

ASSIGNMENTS_DATA = [
    # ── מיקרו כלכלה א' ────────────────────────────────────────────────────────
    {
        'course_key': 'microeconomics',
        'course_name': "מיקרו כלכלה א'",
        'title': 'תרגיל 1',
        'due_date': None,
        'days_left': None,
        'submitted': True,
        'submission_date': None,
        'source': 'moodle',
        'url': f'{MOODLE_BASE}/mod/resource/view.php?id=451815',
        'assignment_file_url': f'{MOODLE_BASE}/mod/resource/view.php?id=451815',
        'submission_file_url': None,
        'notes': 'תרגיל 1 — פתרון פורסם על ידי המרצה',
    },
    {
        'course_key': 'microeconomics',
        'course_name': "מיקרו כלכלה א'",
        'title': 'פתרון תרגיל 1 (מרצה)',
        'due_date': None,
        'days_left': None,
        'submitted': True,
        'source': 'moodle',
        'url': f'{MOODLE_BASE}/mod/resource/view.php?id=451823',
        'assignment_file_url': f'{MOODLE_BASE}/mod/resource/view.php?id=451823',
        'submission_file_url': None,
        'notes': 'פתרון רשמי שפורסם',
    },
    {
        'course_key': 'microeconomics',
        'course_name': "מיקרו כלכלה א'",
        'title': 'תרגיל 2',
        'due_date': '2026-04-30T14:00:00',
        'days_left': 3,
        'submitted': False,
        'source': 'moodle',
        'url': f'{MOODLE_BASE}/mod/assign/view.php?id=460612',
        'assignment_file_url': f'{MOODLE_BASE}/mod/resource/view.php?id=460609',
        'submission_file_url': None,
        'notes': '⚠️ דחוף! הגשה 30/04 14:00',
    },
    # ── מתמטיקה בדידה 2 ───────────────────────────────────────────────────────
    {
        'course_key': 'discrete_math_2',
        'course_name': 'מתמטיקה בדידה 2',
        'title': 'תרגיל 1',
        'due_date': '2026-05-10T23:59:00',
        'days_left': 13,
        'submitted': False,
        'source': 'moodle',
        'url': f'{MOODLE_BASE}/mod/assign/view.php?id=419465',
        'assignment_file_url': f'{MOODLE_BASE}/mod/assign/view.php?id=419465',
        'submission_file_url': None,
    },
    {
        'course_key': 'discrete_math_2',
        'course_name': 'מתמטיקה בדידה 2',
        'title': 'תרגיל 2',
        'due_date': '2026-05-10T23:59:00',
        'days_left': 13,
        'submitted': False,
        'source': 'moodle',
        'url': f'{MOODLE_BASE}/mod/assign/view.php?id=460783',
        'assignment_file_url': f'{MOODLE_BASE}/mod/assign/view.php?id=460783',
        'submission_file_url': None,
    },
    # ── אלגברה לינארית 2ב ─────────────────────────────────────────────────────
    {
        'course_key': 'linear_algebra_2',
        'course_name': 'אלגברה לינארית 2ב',
        'title': 'תרגיל בית 1',
        'due_date': None,
        'days_left': None,
        'submitted': True,
        'submission_date': '2026-04-20T08:16:00',
        'source': 'moodle',
        'url': f'{MOODLE_BASE}/mod/assign/view.php?id=449135',
        'assignment_file_url': f'{MOODLE_BASE}/pluginfile.php/579185/mod_assign/introattachment/0/ex1.pdf?forcedownload=1',
        'submission_file_url': f'{MOODLE_BASE}/pluginfile.php/579185/assignsubmission_file/submission_files/498636/%D7%9C%D7%99%D7%A0%D7%90%D7%A8%D7%99%D7%AA%202%20hw1.pdf?forcedownload=1',
        'submission_filename': 'לינארית 2 hw1.pdf',
    },
    {
        'course_key': 'linear_algebra_2',
        'course_name': 'אלגברה לינארית 2ב',
        'title': 'תרגיל בית 2',
        'due_date': None,
        'days_left': None,
        'submitted': False,
        'source': 'moodle',
        'url': f'{MOODLE_BASE}/mod/assign/view.php?id=459596',
        'assignment_file_url': f'{MOODLE_BASE}/pluginfile.php/590071/mod_assign/introattachment/0/%D7%AA%D7%A8%D7%92%D7%99%D7%9C%20%D7%91%D7%99%D7%AA%202.pdf?forcedownload=1',
        'submission_file_url': None,
    },
    # ── חשבון דיפרנציאלי ואינטגרלי 2ב ────────────────────────────────────────
    {
        'course_key': 'calculus_2',
        'course_name': 'חשבון דיפרנציאלי ואינטגרלי 2ב',
        'title': 'מטלה 1',
        'due_date': None,
        'days_left': None,
        'submitted': True,
        'submission_date': '2026-04-10T00:00:00',
        'source': 'moodle',
        'url': f'{MOODLE_BASE}/mod/assign/view.php?id=440336',
        'assignment_file_url': f'{MOODLE_BASE}/pluginfile.php/570122/mod_assign/introattachment/0/hw_1.pdf?forcedownload=1',
        'submission_file_url': f'{MOODLE_BASE}/pluginfile.php/570122/assignsubmission_file/submission_files/500687/%D7%97%D7%93%D7%95%D7%B4%D7%90%20hw1.pdf?forcedownload=1',
        'submission_filename': 'חדו״א hw1.pdf',
    },
    # ── מבוא מורחב למדמ"ח ────────────────────────────────────────────────────
    {
        'course_key': 'intro_to_cs',
        'course_name': 'מבוא מורחב למדמ"ח',
        'title': 'תרגיל בית 1',
        'due_date': '2026-05-07T23:59:00',
        'days_left': 10,
        'submitted': True,
        'source': 'moodle+wikidot',
        'url': f'{MOODLE_BASE}/mod/assign/view.php?id=454247',
        'assignment_file_url': 'http://tau-cs1001-py.wikidot.com/local--files/home-assignments-2026b/HW1_2026b.pdf',
        'submission_file_url': f'{MOODLE_BASE}/pluginfile.php/111111/assignsubmission_file/submission_files/222222/hw1_STUDENTID.pdf?forcedownload=1',
        'submission_filename': 'hw1_STUDENTID.pdf',
        'extra_files': [
            {
                'url': 'http://tau-cs1001-py.wikidot.com/local--files/home-assignments-2026b/skeleton1.py',
                'name': 'skeleton1.py',
            }
        ],
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Download helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', s).strip('_ ')


def _check_grading(assign_urls: list[dict], ctx) -> dict[str, dict]:
    """
    בודק ציונים והערות מרצה על מטלות שהוגשו.
    מחזיר: {assign_url: {grade, feedback, graded_on}}
    """
    from playwright.sync_api import TimeoutError as PWTimeout
    results = {}
    page = ctx.new_page()

    for item in assign_urls:
        url = item.get('url', '')
        if not url or '/assign/' not in url or not item.get('submitted'):
            continue
        try:
            page.goto(url, wait_until='networkidle', timeout=20000)
            content = page.content()

            # ציון
            grade = None
            grade_patterns = [
                r'ציון[:\s]+([0-9]+(?:\.[0-9]+)?)',
                r'Grade[:\s]+([0-9]+(?:\.[0-9]+)?)',
                r'>([0-9]+(?:\.[0-9]+)?)\s*/\s*[0-9]+<',
            ]
            for pat in grade_patterns:
                m = re.search(pat, content)
                if m:
                    grade = m.group(1)
                    break

            # בדוק אם ניתן ציון בכלל
            grading_status = 'לא ניתן ציון'
            if 'ניתן ציון' in content and 'לא ניתן ציון' not in content:
                grading_status = 'ניתן ציון'
            elif grade:
                grading_status = 'ניתן ציון'

            # הערות מרצה — חפש feedbackcomments
            feedback = None
            fb_patterns = [
                r'<div[^>]*class="[^"]*feedbackcomment[^"]*"[^>]*>(.*?)</div>',
                r'הערות מורה[^<]*</td>\s*<td[^>]*>(.*?)</td>',
                r'Feedback comments[^<]*</td>\s*<td[^>]*>(.*?)</td>',
            ]
            for pat in fb_patterns:
                m = re.search(pat, content, re.DOTALL | re.IGNORECASE)
                if m:
                    raw = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                    if raw and len(raw) > 2:
                        feedback = raw
                        break

            # קובץ פידבק מרצה
            feedback_files = re.findall(r'href="([^"]+assignfeedback_file[^"]+)"', content)

            results[url] = {
                'grade': grade,
                'grading_status': grading_status,
                'feedback': feedback,
                'feedback_files': feedback_files[:3],
            }

            status_icon = '⭐' if grade else ('✅ בודק' if grading_status == 'ניתן ציון' else '⏳ לא נבדק')
            print(f'  {status_icon} {item["title"]} ({item["course_name"]}): ציון={grade or "—"}, פידבק={bool(feedback)}')

        except Exception as e:
            print(f'  ⚠️  שגיאה בבדיקת ציון {url}: {e}')
            results[url] = {}

    page.close()
    return results


def _moodle_login(page, ctx):
    """מתחבר למודל דרך SSO."""
    page.goto(f'{MOODLE_BASE}/login/index.php', wait_until='networkidle', timeout=30000)
    for sel in ['a[href*="shibboleth"]', 'a[href*="saml"]', 'text=כניסה דרך שירות מרכזי']:
        try:
            if page.locator(sel).count() > 0:
                page.click(sel); break
        except Exception:
            pass
    page.wait_for_load_state('networkidle', timeout=20000)
    for u_sel in ['#username', 'input[name="username"]', 'input[type="text"]']:
        try:
            if page.locator(u_sel).count() > 0:
                page.fill(u_sel, USERNAME); break
        except Exception:
            pass
    for p_sel in ['#password', 'input[name="password"]', 'input[type="password"]']:
        try:
            if page.locator(p_sel).count() > 0:
                page.fill(p_sel, PASSWORD); break
        except Exception:
            pass
    for btn in ['button[type="submit"]', 'input[type="submit"]', 'text=כניסה', 'text=Login']:
        try:
            if page.locator(btn).count() > 0:
                page.click(btn); break
        except Exception:
            pass
    page.wait_for_load_state('networkidle', timeout=30000)


def _download_and_check_grades(urls_and_paths: list[tuple[str, Path]], enriched: list[dict]):
    """מוריד קבצים + בודק ציונים בתוך אותה session Playwright."""
    import requests as _req
    from playwright.sync_api import sync_playwright

    if not USERNAME or not PASSWORD:
        print('  ⚠️  TAU_USERNAME/TAU_PASSWORD לא מוגדרים — מדלג על הורדות')
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        print('  🔐 מתחבר למודל...')
        _moodle_login(page, ctx)
        print('  ✅ מחובר')

        # ── הורדות ──────────────────────────────────────────────────────────
        print('\n  📥 מוריד קבצים...')
        for url, out_path in urls_and_paths:
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                if out_path.exists() and out_path.stat().st_size > 500:
                    print(f'  ⏭️  קיים: {out_path.name}')
                    continue

                # Wikidot — ללא auth
                if 'wikidot.com' in url or 'wdfiles.com' in url:
                    r = _req.get(url, timeout=20)
                    if r.status_code == 200 and len(r.content) > 200:
                        out_path.write_bytes(r.content)
                        print(f'  ✅ {out_path.name} ({len(r.content)//1024}KB)')
                    else:
                        print(f'  ⚠️  {out_path.name}: HTTP {r.status_code}')
                    continue

                # Moodle pluginfile — ישיר דרך requests עם cookies
                if 'pluginfile.php' in url or 'forcedownload' in url:
                    cookies = {c['name']: c['value'] for c in ctx.cookies()}
                    s = _req.Session()
                    s.cookies.update(cookies)
                    r = s.get(url, timeout=30, allow_redirects=True)
                    if r.status_code == 200 and len(r.content) > 500:
                        out_path.write_bytes(r.content)
                        print(f'  ✅ {out_path.name} ({len(r.content)//1024}KB)')
                    else:
                        print(f'  ⚠️  {out_path.name}: {r.status_code}')
                    continue

                # Moodle resource/assign page — navigate + download
                try:
                    with ctx.expect_download(timeout=15000) as dl_info:
                        page.goto(url, timeout=20000)
                    dl = dl_info.value
                    dl.save_as(str(out_path))
                    print(f'  ✅ {out_path.name}')
                except Exception:
                    # אם אין download trigger — ייתכן שזה דף HTML
                    # נסה דרך requests עם cookies
                    cookies = {c['name']: c['value'] for c in ctx.cookies()}
                    s = _req.Session()
                    s.cookies.update(cookies)
                    # גלה redirect לקובץ אמיתי
                    page.goto(url, timeout=20000)
                    final_url = page.url
                    if final_url != url and ('pluginfile' in final_url or final_url.endswith(('.pdf','.py','.zip'))):
                        r = s.get(final_url, timeout=30, allow_redirects=True)
                        if r.status_code == 200 and len(r.content) > 500:
                            out_path.write_bytes(r.content)
                            print(f'  ✅ {out_path.name} (redirect, {len(r.content)//1024}KB)')
                        else:
                            print(f'  ⚠️  {out_path.name}: redirect failed {r.status_code}')
                    elif '/mod/assign/' in url or url.endswith('.php') or 'view.php' in url:
                        # 11/11: an assignment / activity is a page, not a file — not a failure; its text and
                        # attachments are read by moodle_assignment_scraper + scripts/download_assignments.py
                        print(f'  ℹ️  {out_path.name}: דף במודל, לא קובץ — מדלג')
                    else:
                        print(f'  ⚠️  {out_path.name}: לא ניתן להוריד')

            except Exception as e:
                print(f'  ❌ {out_path.name}: {e}')

        # ── בדיקת ציונים והערות ──────────────────────────────────────────────
        print('\n  🎓 בודק ציונים והערות מרצה...')
        grade_page = ctx.new_page()
        for entry in enriched:
            url = entry.get('url', '')
            if not url or '/assign/' not in url or not entry.get('submitted'):
                continue
            try:
                grade_page.goto(url, wait_until='networkidle', timeout=20000)
                content = grade_page.content()

                # ציון
                grade = None
                for pat in [
                    r'class="grade"[^>]*>([0-9]+(?:[.,][0-9]+)?)',
                    r'ציון[^<]*</td>\s*<td[^>]*>([0-9]+(?:[.,][0-9]+)?)',
                    r'>([0-9]+(?:[.,][0-9]+)?)\s*/\s*100<',
                ]:
                    m = re.search(pat, content)
                    if m:
                        grade = m.group(1)
                        break

                # סטטוס בדיקה
                graded = 'ניתן ציון' in content and 'לא ניתן ציון' not in content
                entry['grade'] = grade
                entry['graded'] = graded

                # הערות טקסטואליות
                feedback = None
                for pat in [
                    r'feedbackcomments[^>]*>(.*?)</div',
                    r'הערות מורה.*?</td>\s*<td[^>]*>(.*?)</td>',
                    r'Feedback comments.*?</td>\s*<td[^>]*>(.*?)</td>',
                    r'class="submissionstatustable".*?הערה.*?<td[^>]*>(.*?)</td>',
                ]:
                    m = re.search(pat, content, re.DOTALL | re.IGNORECASE)
                    if m:
                        raw = re.sub(r'<[^>]+>', ' ', m.group(1)).strip()
                        if raw and len(raw) > 3 and raw not in ('—', '-', ''):
                            feedback = raw[:500]
                            break
                entry['feedback'] = feedback

                # קבצי פידבק מרצה
                fb_files = re.findall(r'href="([^"]*assignfeedback_file[^"]*)"', content)
                entry['feedback_files'] = fb_files[:3]

                icon = f'⭐ {grade}/100' if grade else ('🔍 נבדק' if graded else '⏳ לא נבדק')
                print(f'  {icon} — {entry["title"]} ({entry["course_name"]})')
                if feedback:
                    print(f'       💬 הערה: {feedback[:80]}...')

            except Exception as e:
                print(f'  ⚠️  {entry["title"]}: {e}')

        grade_page.close()
        browser.close()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all():
    """מוריד את כל הקבצים ובונה cache."""
    print('\n📥 מוריד קבצי מטלות והגשות...\n')

    to_download: list[tuple[str, Path]] = []
    enriched = []

    for a in ASSIGNMENTS_DATA:
        course_key = a['course_key']
        course_dir = ASSIGNMENTS_DIR / course_key
        course_dir.mkdir(parents=True, exist_ok=True)

        entry = dict(a)
        entry['local_path'] = None
        entry['submission_local_path'] = None

        # קובץ המטלה (שאלות)
        if a.get('assignment_file_url'):
            ext = Path(a['assignment_file_url'].split('?')[0]).suffix or '.pdf'
            fname = f"{_safe_name(a['title'])}_assignment{ext}"
            out = course_dir / fname
            to_download.append((a['assignment_file_url'], out))
            entry['local_path'] = str(out)

        # קובץ ההגשה שלי
        if a.get('submission_file_url'):
            fname = a.get('submission_filename', f"{_safe_name(a['title'])}_submission.pdf")
            out = course_dir / f"✅_{_safe_name(fname)}"
            to_download.append((a['submission_file_url'], out))
            entry['submission_local_path'] = str(out)

        # קבצים נוספים (skeleton וכו')
        for ef in a.get('extra_files', []):
            out = course_dir / ef['name']
            to_download.append((ef['url'], out))

        enriched.append(entry)

    # הורד הכל + בדוק ציונים
    _download_and_check_grades(to_download, enriched)

    # עדכן days_left בזמן ריצה אמיתי
    now = datetime.now()
    for e in enriched:
        if e.get('due_date'):
            try:
                due = datetime.fromisoformat(e['due_date'])
                e['days_left'] = (due - now).days
            except Exception:
                pass

    # שמור cache
    cache = {'_ts': time.time(), 'assignments': enriched}
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(f'\n✅ נשמר: {CACHE_PATH}')
    print(f'   {len(enriched)} מטלות, {sum(1 for e in enriched if e.get("submitted")) } הוגשו, {sum(1 for e in enriched if not e.get("submitted"))} ממתינות')

    return enriched


def main() -> list:
    """Run the full assignment fetch + Obsidian sync pipeline.

    Exposed as a module-level callable so collectors.moodle_watcher.sync(run_fetcher=True)
    can trigger it without spawning a subprocess.
    """
    sys.path.insert(0, str(ROOT))
    assignments = fetch_all()

    print('\n🔄 מסנכרן ל-Obsidian...')
    from exporters.obsidian_exporter import (
        export_assignments, write_home_page, write_course_index,
        COURSE_NAMES, _safe_dirname, STUDY_DIR,
    )
    export_assignments()
    write_home_page()
    for ck in COURSE_NAMES:
        if (STUDY_DIR / _safe_dirname(COURSE_NAMES[ck])).exists():
            write_course_index(ck)
    print('✅ Obsidian עודכן')
    return assignments


if __name__ == '__main__':
    main()
