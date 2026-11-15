"""
agent/tutor.py — an interactive review session on WhatsApp, built on the course site Claude made for
The owner (data/course_site/<course>/: lessons.json, data.json questions with official solutions, summaries/).

Per topic: a short WhatsApp lesson (generated from the site's lesson + lecture/recitation summaries,
cached) → 2 multiple-choice questions from the lesson quiz (checked in code) → 1 real exam question
(checked by Gemini against the official solution) → next topic. Wrong answers go to the mistake notebook,
every answer to the mastery map.

Commands during a session: "הבא" (next topic) · "עוד שאלה" · "תסביר שוב" (simpler) · "רמז" · "די" (end).
A free question ("למה 0.1 לא מדויק?") is answered from the material, then the current question repeats.

Scheduled sessions (data/tutor_schedule.json) are started by the WhatsApp worker at their time.
"""
from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / 'data' / 'course_site'
CACHE = ROOT / 'data' / 'tutor_cache'
SCHEDULE = ROOT / 'data' / 'tutor_schedule.json'
MODEL = 'gemini-3.8-flash'
LETTERS = 'אבגד'

PLANS = {
    # The owner 04/10/2026: moed B in Intro CS on Thursday 08/10 — review on Monday 05/10 08:00
    'intro_moed_b': {
        'course': 'intro_to_cs',
        'title': 'חזרה למועד ב׳ — מבוא מורחב',
        'topics': [
            {'title': 'נקודה צפה ו-IEEE 754', 'lessons': ['repr'], 'summaries': ['ייצוג נקודה צפה'],
             'q_topics': ['ייצוג מידע'], 'quiz_filter': r'צפה|float|IEEE|0\.1|exponent|mantissa|fraction'},
            {'title': 'ייצוג שלמים, בסיסים, ASCII ויוניקוד', 'lessons': ['repr'],
             'summaries': ['ייצוג שלמים ובסיסים', 'ייצוג טקסט', 'המרת בסיסים'], 'q_topics': ['ייצוג מידע'],
             'quiz_filter': r'בסיס|ASCII|ord|chr|יוניקוד|unicode|bin|convert|ביט'},
            {'title': 'סיבוכיות', 'lessons': ['cx'], 'summaries': ['מיון וסיבוכיות', 'מבוא לסיבוכיות', 'סיבוכיות של לולאות'],
             'q_topics': ['סיבוכיות']},
            {'title': 'רקורסיה', 'lessons': ['rec'], 'summaries': ['רקורסיה 1', 'רקורסיה 2'], 'q_topics': ['רקורסיה']},
            {'title': 'ממואיזציה', 'lessons': ['memo'], 'summaries': ['ממואיזציה'],
             'q_topics': ['ממואיזציה ותכנון דינמי']},
        ],
    },
}


# ── automatic plans from past exams (13/10: Calculus 2B on 23/10 — the hand-written plan only covered Intro) ──

EXAM_FOLDERS = {'intro_to_cs': 'מבוא מורחב למדמ"ח', 'calculus_2': 'חדו"א 2ב', 'discrete_math_2': 'בדידה 2',
                'linear_algebra_2': 'לינארית 2ב', 'microeconomics': 'מיקרו א', 'probability': 'הסתברות',
                'software_1': 'תוכנה 1', 'microeconomics_2': 'מיקרו ב', 'macroeconomics': 'מאקרו'}

AUTO_PROMPT = """אתה מורה פרטי שמכין את המשתמש למבחן ב{course}. למטה מבחנים קודמים בקורס (ובסוף פתרונות של האחרונים).
בנה תוכנית חזרה של 5 הנושאים שחוזרים הכי הרבה במבחנים — מהשכיח לפחות שכיח. לכל נושא:
- "title": שם קצר · "why": כמה פעמים הופיע (למשל "ב-7 מתוך 9 מבחנים")
- "query": 3–6 מילות חיפוש לחומר הקורס על הנושא
- "mcq": 2 שאלות אמריקאיות קצרות ברמת מבחן: {{"q", "opts": [4 תשובות], "a": אינדקס 0–3, "why": הסבר קצר}}
- "exam": שאלה אמיתית מאחד המבחנים: {{"stem": נוסח השאלה, "sol": [שלבי פתרון קצרים], "cite": "מבחן <שנה> מועד <א/ב/ג> שאלה <n>"}}
  — אם יש פתרון בקבצי הפתרונות, השתמש בו; אחרת פתור בעצמך בזהירות.
כתיב מתמטי פשוט (x², ∫, ≤, √), בלי LaTeX ובלי $. JSON בלבד: {{"topics": [...]}}

## מבחנים קודמים
{exams}

## פתרונות (האחרונים)
{sols}"""


