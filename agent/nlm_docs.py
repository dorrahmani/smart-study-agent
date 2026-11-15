"""
agent/nlm_docs.py — links and files the owner sends go to NotebookLM (the Google account we connected), not the Gemini API.

The owner 14/10: "שאם אני אשאל שאלה זה ילך על החשבון גוגל שכבר חיברנו ולא על ה-API". Each link / PDF / Word / text file
becomes a source in one notebook of its own ("📎 מסמכים מוואטסאפ", kept out of the course index so the nightly course
sync never touches it). Questions are asked against that source; and since the sources stay, the bot REMEMBERS what
was sent — "מה היה כתוב בסילבוס ששלחתי?" works days later. Free (the account's subscription).
Any failure → the caller falls back to the old Gemini path (agent/secretary.py) and NotebookLM is skipped for 6h.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'data' / 'nlm_docs.json'
TITLE = '📎 מסמכים מוואטסאפ'
FILE_TYPES = {'.pdf', '.txt', '.md', '.docx'}
RECALL_RX = re.compile(r'(?:ב|של ה?|את ה)(?:קובץ|מסמך|pdf|PDF|לינק|קישור|סילבוס|מצגת|מאמר|כתבה)')


def _load() -> dict:
    try:
        return json.loads(STORE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'notebook_id': '', 'docs': []}


def _save(d: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')


def _run(coro):
    from generators.notebooklm_agent import _run as run
    return run(coro)


async def _client():
    from notebooklm import NotebookLMClient
    return await NotebookLMClient.from_storage()


def _notebook() -> str:
    d = _load()
    if d.get('notebook_id'):
        return d['notebook_id']

    async def create():
        async with await _client() as c:
            nb = await c.notebooks.create(TITLE)
            return getattr(nb, 'notebook_id', None) or getattr(nb, 'id', None)
    d['notebook_id'] = _run(create())
    _save(d)
    return d['notebook_id']


def supported(path: Path) -> bool:
    return path.suffix.lower() in FILE_TYPES


def add(ref: str, kind: str, title: str) -> str:
    """kind 'url' | 'file' → the new source id (waits until NotebookLM has processed it)."""
    nb = _notebook()

    async def go():
        async with await _client() as c:
            if kind == 'url':
                s = await c.sources.add_url(nb, ref, wait=True, wait_timeout=150, title=title[:90] or None)
            else:
                s = await c.sources.add_file(nb, ref, wait=True, wait_timeout=150, title=title[:90] or None)
            return getattr(s, 'source_id', None) or getattr(s, 'id', None)
    sid = _run(go())
    d = _load()
    d['docs'].append({'source_id': sid, 'title': title, 'kind': kind, 'ref': ref if kind == 'url' else Path(ref).name,
                      'added': datetime.now().isoformat(timespec='seconds')})
    _save(d)
    return sid


def ask(question: str, source_ids: list[str] | None = None, conversation_id: str | None = None) -> tuple[str, str | None]:
    """→ (WhatsApp-ready answer, conversation id). source_ids None = every document the owner ever sent."""
    from agent.nlm_ask import whatsapp_text
    nb = _notebook()

    async def go():
        async with await _client() as c:
            return await c.chat.ask(nb, question, source_ids=source_ids, conversation_id=conversation_id)
    r = _run(go())
    answer = getattr(r, 'answer', None) or getattr(r, 'text', '') or ''
    return whatsapp_text(answer), getattr(r, 'conversation_id', None)


SUMMARY_Q = ('ענה בעברית, קצר ולוואטסאפ: סיכום של המקור בעד 6 נקודות, ואחריו "*מה צריך לעשות:*" — הגשות, תשלומים, '
             'תאריכים, אישורים (אם יש). רק ממה שכתוב במקור.')
DATED_Q = ('רשום רק דברים מהמקור שיש להם תאריך ושכדאי לשים ביומן (דדליין, פגישה, אירוע, מבחן) — שורה לכל אחד: '
           'כותרת, תאריך מלא ושעה אם יש. אם אין כאלה — כתוב רק: אין')
