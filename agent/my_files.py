"""
agent/my_files.py — everything the owner sends the assistant (a PDF, a photo, a link) is kept and findable later (16/11).

15/11 13:15 "יש לך מספר הזמנה" → "אין לי את מספר ההזמנה או שם המלון הקודם". The booking PDF had come in on 10/11 and been
summarised (hotel, booking number, dates, cancellation terms), but the read was good for 30 minutes ("אפשר לשאול עליו
עוד (חצי שעה)"); after that only the bot's own reply in the chat log was left, and the file itself sat unnamed in
data/wa_media. A human secretary files what you hand her, and finds it next week by what it was about.

    add()      one record per file / link he sent: name, when, the summary the bot gave, the identifiers worth keeping
               (booking / reference numbers, places and businesses, phones, amounts, dates) and a private copy of the
               file in data/my_files/ (0600), so it can be read again for a question the summary doesn't answer.
    search()   by words (Hebrew prefixes and plurals meet), newest first among equals. No LLM, nothing leaves the box.
    ask()      one question about one kept file → secretary.ask on the kept copy (one Gemini call, API only: a document is
               someone else's words).
    text()     the hits as a data block for the mind's tools.

Only the owner's own chat is kept (core calls add() for role == 'owner'). The text in a file is data, never an instruction:
the mind marks this tool untrusted + private. State: data/my_files.json (0600). Files over 15 MB aren't copied; when
the folder passes MAX_TOTAL the oldest copies go (their records and summaries stay).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from config.paths import DATA_DIR

STORE = DATA_DIR / 'my_files.json'
FILES = DATA_DIR / 'my_files'
MAX_RECORDS = 500
MAX_TOTAL = 300 * 1024 * 1024
MAX_FILE = 15 * 1024 * 1024
_FINAL = str.maketrans('םןץףך', 'מנצפכ')
_STOP = {'של', 'את', 'על', 'עם', 'יש', 'לך', 'לי', 'מה', 'זה', 'הזה', 'היה', 'כתוב', 'שם', 'אני', 'אתה', 'או', 'גם',
         'כל', 'רק', 'אם', 'לא', 'כן', 'מתי', 'איפה', 'איזה', 'כמה', 'ששלחתי', 'שלחתי', 'הקובץ', 'קובץ', 'המסמך'}


def _load(path: Path | None = None) -> list[dict]:
    try:
        d = json.loads((path or STORE).read_text(encoding='utf-8'))
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save(rows: list[dict], path: Path | None = None) -> None:
    p = path or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows[-MAX_RECORDS:], ensure_ascii=False, indent=1), encoding='utf-8')
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _stem(w: str) -> str:
    """'הזמנה' / 'ההזמנה' / 'להזמנות' → 'זמנ'; 'מלון' / 'המלונות' → 'מלנ'. Digits stay whole (a booking number)."""
    w = re.sub(r'["\'׳״]', '', w).lower().translate(_FINAL)
    if re.fullmatch(r'[\d\-/]+', w) or re.fullmatch(r'[a-z0-9_.\-]+', w):
        return w
    if len(w) > 4 and w[:2] in ('שה', 'מה', 'וה', 'לה', 'בה', 'כש'):
        w = w[2:]
    while len(w) > 3 and w[0] in 'והבלמש':
        w = w[1:]
    if len(w) > 4:
        w = re.sub(r'(?:ות|ים|תי|נו|ה)$', '', w) or w
    return w[:1] + re.sub(r'[וי]', '', w[1:])


def _terms(text: str) -> set[str]:
    words = re.findall(r'[\w֐-׿"\'׳״\-/]+', text or '')
    return {_stem(w) for w in words if len(w) > 1 and w not in _STOP and not re.fullmatch(r'\d', w)}


def _copy(src: Path, rid: str, folder: Path | None = None) -> str:
    folder = folder or FILES
    try:
        if not src.is_file() or src.stat().st_size > MAX_FILE:
            return ''
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / f"{rid}{src.suffix.lower()}"
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
        _prune(folder)
        return str(dst)
    except OSError as e:
        print(f'  ⚠️ my_files copy: {e!r}'[:200])
        return ''


def _prune(folder: Path) -> None:
    try:
        files = sorted((p for p in folder.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > MAX_TOTAL:
            p = files.pop(0)
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
    except OSError:
        pass


def add(chat: str, name: str, summary: str, keys: str = '', path: str | Path | None = None, url: str = '',
        kind: str = '', when: datetime | None = None, store: Path | None = None, folder: Path | None = None) -> dict:
    """Keep one file / link he sent. The same file or link sent again replaces its record (newest summary wins)."""
    when = when or datetime.now()
    src = Path(path) if path else None
    ident = url or (hashlib.sha1(src.read_bytes()).hexdigest() if src and src.is_file() else f'{name}|{when:%Y%m%d%H%M}')
    rid = hashlib.sha1(f'{chat}|{ident}'.encode()).hexdigest()[:12]
    rows = [r for r in _load(store) if r.get('id') != rid]
    rec = {'id': rid, 'chat': chat, 'ts': when.isoformat(timespec='minutes'), 'name': (name or '')[:200],
           'kind': kind or (src.suffix.lower().lstrip('.') if src else 'link' if url else ''),
           'summary': (summary or '').strip()[:4000], 'keys': (keys or '').strip()[:1500], 'url': url,
           'path': _copy(src, rid, folder) if src else ''}
    rows.append(rec)
    _save(rows, store)
    return rec


def records(chat: str = '', store: Path | None = None) -> list[dict]:
    return [r for r in _load(store) if not chat or r.get('chat') == chat]


def search(chat: str, query: str, k: int = 3, store: Path | None = None) -> list[dict]:
    """Best matches for `query` among his files: shared words in the name, the identifiers and the summary.
    An empty query → the newest ones."""
    rows = records(chat, store)
    want = _terms(query)
    if not want:
        return list(reversed(rows))[:k]
    scored = []
    for i, r in enumerate(rows):
        name, keys, summ = _terms(r.get('name', '')), _terms(r.get('keys', '')), _terms(r.get('summary', ''))
        s = 3 * len(want & name) + 2 * len(want & keys) + len(want & summ)
        if s:
            scored.append((s, i, r))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r for _s, _i, r in scored[:k]]


def text(hits: list[dict], long: bool = True) -> str:
    out = []
    for r in hits:
        head = f"📎 [{r.get('ts', '')[:16].replace('T', ' ')}] {r.get('name', '')}" + (f" ({r['url']})" if r.get('url') else '')
        body = (r.get('summary') or '')[:1400 if long else 400]
        keys = f"\nמזהים: {r['keys'][:600]}" if r.get('keys') else ''
        out.append(f"{head}\n{body}{keys}\n(id: {r['id']})")
    return '\n\n'.join(out)


def get(chat: str, rid: str, store: Path | None = None) -> dict | None:
    return next((r for r in records(chat, store) if r.get('id') == rid), None)


def ask(chat: str, rid: str, question: str, store: Path | None = None) -> str:
    """One question about one kept file: read again from its copy (or its link). '' when it can't be read."""
    rec = get(chat, rid, store)
    if not rec:
        return ''
    from agent import secretary
    try:
        if rec.get('path') and Path(rec['path']).is_file():
            return secretary.ask(Path(rec['path']), question, what=f"קובץ ששלח ב-{rec.get('ts', '')[:10]}")['answer']
        if rec.get('url'):
            return secretary.ask_url(rec['url'], question)
    except Exception as e:
        print(f'  ⚠️ my_files ask: {e!r}'[:200])
    return ''