def _exam_texts(course: str, max_exams: int = 60000, max_sols: int = 25000) -> tuple[str, str]:
    from config.paths import VAULT_DIR
    d = VAULT_DIR / '🧾 מבחנים' / EXAM_FOLDERS.get(course, '')
    if not d.is_dir():
        return '', ''
    files = sorted(d.glob('*.md'), key=lambda p: p.stem, reverse=True)           # newest first
    exams = ''.join(f'\n\n### {p.stem}\n' + p.read_text(encoding='utf-8', errors='ignore')
                    for p in files if 'SOL' not in p.stem)[:max_exams]
    sols = ''.join(f'\n\n### {p.stem}\n' + p.read_text(encoding='utf-8', errors='ignore')
                   for p in files if 'SOL' in p.stem)[:max_sols]
    return exams, sols


def build_auto_plan(course: str) -> dict:
    """One Gemini call over the past exams (~a few cents), cached per course in data/tutor_cache/auto:<course>/."""
    from config.paths import COURSE_NAMES
    key = f'auto:{course}'
    path = CACHE / key.replace(':', '_') / 'plan.json'          # no ':' in folder names (Windows)
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    exams, sols = _exam_texts(course)
    if not exams:
        raise ValueError(f'אין לי מבחנים קודמים ב{COURSE_NAMES.get(course, course)}')
    name = COURSE_NAMES.get(course, course)
    d = json.loads(_gemini(AUTO_PROMPT.format(course=name, exams=exams, sols=sols), json_mode=True, max_tokens=12000))
    topics = [t for t in (d.get('topics') or []) if t.get('title')][:5]
    if not topics:
        raise ValueError('לא הצלחתי לבנות תוכנית מהמבחנים')
    plan = {'course': course, 'title': f'חזרה למבחן ב{name}', 'auto': True, 'topics': topics}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding='utf-8')
    return plan


def build_topic_plan(course: str, unit: str) -> dict:
    """26/10: "בוא נעשה חזרה קצרה על בייס" opened a 5-topic review for the next exam (discrete 2). One unit → a one-topic
    session: the lesson from this semester's summaries (RAG on the unit), then real past-exam questions on it with their
    solutions (agent/exam_weight's map + vault notes). No extra model call to build it."""
    from agent import exam_weight as ew
    from config.paths import VAULT_DIR
    data = ew.load(course)
    vdir = VAULT_DIR / '🧾 מבחנים' / ew._vault(data)
    exams = []
    for e in sorted(data.get('exams') or [], key=lambda e: e.get('date', ''), reverse=True):
        for q in e.get('questions') or []:
            if unit not in (q.get('topics') or []):
                continue
            stem = ew._section(vdir / f"{e['date']} שאלות.md", q['q'])
            if not stem:
                continue
            sol = ew._section(vdir / f"{e['date']} פתרון.md", q['q'])
            from agent.nlm_ask import whatsapp_text
            exams.append({'stem': whatsapp_text(stem)[:2500], 'sol': [whatsapp_text(sol)[:2500]] if sol else [],
                          'cite': f"מבחן {e['date'][8:10]}/{e['date'][5:7]}/{e['date'][:4]} שאלה {q['q']}"})
            break
        if len(exams) >= 2:
            break
    short = unit.split(' (')[0]
    topic = {'title': short, 'why': '', 'query': short, 'mcq': [], 'exam': exams[0] if exams else {}}
    from config.paths import COURSE_NAMES
    return {'course': course, 'title': f'חזרה על {short} ({COURSE_NAMES.get(course, course)})', 'auto': True,
            'topics': [topic]}


