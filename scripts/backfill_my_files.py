"""
scripts/backfill_my_files.py — the files the owner sent before agent/my_files existed (16/11), from the chat log.

Each owner 'document' / 'image' row of data/agent_conversations.jsonl whose reply starts "📎 *<name>*" becomes a
my_files record with that reply as its summary; the original in data/wa_media/ (named "<id>__<name>") is copied when
it's still there. Owner links ("🔗"/"📎" replies to a text with a URL) get a record with the URL. No LLM, idempotent
(a record's id is the file's hash / the URL).

    venv/bin/python3 scripts/backfill_my_files.py            # dry run: what would be kept
    venv/bin/python3 scripts/backfill_my_files.py --apply
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import my_files                                  # noqa: E402
from config.paths import DATA_DIR                           # noqa: E402

URL_RX = re.compile(r'https?://[^\s<>"\'״]+', re.I)


def rows(log: Path):
    for ln in log.read_text(encoding='utf-8').splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get('role') != 'owner' or r.get('group'):
            continue
        outs = [o for o in r.get('out') or [] if isinstance(o, (list, tuple)) and len(o) > 1]
        first = str(outs[0][1]) if outs else ''
        m = re.match(r'^(?:📎|🔗)\s*\*?([^*\n]*)\*?\n', first)
        if not m:
            continue
        url = ''
        if r.get('kind') == 'text':
            u = URL_RX.search(r.get('in') or '')
            if not u:
                continue
            url = u.group(0).rstrip('.,)!?')
        elif r.get('kind') not in ('document', 'image'):
            continue
        yield r, m.group(1).strip() or ('התמונה' if r.get('kind') == 'image' else 'קובץ'), first[m.end():].strip(), url


def main(apply: bool) -> int:
    log = DATA_DIR / 'agent_conversations.jsonl'
    media = DATA_DIR / 'wa_media'
    n = 0
    for r, name, summary, url in rows(log):
        path = None
        if r.get('kind') == 'document' and media.is_dir():
            path = next((p for p in media.iterdir() if p.name.split('__', 1)[-1] == name), None)
        print(f"{r['ts'][:16]} {r['kind']:<8} {name[:60]} {'(file)' if path else '(url)' if url else ''}")
        if apply:
            my_files.add(r.get('chat') or _owner(), name, summary, path=path, url=url,
                         when=datetime.fromisoformat(r['ts'][:19]))
        n += 1
    print(f'{n} {"kept" if apply else "would be kept (--apply)"}')
    return 0


def _owner() -> str:
    from whatsapp import outbox
    return outbox.owner_chat()


if __name__ == '__main__':
    raise SystemExit(main('--apply' in sys.argv))
