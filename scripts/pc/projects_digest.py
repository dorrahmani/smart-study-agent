"""
scripts/pc/projects_digest.py — OPTIONAL. Your other projects, on this PC and on GitHub, as one safe digest for the bot (17/11).

The bot on the server sees only its own repo; without this script it answers about itself only. Run it on your PC (where
the folders and a logged-in `gh` CLI are), by hand or from a daily scheduled task, and `--push` copies the digest to the
server's data/projects_digest.json, which agent/project_qa.py answers from (owner only).

Settings (in .env on the PC, or the environment):
    SSA_SERVER           user@host of the server (the same value scripts use elsewhere); needed for --push only
    SSH_KEY              the SSH key for it (default ~/.ssh/id_ed25519)
    SSA_REMOTE_ROOT      the repo's folder on the server (default /home/ubuntu/smart_study_agent)
    SSA_PROJECT_ROOTS    comma list of folders to look in (default: your home folder and ~/Documents)
    SSA_SENSITIVE_RX     a regex of project names that may hold other people's data (default: none) — docs + structure only

Per project:
  local folder  CLAUDE.md / README.md / PROJECT.md / docs/*.md, the top-level layout, the dependency names, which AI
                providers the code uses (keywords only), the last 20 commit subjects, the branch's uncommitted-file count,
                TODO/FIXME lines and Python module docstrings (not for a sensitive project).
  GitHub        description, visibility, languages, stars/forks, created/pushed/updated, the README, the top-level tree, the
                last 20 commit subjects, open issues and PR titles. Repos only on GitHub are covered too; a folder is
                matched to its repo by its `origin` remote.
Never read: .env*, credentials, tokens, keys, *.pickle, storage_state, data/, logs/, node_modules/, venv/ — and of a
SENSITIVE project (SSA_SENSITIVE_RX: may hold other people's data) only its docs and structure, with e-mails and handles removed.
Every text goes through agent/project_qa.scrub (keys, tokens, passwords, phone numbers). README and issue text are data:
the answering prompt says so and the question block guard applies.

    python scripts/pc/projects_digest.py            # build → data/projects_digest.json (here) and print a summary
    python scripts/pc/projects_digest.py --push     # and copy it to the server (scp, chmod 600)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.project_qa import DENY, scrub                 # noqa: E402  (the same filter as the bot's own index)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False)
except ImportError:
    pass
import os                                                # noqa: E402

HOME = Path.home()
ROOTS = [Path(x).expanduser() for x in os.environ.get('SSA_PROJECT_ROOTS', '').split(',') if x.strip()] or [HOME, HOME / 'Documents']
OUT = ROOT / 'data' / 'projects_digest.json'
VM = os.environ.get('SSA_SERVER', '').strip()
KEY = os.environ.get('SSH_KEY', '').strip() or str(HOME / '.ssh' / 'id_ed25519')
REMOTE = os.environ.get('SSA_REMOTE_ROOT', '/home/ubuntu/smart_study_agent').rstrip('/') + '/data/projects_digest.json'
SKIP = {'AppData', 'Application Data', 'OneDrive', 'Music', 'Pictures', 'Videos', 'Desktop', 'Downloads', 'Favorites',
        'Links', 'Contacts', 'Searches', 'Saved Games', 'Start Menu', 'Templates', 'SendTo', 'Recent', 'NetHood',
        'PrintHood', 'Cookies', 'Local Settings', 'My Documents', 'Documents', 'Obsidian Vault', 'Zoom', 'Dell',
        'PowerShell', 'WindowsPowerShell', 'My Music', 'My Pictures', 'My Videos', 'bin', 'lib', 'data', 'גיבוי'}
SELF = os.environ.get('SSA_SELF_PROJECT', '').strip() or ROOT.name   # the bot's own repo: the server indexes it in full
SENSITIVE = re.compile(os.environ.get('SSA_SENSITIVE_RX', '').strip() or r'(?!x)x', re.I)   # docs + structure only
NOISE = {'node_modules', 'venv', '.venv', '__pycache__', '.git', 'dist', 'build', '.pytest_cache', '.next', '.idea',
         '.vscode', 'site-packages'}
DATA_EXT = re.compile(r'\.(?:csv|tsv|xlsx?|db|sqlite3?|json|jsonl|parquet|pdf|docx?|png|jpe?g|gif|mp[34]|wav|zip|gz|txt|log|'
                      r'pickle|pkl|html?)$', re.I)
CODE_EXT = re.compile(r'\.(?:py|js|jsx|ts|tsx|mjs|java|c|cpp|h|go|rs|cs|kt|rb|php|sh|ps1)$', re.I)
AI = {   # what the code imports / depends on, not what it mentions (a rule "never Anthropic" is not a use)
    'Gemini': r"""(?:from|import)\s+google(?:\.genai|\.generativeai|\s+import\s+genai)\b|["']@google/(?:genai|generative-ai)["']|^google-(?:genai|generativeai)\b""",
    'OpenAI': r"""^\s*(?:from|import)\s+openai\b|["']openai["']\s*:|^openai\b""",
    'Anthropic/Claude': r"""^\s*(?:from|import)\s+anthropic\b|["']@anthropic-ai/sdk["']|^anthropic\b""",
    'Ollama (local)': r"""^\s*(?:from|import)\s+ollama\b|localhost:11434|^ollama\b""",
    'NotebookLM': r"""^\s*(?:from|import)\s+notebooklm\b|^notebooklm-py\b"""}
EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')
HANDLE = re.compile(r'(?<![\w/])@[A-Za-z0-9_]{3,}')


def _run(args: list[str], cwd: Path | None = None, timeout: int = 60) -> str:
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
        return r.stdout.decode('utf-8', 'replace') if r.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def clean(text: str, sensitive: bool = False, cap: int = 12000) -> str:
    t = scrub((text or '')[:cap])
    if sensitive:
        t = HANDLE.sub('@…', EMAIL.sub('[מייל]', t))
    return t.strip()


def md_chunks(project: str, path: str, text: str, sensitive: bool) -> list[dict]:
    from agent.starter_help import _chunks_md
    out = []
    for c in _chunks_md(path, clean(text, sensitive, 30000))[:14]:
        out.append({'p': f'{project} · {path}', 'h': c['h'], 't': c['t'][:1800]})
    return out


def _skip_path(rel: str) -> bool:
    parts = rel.replace('\\', '/').split('/')
    return any(p in NOISE for p in parts) or bool(DENY.search(rel)) or parts[0] in ('data', 'logs')


# ── a local folder ────────────────────────────────────────────────────────────────────────────────

def is_project(p: Path) -> bool:
    if (p / '.git').is_file():                             # a git worktree of another project (a Claude session's), not one
        return False
    return p.is_dir() and p.name not in SKIP and not p.name.startswith('.') and any(
        (p / f).exists() for f in ('.git', 'CLAUDE.md', 'README.md', 'PROJECT.md', 'package.json', 'requirements.txt'))


def local_folders() -> list[Path]:
    seen, out = set(), []
    for root in ROOTS:
        for p in sorted(root.iterdir()) if root.exists() else []:
            if is_project(p) and p.resolve() not in seen:
                seen.add(p.resolve())
                out.append(p)
    return out


def origin_repo(p: Path) -> str:
    url = _run(['git', '-C', str(p), 'remote', 'get-url', 'origin']).strip()
    m = re.search(r'github\.com[:/][^/]+/([^/\s]+?)(?:\.git)?$', url)
    return m.group(1) if m else ''


def _files(p: Path, most: int = 4000) -> list[str]:
    out = []
    for f in p.rglob('*'):
        if len(out) >= most:
            break
        rel = f.relative_to(p).as_posix()
        if f.is_file() and not _skip_path(rel):
            out.append(rel)
    return out


def layout(p: Path) -> str:
    rows = []
    for f in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if f.name in NOISE or f.name.startswith('.git') or DENY.search(f.name) or f.name.startswith('.env'):
            continue
        if f.is_dir():
            if f.name in ('data', 'logs'):
                rows.append(f'{f.name}/ (לא נקרא)')
                continue
            kids = [k.name for k in sorted(f.iterdir())[:200] if k.name not in NOISE and not DATA_EXT.search(k.name)
                    and not DENY.search(k.name)][:12]
            rows.append(f'{f.name}/' + (f" ({', '.join(kids)})" if kids else ''))
        elif not DATA_EXT.search(f.name) or f.suffix.lower() in ('.json',) and f.name in ('package.json', 'tsconfig.json'):
            rows.append(f.name)
    return '\n'.join(rows[:60])


