"""Organize all summaries from this week (2026-05-17 to 2026-05-21):
- Place each summary in the correct folder (lectures vs tutorials).
- Regenerate if missing or generated with old simple prompt (< 30KB).
- Skip Reuven's tutorial (user always wants lecturer_e's).
- Skip duplicate Yelena recordings ('הרצאה מרצה_צ 2' = part 2, already covered).
"""
import sys, os
from pathlib import Path
sys.path.insert(0, '/home/ubuntu/smart_study_agent')
os.chdir('/home/ubuntu/smart_study_agent')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/smart_study_agent/.env', override=True)
if os.environ.get('GEMINI_API_KEY'):
    os.environ.pop('GOOGLE_API_KEY', None)

import yaml
cfg = yaml.safe_load(open('config/courses.yaml', encoding='utf-8'))
from generators.summarizer import AISummarizer
from config.paths import COURSE_NAMES, PANOPTO_DIR, SUMMARIES_DIR
from utils.srt_quality import srt_is_good_hebrew

WEEK_START = '2026-05-17'  # Saturday (last week's start)
MIN_QUALITY_KB = 30        # MD < this = old simple prompt, regenerate

# ---- Discover candidates
candidates = []
for srt in sorted(PANOPTO_DIR.glob('*.srt')):
    if srt.name.endswith('.bad') or srt.name.endswith('.bak'):
        continue
    stem = srt.stem
    course_key = None
    for ck in sorted(COURSE_NAMES, key=len, reverse=True):
        if stem.startswith(ck + '_'):
            course_key = ck; break
    if not course_key: continue
    rest = stem[len(course_key) + 1:]
    if len(rest) < 11 or rest[4] != '-' or rest[7] != '-': continue
    date_p = rest[:10]
    if date_p < WEEK_START: continue
    title = rest[11:]
    # Skip Reuven (user always wants lecturer_e in calculus)
    if course_key == 'calculus_2' and ('ראובני' in title or 'ראובן' in title):
        continue
    # Skip "Yelena 2" (already have Yelena part 1)
    if course_key == 'linear_algebra_2' and 'מרצה_צ 2' in title:
        continue
    item_type = 'tutorial' if any(k in title.lower() for k in ('תרגול','תרגיל','tut','tirgul','recitation')) else 'lecture'
    md_path = Path(AISummarizer.build_summary_path(course_key, item_type, title, date_p))
    candidates.append((course_key, item_type, date_p, title, srt, md_path))

# Sort: oldest first so list is chronological
candidates.sort(key=lambda x: (x[2], x[0]))

print(f'\n=== Week summary: {len(candidates)} SRTs from {WEEK_START}+ ===\n')
todo = []
for ck, it, d, t, srt, md in candidates:
    if md.exists() and md.stat().st_size >= MIN_QUALITY_KB * 1024:
        size_kb = md.stat().st_size // 1024
        print(f'  ✅ {ck:18s} | {d} | {t:50s} [{size_kb}KB OK]')
    elif md.exists():
        size_kb = md.stat().st_size // 1024
        print(f'  🔄 {ck:18s} | {d} | {t:50s} [{size_kb}KB → regen]')
        todo.append((ck, it, d, t, srt, md))
    else:
        print(f'  ➕ {ck:18s} | {d} | {t:50s} [MISSING → create]')
        todo.append((ck, it, d, t, srt, md))

if not todo:
    print(f'\n✅ הכל מסודר. {len(candidates)} סיכומים תקינים בדאשבורד.')
    sys.exit(0)

print(f'\n=== Regenerating {len(todo)} summaries with gemini-3.1-pro-preview (16K tokens) ===\n')

def get_pdfs(ck):
    pdfs = []
    sp = cfg['courses'][ck]['sources'].get('static_pdf','')
    if sp and Path(sp).exists(): pdfs.append(sp)
    mdir = Path(f'data/moodle/{ck}')
    if mdir.exists(): pdfs += sorted(str(f) for f in mdir.glob('*.pdf'))
    return pdfs or None

s = AISummarizer()
ok_count = 0
for ck, item_type, d, t, srt, md_path in todo:
    print(f'\n📚 {ck} | {d} | {t}', flush=True)
    if not srt_is_good_hebrew(str(srt)):
        print('  ⚠️ SRT not good Hebrew — skipping'); continue
    course_name = COURSE_NAMES[ck]
    pdf_path = md_path.with_suffix('.pdf')
    # Remove old smaller version (if any) before regen
    for p in [md_path, pdf_path]:
        if p.exists():
            p.unlink()

    try:
        summary, sources = s.generate_summary(
            course_name=course_name, lecture_title=t,
            caption_path=str(srt), pdf_path=get_pdfs(ck),
            pdf_source_type='moodle', video_path=None,
        )
    except Exception as e:
        print(f'  ❌ generate_summary failed: {str(e)[:100]}'); continue

    if not summary or len(summary) < 2000:
        print(f'  ❌ summary empty/short'); continue
    print(f'  ✅ summary {len(summary)} chars')

    pdf_result = s.save_summary(
        summary=summary, output_path=str(md_path),
        course_name=course_name, lecture_title=t, sources=sources,
    )
    print(f'  💾 saved (PDF: {bool(pdf_result)})')
    ok_count += 1

print(f'\n✅ סיכום: {ok_count}/{len(todo)} סיכומים נוצרו.')
print(f'   {len(candidates) - len(todo) + ok_count} סה"כ זמינים בדאשבורד.')
