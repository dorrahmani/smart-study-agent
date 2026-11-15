"""Migrate old reviewer sent log (list) to new format (dict with paths)"""
import json
from pathlib import Path

old = json.loads(Path('data/summaries/sent_log.json').read_text(encoding='utf-8'))
print(f'Old entries: {len(old)}')

new_log = {}
summaries_dir = Path('data/summaries')

for name in old:
    found = list(summaries_dir.rglob(name))
    if found:
        key = found[0].as_posix()
        new_log[key] = '2026-04-24T00:00:00'

log_path = Path('config/reviewer_sent_log.json')
log_path.write_text(json.dumps(new_log, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'Migrated {len(new_log)} entries to reviewer_sent_log.json')
