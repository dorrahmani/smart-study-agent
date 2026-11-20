"""
scripts/generate_missing_summaries.py
Generates summaries for all the SRTs that lack a summary,
including board reading when relevant,
syncing to the Vault and NotebookLM, and sending by email.
"""

import sys, os
sys.path.insert(0, '.')
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from utils.env import _load_env_dotfile
_load_env_dotfile()

from pathlib import Path
from generators.summarizer import AISummarizer
from generators.gmail_sender import GmailSender
from collectors.panopto_collector import PanoptoCollector
import yaml, json

# ─── Config ────────────────────────────────────────────────────────────────

config = yaml.safe_load(open('config/courses.yaml', encoding='utf-8').read())
courses_cfg = config.get('courses', config)

s = AISummarizer()
gmail = GmailSender()

print(f'Summarizer model: {s.model}')

# ─── Sessions to process ───────────────────────────────────────────────────
# (srt_name, kind, title, date, course_key, board_reading)

PENDING = [
    ('discrete_math_2_2026-05-03_מרצה_א מרצה_ע.srt',
     'lecture', 'מרצה_א מרצה_ע', '2026-05-03', 'discrete_math_2', True),

    ('microeconomics_2026-05-03_מיקרו כלכלה א - קבלת החלטות כלכליות.srt',
     'lecture', "מיקרו כלכלה א' - קבלת החלטות כלכליות", '2026-05-03', 'microeconomics', False),
    ('microeconomics_2026-05-03_מיקרו א - פתרון תרגיל 2.srt',
     'tutorial', 'מיקרו א - פתרון תרגיל 2', '2026-05-03', 'microeconomics', False),

    ('linear_algebra_2_2026-05-04_הרצאה מרצה_צ.srt',
     'lecture', 'הרצאה מרצה_צ', '2026-05-04', 'linear_algebra_2', True),

    ('intro_to_cs_2026-05-03_Lecture 4b.srt',
     'lecture', 'Lecture 4b', '2026-05-03', 'intro_to_cs', False),
    ('intro_to_cs_2026-05-04_תרגול 3 - טל.srt',
     'tutorial', 'תרגול 3 - טל', '2026-05-04', 'intro_to_cs', False),
]


# ─── Get video paths from Panopto (for board reading) ──────────────────────

def get_video_paths(needs_board: list) -> dict:
    if not any(b for *_, b in needs_board):
        return {}
    print('\n📡 מתחבר ל-Panopto לקריאת מסלולי וידאו...')
    try:
        c = PanoptoCollector()
        c.login()
    except Exception as e:
        print(f'Login failed: {e} — board reading disabled')
        return {}

    video_map = {}
    for srt_name, kind, title, date, course_key, board in needs_board:
        if not board:
            continue
        panopto_cfg = courses_cfg.get(course_key, {}).get('sources', {}).get('panopto', {})
        folder_id = panopto_cfg.get('folder_id')
        if folder_id == 'TO_BE_FILLED':
            folder_id = None
        try:
            sessions = c.search_sessions('', days_back=14, folder_id=folder_id)
            for sess in sessions:
                sess_title = c._clean_title(sess.get('SessionName', ''))
                dt = c.parse_panopto_date(sess.get('StartTime', ''))
                sess_date = dt.strftime('%Y-%m-%d') if dt else ''
                if sess_date == date and (title in sess_title or sess_title in title):
                    video_id = sess.get('DeliveryID', '')
                    vid = c._get_video_mp4_url(video_id)
                    if vid:
                        video_map[srt_name] = vid
                        print(f'  ✅ video: {title}')
                    break
        except Exception as e:
            print(f'  ⚠ {title}: {e}')
    return video_map


# ─── Build PDF list per course ─────────────────────────────────────────────