def deps(p: Path) -> list[str]:
    out = []
    for name in ('requirements.txt', 'requirements-dev.txt'):
        f = p / name
        if f.exists():
            out += [re.split(r'[<>=!~\[; ]', ln.strip())[0] for ln in f.read_text(encoding='utf-8', errors='replace').splitlines()
                    if ln.strip() and not ln.startswith(('#', '-'))]
    f = p / 'package.json'
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
            out += list((d.get('dependencies') or {}).keys()) + list((d.get('devDependencies') or {}).keys())
        except ValueError:
            pass
    f = p / 'pyproject.toml'
    if f.exists():
        out += re.findall(r'^\s*"([A-Za-z0-9_.\-]+)[<>=~!\[ ;"]', f.read_text(encoding='utf-8', errors='replace'), re.M)
    return sorted(dict.fromkeys(x for x in out if x))[:80]


def ai_used(p: Path, files: list[str], dep: list[str]) -> list[str]:
    blob = '\n'.join(dep).lower()
    found = {k for k, rx in AI.items() if re.search(rx, blob, re.I | re.M)}
    for rel in [f for f in files if CODE_EXT.search(f)][:1500]:
        if len(found) == len(AI):
            break
        try:
            t = (p / rel).read_text(encoding='utf-8', errors='ignore')[:200_000]
        except OSError:
            continue
        found |= {k for k, rx in AI.items() if k not in found and re.search(rx, t, re.I | re.M)}
    return sorted(found)


def docstrings(p: Path, files: list[str], project: str) -> list[dict]:
    out = []
    for rel in [f for f in files if f.endswith('.py') and f.count('/') <= 1 and not f.startswith('tests/')][:60]:
        try:
            t = (p / rel).read_text(encoding='utf-8', errors='ignore')[:20000]
        except OSError:
            continue
        m = re.match(r'\s*(?:#.*\n)*\s*[rubRUB]*("""|\'\'\')(.*?)\1', t, re.S)
        if m and len(m.group(2).strip()) > 40:
            out.append({'p': f'{project} · {rel}', 'h': 'docstring', 't': clean(m.group(2), cap=900)})
    return out[:30]


def todos(p: Path, files: list[str]) -> list[str]:
    out = []
    for rel in [f for f in files if CODE_EXT.search(f) or f.endswith('.md')][:1500]:
        try:
            for i, ln in enumerate((p / rel).read_text(encoding='utf-8', errors='ignore').splitlines()[:5000]):
                if re.search(r'\b(?:TODO|FIXME|XXX|HACK)\b', ln):
                    out.append(f'{rel}:{i + 1}: {ln.strip()[:140]}')
                    if len(out) >= 25:
                        return out
        except OSError:
            continue
    return out


def local(p: Path) -> dict:
    name, sens = p.name, bool(SENSITIVE.search(p.name))
    git = (p / '.git').exists()
    rec = {'folder': str(p), 'sensitive': sens, 'git': git, 'repo': origin_repo(p) if git else ''}
    chunks: list[dict] = []
    if git:
        log = _run(['git', '-C', str(p), 'log', '-20', '--date=short', '--format=%ad %s'])
        rec['last_commit'] = (log.splitlines() or [''])[0][:10]
        rec['branch'] = _run(['git', '-C', str(p), 'rev-parse', '--abbrev-ref', 'HEAD']).strip()
        rec['uncommitted'] = len([ln for ln in _run(['git', '-C', str(p), 'status', '--porcelain']).splitlines() if ln.strip()])
        if log:
            chunks.append({'p': f'{name} · git log (מקומי)', 'h': '20 הקומיטים האחרונים', 't': clean(log, sens, 4000)})
    if name == SELF:                                       # the VM has the whole repo: its own index answers
        rec['chunks'] = chunks
        return rec
    files = _files(p)
    rec['deps'] = deps(p)
    rec['ai'] = ai_used(p, files, rec['deps'])
    rec['files'] = len(files)
    chunks.append({'p': f'{name} · מבנה התיקייה', 'h': str(p), 't': clean(layout(p), sens, 4000)})
    if rec['deps']:
        chunks.append({'p': f'{name} · תלויות', 'h': '', 't': ', '.join(rec['deps'])})
    for doc in ['CLAUDE.md', 'README.md', 'PROJECT.md', 'CHANGELOG.md', 'SECURITY.md'] + \
            sorted(f for f in files if f.startswith('docs/') and f.endswith('.md'))[:8]:
        f = p / doc
        if f.is_file():
            chunks += md_chunks(name, doc, f.read_text(encoding='utf-8', errors='replace'), sens)
    if not sens:
        chunks += docstrings(p, files, name)
        if t := todos(p, files):
            chunks.append({'p': f'{name} · TODO בקוד', 'h': f'{len(t)} שורות', 't': clean('\n'.join(t), cap=4000)})
    rec['chunks'] = chunks
    return rec


