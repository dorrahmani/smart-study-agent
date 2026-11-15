"""
agent/known_alerts.py — the bot's own system alerts, and how the owner fixes each one (10/11).

10/11 11:02 the owner pasted the bot's alert "⚠️ NotebookLM לא עונה… כנראה צריך לחדש את ההתחברות. איך לבצע ?" and got
"אני מטפל בזה ברמת המערכת" and a Claude job. A login is renewed on his PC, by him: nothing on the server can do it.
So an alert he pastes or asks about gets the real steps, read from docs/RUNBOOK.md (the one place they're kept), with a
copy here for when the file can't be read.

    how_to(text)  → the steps for the alert this message is about, or None
No LLM, nothing is run.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / 'docs' / 'RUNBOOK.md'

# the alert as the bot sends it, or the owner's words about it; the RUNBOOK heading; the steps if the file can't be read
ALERTS = [
    {'rx': re.compile(r'NotebookLM\s+לא\s+עונה|(?:notebook\s*lm|נוטבוק).{0,40}(?:התחבר|חיבור|cookie|קוקי|login|לוגין|authentication)|'
                      r'(?:התחבר|חיבור|cookie|קוקי|login|לוגין).{0,40}(?:notebook\s*lm|נוטבוק)',
                      re.I | re.S),
     'title': '📓 *NotebookLM — חידוש ההתחברות*',
     'heading': '### 📓 NotebookLM',
     'steps': ('במחשב: `python -m notebooklm login --browser chrome` (חשבון `user3@example.com` — שם 16 המחברות) ← ואז:\n'
               '```\nscp -i ~/.ssh/smart_study.key ~/.notebooklm/profiles/default/storage_state.json '
               'ubuntu@<your-server>:.notebooklm/profiles/default/storage_state.json && ssh -i ~/.ssh/smart_study.key '
               "ubuntu@<your-server> 'chmod 600 ~/.notebooklm/profiles/default/storage_state.json'\n```"),
     'after': 'עד אז שום דבר לא נתקע: התזכורות לפני שיעור ממשיכות לצאת דרך Gemini.'},
    {'rx': re.compile(r'invalid_grant|(?:gmail|ג׳?ימייל).{0,40}(?:טוקן|token|התחבר|פג)', re.I | re.S),
     'title': '📧 *Gmail — חידוש הטוקן*',
     'heading': '### 📧 Gmail',
     'steps': ('במחשב: `python scripts/gmail_reauth.py` (פותח דפדפן) ← ואז:\n```\nscp -i ~/.ssh/smart_study.key '
               'config/gmail_token.pickle ubuntu@<your-server>:smart_study_agent/config/ && ssh -i ~/.ssh/smart_study.key '
               "ubuntu@<your-server> 'chmod 600 ~/smart_study_agent/config/gmail_token.pickle'\n```"),
     'after': ''},
]
# what turns a mention into a request for the steps; the alert itself pasted back counts too (it starts with ⚠️)
HOW_RX = re.compile(r'איך|מה\s+(?:לעשות|עושים|צריך)|לבצע|לתקן|לחדש|תסביר|הסבר|שלבים|\?')
NOT_ABOUT_RX = re.compile(r'^\s*(?:קלוד|claude)\s*[:：]', re.I)          # "קלוד: …" is a job he writes himself


def _section(heading: str, path: Path | None = None) -> str:
    """The RUNBOOK section under this heading, up to the next one, for WhatsApp ("```bash" → "```")."""
    try:
        lines = (path or RUNBOOK).read_text(encoding='utf-8').splitlines()
    except OSError:
        return ''
    out, inside = [], False
    for ln in lines:
        if ln.startswith(heading):
            inside = True
            continue
        if inside and re.match(r'#{1,3}\s', ln):
            break
        if inside:
            out.append(re.sub(r'^```\w+$', '```', ln))
    return '\n'.join(out).strip()


def how_to(text: str, path: Path | None = None) -> str | None:
    t = text or ''
    if NOT_ABOUT_RX.match(t):
        return None
    for a in ALERTS:
        if a['rx'].search(t) and (HOW_RX.search(t) or t.lstrip().startswith('⚠️')):
            steps = _section(a['heading'], path) or a['steps']
            return '\n\n'.join(x for x in (a['title'] + ' (את זה עושים אצלך, לא מהשרת)', steps, a['after'],
                                            '(מתוך docs/RUNBOOK.md)') if x)
    return None