def plan_for(key: str) -> dict:
    if key in PLANS:
        return PLANS[key]
    if key.startswith('auto:'):
        return build_auto_plan(key[5:])
    if key.startswith('topic:'):
        _, course, unit = key.split(':', 2)
        return build_topic_plan(course, unit)
    raise KeyError(key)


# ── material ────────────────────────────────────────────────────────────────

def _site(course: str) -> dict:
    d = SITE / course
    lessons = {l['id']: l for l in json.loads((d / 'lessons.json').read_text(encoding='utf-8'))['lessons']}
    data = json.loads((d / 'data.json').read_text(encoding='utf-8'))
    sums = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in (d / 'summaries').glob('*.json')}
    return {'lessons': lessons, 'questions': data.get('questions') or [], 'summaries': sums}


def _clean(x: str) -> str:
    return re.sub(r'⟪|⟫', '', str(x or ''))


def material(course: str, topic: dict, limit: int = 14000) -> str:
    if topic.get('query'):                                   # automatic plan → the course's summaries (RAG)
        from rag.vault_indexer import search
        hits = search(topic['query'], k=8, course_filter=course) or []
        return '\n\n'.join(f"## {h.get('stem', '')}\n{h.get('text', '')}" for h in hits)[:limit]
    s = _site(course)
    parts = []
    for lid in topic['lessons']:
        l = s['lessons'].get(lid)
        if l:
            parts.append(f"## שיעור: {l['title']}\n" + '\n'.join(_clean(b.get('x', '')) for b in l.get('blocks') or []))
            if l.get('patterns'):
                parts.append('דפוסי מבחן: ' + _clean(json.dumps(l['patterns'], ensure_ascii=False)))
    for key in topic['summaries']:
        for name, sm in s['summaries'].items():
            if key in name:
                parts.append(f"## סיכום: {sm.get('title', name)}\n" + _clean(json.dumps(
                    {k: sm.get(k) for k in ('overview', 'sections', 'definitions', 'exam_tips')}, ensure_ascii=False)))
    return '\n\n'.join(parts)[:limit]


def questions(course: str, topic: dict, seed: int = 0) -> list[dict]:
    """2 MCQs from the lesson quiz (preferring ones that match the topic) + 1 exam question with solution."""
    if 'mcq' in topic or 'exam' in topic:                   # automatic plan: built with the plan
        out = [{'kind': 'mcq', 'q': q.get('q', ''), 'code': '', 'opts': list(q.get('opts') or [])[:4],
                'a': int(q.get('a') or 0), 'why': q.get('why', '')} for q in topic.get('mcq') or [] if q.get('opts')]
        ex = topic.get('exam') or {}
        if ex.get('stem'):
            out.append({'kind': 'exam', 'q': ex['stem'], 'cite': ex.get('cite', ''), 'sol': list(ex.get('sol') or []),
                        'id': f"auto-{abs(hash(ex['stem'])) % 10**8}"})
        return out
    s = _site(course)
    rnd = random.Random(seed)
    mcq = []
    for lid in topic['lessons']:
        for q in (s['lessons'].get(lid) or {}).get('quiz') or []:
            mcq.append({'kind': 'mcq', 'q': _clean(q.get('q', '')), 'code': q.get('code', ''),
                        'opts': [_clean(o) for o in q.get('opts') or []],
                        'a': int(q.get('a', 0)), 'why': _clean(q.get('why', ''))})
    rx = topic.get('quiz_filter')
    if rx:
        hit = [q for q in mcq if re.search(rx, q['q'] + q['code'] + ' '.join(q['opts']) + q['why'], re.I)]
        mcq = hit + [q for q in mcq if q not in hit]
    else:
        rnd.shuffle(mcq)
    exam = [q for q in s['questions'] if any(t in q.get('topic', '') for t in topic['q_topics'])]
    exam.sort(key=lambda q: -int(q.get('stars') or 0))
    out = mcq[:2]
    if exam:
        q = exam[seed % len(exam)] if seed else exam[0]
        out.append({'kind': 'exam', 'q': _clean(q['stem']), 'cite': q.get('cite', ''),
                    'sol': [_clean(x) for x in q.get('sol') or []], 'id': q['id']})
    return out


# ── Gemini helpers ──────────────────────────────────────────────────────────

