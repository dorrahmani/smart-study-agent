"""
generators/practice_set.py — practice problems with full worked solutions.

Built from the course's own recent summaries (what was actually taught), so the
problems match the lecturer's notation and level. Problems first, then a
separate solutions section — try before you look. Graded easy → hard.

    build_practice_set('probability')                    # last 3 lectures/tutorials
    build_practice_set('probability', topic='בינומית')   # summaries that mention it
    build_practice_set('probability', days=7)            # this week (weekly job)
    build_practice_set('probability', homework=next_assignment('probability'))
        # aimed at the open assignment. Three guards so the set never solves graded work:
        # the generator never sees the assignment (only generic skills + a list of its
        # specific objects to avoid), and a final check against the PDF drops any
        # exercise whose solution would answer an assignment question.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = 'gemini-3.8-flash'
MAX_SOURCE_CHARS = 90_000


def topic_sections(md: str, topic: str, min_chars: int = 1500) -> str:
    """Only the summary sections (## / ### blocks) that mention the topic — 08/10: a topic set sent whole
    summaries (~32k tokens) when a few sections were relevant. Falls back to the full text if too little matches."""
    words = [w for w in re.findall(r'[\w"׳\']{3,}', topic)] or [topic]
    blocks = re.split(r'(?m)^(?=#{2,3} )', md)
    keep = [b for b in blocks if any(w in b for w in words)]
    out = '\n'.join(keep)
    return out if len(out) >= min_chars else md

PROMPT = """אתה מתרגל אוניברסיטאי מעולה בקורס "{course}". על סמך חומר הקורס למטה (סיכומי ההרצאות והתרגולים
האחרונים), כתוב סט תרגול של {n} תרגילים{topic_line}.

דרישות:
- התרגילים בסגנון ובסימון של החומר הזה (לא של ספר אחר), ברמת מבחן אוניברסיטאי.
- מדורגים: {n_easy} קלים, {n_mid} בינוניים, {n_hard} קשים (בסגנון שאלת מבחן).
- כל נוסחה ב-LaTeX ($...$ / $$...$$).{course_rule}
- **בלי דיאגרמות Mermaid.** גרף — תאר במילים.{homework_rules}

מבנה הפלט (Markdown, בעברית):
# 🏋️ סט תרגול — {course}{topic_title}
## חלק א׳ — תרגילים
### תרגיל 1 (קל) — <נושא>
<נוסח התרגיל בלבד, בלי רמזים לפתרון>
... (כל התרגילים)
## חלק ב׳ — פתרונות מלאים
### פתרון תרגיל 1
<פתרון צעד-אחר-צעד: מה מזהים בשאלה, איזה כלי בוחרים ולמה, החישוב המלא, תשובה סופית מודגשת>
**טעות נפוצה:** <שורה אחת>
... (כל הפתרונות)

## חומר הקורס
{source}
"""

HOMEWORK_RULES = """
- הסטודנט מתכונן למטלה שמוגשת בקרוב. כוון את הסט למיומנויות האלה (תרגיל אחד לפחות לכל שורה):
{skills}
- **אסור** להשתמש באובייקטים של המטלה (היא עבודה להגשה) — לא בהם, לא בגרסה עם פרמטר אחר ולא במקרה פרטי שלהם.
  בחר פונקציות/משוואות/טענות *אחרות* שמתרגלות את אותה מיומנות:
{avoid}"""

HOMEWORK_HEADER = """
🎯 **מכוון למטלה:** {desc}
אותן מיומנויות, תרגילים אחרים — המטלה עצמה נשארת לך.

**מה צריך לשלוט בו לפני המטלה:**
{skills}
"""

COURSE_RULES = {
    'software_1': '\n- תרגילי קוד ב-Java בבלוקי ```java; בפתרון — קוד מלא ומוסבר + מה יודפס.',
    'probability': '\n- בכל פתרון: הגדרת המאורעות/המשתנים המקריים, זיהוי ההתפלגות, ובדיקת סבירות של התשובה.',
}


def _recent_summaries(course_key: str, days: int | None, topic: str | None, limit: int = 3) -> list[Path]:
    files = []
    for sub in ('lectures', 'tutorials'):
        d = ROOT / 'data' / 'summaries' / course_key / sub
        if d.is_dir():
            files += [p for p in d.glob('*.md') if p.stat().st_size > 2000]
    files.sort(key=lambda p: p.name, reverse=True)          # YYYY-MM-DD prefix → newest first
    if topic:
        files = [p for p in files if topic in p.read_text(encoding='utf-8', errors='ignore')]
        return files[:limit + 2]
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        return [p for p in files if p.name[:10] >= cutoff]
    return files[:limit]


def build_practice_set(course_key: str, topic: str | None = None, days: int | None = None,
                       n: int = 6, homework: dict | None = None) -> tuple[str, list[str]]:
    """(markdown, source file names). Raises ValueError when there is no material.
    `homework`: an item from utils.open_homework (used only when it has a PDF)."""
    from utils.open_homework import describe, homework_profile, overlapping_exercises, drop_exercises
    prof = homework_profile(homework) if homework else {'skills': '', 'avoid': []}
    skills = prof['skills']
    from config.paths import COURSE_NAMES
    course = COURSE_NAMES.get(course_key, course_key)
    srcs = _recent_summaries(course_key, days, topic)
    if not srcs:
        raise ValueError(f'אין עדיין סיכומים בקורס {course}' + (f' שמזכירים "{topic}"' if topic else ''))
    source, used = '', []
    for p in srcs:
        txt = p.read_text(encoding='utf-8', errors='ignore')
        txt = re.sub(r'\]\(https?://[^)]+\)', ']', txt)        # drop Panopto link URLs
        if topic:
            txt = topic_sections(txt, topic)
        if len(source) + len(txt) > MAX_SOURCE_CHARS:
            break
        source += f'\n\n=== {p.stem} ===\n{txt}'
        used.append(p.stem)
    n = max(3, min(int(n), 12))
    n_hard = max(1, n // 3)
    n_easy = max(1, n // 3)
    prompt = PROMPT.format(course=course, n=n, n_easy=n_easy, n_mid=n - n_easy - n_hard, n_hard=n_hard,
                           topic_line=f' בנושא "{topic}"' if topic else '',
                           topic_title=f' · {topic}' if topic else '',
                           course_rule=COURSE_RULES.get(course_key, ''), source=source,
                           homework_rules=HOMEWORK_RULES.format(
                               skills=skills, avoid='\n'.join(f'  - {x}' for x in prof['avoid']) or '  - (אין)')
                           if skills else '')

    import google.genai as genai
    from google.genai import types as T
    key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
    client = genai.Client(api_key=key)
    # worked solutions need real reasoning → medium thinking, generous budget
    resp = client.models.generate_content(
        model=MODEL, contents=prompt,
        config=T.GenerateContentConfig(max_output_tokens=24000, temperature=0.4,
                                       thinking_config=T.ThinkingConfig(thinking_level='medium')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op='practice_set')
    md = (resp.text or '').strip()
    if len(md) < 500:
        raise RuntimeError('המודל החזיר סט תרגול ריק')
    if skills:
        part_a = md.split('## חלק ב', 1)[0]
        bad = overlapping_exercises(homework, part_a)
        total = len(re.findall(r'^###\s*תרגיל\s+\d+', part_a, re.M))
        if bad is None or (total and len(bad) >= total):
            # unchecked or nothing left → a regular set; never ship unchecked homework-aimed work
            print(f'  ⚠️ homework overlap: {"check failed" if bad is None else "all exercises overlap"} — regular set')
            return build_practice_set(course_key, topic=topic, days=days, n=n, homework=None)
        if bad:
            md = drop_exercises(md, bad)
            md += (f'\n\n_הוסרו {len(bad)} תרגילים (מס׳ {", ".join(map(str, sorted(bad)))}) '
                   'שהיו קרובים מדי לשאלות המטלה — היא נשארת לך._\n')
        # our own header, right under the title
        head, _, rest = md.partition('\n')
        md = head + '\n' + HOMEWORK_HEADER.format(desc=describe(homework), skills=skills) + rest
    md += '\n\n---\n*נבנה מתוך: ' + ' · '.join(used) + '*\n'
    return md, used


def aimed_at_homework(md: str) -> bool:
    return '🎯 **מכוון למטלה:**' in md


def practice_pdf(course_key: str, topic: str | None = None, days: int | None = None,
                 n: int = 6, homework: dict | None = None) -> Path:
    """Build the set and render it to data/practice/<course>/<date> - <topic>.pdf."""
    from config.paths import COURSE_NAMES
    from generators.pdf_generator import markdown_to_pdf
    md, _ = build_practice_set(course_key, topic=topic, days=days, n=n, homework=homework)
    if aimed_at_homework(md) and not topic:          # not when it fell back to a regular set
        topic = f"לקראת {homework.get('title', 'המטלה')}"
    out_dir = ROOT / 'data' / 'practice' / course_key
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^\w ֐-׿-]', '', topic or 'סט תרגול')[:40].strip()
    stem = f'{datetime.now():%Y-%m-%d} - {safe}'
    (out_dir / f'{stem}.md').write_text(md, encoding='utf-8')
    pdf = out_dir / f'{stem}.pdf'
    markdown_to_pdf(md, str(pdf), title=f'סט תרגול — {topic or ""}', course_name=COURSE_NAMES.get(course_key, course_key))
    return pdf
