"""Generate PDFs from existing MD summaries — FREE (Playwright, no LLM)."""
import sys, os
from pathlib import Path
sys.path.insert(0, '/home/ubuntu/smart_study_agent')
os.chdir('/home/ubuntu/smart_study_agent')

from generators.pdf_generator import markdown_to_pdf
from config.paths import SUMMARIES_DIR, COURSE_NAMES
from utils.other_group import keep

mds = list(SUMMARIES_DIR.rglob('*.md'))
missing = [m for m in mds if not m.with_suffix('.pdf').exists()]
print(f'Generating PDFs for {len(missing)} summaries (no LLM, free).\n')

for md in missing:
    course = md.parts[-3]
    course_name = COURSE_NAMES.get(course, course)
    md_text = md.read_text(encoding='utf-8')
    # Strip metadata header (everything before first "---\n\n")
    if '\n---\n\n' in md_text:
        md_text = keep(md_text, md_text.split('\n---\n\n', 1)[-1])   # 14/11: "from another group" stays
    pdf_path = md.with_suffix('.pdf')
    print(f'📄 {course}/{md.stem[:55]} ...', flush=True)
    try:
        markdown_to_pdf(md_text, str(pdf_path),
                        title=md.stem, course_name=course_name)
        sz = pdf_path.stat().st_size // 1024
        print(f'  ✅ {sz}KB')
    except Exception as e:
        print(f'  ❌ {e}')

print('\nDone.')
