"""List upcoming assignments + their PDF status."""
import json
from datetime import datetime
from pathlib import Path

d = json.load(open('/home/ubuntu/smart_study_agent/data/assignments/_cache.json', encoding='utf-8'))
items = d.get('assignments', []) if isinstance(d, dict) and 'assignments' in d else (
    list(d.values()) if isinstance(d, dict) else d
)
items = [a for a in items if isinstance(a, dict)]
print(f'Total assignments in cache: {len(items)}\n')

upcoming, past, no_date = [], [], []
now = datetime.now()
for a in items:
    due = a.get('due_date')
    if not due:
        no_date.append(a); continue
    try:
        dt = datetime.fromisoformat(due)
        (upcoming if dt > now else past).append((dt, a))
    except Exception:
        no_date.append(a)
upcoming.sort()

print(f'=== {len(upcoming)} upcoming deadlines ===')
for dt, a in upcoming:
    fmt_date = dt.strftime('%d/%m %H:%M')
    pdf = a.get('pdf_path') or a.get('file_path') or a.get('local_file') or ''
    has_pdf = '✅' if pdf and Path(pdf).exists() else '❌ NO PDF'
    ck = a.get('course_key', '?')
    title = a.get('title', '?')
    print(f'  {fmt_date}  {ck:18s}  {title:40s}  {has_pdf}')

print(f'\n=== {len(no_date)} no-deadline (older or solutions) ===')
print(f'=== {len(past)} past deadlines ===')