def _gemini(prompt: str, json_mode: bool = False, max_tokens: int = 3000) -> str:
    import google.genai as genai
    from google.genai import types as T
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    cfg = dict(max_output_tokens=max_tokens, temperature=0.3, thinking_config=T.ThinkingConfig(thinking_level='low'))
    if json_mode:
        cfg['response_mime_type'] = 'application/json'
    resp = client.models.generate_content(model=MODEL, contents=prompt, config=T.GenerateContentConfig(**cfg))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op='agent.tutor')
    return (resp.text or '').strip()


STYLE = ('כתוב בעברית, לוואטסאפ: שורות קצרות, *מודגש* לכותרות, בלי LaTeX ובלי $ — כתיב מתמטי פשוט (2^-1, x², ≤). '
         'קוד בתוך ```.')


def lesson(plan_key: str, i: int, simpler: bool = False) -> str:
    plan = plan_for(plan_key)
    topic = plan['topics'][i]
    cache = CACHE / plan_key.replace(':', '_') / f"{i}{'_simple' if simpler else ''}.md"
    if cache.exists():
        return cache.read_text(encoding='utf-8')
    mat = material(plan['course'], topic)
    prompt = (f'אתה מורה פרטי מצוין שמכין את המשתמש ל{plan["title"].replace("חזרה ", "")}. נושא: {topic["title"]}.\n'
              f'{"הסבר שוב, פשוט יותר, עם אנלוגיה ודוגמה קטנה מאוד." if simpler else ""}\n'
              f'כתוב שיעור חזרה קצר (עד 1,400 תווים) מתוך החומר למטה בלבד: הרעיון המרכזי, הכלל/הנוסחה, דוגמה פתורה '
              f'קטנה אחת, והטעות הנפוצה במבחנים. {STYLE}\n\n## חומר הקורס\n{mat}')
    text = _gemini(prompt)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding='utf-8')
    return text


def check_exam_answer(q: dict, answer: str) -> dict:
    # 05/10: The owner answered exactly the part the question was cited for ("2^(e-4)", the spacing) and got
    # 33/100 ❌ for not answering the other parts. Grade only what was attempted.
    sol = '\n'.join(f'- {x}' for x in q.get('sol') or [])
    raw = _gemini(f'בדוק תשובה של סטודנט לשאלת מבחן רב-סעיפית. הסטודנט רשאי לענות רק על חלק מהסעיפים.\n'
                  f'1. זהה על אילו סעיפים הוא ענה. 2. תן ציון רק על מה שענה (סעיף שלא נוסה אינו טעות).\n'
                  f'JSON בלבד: {{"attempted": "<הסעיפים שענה, למשל: המרווח>", "score": 0-100, '
                  f'"verdict": "correct|partial|wrong", "feedback": "<עד 450 תווים: מה נכון ומה לתקן בחלק שענה. '
                  f'בסוף, בשורה אחת, התשובות של הסעיפים שלא ענה. {STYLE}>"}}\n\n'
                  f'## השאלה ({q.get("cite", "")})\n{q["q"]}\n\n## הפתרון הרשמי\n{sol}\n\n## תשובת הסטודנט\n{answer}',
                  json_mode=True, max_tokens=1500)
    try:
        r = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', raw))
        return r[0] if isinstance(r, list) else r
    except ValueError:
        return {'score': 0, 'verdict': 'partial', 'feedback': raw[:500]}


def answer_free_question(plan_key: str, i: int, question: str) -> str:
    plan = plan_for(plan_key)
    return _gemini(f'המשתמש שואל באמצע חזרה על "{plan["topics"][i]["title"]}": {question}\nענה בקצרה (עד 700 תווים) '
                   f'מתוך החומר. {STYLE}\n\n## חומר\n{material(plan["course"], plan["topics"][i], 10000)}', max_tokens=1500)


# ── the session state machine (state lives in agent state: u['tutor']) ─────

def _ask(q: dict) -> str:
    if q['kind'] == 'mcq':
        code = f"\n```\n{q['code']}\n```" if q.get('code') else ''
        opts = '\n'.join(f'{LETTERS[k]}) {o}' for k, o in enumerate(q['opts']))
        return f"❓ *{q['q']}*{code}\n{opts}\n\nענה באות."
    return (f"📝 *שאלת מבחן* ({q.get('cite', '')}):\n{q['q']}\n\n"
            f"ענה על סעיף אחד או יותר — אבדוק רק את מה שענית. \"רמז\" אם צריך, \"הבא\" לדלג.")