def get_pdf_paths(course_key: str):
    course = courses_cfg.get(course_key, {})
    sources = course.get('sources', {})
    paths = []
    source_type = 'moodle'

    static = sources.get('static_pdf')
    if static and Path(static).exists():
        paths.append(static)
        source_type = 'static_pdf'

    moodle_dir = Path(f'data/moodle/{course_key}')
    if moodle_dir.exists():
        for f in sorted(moodle_dir.glob('*.pdf')):
            p = str(f)
            if p not in paths:
                paths.append(p)
                if 'wikidot' in f.name:
                    source_type = 'wikidot'

    return paths, source_type


# ─── Main loop ─────────────────────────────────────────────────────────────

video_map = get_video_paths(PENDING)
generated = []
failed = []

for srt_name, kind, title, date, course_key, board_reading in PENDING:
    srt_path = f'data/panopto/{srt_name}'
    out_path = s.build_summary_path(course_key, kind, title, date)

    if not Path(srt_path).exists():
        print(f'\n⚠ SRT not found: {srt_name}')
        continue

    if Path(out_path).exists():
        print(f'\n⏭  Already exists: {Path(out_path).name}')
        continue

    course = courses_cfg.get(course_key, {})
    course_name = course.get('name', course_key)
    notebook_offset = course.get('notebook_offset', 0)

    pdf_paths, pdf_source_type = get_pdf_paths(course_key)
    video_path = video_map.get(srt_name) if board_reading else None

    print(f'\n{"="*55}')
    print(f'📚 {course_name} — {title} ({date})')
    print(f'   kind={kind}  board={bool(video_path)}  pdfs={len(pdf_paths)}')
    print(f'{"="*55}')

    try:
        summary, sources = s.generate_summary(
            course_name=course_name,
            lecture_title=title,
            caption_path=srt_path,
            pdf_path=pdf_paths if pdf_paths else None,
            pdf_source_type=pdf_source_type,
            video_path=video_path,
            course_key=course_key,
            notebook_offset=notebook_offset,
        )

        header = s.build_sources_header(
            transcript=sources.get('transcript', ''),
            pdf_content=sources.get('pdf_content', ''),
            board_text=sources.get('board_text', ''),
            caption_path=srt_path,
            pdf_path=pdf_paths,
            pdf_source_type=pdf_source_type,
            video_path=video_path,
            usage=sources.get('usage'),
        )

        s.save_summary(header + summary, out_path, course_name)
        print(f'✅ Saved: {out_path}')
        generated.append((course_key, course_name, title, date, kind, out_path))

    except Exception as e:
        print(f'❌ Failed {title}: {e}')
        import traceback; traceback.print_exc()
        failed.append((title, str(e)))

# ─── Send emails ────────────────────────────────────────────────────────────

if generated:
    print(f'\n{"="*55}')
    print(f'📧 שולח {len(generated)} מיילים...')
    for course_key, course_name, title, date, kind, out_path in generated:
        pdf_path = out_path.replace('.md', '.pdf')
        try:
            ok = gmail.send_summary(
                course_name=course_name,
                lecture_title=title,
                summary_md_path=out_path,
                summary_pdf_path=pdf_path if Path(pdf_path).exists() else None,
                recipient='user6@example.com',
            )
            print(f'  {"✅" if ok else "❌"} {title}')
        except Exception as e:
            print(f'  ❌ {title}: {e}')

# ─── Sync vault + NotebookLM ───────────────────────────────────────────────

if generated:
    print(f'\n{"="*55}')
    print('🔄 מסנכרן ל-Vault ו-NotebookLM...')
    try:
        from pipelines.sync_all import sync_all
        result = sync_all(
            budget_usd=2.0,
            include_panopto=False,
            include_whisper=False,
            include_summaries=False,
            include_notebooklm=True,
            headless=True,
        )
        print('Sync:', result.get('summary_text', 'done'))
    except Exception as e:
        print(f'⚠ Sync failed: {e}')
        import traceback; traceback.print_exc()

# ─── Final report ──────────────────────────────────────────────────────────

print(f'\n{"="*55}')
print(f'✅ הושלם: {len(generated)} סיכומים  |  {len(failed)} כשלונות')
for t, err in failed:
    print(f'   ❌ {t}: {err[:80]}')