# ── GitHub ────────────────────────────────────────────────────────────────────────────────────────

def gh_json(args: list[str]):
    out = _run(['gh', *args], timeout=90)
    try:
        return json.loads(out) if out.strip() else None
    except ValueError:
        return None


def github() -> tuple[str, list[dict]]:
    owner = _run(['gh', 'api', 'user', '-q', '.login']).strip()
    repos = gh_json(['repo', 'list', owner, '--limit', '100', '--json',
                     'name,description,visibility,stargazerCount,forkCount,updatedAt,pushedAt,createdAt,primaryLanguage,'
                     'languages,url,isArchived,isFork']) or []
    out = []
    for r in repos:
        full, name = f"{owner}/{r['name']}", r['name']
        sens = bool(SENSITIVE.search(name))
        rec = {'name': name, 'visibility': r.get('visibility', '').lower(), 'description': clean(r.get('description') or '', sens, 400),
               'stars': r.get('stargazerCount', 0), 'forks': r.get('forkCount', 0), 'updated': (r.get('updatedAt') or '')[:10],
               'pushed': (r.get('pushedAt') or '')[:10], 'created': (r.get('createdAt') or '')[:10], 'url': r.get('url', ''),
               'archived': r.get('isArchived', False), 'sensitive': sens,
               'languages': [l['node']['name'] for l in (r.get('languages') or []) if l.get('node')][:8]}
        chunks = []
        readme = _run(['gh', 'api', f'repos/{full}/readme', '-H', 'Accept: application/vnd.github.raw'])
        if readme:
            chunks += md_chunks(name, 'README.md (GitHub)', readme, sens)
        tree = gh_json(['api', f'repos/{full}/contents']) or []
        names = [f"{t['name']}/" if t.get('type') == 'dir' else t['name'] for t in tree if isinstance(t, dict)
                 and not DENY.search(t.get('name', '')) and not t.get('name', '').startswith('.env')]
        rec['tree'] = names[:60]
        if names:
            chunks.append({'p': f'{name} · מבנה בגיטהאב', 'h': '', 't': ', '.join(names[:60])})
        cm = gh_json(['api', f'repos/{full}/commits?per_page=20']) or []
        rows = [f"{c['commit']['author']['date'][:10]} {c['commit']['message'].splitlines()[0][:140]}" for c in cm
                if isinstance(c, dict) and c.get('commit')]
        if rows:
            rec['last_commit'] = rows[0][:10]
            chunks.append({'p': f'{name} · commits (GitHub)', 'h': '20 האחרונים', 't': clean('\n'.join(rows), sens, 4000)})
        issues = gh_json(['issue', 'list', '-R', full, '--state', 'open', '-L', '30', '--json', 'number,title,createdAt']) or []
        prs = gh_json(['pr', 'list', '-R', full, '--state', 'open', '-L', '20', '--json', 'number,title,createdAt']) or []
        rec['open_issues'], rec['open_prs'] = len(issues), len(prs)
        if issues or prs:
            chunks.append({'p': f'{name} · issues/PRs פתוחים', 'h': '', 't': clean(
                '\n'.join([f"issue #{i['number']} ({i['createdAt'][:10]}): {i['title']}" for i in issues] +
                          [f"PR #{x['number']} ({x['createdAt'][:10]}): {x['title']}" for x in prs]), sens, 4000)})
        rec['chunks'] = chunks
        out.append(rec)
    return owner, out


# ── the digest ────────────────────────────────────────────────────────────────────────────────────

