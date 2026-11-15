"""scripts/ingest_course_site.py — put the owner's course site (made by Claude, copied to data/course_site/<course>/)
into the vault so the RAG "brain" answers from it, not only the tutor.

The owner 04/10: he asked the bot about IEEE 754 "from the course site" and got a generic answer — the site
lived only on his PC. Writes markdown with frontmatter (yaml.safe_dump) to <course folder>/🌐 אתר הקורס/:
one note per lesson (text + quiz with answers), per lecture/recitation summary, and per question topic
(exam questions with official solutions). Idempotent; then the index re-embeds only new chunks.

    venv/bin/python3 scripts/ingest_course_site.py [course_key] [--no-index]
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from config.paths import VAULT_DIR, VAULT_COURSE_FOLDER_TO_KEY  # noqa: E402

SITE = ROOT / 'data' / 'course_site'
LETTERS = 'אבגד'


def _clean(x) -> str:
    return re.sub(r'⟪|⟫', '', str(x or ''))


def _folder(ck: str) -> Path:
    names = [f for f, k in VAULT_COURSE_FOLDER_TO_KEY.items() if k == ck]
    existing = [f for f in names if (VAULT_DIR / f).exists()]
    return VAULT_DIR / (existing or names or [ck])[0] / '🌐 אתר הקורס'


def _note(path: Path, meta: dict, title: str, body: str) -> bool:
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    content = f'---\n{fm}---\n\n# {title}\n\n{body.strip()}\n'
    if path.exists() and path.read_text(encoding='utf-8') == content:
        return False
    path.write_text(content, encoding='utf-8')
    return True


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '-', name)[:90].strip()


def lesson_md(l: dict) -> str:
    out = []
    for b in l.get('blocks') or []:
        x = _clean(b.get('x', ''))
        if b.get('t') == 'h':
            out.append(f'## {x}')
        elif b.get('t') == 'code':
            out.append(f'```python\n{x}\n```')
        else:
            out.append(x)
    if l.get('patterns'):
        out.append('## דפוסים חוזרים במבחנים\n' + '\n'.join(f'- {_clean(p)}' for p in l['patterns']))
    quiz = l.get('quiz') or []
    if quiz:
        out.append('## שאלות תרגול (עם תשובות)')
        for q in quiz:
            code = f"\n```python\n{q['code']}\n```" if q.get('code') else ''
            opts = ' · '.join(f'{LETTERS[i]}) {_clean(o)}' for i, o in enumerate(q.get('opts') or []))
            a = int(q.get('a', 0))
            out.append(f"**{_clean(q.get('q'))}**{code}\n{opts}\n✅ {LETTERS[a]}) {_clean((q.get('opts') or [''])[a])} — {_clean(q.get('why'))}")
    return '\n\n'.join(out)


def summary_md(s: dict) -> str:
    out = []
    if s.get('overview'):
        out.append(_clean(s['overview']))
    for sec in s.get('sections') or []:
        if isinstance(sec, dict):
            head = _clean(sec.get('title') or sec.get('h') or '')
            body = sec.get('points') or sec.get('body') or sec.get('text') or ''
            body = '\n'.join(f'- {_clean(p)}' for p in body) if isinstance(body, list) else _clean(body)
            out.append(f'## {head}\n{body}' if head else body)
        else:
            out.append(_clean(sec))
    for key, title in (('definitions', 'הגדרות'), ('complexities', 'סיבוכיות'), ('exam_tips', 'טיפים למבחן'),
                       ('self_check', 'בדיקה עצמית')):
        v = s.get(key)
        if not v:
            continue
        items = v.items() if isinstance(v, dict) else enumerate(v)
        lines = []
        for k, x in items:
            if isinstance(x, dict):
                lines.append('- ' + ' — '.join(_clean(y) for y in x.values() if y))
            else:
                lines.append(f'- {_clean(k) + ": " if isinstance(k, str) else ""}{_clean(x)}')
        out.append(f'## {title}\n' + '\n'.join(lines))
    return '\n\n'.join(out)


def ingest(ck: str) -> tuple[int, int]:
    d = SITE / ck
    folder = _folder(ck)
    folder.mkdir(parents=True, exist_ok=True)
    written = total = 0
    base = {'type': 'course_site', 'course_key': ck, 'source': 'אתר הקורס (הוכן ע"י Claude)'}
    for l in json.loads((d / 'lessons.json').read_text(encoding='utf-8'))['lessons']:
        total += 1
        written += _note(folder / f"שיעור - {_safe(l['title'])}.md", {**base, 'kind': 'שיעור', 'title': l['title']},
                         f"שיעור: {l['title']}", lesson_md(l))
    for p in sorted((d / 'summaries').glob('*.json')):
        s = json.loads(p.read_text(encoding='utf-8'))
        total += 1
        written += _note(folder / f"סיכום - {_safe(p.stem)}.md",
                         {**base, 'kind': s.get('kind', 'סיכום'), 'title': s.get('title', p.stem)},
                         f"סיכום: {s.get('title', p.stem)}", summary_md(s))
    by_topic = defaultdict(list)
    for q in json.loads((d / 'data.json').read_text(encoding='utf-8')).get('questions') or []:
        by_topic[q.get('topic') or 'כללי'].append(q)
    for topic, qs in by_topic.items():
        body = '\n\n'.join(f"## {q.get('cite', '')} ({'⭐' * int(q.get('stars') or 0)})\n**השאלה:** {_clean(q['stem'])}\n\n"
                           f"**פתרון:**\n" + '\n'.join(f'- {_clean(x)}' for x in q.get('sol') or []) for q in qs)
        total += 1
        written += _note(folder / f"שאלות מבחן - {_safe(topic)}.md", {**base, 'kind': 'שאלות מבחן', 'title': topic},
                         f"שאלות מבחן עם פתרונות: {topic}", body)
    return written, total


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    courses = args or [p.name for p in SITE.iterdir() if p.is_dir()]
    for ck in courses:
        w, t = ingest(ck)
        print(f'{ck}: {t} notes ({w} new/changed)')
    if '--no-index' not in sys.argv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=True)
        from rag.vault_indexer import build_index
        r = build_index(force=False)
        print('index:', {k: r.get(k) for k in ('chunks', 'new_chunks', 'cached', 'backend') if k in r})
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    sys.exit(main())
