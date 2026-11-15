"""
scripts/find_panopto_folders.py — find the Panopto folder_id for each course.

Read-only. Logs into Panopto (TAU SSO) and searches the folders YOUR account can
see. A new semester's folders appear only once the course is provisioned (around
the Moodle course opening), so an empty result early on is normal.

    venv/bin/python3 scripts/find_panopto_folders.py                 # all ACTIVE_COURSES
    venv/bin/python3 scripts/find_panopto_folders.py "תוכנה 1"       # free-text query

Copy the right ID into config/courses.yaml → courses.<key>.sources.panopto.folder_id,
commit, `git push vm`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

import yaml  # noqa: E402


def search_folders(pc, query: str) -> list[dict]:
    r = pc.session.post(
        f'{pc.BASE_URL}/Panopto/Services/Data.svc/GetFolders',
        json={'queryParameters': {'query': query, 'sortColumn': 1, 'sortAscending': False,
                                  'maxResults': 50, 'page': 0}},
        timeout=30,
    )
    r.raise_for_status()
    return (r.json().get('d') or {}).get('Results') or []


def main() -> int:
    from collectors.panopto_collector import PanoptoCollector
    from config.paths import ACTIVE_COURSES

    import os
    os.chdir(ROOT)
    pc = PanoptoCollector()
    pc.login()

    if len(sys.argv) > 1:
        queries = {'query': [' '.join(sys.argv[1:])]}
    else:
        cfg = yaml.safe_load(open(ROOT / 'config' / 'courses.yaml', encoding='utf-8'))
        queries = {}
        for ck in ACTIVE_COURSES:
            c = cfg['courses'][ck]
            q = {c['name'], (c['sources']['panopto'].get('search_query') or '')}
            if str(c.get('course_code', '')).isdigit():
                q.add(str(c['course_code']))
            queries[ck] = [x for x in q if x]

    found_any = False
    for ck, qs in queries.items():
        print(f'\n== {ck}')
        seen = {}
        for q in qs:
            for f in search_folders(pc, q):
                seen[f['ID']] = f
        if not seen:
            print('   (no visible folder yet)')
        for fid, f in seen.items():
            found_any = True
            name = re.sub(r'<[^>]+>', '', f.get('Name') or '')
            print(f'   {fid}  |  {name}  |  sessions={f.get("SessionCount")}  |  parent={f.get("ParentFolderName")}')
    return 0 if found_any else 1


if __name__ == '__main__':
    sys.exit(main())
