"""
add_board_reading.py — מוסיף board reading לסיכומים קיימים שחסר להם.
מריץ על הרצאות עם board_reading=true שכבר סוכמו אבל ללא תוכן לוח.
"""
import sys, json, re
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
with open('config/courses.yaml', encoding='utf-8') as f:
    cfg = yaml.safe_load(f)

SUMMARIES_DIR = Path('data/summaries')
PANOPTO_DIR   = Path('data/panopto')

BOARD_COURSES = {
    k for k, v in cfg['courses'].items()
    if v.get('board_reading', False)
}
print(f"קורסים עם board_reading: {BOARD_COURSES}\n")


def summary_has_board(md_path: Path) -> bool:
    content = md_path.read_text(encoding='utf-8', errors='replace')
    # Check if board section exists and has content
    if 'לוח הכיתה' not in content and 'board' not in content.lower():
        return False
    m = re.search(r'board_frames["\s:]+(\d+)', content)
    if m and int(m.group(1)) > 0:
        return True
    if 'לוח הכיתה' in content:
        # Check it's not empty
        idx = content.find('לוח הכיתה')
        snippet = content[idx:idx+200]
        if len(snippet.strip()) > 30:
            return True
    return False


def append_board_to_summary(md_path: Path, board_text: str, usage: dict):
    """מוסיף תוכן לוח לסיכום קיים."""
    content = md_path.read_text(encoding='utf-8', errors='replace')

    board_section = f"""
## לוח הכיתה

{board_text}
"""

    # Update cost in sources section
    frame_count = usage.get('frame_count', 0)
    board_cost = 0.0
    model = usage.get('model', '')
    inp = usage.get('input_tokens', 0)
    out = usage.get('output_tokens', 0)
    if 'sonnet' in model:
        board_cost = inp * 3 / 1_000_000 + out * 15 / 1_000_000
    else:
        board_cost = inp * 0.8 / 1_000_000 + out * 4 / 1_000_000

    # Insert board section before the separator line or at end of sources section
    if '\n---\n' in content:
        insert_pos = content.find('\n---\n')
        content = content[:insert_pos] + board_section + content[insert_pos:]
    else:
        content += board_section

    # Update metadata: board_frames count
    content = re.sub(
        r'"board_frames":\s*\d+',
        f'"board_frames": {frame_count}',
        content
    )
    # Add board cost to API section
    if 'board_reading' not in content and board_cost > 0:
        cost_line = f'- **Board reading** ({model.split("-")[1]}): {inp} in + {out} out = ${board_cost:.4f}\n'
        # Find the total cost line and insert before it
        if '**סה"כ**' in content:
            pos = content.find('**סה"כ**')
            content = content[:pos] + cost_line + content[pos:]

    md_path.write_text(content, encoding='utf-8')
    print(f"  ✅ עודכן: {md_path.name}")


def find_summary_for_metadata(course_key: str, meta: dict) -> Path | None:
    """מוצא קובץ סיכום לפי תאריך וכותרת."""
    course_dir = SUMMARIES_DIR / course_key / 'lectures'
    if not course_dir.exists():
        return None

    title = meta.get('title', '')
    date = meta.get('date', '')

    # Try exact date match first
    for md in course_dir.glob('*.md'):
        if date and date in md.name:
            return md

    # Fuzzy match by title
    for md in course_dir.glob('*.md'):
        # Strip Hebrew encoding artifacts
        md_stem = md.stem.lower()
        title_clean = re.sub(r'[^\w\s]', '', title.lower())
        if any(word in md_stem for word in title_clean.split() if len(word) > 3):
            return md

    return None


def main():
    total_processed = 0
    total_skipped = 0
    total_errors = 0

    for course_key in BOARD_COURSES:
        meta_path = PANOPTO_DIR / f'{course_key}_metadata.json'
        if not meta_path.exists():
            print(f"⚠️ אין metadata ל-{course_key}")
            continue

        metadata = json.load(meta_path.open(encoding='utf-8'))
        course_name = cfg['courses'][course_key]['name']

        print(f"\n{'='*50}")
        print(f"📚 {course_name} ({course_key})")
        print(f"{'='*50}")

        use_sonnet = False  # always use Haiku for cost efficiency

        for meta in metadata:
            if meta.get('type') != 'lecture':
                continue
            video_path = meta.get('video_path', '')
            if not video_path:
                print(f"  ⏭️  ללא video_path: {meta.get('title', '?')}")
                continue

            # Find matching summary
            md_path = find_summary_for_metadata(course_key, meta)
            if not md_path:
                print(f"  ⏭️  לא נמצא סיכום עבור: {meta.get('title', '?')}")
                continue

            if summary_has_board(md_path):
                print(f"  ✓  כבר יש board reading: {md_path.name}")
                total_skipped += 1
                continue

            print(f"\n🎥 מריץ board reading: {md_path.name}")
            print(f"   video: {video_path[:70]}...")

            try:
                from collectors.board_reader import extract_board_transcript
                board_text, _frames, usage = extract_board_transcript(
                    video_path, course_name, use_sonnet=use_sonnet
                )

                if not board_text:
                    print(f"  ⚠️  לא חולצו frames")
                    total_errors += 1
                    continue

                append_board_to_summary(md_path, board_text, usage)
                inp = usage.get('input_tokens', 0)
                out = usage.get('output_tokens', 0)
                print(f"  💰 {inp}in + {out}out tokens")
                total_processed += 1

            except Exception as e:
                print(f"  ❌ שגיאה: {e}")
                import traceback; traceback.print_exc()
                total_errors += 1

    print(f"\n{'='*50}")
    print(f"✅ הושלם: {total_processed} עודכנו, {total_skipped} דולגו, {total_errors} שגיאות")


if __name__ == '__main__':
    main()
