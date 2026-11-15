"""
Moodle Collector - מוריד קבצי PDF ומצגות מ-Moodle
"""

import os
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass
import io
import re
import time
import requests

from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from typing import List, Dict, Optional
import yaml
import json
from datetime import datetime


class MoodleCollector:
    def __init__(self, config_path: str = "config/courses.yaml"):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        self.base_url = self.config['settings']['moodle']['base_url']
        self.username = os.getenv('TAU_USERNAME')
        self.tau_id = os.getenv('TAU_ID', '')
        self.password = os.getenv('TAU_PASSWORD')
        self.output_dir = Path("data/moodle")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._session = None  # requests session with cookies after login

    # ─────────────────────────── login ───────────────────────────

    def _login_and_get_session(self) -> requests.Session:
        """Login via Playwright, return requests.Session with cookies"""
        if self._session:
            return self._session

        print("🔐 מתחבר ל-Moodle...")
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()

            page.goto(f"{self.base_url}/login/index.php")
            page.wait_for_load_state('networkidle')

            # Try to click SSO button
            for selector in [
                'a[href*="shibboleth"]', 'a[href*="saml"]', 'a[href*="nidp"]',
                'text=כניסה דרך שירות מרכזי', 'text=Shibboleth', 'text=SSO',
                'a[href*="auth/shibboleth"]'
            ]:
                try:
                    page.click(selector, timeout=3000)
                    break
                except:
                    continue

            page.wait_for_load_state('networkidle')

            # TAU SAML login
            if 'nidp.tau.ac.il' in page.url or 'tau.ac.il' in page.url:
                page.fill('#Ecom_User_ID', self.username)
                if self.tau_id:
                    page.fill('#Ecom_User_Pid', self.tau_id)
                page.fill('#Ecom_Password', self.password)
                page.click('#loginButton')
                page.wait_for_timeout(8000)
                page.wait_for_load_state('networkidle')

            # Transfer cookies to requests session
            for cookie in ctx.cookies():
                session.cookies.set(
                    cookie['name'],
                    cookie['value'],
                    domain=cookie.get('domain', '').lstrip('.')
                )

            browser.close()

        print("✅ התחברות ל-Moodle הצליחה!")
        self._session = session
        return session

    # ─────────────────────────── collect files ───────────────────────────

    def get_course_files(self, session: requests.Session, course_id: str,
                         file_types: List[str]) -> List[Dict]:
        """Scrape course page for resource links"""
        print(f"📂 סורק קבצים בקורס {course_id}...")

        r = session.get(f"{self.base_url}/course/view.php?id={course_id}")
        if r.status_code != 200:
            print(f"❌ שגיאה בגישה לקורס {course_id}: {r.status_code}")
            return []

        # Find all resource links
        resource_links = re.findall(
            r'href="(https://moodle\.tau\.ac\.il/mod/resource/view\.php\?id=\d+)"[^>]*>(.*?)</a>',
            r.text, re.DOTALL
        )

        files = []
        seen_ids = set()

        for href, raw_text in resource_links:
            res_id = re.search(r'id=(\d+)', href)
            if not res_id or res_id.group(1) in seen_ids:
                continue
            seen_ids.add(res_id.group(1))

            text = re.sub(r'<[^>]+>', '', raw_text).strip()
            text = re.sub(r'\s+', ' ', text).strip()
            # Remove trailing "קובץ" suffix added by Moodle icon spans
            text = re.sub(r'\s+קובץ$', '', text).strip()

            files.append({'title': text or f'resource_{res_id.group(1)}', 'url': href})

        print(f"✅ נמצאו {len(files)} קבצים")
        return files

    def _resolve_pluginfile_url(self, session: requests.Session, resource_url: str) -> Optional[str]:
        """Fetch a Moodle resource page and extract the actual pluginfile.php download URL"""
        try:
            r = session.get(resource_url, timeout=15)
            matches = re.findall(r'href="(https?://[^"]*pluginfile\.php[^"]+)"', r.text)
            if matches:
                return matches[0]
            # Fallback: forcedownload
            matches = re.findall(r'href="([^"]+forcedownload[^"]+)"', r.text)
            if matches:
                return matches[0]
        except Exception:
            pass
        return None

    def download_file(self, session: requests.Session, file_info: Dict,
                      course_key: str) -> Optional[str]:
        """Download a Moodle resource — resolves pluginfile.php URL first"""
        print(f"⬇️  מוריד: {file_info['title']}")

        course_dir = self.output_dir / course_key
        course_dir.mkdir(exist_ok=True)

        try:
            # Resolve actual file URL from resource page
            real_url = self._resolve_pluginfile_url(session, file_info['url'])
            if not real_url:
                print(f"⚠️  לא נמצא קישור להורדה: {file_info['title']}")
                return None

            r = session.get(real_url, allow_redirects=True, timeout=30)

            content_type = r.headers.get('Content-Type', '')
            if 'pdf' in content_type:
                ext = '.pdf'
            elif 'presentation' in content_type or 'powerpoint' in content_type:
                ext = '.pptx'
            elif 'python' in content_type or 'text/plain' in content_type:
                ext = '.py'
            else:
                url_ext = Path(real_url.split('?')[0]).suffix
                ext = url_ext if url_ext in ['.pdf', '.pptx', '.py', '.ipynb'] else '.pdf'

            # Keep Hebrew characters in filename
            safe_title = re.sub(r'[^\w\s\-\u0590-\u05FF]', '', file_info['title']).strip()
            safe_title = re.sub(r'\s+', '_', safe_title)
            if not safe_title:
                safe_title = f"file_{int(time.time())}"

            output_path = course_dir / f"{safe_title}{ext}"

            with open(output_path, 'wb') as f:
                f.write(r.content)

            print(f"✅ נשמר: {output_path}")
            return str(output_path)

        except Exception as e:
            print(f"❌ שגיאה בהורדה: {e}")
            return None

    # ─────────────────────────── wikidot ───────────────────────────

    def _scrape_wikidot_page(self, url: str, prefix: str, course_dir: Path) -> List[Dict]:
        """
        Scrapes a wikidot page for PDF links.
        Uses BeautifulSoup to properly handle:
        - Links where the content is <img> (no text)
        - wdfiles.com subdomain links (tau-cs1001-py.wdfiles.com/local--files/...)
        - Relative /local--files/ links
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            print("  ⚠️  BeautifulSoup לא מותקן — מנסה pip install beautifulsoup4")
            try:
                import subprocess, sys
                subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'beautifulsoup4', '-q'])
                from bs4 import BeautifulSoup
            except Exception as e:
                print(f"  ❌ לא ניתן להתקין BeautifulSoup: {e}")
                return []

        PDF_EXTS = {'.pdf', '.ppt', '.pptx', '.zip'}
        SKIP_DOMAINS = {'wikidot.com', 'colab.research.google.com', 'creativecommons.org'}

        try:
            r = requests.get(url, timeout=20)
            r.encoding = 'utf-8'
            soup = BeautifulSoup(r.text, 'html.parser')
            base = url.split('/')[0] + '//' + url.split('/')[2]

            seen_hrefs = set()
            all_links = []  # list of (href, title)

            for a in soup.find_all('a', href=True):
                href = a['href'].strip()

                # Make absolute
                if href.startswith('/local--files/'):
                    href = base + href
                elif href.startswith('//'):
                    href = 'http:' + href
                elif not href.startswith('http'):
                    continue

                # Filter by extension
                path_lower = href.split('?')[0].lower()
                if not any(path_lower.endswith(ext) for ext in PDF_EXTS):
                    continue

                # Skip non-file domains
                from urllib.parse import urlparse
                parsed = urlparse(href)
                if any(d in parsed.netloc for d in SKIP_DOMAINS):
                    continue

                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                # Get title: link text → img alt → surrounding <td> text → filename
                title = a.get_text(strip=True)
                if not title:
                    img = a.find('img')
                    if img:
                        title = img.get('alt', '').strip()
                if not title:
                    td = a.find_parent('td')
                    if td:
                        # get sibling tds in same row for context
                        tr = td.find_parent('tr')
                        if tr:
                            cells = [c.get_text(strip=True) for c in tr.find_all('td')]
                            title = ' '.join(c for c in cells if c and c.lower() not in ('pdf', 'py', ''))
                if not title:
                    # fallback: filename without extension
                    title = Path(path_lower).stem

                # Clean up: remove "PDF" label alone
                if title.lower() in ('pdf', 'ppt', 'pptx', ''):
                    title = Path(href.split('?')[0]).stem

                all_links.append((href, title))

            if not all_links:
                print(f"  ℹ️  לא נמצאו קישורים להורדה בעמוד {url}")
            else:
                print(f"  🔗 נמצאו {len(all_links)} קישורים בעמוד Wikidot")

            downloaded = []
            for href, title in all_links:
                try:
                    dr = requests.get(href, timeout=20)
                    if dr.status_code == 200 and len(dr.content) > 500:
                        ext = '.pdf'
                        ct = dr.headers.get('content-type', '')
                        if 'presentationml' in ct or href.lower().endswith('.pptx'):
                            ext = '.pptx'
                        elif href.lower().endswith('.ppt'):
                            ext = '.ppt'
                        elif href.lower().endswith('.zip'):
                            ext = '.zip'
                        safe = re.sub(r'[^\w\s\-]', '', title).strip()
                        safe = re.sub(r'\s+', '_', safe) or Path(href).stem
                        path = course_dir / f"{prefix}_{safe}{ext}"
                        with open(path, 'wb') as f:
                            f.write(dr.content)
                        downloaded.append({'title': title, 'url': href, 'local_path': str(path)})
                        print(f"  ✅ ויקידוט: {title} ({ext})")
                    else:
                        print(f"  ⚠️ ויקידוט: {href} — status={dr.status_code}, size={len(dr.content)}")
                except Exception as e:
                    print(f"  ⚠️ שגיאה בהורדה ({href}): {e}")

            return downloaded

        except Exception as e:
            print(f"❌ שגיאה ב-Wikidot ({url}): {e}")
            return []

    def collect_wikidot(self, course_key: str) -> List[Dict]:
        """Collects lecture and recitation PDFs from wikidot (for מדמ"ח)"""
        course = self.config['courses'][course_key]
        wikidot_cfg = course['sources'].get('wikidot', {})

        if not wikidot_cfg.get('enabled'):
            return []

        course_dir = self.output_dir / course_key
        course_dir.mkdir(exist_ok=True)

        all_downloaded = []

        lectures_url = wikidot_cfg.get('lectures_url')
        if lectures_url:
            print(f"\n🌐 אוסף הרצאות מ-Wikidot: {lectures_url}")
            items = self._scrape_wikidot_page(lectures_url, 'wikidot_lecture', course_dir)
            all_downloaded.extend(items)

        recitations_url = wikidot_cfg.get('recitations_url')
        if recitations_url:
            print(f"\n🌐 אוסף תרגולים מ-Wikidot: {recitations_url}")
            items = self._scrape_wikidot_page(recitations_url, 'wikidot_recitation', course_dir)
            all_downloaded.extend(items)

        print(f"✅ הורדו {len(all_downloaded)} קבצים מ-Wikidot")
        return all_downloaded

    # ─────────────────────────── collect course ───────────────────────────

    def collect_course(self, course_key: str) -> List[Dict]:
        """Collects all files for a course (Moodle + wikidot)"""
        course = self.config['courses'][course_key]

        print(f"\n{'='*50}")
        print(f"📚 אוסף קבצים עבור: {course['name']}")
        print(f"{'='*50}\n")

        results = []

        # Moodle
        moodle_cfg = course['sources'].get('moodle', {})
        if moodle_cfg.get('enabled'):
            course_id = str(moodle_cfg.get('course_id', ''))
            from collectors.announcement_scraper import moodle_ids
            ids = moodle_ids(course_key, course_id)     # yaml id, else the discovered group ids (15/10)

            if ids:
                file_types = moodle_cfg.get('file_types', ['.pdf'])
                session = self._login_and_get_session()
                files, seen_urls = [], set()
                for cid in ids:
                    for f in self.get_course_files(session, cid, file_types):
                        if f.get('url') not in seen_urls:
                            seen_urls.add(f.get('url'))
                            files.append(f)

                for file_info in files:
                    local_path = self.download_file(session, file_info, course_key)
                    if local_path:
                        results.append({
                            'course': course_key,
                            'type': 'moodle',
                            'file_info': file_info,
                            'local_path': local_path,
                            'downloaded_at': datetime.now().isoformat()
                        })
                    time.sleep(0.3)
            else:
                print(f"⚠️  יש למלא course_id עבור {course['name']}")

        # Wikidot
        wikidot_results = self.collect_wikidot(course_key)
        for item in wikidot_results:
            if item.get('local_path'):
                results.append({
                    'course': course_key,
                    'type': 'wikidot',
                    'file_info': {'title': item['title'], 'url': item['url']},
                    'local_path': item['local_path'],
                    'downloaded_at': datetime.now().isoformat()
                })

        # Static PDF (e.g. חדו"א notes)
        static_pdf = course['sources'].get('static_pdf')
        if static_pdf and Path(static_pdf).exists():
            results.append({
                'course': course_key,
                'type': 'static_pdf',
                'file_info': {'title': Path(static_pdf).stem, 'url': static_pdf},
                'local_path': static_pdf,
                'downloaded_at': datetime.now().isoformat()
            })
            print(f"✅ Static PDF: {static_pdf}")

        # Save metadata
        metadata_path = self.output_dir / f"{course_key}_metadata.json"
        # Load existing
        existing = []
        if metadata_path.exists():
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except:
                existing = []

        # Merge by local_path (avoid re-downloading same files)
        existing_paths = {r.get('local_path') for r in existing}
        new_results = [r for r in results if r.get('local_path') not in existing_paths]
        existing.extend(new_results)

        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

        print(f"\n✅ הושלם איסוף עבור {course['name']}: {len(results)} קבצים")
        return results

    def collect_all(self) -> Dict[str, List]:
        """Collects all courses"""
        all_results = {}
        for course_key in self.config['courses'].keys():
            results = self.collect_course(course_key)
            all_results[course_key] = results
        return all_results


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    collector = MoodleCollector()
    results = collector.collect_course('calculus_2')
    print(f"\nסיכום: נאספו {len(results)} קבצים")
