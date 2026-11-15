"""scripts/lecturer_rebuild.py — the lecturer updated his site → rebuild that lecture's unified summary and send it.

The owner 18/10: "תוסיף בנייה מחדש אוטומטית כשהמרצה מעדכן". The nightly summary may run before Naftali posts the week's
summaries (or adds more later). Twice a day: every probability summary ≤3 weeks old whose week now has lecturer PDFs
it didn't include (collectors/lecturer_site.stale) is regenerated in place with them and sent to WhatsApp as
"🔄 מעודכן". At most MAX_PER_RUN per run (~$0.10 each), the budget lock applies (inside the summarizer).

cron (VM): 0 9,20 * * * TZ=Asia/Jerusalem → 12:00 / 23:00 Israel (after the 22:00 pipeline).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

MAX_PER_RUN = 3


def rebuild(md: str, rec: dict, lecturer: list[str], summarizer=None, send=None) -> bool:
    """Regenerate one summary with the lecturer's PDFs; overwrite its .md/.pdf; send it. → True if done."""
    from collectors.lecturer_site import record
    from config.paths import COURSE_NAMES
    from generators.summarizer import AISummarizer
    ck = rec['course']
    summarizer = summarizer or AISummarizer()
    moodle_dir = ROOT / 'data' / 'moodle' / ck
    moodle = sorted(str(f) for f in moodle_dir.glob('*.pdf')) if moodle_dir.exists() else []
    summary, sources = summarizer.generate_summary(
        course_name=COURSE_NAMES.get(ck, ck), lecture_title=rec['title'], caption_path=rec['srt'],
        pdf_path=(lecturer + moodle) or None, course_key=ck, pdf_source_type='moodle', video_path=None)
    if not summary or len(summary) < 2000 or summary.startswith('# שגיאה'):
        print(f"  ⚠️ rebuild produced nothing usable for {rec['title']}")
        return False
    from utils.other_group import with_label              # 14/11: a rebuild keeps "from another group"
    summary = with_label(summary, ck, str(rec['date'])[:10], rec['title'])
    summarizer.save_summary(summary=summary, output_path=md, course_name=COURSE_NAMES.get(ck, ck),
                            lecture_title=rec['title'], sources=sources)
    record(ck, md, rec['srt'], rec['title'], rec['date'], lecturer)
    pdf = Path(md).with_suffix('.pdf')
    if pdf.exists():
        names = ', '.join(Path(p).stem for p in lecturer)
        caption = (f"🔄 *סיכום מעודכן — {COURSE_NAMES.get(ck, ck)}*\n{rec['title']} ({rec['date'][8:10]}/{rec['date'][5:7]})\n"
                   f"📘 עכשיו כולל את סיכומי המרצה: {names}")
        if send is None:
            from whatsapp import outbox
            chat = outbox.owner_chat()
            if chat:
                outbox.send_media(chat, pdf, caption)
        else:
            send(pdf, caption)
    print(f"  ✅ rebuilt {Path(md).name} with {len(lecturer)} lecturer summaries")
    return True


def main(today: date | None = None) -> int:
    from collectors.lecturer_site import stale, homework_sync, HOMEWORK
    for ck in HOMEWORK:                                 # 19/10: exercises posted on his site → deadlines + calendar question
        try:
            new = homework_sync(ck)
            print(f'homework ({ck}): {len(new)} new')
        except Exception as e:
            print(f'  ⚠️ homework ({ck}): {e!r}'[:200])
    todo = stale(today)
    if not todo:
        print('lecturer rebuild: nothing new on the lecturers\' sites')
        return 0
    done = 0
    for md, rec, lecturer in todo[:MAX_PER_RUN]:
        try:
            done += rebuild(md, rec, lecturer)
        except Exception as e:
            print(f"  ⚠️ rebuild {rec.get('title')}: {e!r}"[:300])
    print(f'lecturer rebuild: {done}/{len(todo)} rebuilt')
    return 0


if __name__ == '__main__':
    sys.exit(main())
