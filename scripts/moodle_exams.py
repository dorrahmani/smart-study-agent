"""scripts/moodle_exams.py — a course's past exams from Moodle → vault + a topic-weight map (like lecturer_exams.py).

The owner 20/10 (after downloading micro's questions + solutions): the same "מופיע הרבה במבחנים?" / "תביא לי שאלה על …" /
"פתרון" as in probability, for every course whose Moodle has past exams. Micro B's Moodle has 6 past exams with
solutions + last year's two. Once (and again only for new exams):
  1. the exam / solution PDFs already downloaded by collectors/course_files (data/moodle/<course>/files)
     → Gemini reads each once (lecturer_site.transcribe) → clean Hebrew markdown with LaTeX
  2. → vault "🧾 מבחנים/<course>/<date> שאלות|פתרון.md"
  3. one cheap call per exam tags each question by the course's units (micro: its 10 slide decks)
  4. data/exam_topics/<course>.json (agent/exam_weight reads it) + a vault note
    venv/bin/python3 scripts/moodle_exams.py microeconomics_2
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

COURSES = {
    'microeconomics_2': {
        'name': 'מיקרו כלכלה ב׳', 'vault': 'מיקרו כלכלה ב', 'by': 'מבחני הקורס',
        'sections': ('מבחנים משנים קודמות', 'מבחן'),
        'topics': ['יעילות בכלכלת חליפין (פארטו, קופסת אדג׳וורת׳)', 'יעילות בתנאי אי-ודאות',
                   'יעילות בכלכלה עם ייצור', 'שיווי משקל תחרותי בכלכלת חליפין',
                   'שיווי משקל תחרותי בתנאי אי-ודאות', 'שיווי משקל תחרותי בכלכלה עם ייצור',
                   'משפטי הרווחה', 'מיסים וסובסידיות', 'השפעות חיצוניות', 'מוצרים ציבוריים'],
    },
}
EXAM_Q = ('זו בחינה בקורס {name}. העתק אותה במלואה לטקסט עברי תקין (כיוון קריאה רגיל), כל נוסחה ב-LaTeX בין $...$. '
          'כל שאלה מתחילה בשורה "## שאלה N" וכל סעיף "(א)", "(ב)"… שמור על הנתונים והניקוד. בלי הערות משלך.')
SOL_Q = ('זה פתרון של בחינה בקורס {name}. העתק אותו במלואו לטקסט עברי תקין (כיוון קריאה רגיל), כל נוסחה ב-LaTeX '
         'בין $...$, כל שאלה מתחילה בשורה "## שאלה N". בלי הערות משלך.')
TAG_Q = """להלן בחינה בקורס {name} ורשימת נושאי הקורס. לכל שאלה (ולכל סעיף שעוסק בנושא אחר) קבע את הנושאים.
רשימת הנושאים (השתמש רק בהם, בדיוק בכתיב הזה):
{topics}

הבחינה:
{exam}

