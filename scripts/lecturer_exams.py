"""scripts/lecturer_exams.py — the probability lecturer's own past exams → vault + a topic-weight map.

The owner 18/10: "אם אני מתעכב על נושא קשה — שידע אם יופיע הרבה במבחנים". Naftali Zevuloni's site lists ~30 exams he wrote
(2006–2026), each with his solution. Once (and again only for new exams):
  1. download questions + solution PDFs → Gemini reads each once → clean Hebrew markdown (with LaTeX)
  2. the markdown → vault "🧾 מבחנים/הסתברות דו-חוגי/" (so RAG / NotebookLM / the tutor see it)
  3. one cheap call per exam tags every question by the course's own topic list (from his summaries table)
  4. data/exam_topics/probability.json + vault note "📊 הסתברות — משקל נושאים במבחנים.md" (agent/exam_weight.py reads it)
Cost: ~$1 the first time (60 PDFs read + 30 tagging calls), then only new exams.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

COURSE = 'probability'
EXAMS_URL = 'https://lecturer-site.example.com/courses8/course1115/pr_sta_exams/exams5.html'
SUMS_URL = 'https://lecturer-site.example.com/duhugi/sum/sum.html'
CACHE = ROOT / 'data' / 'exams' / COURSE
OUT = ROOT / 'data' / 'exam_topics' / f'{COURSE}.json'
EXAM_Q = ('זו בחינה בהסתברות וסטטיסטיקה. העתק אותה במלואה לטקסט עברי תקין (כיוון קריאה רגיל), כל נוסחה ב-LaTeX בין $...$. '
          'כל שאלה מתחילה בשורה "## שאלה N" וכל סעיף "(א)", "(ב)"… שמור על הנתונים והניקוד. בלי הערות משלך.')
SOL_Q = ('זה פתרון של בחינה בהסתברות וסטטיסטיקה. העתק אותו במלואו לטקסט עברי תקין (כיוון קריאה רגיל), כל נוסחה ב-LaTeX '
         'בין $...$, כל שאלה מתחילה בשורה "## שאלה N". בלי הערות משלך.')
# the course's units (his 42 summary rows grouped — "טבלת התפלגויות" / "שאלות חזרה" aren't topics)
TOPICS = ['קומבינטוריקה וספירה', 'מרחבי הסתברות סימטריים', 'עקרון ההכלה וההפרדה',
          'הסתברות מותנה, הסתברות שלמה ובייס', 'אי-תלות',
          'משתנים מקריים בדידים (בינומי, גאומטרי, פואסון, היפרגאומטרי)',
          'משתנים מקריים רציפים (אחיד, מעריכי, נורמלי)', 'התפלגות משותפת ומשתנים דו-ממדיים',
          'תוחלת (לינאריות, אינדיקטורים)', 'שונות, שונות משותפת ומתאם', "אי-שוויונות מרקוב, צ'בישב וצ'רנוף",
          'משפט הגבול המרכזי וקירוב נורמלי', 'חוקי המספרים הגדולים וסוגי התכנסות', 'הלמה של בורל-קנטלי',
          'פונקציות יוצרות מומנטים', 'הילוכים מקריים ובעיות מיוחדות']
TAG_Q = """להלן בחינה בהסתברות וסטטיסטיקה ורשימת נושאי הקורס. לכל שאלה (ולכל סעיף שעוסק בנושא אחר) קבע את הנושאים.
רשימת הנושאים (השתמש רק בהם, בדיוק בכתיב הזה):
{topics}

הבחינה:
{exam}

