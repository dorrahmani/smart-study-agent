"""Download all assignment PDFs from Moodle (uses scraped cache).
11/11: and what the PDF alone misses — the assignment page's description (.md, no login needed) and its other attached
files (collectors.moodle_assignment_scraper.save_assignment_extras). GET only; nothing is submitted."""
import sys, os, json, re
from pathlib import Path
sys.path.insert(0, '/home/ubuntu/smart_study_agent')
os.chdir('/home/ubuntu/smart_study_agent')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/smart_study_agent/.env', override=True)

from collectors.moodle_collector import MoodleCollector
from collectors.moodle_assignment_scraper import save_assignment_extras, extras_missing
import requests

CACHE = Path('data/assignments/_cache.json')
ASSIGN_DIR = Path('data/assignments')

d = json.load(open(CACHE, encoding='utf-8'))
items = [a for a in d.get('assignments', []) if isinstance(a, dict)]
print(f'Total assignments: {len(items)}\n')

# Filter: those with a file URL but no local file
def safe_name(t: str) -> str:
    t = re.sub(r'[<>:"/\\|?*]', '', t).strip()
    return t[:80]

texts = []
for a in items:                                  # the descriptions: from the cache, no login
    if a.get('course_key'):
        texts += save_assignment_extras(a, ASSIGN_DIR / a['course_key'])
if texts:
    print(f'📝 assignment descriptions saved: {len(texts)}\n')

needs_download = []
for a in items:
    url = a.get('assignment_file_url')
    if not url: continue
    ck = a.get('course_key')
    title = safe_name(a.get('title', 'untitled'))
    # Guess file name from URL OR use title
    out = ASSIGN_DIR / ck / f'{title}_assignment.pdf'
    if out.exists() and out.stat().st_size > 1000:
        continue
    needs_download.append((a, out))
extras = [a for a in items if a.get('course_key') and extras_missing(a, ASSIGN_DIR / a['course_key'])]

print(f'Need to download: {len(needs_download)}' + (f' (+ more attachments of {len(extras)})' if extras else '') + '\n')
if not needs_download and not extras:
    print('✅ All assignment PDFs already downloaded.'); sys.exit(0)

# Login to Moodle
print('🔐 Logging into Moodle...', flush=True)
mc = MoodleCollector()
sess = mc._login_and_get_session()
print('✅ Logged in\n')

ok = 0
for a, out in needs_download:
    url = a['assignment_file_url']
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f'⬇️  {a["course_key"]:18s} | {a["title"]} ...', flush=True)
    try:
        r = sess.get(url, timeout=60, allow_redirects=True)
        r.raise_for_status()
        ct = r.headers.get('Content-Type', '')
        if 'pdf' not in ct.lower() and not r.content[:4] == b'%PDF':
            print(f'    ⚠️ not a PDF ({ct[:40]}) — saving anyway')
        out.write_bytes(r.content)
        print(f'    ✅ {out.name} ({len(r.content)//1024}KB)')
        ok += 1
    except Exception as e:
        print(f'    ❌ {e}')
for a in extras:
    for p in save_assignment_extras(a, ASSIGN_DIR / a['course_key'], sess):
        print(f'    ✅ {p.name}')

print(f'\n✅ Downloaded {ok}/{len(needs_download)} assignment PDFs.')