def overview(p: dict) -> str:
    g, ls = p.get('github') or {}, p.get('local') or []
    bits = [f"*{p['name']}*"]
    if g:
        bits.append(f"GitHub {g['visibility']} ({g.get('url', '')}), ⭐{g.get('stars', 0)} forks {g.get('forks', 0)}, "
                    f"עודכן {g.get('updated', '')}, push אחרון {g.get('pushed', '')}, issues פתוחים {g.get('open_issues', 0)}, "
                    f"PRs פתוחים {g.get('open_prs', 0)}" + (f", שפות: {', '.join(g['languages'])}" if g.get('languages') else '')
                    + (f" — {g['description']}" if g.get('description') else ''))
    else:
        bits.append('לא בגיטהאב')
    for l in ls:
        bits.append(f"תיקייה {l['folder']}" + (f", קומיט אחרון {l['last_commit']}" if l.get('last_commit') else ', בלי git')
                    + (f", ענף {l['branch']}" if l.get('branch') else '') + (f", {l['uncommitted']} קבצים לא בקומיט" if l.get('uncommitted') else '')
                    + (f", AI לפי import/תלויות: {', '.join(l['ai'])}" if l.get('ai') else (', AI: אין import של ספק AI' if 'ai' in l else '')))
    if not ls:
        bits.append('אין תיקייה במחשב')
    if p['name'] == SELF or (ls and any(Path(l['folder']).name == SELF for l in ls)):
        bits.append('זה הבוט עצמו (רץ על השרת, Gemini בלבד)')
    if any(x.get('sensitive') for x in ls + ([g] if g else [])):
        bits.append('רגיש: רק תיעוד ומבנה נקראו')
    return ' · '.join(bits)


def build() -> dict:
    owner, repos = github()
    by_repo = {r['name'].lower(): {'name': r['name'], 'github': r, 'local': []} for r in repos}
    for f in local_folders():
        rec = local(f)
        key = (rec.get('repo') or '').lower()
        if key in by_repo:
            by_repo[key]['local'].append(rec)
        else:
            by_repo.setdefault(f.name.lower(), {'name': f.name, 'github': None, 'local': []})['local'].append(rec)
    projects = []
    for p in by_repo.values():
        chunks = list((p['github'] or {}).pop('chunks', []) if p['github'] else [])
        seen_docs = {c['t'][:200] for c in chunks}
        for l in p['local']:
            folder = Path(l['folder']).name
            for c in l.pop('chunks', []):
                if folder != p['name'] and c['p'].startswith(f'{folder} ·'):      # "my-tool (My Tool) · README.md"
                    c['p'] = f"{p['name']} ({folder})" + c['p'][len(folder):]
                if c['t'][:200] not in seen_docs:              # the same README on GitHub and on disk: once
                    chunks.append(c)
                    seen_docs.add(c['t'][:200])
        p['overview'] = overview(p)
        p['chunks'] = [c for c in chunks if c['t'].strip()]
        projects.append(p)
    projects.sort(key=lambda p: max([(p['github'] or {}).get('pushed', '')] + [l.get('last_commit', '') for l in p['local']]),
                  reverse=True)
    return {'built': datetime.now().isoformat(timespec='seconds'), 'owner': owner, 'projects': projects}


def push(path: Path) -> None:
    if not VM:
        raise SystemExit('SSA_SERVER is not set (user@host of the server) — nothing pushed')
    tmp = REMOTE + '.tmp'
    subprocess.run(['scp', '-q', '-i', KEY, '-o', 'BatchMode=yes', str(path), f'{VM}:{tmp}'], check=True, timeout=120)
    subprocess.run(['ssh', '-i', KEY, '-o', 'BatchMode=yes', VM, f'chmod 600 {tmp} && mv {tmp} {REMOTE}'], check=True, timeout=60)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--push', action='store_true')
    ap.add_argument('--out', default=str(OUT))
    a = ap.parse_args(argv)
    d = build()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    n = sum(len(p['chunks']) for p in d['projects'])
    print(f"✅ projects digest: {len(d['projects'])} projects, {n} chunks → {out}")
    for p in d['projects']:
        print('  ' + p['overview'][:160])
    if a.push:
        push(out)
        print(f'✅ pushed → {VM}:{REMOTE}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