def start(u: dict, plan_key: str) -> list[str]:
    plan = plan_for(plan_key)
    u['tutor'] = {'plan': plan_key, 'i': 0, 'qi': 0, 'qs': questions(plan['course'], plan['topics'][0]),
                  'right': 0, 'total': 0, 'active': True, 'started': datetime.now().isoformat(timespec='minutes')}
    qs = u['tutor']['qs']
    hour = datetime.now().hour
    hello = '☀️ בוקר טוב!' if 5 <= hour < 12 else '🌙 ערב טוב!' if hour >= 18 or hour < 5 else '📚'   # 26/10: "בוקר טוב" at night
    if len(plan['topics']) == 1:                       # one unit (topic:) — no agenda, say what's coming
        head = (f"{hello} *{plan['title']}*\n\nשיעור קצר" + (' → שאלה אמיתית ממבחן, עם הפתרון' if qs else '') +
                ".\nפקודות: \"תסביר שוב\" · \"רמז\" · \"די\" — ואפשר לשאול כל שאלה באמצע 🙂")
    else:
        agenda = '\n'.join(f'{k + 1}. {t["title"]}' for k, t in enumerate(plan['topics']))
        head = (f"{hello} *{plan['title']}*\n\nהתוכנית:\n{agenda}\n\n"
                f"בכל נושא: שיעור קצר → 2 שאלות אמריקאיות → שאלת מבחן אמיתית.\n"
                f"פקודות: \"הבא\" · \"עוד שאלה\" · \"תסביר שוב\" · \"רמז\" · \"די\".\n"
                f"ואפשר לשאול כל שאלה באמצע 🙂")
    out = [head, f"*1/{len(plan['topics'])} · {plan['topics'][0]['title']}*\n\n" + lesson(plan_key, 0)]
    if qs:
        out.append(_ask(qs[0]))
    return out


def _next_topic(u: dict) -> list[str]:
    t = u['tutor']
    plan = plan_for(t['plan'])
    t['i'] += 1
    if t['i'] >= len(plan['topics']):
        return finish(u)
    t['qi'] = 0
    t['qs'] = questions(plan['course'], plan['topics'][t['i']])
    return [f"*{t['i'] + 1}/{len(plan['topics'])} · {plan['topics'][t['i']]['title']}*\n\n" + lesson(t['plan'], t['i']),
            _ask(t['qs'][0]) if t['qs'] else 'אין לי שאלות לנושא הזה — כתוב "הבא".']


def finish(u: dict) -> list[str]:
    t = u.pop('tutor', None) or {}
    r, n = t.get('right', 0), t.get('total', 0)
    from config.paths import COURSE_NAMES
    try:
        course = COURSE_NAMES.get(plan_for(t['plan'])['course'], '') if t.get('plan') else ''
    except Exception:
        course = ''
    return [f"🏁 סיימנו! ענית נכון על {r} מתוך {n}." +
            (f'\nהטעויות נכנסו למחברת הטעויות — "מחברת טעויות ב{course}" כדי לראות.' if n > r and course else '') +
            '\nבהצלחה במבחן! 💪']


def _advance_q(u: dict) -> list[str]:
    t = u['tutor']
    t['qi'] += 1
    if t['qi'] < len(t['qs']):
        return [_ask(t['qs'][t['qi']])]
    plan = plan_for(t['plan'])
    if t['i'] + 1 >= len(plan['topics']):
        return finish(u)
    t['await_continue'] = True          # a breath between topics instead of a wall of messages
    return [f"✅ סיימנו את \"{plan['topics'][t['i']]['title']}\" ({t['right']}/{t['total']} נכון עד עכשיו).\n"
            f"נמשיך ל\"{plan['topics'][t['i'] + 1]['title']}\"? (כן / די לעכשיו)"]