החזר JSON בלבד: [{{"q": מספר השאלה, "topics": ["נושא", ...], "gist": "משפט קצר: מה שואלים"}}]"""


def exam_files(idx: dict, sections: tuple) -> list[dict]:
    """index.json → [{'date', 'label', 'q': path, 's': [paths]}] — one per exam, a question file required.
    Titles: "01052026_MoedB" / "…_Sol", "מבחן מועד א 13072026" / "פתרון מועד א 13072026", "מבחן מועד ב" /
    "פתרון מועד ב שאלה 2" + "פתרון מועד ב שאלות 1, 3" (no date → the latest year's מועד ב)."""
    seen, by = set(), {}
    latest = '2026'
    for e in idx.values():
        m = re.search(r'(\d{2})(\d{2})(20\d{2})', e.get('title', ''))
        if m:
            latest = max(latest, m.group(3))
    for e in sorted(idx.values(), key=lambda e: e.get('title', '')):
        t, f = e.get('title', ''), e.get('file')
        if not f or e.get('section') not in sections or t in seen or not f.lower().endswith('.pdf'):
            continue
        seen.add(t)
        sol = bool(re.search(r'sol|פתרון', t, re.I))
        m = re.search(r'(\d{2})(\d{2})(20\d{2})', t)
        if m:
            date = f'{m.group(3)}-{m.group(2)}-{m.group(1)}'
            moed = 'ב' if re.search(r'moedb|מועד ב', t, re.I) else 'א'
        elif re.search(r'מועד ב', t):
            date, moed = f'{latest}-08-01', 'ב'
        elif re.search(r'מועד א', t):
            date, moed = f'{latest}-07-01', 'א'
        else:
            continue
        x = by.setdefault(date, {'date': date, 'label': f'מועד {moed} {date[:4]}', 'q': '', 's': []})
        if sol:
            x['s'].append(f)
        else:
            x['q'] = f
    return sorted((x for x in by.values() if x['q']), key=lambda x: x['date'])


def _tag(exam_md: str, cfg: dict) -> list[dict]:
    import google.genai as genai
    from google.genai import types as T
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    resp = client.models.generate_content(
        model='gemini-3.8-flash',
        contents=TAG_Q.format(name=cfg['name'], topics='\n'.join(f'- {t}' for t in cfg['topics']), exam=exam_md[:20000]),
        config=T.GenerateContentConfig(max_output_tokens=4000, temperature=0, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, 'gemini-3.8-flash', op='moodle_exams.tag')
    try:
        rows = json.loads(resp.text or '[]')
    except ValueError:
        return []
    ok = set(cfg['topics'])
    return [{'q': r.get('q'), 'topics': [t for t in r.get('topics') or [] if t in ok], 'gist': (r.get('gist') or '')[:200]}
            for r in rows if isinstance(r, dict)]


def vault_note(ck: str, cfg: dict, rows: list[dict], n_exams: int) -> str:
    import yaml
    head = '---\n' + yaml.safe_dump({'type': 'exam-weights', 'course_key': ck, 'updated': datetime.now().strftime('%Y-%m-%d'),
                                     'tags': ['מבחנים']}, allow_unicode=True, sort_keys=False) + '---\n\n'
    lines = [f"# 📊 {cfg['name']} — משקל הנושאים במבחנים ({n_exams} מבחנים)", '']
    for r in rows:
        lines.append(f"- **{r['topic']}**: {r['exams']}/{n_exams} מבחנים ({r['exam_pct']}%) · {r['question_pct']}% מהשאלות · "
                     f"{r['recent_5']}/5 אחרונים · לאחרונה {r['last']}")
    return head + '\n'.join(lines) + '\n'


def main(ck: str) -> int:
    from collectors.course_files import load_index
    from collectors.lecturer_site import transcribe
    from config.paths import VAULT_DIR
    from scripts.lecturer_exams import stats
    cfg = COURSES[ck]
    exams = exam_files(load_index(ck), cfg['sections'])
    print(f'{ck}: {len(exams)} exams')
    out = ROOT / 'data' / 'exam_topics' / f'{ck}.json'
    data = json.loads(out.read_text(encoding='utf-8')) if out.exists() else {'exams': []}
    have = {e['date'] for e in data['exams'] if e.get('questions')}
    vdir = VAULT_DIR / '🧾 מבחנים' / cfg['vault']
    vdir.mkdir(parents=True, exist_ok=True)
    for e in exams:
        if e['date'] in have:
            continue
        try:
            q_md = transcribe(ROOT / e['q'], EXAM_Q.format(name=cfg['name'])).read_text(encoding='utf-8')
            s_md = '\n\n'.join(transcribe(ROOT / s, SOL_Q.format(name=cfg['name'])).read_text(encoding='utf-8') for s in e['s'])
            (vdir / f"{e['date']} שאלות.md").write_text(q_md, encoding='utf-8')
            if s_md:
                (vdir / f"{e['date']} פתרון.md").write_text(s_md, encoding='utf-8')
            qs = _tag(q_md, cfg)
            data['exams'] = [x for x in data['exams'] if x['date'] != e['date']] + [
                {'date': e['date'], 'by': e['label'], 'q': e['q'], 's': ', '.join(e['s']), 'questions': qs}]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
            print(f"  ✅ {e['date']} ({e['label']}): {len(qs)} questions")
        except Exception as ex:
            print(f"  ⚠️ {e['date']}: {ex!r}"[:200])
    rows = stats(data)
    data.update(stats=rows, topics=cfg['topics'], vault_dir=cfg['vault'], by=cfg['by'], course_name=cfg['name'])
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    (vdir / '📊 משקל נושאים במבחנים.md').write_text(vault_note(ck, cfg, rows, len(data['exams'])), encoding='utf-8')
    print(f"stats: {len(rows)} topics over {len(data['exams'])} exams")
    for r in rows:
        print(f"  {r['exam_pct']:3d}%  {r['topic']}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'microeconomics_2'))