החזר JSON בלבד: [{{"q": מספר השאלה, "topics": ["נושא", ...], "gist": "משפט קצר: מה שואלים"}}]"""


def exam_list(get) -> list[dict]:
    from collectors.lecturer_site import _cells, _text
    out = []
    for r in re.findall(r'<tr[^>]*>(.*?)</tr>', get(EXAMS_URL), re.S | re.I):
        cells = [_text(c) for c in _cells(r)]
        pdfs = re.findall(r'href="([^"]+\.pdf)"', r, re.I)
        if cells and re.match(r'\d{2}/\d{2}/\d{2}', cells[0]) and pdfs:
            d, m, y = cells[0].split('/')
            base = EXAMS_URL.rsplit('/', 1)[0] + '/'
            out.append({'date': f'{2000 + int(y)}-{m}-{d}', 'by': cells[1],
                        'q': base + next((p for p in pdfs if p.endswith('_q.pdf')), pdfs[0]),
                        's': base + next((p for p in pdfs if p.endswith('_s.pdf')), pdfs[-1])})
    return out


def topic_list(get) -> list[str]:
    from collectors.lecturer_site import parse_summaries
    seen, out = set(), []
    for r in parse_summaries(get(SUMS_URL), SUMS_URL):
        t = re.sub(r'\s*\(.*?\)\s*', ' ', r['topic']).strip()
        if t and t not in seen and not t.startswith(('סוגיות נוספות', 'סוגית הקדמה')):
            seen.add(t)
            out.append(t)
    return out


def _download(url: str, dest: Path) -> Path | None:
    import requests
    from collectors.lecturer_site import HEADERS
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    r = requests.get(url, headers=HEADERS, timeout=60)
    if r.status_code != 200 or not r.content.startswith(b'%PDF'):
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return dest


def _tag(exam_md: str, topics: list[str]) -> list[dict]:
    import google.genai as genai
    from google.genai import types as T
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    resp = client.models.generate_content(
        model='gemini-3.8-flash', contents=TAG_Q.format(topics='\n'.join(f'- {t}' for t in topics), exam=exam_md[:20000]),
        config=T.GenerateContentConfig(max_output_tokens=4000, temperature=0, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, 'gemini-3.8-flash', op='lecturer_exams.tag')
    try:
        rows = json.loads(resp.text or '[]')
    except ValueError:
        return []
    ok = set(topics)
    return [{'q': r.get('q'), 'topics': [t for t in r.get('topics') or [] if t in ok], 'gist': (r.get('gist') or '')[:200]}
            for r in rows if isinstance(r, dict)]


def stats(data: dict) -> list[dict]:
    """Per topic: in how many exams, share of all questions, the last time, example questions — most frequent first."""
    exams = data['exams']
    n_exams = len(exams) or 1
    n_q = sum(len(e['questions']) for e in exams) or 1
    by = defaultdict(lambda: {'exams': set(), 'questions': 0, 'last': '', 'examples': []})
    for e in exams:
        for q in e['questions']:
            for t in set(q['topics']):
                b = by[t]
                b['exams'].add(e['date'])
                b['questions'] += 1
                b['last'] = max(b['last'], e['date'])
                if len(b['examples']) < 4:
                    b['examples'].append(f"{e['date']} ש' {q['q']}: {q['gist']}")
    rows = [{'topic': t, 'exams': len(b['exams']), 'exam_pct': round(100 * len(b['exams']) / n_exams),
             'question_pct': round(100 * b['questions'] / n_q), 'last': b['last'],
             'recent_5': sum(1 for e in sorted(exams, key=lambda e: e['date'])[-5:] if e['date'] in b['exams']),
             'examples': sorted(b['examples'], reverse=True)} for t, b in by.items()]
    return sorted(rows, key=lambda r: (-r['exam_pct'], -r['question_pct']))


def vault_note(rows: list[dict], n_exams: int) -> str:
    import yaml
    head = '---\n' + yaml.safe_dump({'type': 'exam-weights', 'course_key': COURSE, 'updated': datetime.now().strftime('%Y-%m-%d'),
                                     'tags': ['מבחנים', 'הסתברות']}, allow_unicode=True, sort_keys=False) + '---\n\n'
    lines = [f'# 📊 הסתברות — משקל הנושאים במבחנים של נפתלי ({n_exams} מבחנים)', '',
             'כמה מבחנים הנושא הופיע בהם · כמה מכל השאלות · ב-5 המבחנים האחרונים · מתי לאחרונה.', '']
    for r in rows:
        lines.append(f"- **{r['topic']}**: {r['exams']}/{n_exams} מבחנים ({r['exam_pct']}%) · {r['question_pct']}% מהשאלות · "
                     f"{r['recent_5']}/5 אחרונים · לאחרונה {r['last']}")
    return head + '\n'.join(lines) + '\n'


def main() -> int:
    from collectors.lecturer_site import _get, transcribe
    from config.paths import VAULT_DIR
    exams, topics = exam_list(_get), TOPICS
    print(f'{len(exams)} exams, {len(topics)} topics')
    data = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {'exams': []}
    have = {e['date'] for e in data['exams'] if e.get('questions')}
    vdir = VAULT_DIR / '🧾 מבחנים' / 'הסתברות דו-חוגי'
    vdir.mkdir(parents=True, exist_ok=True)
    for e in exams:
        if e['date'] in have:
            continue
        try:
            q = _download(e['q'], CACHE / f"{e['date']}_q.pdf")
            s = _download(e['s'], CACHE / f"{e['date']}_s.pdf")
            if not q:
                continue
            q_md = transcribe(q, EXAM_Q).read_text(encoding='utf-8')
            s_md = transcribe(s, SOL_Q).read_text(encoding='utf-8') if s else ''
            (vdir / f"{e['date']} שאלות.md").write_text(q_md, encoding='utf-8')
            if s_md:
                (vdir / f"{e['date']} פתרון.md").write_text(s_md, encoding='utf-8')
            qs = _tag(q_md, topics)
            data['exams'] = [x for x in data['exams'] if x['date'] != e['date']] + [{**e, 'questions': qs}]
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
            print(f"  ✅ {e['date']}: {len(qs)} questions")
        except Exception as ex:
            print(f"  ⚠️ {e['date']}: {ex!r}"[:200])
    rows = stats(data)
    data['stats'] = rows
    data['topics'] = topics
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    (VAULT_DIR / '🧾 מבחנים' / 'הסתברות דו-חוגי' / '📊 משקל נושאים במבחנים.md').write_text(
        vault_note(rows, len(data['exams'])), encoding='utf-8')
    print(f"stats: {len(rows)} topics over {len(data['exams'])} exams")
    return 0


if __name__ == '__main__':
    sys.exit(main())
