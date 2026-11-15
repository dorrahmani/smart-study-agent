# -*- coding: utf-8 -*-
"""
scripts/ingest_transcripts.py — מכניס את כל תמלולי ה-SRT אל המוח (vault).

- קורא את כל data/panopto/*.srt
- מסנן איכות (srt_is_good_hebrew — הבאג הקירילי)
- מסיר כפילויות: גרסה מתוארכת עדיפה על לא-מתוארכת; merged עדיף על חלקיו
- ממיר SRT לטקסט נקי וכותב md עם frontmatter (yaml.safe_dump!) אל
  <קורס>/🎙️ תמלולים/ ב-vault
- הרצה חוזרת בטוחה: כותב מחדש רק אם התוכן השתנה
"""
import sys, re, io
sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import yaml
from pathlib import Path
from config.paths import VAULT_DIR, VAULT_COURSE_FOLDER_TO_KEY
from utils.srt_quality import srt_is_good_hebrew

PANOPTO = Path('data/panopto')
KEY_TO_FOLDER = {}
for folder, key in VAULT_COURSE_FOLDER_TO_KEY.items():
    # prefer the folder that actually exists on disk
    if key not in KEY_TO_FOLDER or (VAULT_DIR / folder).exists():
        if (VAULT_DIR / folder).exists() or key not in KEY_TO_FOLDER:
            KEY_TO_FOLDER[key] = folder

DATE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})_')
TS_RE = re.compile(r'^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->')

def parse_name(p: Path):
    """-> (course_key, date|None, base_title)"""
    name = p.stem
    ck = next((k for k in sorted(KEY_TO_FOLDER, key=len, reverse=True) if name.startswith(k + '_')), None)
    if not ck:
        return None, None, name
    rest = name[len(ck) + 1:]
    m = DATE_RE.match(rest)
    date = m.group(1) if m else None
    base = rest[m.end():] if m else rest
    return ck, date, base.strip()

def srt_to_text(path: Path) -> str:
    lines_out, prev = [], None
    raw = path.read_text(encoding='utf-8', errors='replace')
    for ln in raw.splitlines():
        t = ln.strip()
        if not t or t.isdigit() or TS_RE.match(t):
            continue
        if t == prev:      # Whisper repetition collapse
            continue
        lines_out.append(t)
        prev = t
    # glue into paragraphs of ~8 caption lines so chunking has boundaries
    paras, buf = [], []
    for t in lines_out:
        buf.append(t)
        if len(buf) >= 8:
            paras.append(' '.join(buf)); buf = []
    if buf:
        paras.append(' '.join(buf))
    return '\n\n'.join(paras)

def main():
    srts = sorted(PANOPTO.glob('*.srt'))
    groups = {}   # (ck, base) -> list[(path, date)]
    skipped_bad = []
    for p in srts:
        ck, date, base = parse_name(p)
        if ck is None:
            print(f'  ? לא זוהה קורס: {p.name}'); continue
        if not srt_is_good_hebrew(p):
            skipped_bad.append(p.name); continue
        groups.setdefault((ck, base), []).append((p, date))

    # dated version wins inside each group
    chosen = {}
    for (ck, base), items in groups.items():
        dated = [x for x in items if x[1]]
        pick = max(dated or items, key=lambda x: x[0].stat().st_size)
        chosen[(ck, base)] = pick

    # merged beats its parts (same course, base startswith merged-stem)
    drop = set()
    for (ck, base) in list(chosen):
        if '_merged' in base:
            stem = base.replace('_merged', '').strip()
            for (ck2, base2) in chosen:
                if ck2 == ck and base2 != base and base2.startswith(stem):
                    drop.add((ck2, base2))
    for k in drop:
        print(f'  ⏭️ מדלג (מכוסה ע"י merged): {k[1]}')
        chosen.pop(k, None)

    written = same = 0
    for (ck, base), (path, date) in sorted(chosen.items()):
        folder = VAULT_DIR / KEY_TO_FOLDER[ck] / '🎙️ תמלולים'
        folder.mkdir(parents=True, exist_ok=True)
        body = srt_to_text(path)
        kind = 'תרגול' if 'תרגול' in base or 'tut' in base.lower() else 'הרצאה'
        fm = yaml.safe_dump({
            'type': 'transcript',
            'kind': kind,
            'course': KEY_TO_FOLDER[ck],
            'course_key': ck,
            'date': date or '',
            'source_srt': path.name,
            'title': base,
        }, allow_unicode=True, sort_keys=False)
        content = f'---\n{fm}---\n\n# תמלול · {base}\n\n{body}\n'
        safe = re.sub(r'[\\/:*?"<>|]', '-', base)
        out = folder / (f'{date} - {safe}.md' if date else f'{safe}.md')
        if out.exists() and out.read_text(encoding='utf-8') == content:
            same += 1; continue
        out.write_text(content, encoding='utf-8')
        written += 1
    print(f'\n✅ נכתבו {written} תמלולים (ללא שינוי: {same}) | סוננו כ-bad: {len(skipped_bad)}')
    if skipped_bad:
        print('⚠️ תמלולים פסולים (קירילית/ריקים) — דורשים תמלול Whisper מחדש:')
        for n in skipped_bad: print('   -', n)

if __name__ == '__main__':
    main()