TUTOR_WORDS = {'די', 'מספיק', 'סיים', 'סיום', 'עצור חזרה', 'די לחזרה', 'די לעכשיו', 'הבא', 'נושא הבא', 'דלג',
               'תדלג', 'תסביר שוב', 'לא הבנתי', 'עוד הסבר', 'תסביר יותר פשוט', 'עוד שאלה', 'עוד', 'שאלה נוספת',
               'רמז', 'נמשיך', 'המשך', 'איפה עצרנו', 'כן', 'לא', 'יאללה', 'קדימה'}
OTHER_REQUEST = re.compile(r'יומן|תזכיר|תזכורת|רשימת|תוסיף|תוסיפי|תמחק|תנסח|מייל|מה יש לי|מתי|תקבע|'
                           r'פגישה|טיסה|קניות|בת הזוג|תשלח|קלוד|גישה|מטלה|להגיש|היום|מחר|השבוע|'
                           # 26/10: "שלח לי את הסיכום של ההרצאה האחרונה" was graded 0/100 as the exam answer
                           r'(?:^|\s)(?:שלח|תביא|תן לי|הבא לי|תראה לי)\b|סיכום|ההרצאה האחרונה|ציון|איפה|באיזה חדר|'
                           r'התאמנתי|יצאתי עם|הגשתי|תעביר|מטרות|משימות|עובד ב')


def is_tutor_reply(t: dict, text: str) -> bool:
    """Does this message belong to the review (an answer / a review command), or is it something else?"""
    s = (text or '').strip()
    low = s.strip(' .!?').lower()
    if low in TUTOR_WORDS or re.fullmatch(r'[אבגדabcd1-4]\s*[).]?', low):
        return True
    if t.get('await_continue'):
        return False
    q = (t.get('qs') or [None])[t.get('qi', 0)] if t.get('qs') else None
    if not q or OTHER_REQUEST.search(s) or '\n' in s.strip() and re.search(r'\d{1,2}[./]\d{1,2}', s):
        return False
    if q['kind'] == 'exam':
        # a short question with no math in it is the owner asking something else, not his answer
        if s.endswith('?') and len(s) < 120 and not re.search(r'[\d=+\-*/^√∑∫≤≥()]|P\(|E\[', s):
            return False
        return True                                   # free text while an exam question is open = an answer
    return bool(re.search(r'למה|איך|מה זה|מה ההבדל|תסביר', s)) and len(s) < 200   # a question about the material


