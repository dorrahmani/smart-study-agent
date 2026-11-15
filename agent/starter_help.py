"""
agent/starter_help.py — helping FriendA & FriendB set up tau-study-starter, from the WhatsApp group (The owner tags the bot).

The owner 17/10: "אני יכול להוסיף את הבוט לקבוצה ואז שרק אני אתייג אותך תעזור להם". The project is public on GitHub
(a public starter template repo, set SSA_STARTER_REPO). Its docs, code and workflows are mirrored into a NotebookLM notebook of their
own ("🛠️ tau-study-starter") — NOT the personal brain, so nothing of the owner's can leak into the group — and questions
are answered from there (free). An error screenshot → Gemini reads it with the README + setup prompt (a few cents).
Re-synced nightly (scripts/brain_sync.py) when the repo's HEAD changed.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'starter_nlm.json'
REPO = os.environ.get('SSA_STARTER_REPO', '')       # 'owner/repo' of a public starter template; empty = feature off
TITLE = '🛠️ tau-study-starter — עזרה בהקמה'
RX = re.compile(r'github|גיטהאב|גיט האב|actions|אקשנס|template|טמפלייט|secret|סוד(?:ות)?\b|workflow|הקמה|להקים|starter|'
                r'setup|feasibility|timetable\.yaml|settings\.yaml|pytest|שגיאה|error|traceback|exception|נכשל|'
                r'app password|סיסמת אפליקציה|מפתח (?:gemini|ג.מיני)|gemini key|notebooklm login|playwright|\.env\b', re.I)
GROUPS = {                                       # what goes into which source (few sources = few calls)
    'מדריך ופרומפט ההקמה': lambda p: p in ('README.md', 'SETUP_PROMPT.md', '.env.example'),
    'הגדרות ו-workflows': lambda p: p.startswith(('.github/', 'config/')) or p.startswith('requirements'),
    'הקוד (src) והבדיקות': lambda p: (p.startswith('src/') and p != 'src/pdf.py') or p.startswith('tests/'),
}
ASK = ('אתה עוזר לחברים של המשתמש להקים את הפרויקט tau-study-starter (המקורות כאן: המדריך, הקוד וה-workflows). '
       'ענה בעברית, קצר ומעשי לוואטסאפ (עד 10 שורות): מה הבעיה ומה בדיוק ללחוץ/להריץ/לשנות. רק לפי המקורות; '
       'אם אין שם תשובה — אמור זאת. השאלה: ')


def _load() -> dict:
    try:
        return json.loads(STORE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')


def _repo_files() -> tuple[str, dict[str, str]]:
    """(HEAD sha, {path: text}) from the public repo — no token needed."""
    if not REPO:
        return '', {}
    tree = requests.get(f'https://api.github.com/repos/{REPO}/git/trees/main?recursive=1', timeout=30).json()
    sha = tree.get('sha', '')
    out = {}
    for it in tree.get('tree') or []:
        p = it.get('path', '')
        if it.get('type') == 'blob' and any(f(p) for f in GROUPS.values()):
            r = requests.get(f'https://raw.githubusercontent.com/{REPO}/main/{p}', timeout=30)
            if r.status_code == 200:
                out[p] = r.text
    return sha, out


KNOWLEDGE = ROOT / 'docs' / 'STARTER_KNOWLEDGE.md'      # The owner's system's lessons (no personal data)


def sources(files: dict[str, str]) -> dict[str, str]:
    """{source title: one text with every file of that group, each under its path} + the lessons from the owner's system."""
    out = {}
    for title, want in GROUPS.items():
        parts = [f'===== {p} =====\n{t}' for p, t in sorted(files.items()) if want(p)]
        if parts:
            out[title] = '\n\n'.join(parts)
    if KNOWLEDGE.exists():
        out['ניסיון מהמערכת של המשתמש'] = KNOWLEDGE.read_text(encoding='utf-8')
    return out


def _fingerprint(sha: str) -> str:
    """The repo's HEAD + the lessons file — a change in either re-syncs the notebook."""
    import hashlib
    k = KNOWLEDGE.read_bytes() if KNOWLEDGE.exists() else b''
    return f'{sha}:{hashlib.sha1(k).hexdigest()[:10]}'


def sync(force: bool = False) -> str:
    """Mirror the repo into the notebook when its HEAD changed. → what happened."""
    from agent import nlm_docs
    d = _load()
    sha, files = _repo_files()
    if not files:
        return 'no files from GitHub'
    fp = _fingerprint(sha)
    if sha and fp == d.get('sha') and not force:
        return 'up to date'

    async def go():
        async with await nlm_docs._client() as c:
            nb = d.get('notebook_id')
            if not nb:
                created = await c.notebooks.create(TITLE)
                nb = getattr(created, 'notebook_id', None) or getattr(created, 'id', None)
            for sid in d.get('sources') or []:
                try:
                    await c.sources.delete(nb, sid)
                except Exception:
                    pass
            new = []
            for title, text in sources(files).items():
                s = await c.sources.add_text(nb, title, text, wait=True, wait_timeout=150)
                new.append(getattr(s, 'source_id', None) or getattr(s, 'id', None))
            return nb, new
    nb, new = nlm_docs._run(go())
    d.update(notebook_id=nb, sources=new, sha=fp, synced=datetime.now().isoformat(timespec='seconds'))
    _save(d)
    return f'synced {len(new)} sources ({len(files)} files)'


def ask(question: str) -> str:
    """A setup question → NotebookLM over the project's own docs and code."""
    from agent import nlm_docs
    from agent.nlm_ask import whatsapp_text
    d = _load()
    if not d.get('notebook_id'):
        sync()
        d = _load()

    async def go():
        async with await nlm_docs._client() as c:
            return await c.chat.ask(d['notebook_id'], ASK + question)
    r = nlm_docs._run(go())
    return whatsapp_text(getattr(r, 'answer', None) or getattr(r, 'text', '') or '')


def screenshot(image: Path, question: str = '') -> str:
    """An error screenshot (GitHub Actions log, a terminal) → what's wrong and how to fix it, per the project's docs."""
    import google.genai as genai
    from google.genai import types as T
    sha, files = _repo_files()
    docs = '\n\n'.join(f'===== {p} =====\n{files[p]}' for p in ('README.md', 'SETUP_PROMPT.md') if p in files)
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    mime = 'image/png' if image.suffix.lower() == '.png' else 'image/jpeg'
    resp = client.models.generate_content(
        model='gemini-3.8-flash',
        contents=[T.Part.from_bytes(data=image.read_bytes(), mime_type=mime),
                  f'{ASK}{question or "מה השגיאה בצילום ואיך מתקנים?"}\n\nהמדריך של הפרויקט:\n{docs[:30000]}'],
        config=T.GenerateContentConfig(max_output_tokens=2500, temperature=0.2,
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, 'gemini-3.8-flash', op='starter_help.screenshot')
    return (resp.text or '').strip()
