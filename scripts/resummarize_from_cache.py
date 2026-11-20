"""
resummarize_from_cache.py — re-summarizes lectures that have a saved SRT but no summary.
Usage: python scripts/resummarize_from_cache.py [course_key]
        If no course is given — scans all the courses.
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

from dotenv import load_dotenv
load_dotenv(override=True)

import yaml
from generators.summarizer import AISummarizer
from main import SmartStudyAgent

CONFIG_PATH = 'config/courses.yaml'
PANOPTO_DIR = Path('data/panopto')

with open(CONFIG_PATH, encoding='utf-8') as f:
    config = yaml.safe_load(f)

target_course = sys.argv[1] if len(sys.argv) > 1 else None

# initialize the agent without a panopto login (no need to collect again)
class CacheAgent(SmartStudyAgent):
    def __init__(self, config_path):
        import yaml
        self.config_path = config_path
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        from generators.summarizer import AISummarizer
        from generators.gmail_sender import GmailSender
        self.summarizer = AISummarizer()
        self.mailer = GmailSender()
        self.output_dir = Path("data/summaries")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        print("✅ CacheAgent מוכן (ללא Panopto login)")

agent = CacheAgent(CONFIG_PATH)
summarizer = agent.summarizer

total_done = 0

for course_key, course_cfg in config['courses'].items():
    if target_course and course_key != target_course:
        continue

    meta_path = PANOPTO_DIR / f'{course_key}_metadata.json'
    if not meta_path.exists():
        continue

    sessions = json.loads(meta_path.read_text(encoding='utf-8'))
    course_name = course_cfg['name']

    print(f'\n{"="*60}')
    print(f'📚 {course_name} ({len(sessions)} sessions in cache)')
    print(f'{"="*60}')

    # load moodle items from the cache
    moodle_meta = Path(f'data/moodle/{course_key}_metadata.json')
    moodle_items = []
    if moodle_meta.exists():
        try:
            moodle_items = json.loads(moodle_meta.read_text(encoding='utf-8'))
        except Exception:
            pass

    # add static_pdf if defined
    static_pdf = course_cfg.get('sources', {}).get('static_pdf')
    if static_pdf and Path(static_pdf).exists():
        moodle_items.append({
            'type': 'static_pdf',
            'file_info': {'title': Path(static_pdf).stem},
            'local_path': static_pdf,
        })

    # count lectures and tutorials separately
    lecture_index = 0
    tutorial_index = 0

    for session in sorted(sessions, key=lambda s: s.get('date', '')):
        title = session['title']
        item_type = session.get('type', 'lecture')
        date = session.get('date', '')[:10]
        srt_path = session.get('srt_path', '')

        summary_path = AISummarizer.build_summary_path(course_key, item_type, title, date=date)

        if Path(summary_path).exists():
            print(f'  ⏭️  קיים: {title}')
            if item_type == 'lecture':
                lecture_index += 1
            else:
                tutorial_index += 1
            continue

        if not srt_path or not Path(srt_path).exists():
            print(f'  ⚠️  אין SRT: {title}')
            continue

        current_index = lecture_index if item_type == 'lecture' else tutorial_index
        if item_type == 'lecture':
            lecture_index += 1
        else:
            tutorial_index += 1

        print(f'\n  🤖 מסכם ({item_type}): {title} ({date})')

        # find a matching PDF
        matching_pdf, pdf_source_type = agent.match_pdf_to_video(
            title, moodle_items, session_index=current_index, item_type=item_type
        )

        try:
            notebook_offset = course_cfg.get('notebook_offset', 0)
            summary, sources = summarizer.generate_summary(
                course_name=course_name,
                lecture_title=title,
                caption_path=srt_path,
                pdf_path=matching_pdf,
                pdf_source_type=pdf_source_type,
                video_path=None,
                course_key=course_key,
                notebook_offset=notebook_offset,
            )
            summarizer.save_summary(summary, summary_path,
                                    course_name=course_name,
                                    lecture_title=title,
                                    sources=sources)
            print(f'  ✅ נשמר: {summary_path}')
            total_done += 1
        except Exception as e:
            print(f'  ❌ שגיאה: {e}')

print(f'\n✅ סה"כ נוצרו {total_done} סיכומים חדשים')