def handle(u: dict, text: str) -> list[str]:
    """One message from the owner during an active session → replies."""
    t = u['tutor']
    plan = plan_for(t['plan'])
    topic = plan['topics'][t['i']]
    s = (text or '').strip()
    low = s.strip(' .!?').lower()
    t['last_ts'] = datetime.now().isoformat(timespec='minutes')
    t.pop('nudged', None)
    if low in ('די', 'מספיק', 'סיים', 'סיום', 'עצור חזרה', 'די לחזרה', 'די לעכשיו', 'לא'):
        return finish(u)
    if t.pop('await_continue', False):
        if low in ('כן', 'נמשיך', 'המשך', 'יאללה', 'קדימה', 'כן בבקשה', 'בטח', 'סבבה'):
            return _next_topic(u)
        t['await_continue'] = True
        return [f"\"כן\" כדי להמשיך, \"די\" כדי לעצור."]
    if low in ('נמשיך', 'המשך', 'איפה עצרנו'):
        return [_ask(t['qs'][t['qi']])] if t['qs'] else _next_topic(u)
    if low in ('הבא', 'נושא הבא', 'דלג', 'תדלג'):
        return _next_topic(u)
    if low in ('תסביר שוב', 'לא הבנתי', 'עוד הסבר', 'תסביר יותר פשוט'):
        return [lesson(t['plan'], t['i'], simpler=True), _ask(t['qs'][t['qi']]) if t['qs'] else '']
    if low in ('עוד שאלה', 'עוד', 'שאלה נוספת'):
        extra = questions(plan['course'], topic, seed=t['total'] + 7)
        if extra:
            t['qs'].append(extra[-1])
        return _advance_q(u)
    if not t['qs']:
        return _next_topic(u)
    q = t['qs'][t['qi']]
    if low == 'רמז':
        if q['kind'] == 'mcq':
            return [f"💡 רמז: {q['why'][:len(q['why']) // 2]}…"]
        return [f"💡 רמז: {str((q.get('sol') or [''])[0])[:160]}…"]
    from utils.mastery import record as mastery
    from utils.mistakes import record as mistake
    if q['kind'] == 'mcq':
        pick = None
        m = re.fullmatch(r'\s*([אבגדabcd1-4])\s*[).]?\s*', s)
        if m:
            c = m.group(1)
            pick = LETTERS.index(c) if c in LETTERS else ('abcd'.index(c) if c in 'abcd' else int(c) - 1)
        elif s in q['opts']:
            pick = q['opts'].index(s)
        if pick is None:                                     # a free question in the middle
            return [answer_free_question(t['plan'], t['i'], s), '↩️ ובחזרה לשאלה:\n' + _ask(q)]
        ok = pick == q['a']
        t['total'] += 1
        t['right'] += ok
        mastery(plan['course'], topic['title'], 1.0 if ok else 0.0, 'tutor')
        if not ok:
            mistake(plan['course'], 'tutor', topic=topic['title'], question=q['q'] + (f"\n{q['code']}" if q.get('code') else ''),
                    wrong=q['opts'][pick] if 0 <= pick < len(q['opts']) else s,
                    correct=f"{q['opts'][q['a']]} — {q['why']}")
        head = '✅ נכון!' if ok else f"❌ התשובה: {LETTERS[q['a']]}) {q['opts'][q['a']]}"
        return [f"{head}\n{q['why']}"] + _advance_q(u)
    if s.endswith('?') and len(s) < 160:                     # a question, not an answer
        return [answer_free_question(t['plan'], t['i'], s), '↩️ ובחזרה לשאלת המבחן — כשמוכן, כתוב תשובה.']
    r = check_exam_answer(q, s)
    score = int(r.get('score') or 0)
    t['total'] += 1
    t['right'] += score >= 70
    mastery(plan['course'], topic['title'], score / 100, 'tutor')
    if score < 70:
        mistake(plan['course'], 'tutor', topic=topic['title'], question=q['q'][:400], wrong=s[:300],
                correct=str(r.get('feedback', ''))[:500])
    icon = '✅' if score >= 85 else ('🟡' if score >= 50 else '❌')
    part = f" (על: {r['attempted']})" if r.get('attempted') else ''
    return [f"{icon} {score}/100{part}\n{r.get('feedback', '')}"] + _advance_q(u)


def idle_hours(t: dict, now: datetime | None = None) -> float:
    try:
        last = datetime.fromisoformat(t.get('last_ts') or t.get('started'))
    except (TypeError, ValueError):
        return 0.0
    return ((now or datetime.now()) - last).total_seconds() / 3600


def needs_nudge(t: dict, now: datetime | None = None, idle_hours: float = 2.0) -> bool:
    """An active session left idle for a while gets ONE "shall we continue?" (the worker sends it)."""
    if not t or not t.get('active') or t.get('nudged'):
        return False
    last = datetime.fromisoformat(t.get('last_ts') or t.get('started'))
    return ((now or datetime.now()) - last).total_seconds() > idle_hours * 3600


def nudge_text(t: dict) -> str:
    plan = plan_for(t['plan'])
    title = plan['topics'][min(t['i'], len(plan['topics']) - 1)]['title']
    return (f"📚 נמשיך את החזרה מאיפה שעצרנו? אנחנו ב\"{title}\" "
            f"({t.get('right', 0)}/{t.get('total', 0)} נכון עד עכשיו).\nכתוב \"נמשיך\" או \"די\".")


# ── scheduling ──────────────────────────────────────────────────────────────

def schedule(chat: str, plan_key: str, at: str, path: Path | None = None) -> None:
    p = path or SCHEDULE
    try:
        items = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        items = []
    items.append({'chat': chat, 'plan': plan_key, 'at': at, 'started': False})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding='utf-8')


def due_sessions(now: datetime | None = None, path: Path | None = None) -> list[dict]:
    p = path or SCHEDULE
    try:
        items = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    now = now or datetime.now()
    due = [s for s in items if not s['started'] and datetime.fromisoformat(s['at']) <= now]
    for s in due:
        s['started'] = True
    if due:
        p.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding='utf-8')
    return due
