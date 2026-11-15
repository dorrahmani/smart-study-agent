"""Comprehensive bot tests — intent classification + resolve_standalone + file search."""
import sys, os, time, json, re
sys.path.insert(0, '/home/ubuntu/smart_study_agent')
os.chdir('/home/ubuntu/smart_study_agent')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/smart_study_agent/.env', override=True)
if os.environ.get('GEMINI_API_KEY'):
    os.environ.pop('GOOGLE_API_KEY', None)

from bot import brain
from pathlib import Path

passed = 0
failed = 0
fail_details = []

def check(name, ok, info=''):
    global passed, failed
    if ok:
        passed += 1
        print(f'PASS  {name}  {info}')
    else:
        failed += 1
        fail_details.append(f'{name}  {info}')
        print(f'FAIL  {name}  {info}')


print('\n=== TEST 1: Intent classification ===')
intent_cases = [
    ('basic Q&A',          'מה זה משפט קיילי-המילטון?',           'ask',           None),
    ('summarize lecture',  'תסכם את ההרצאה האחרונה בבדידה',       'summarize',     'discrete_math_2'),
    ('topics overview',    'תסכם לי את כל הנושאים שלמדנו במבוא',  'ask',           None),
    ('file send',          'תשלח לי את ההרצאה האחרונה בחשבון',     'file_send',     None),
    ('quiz request',       'בחן אותי על לינארית',                  'quiz',          'linear_algebra_2'),
    ('status',             'מה הסטטוס?',                            'status',        None),
    ('greeting',           'היי',                                   'greeting',      None),
    ('reminder',           'תזכיר לי מחר 14:00 לקרוא חדוא',          'reminder',      None),
    ('list summaries',     'תראה לי את הסיכומים של בדידה',           'list_summaries', None),
    ('today',              'מה חדש?',                               'today',         None),
]
for name, q, exp_act, exp_param_contains in intent_cases:
    t0 = time.time()
    r = brain.classify(q)
    dt = time.time() - t0
    actual_act = r.get('action')
    ok = (actual_act == exp_act)
    info = f'({dt:.1f}s) got action={actual_act}'
    if exp_param_contains:
        params_str = json.dumps(r.get('params', {}), ensure_ascii=False)
        if exp_param_contains not in params_str:
            ok = False
            info += f' params={params_str} (expected to contain "{exp_param_contains}")'
    check(name, ok, info)


print('\n=== TEST 2: resolve_standalone with conversation history ===')
hist_calc = "user: מה זה משפט קיילי-המילטון?\nbot: משפט קיילי-המילטון אומר שכל מטריצה מקיימת את הפולינום האופייני של עצמה"
followups = [
    ('פרט יותר',     hist_calc, 'קיילי'),
    ('תן דוגמה',     hist_calc, 'קיילי'),
    ('ועוד?',        hist_calc, 'קיילי'),
    ('למה זה נכון?', hist_calc, 'קיילי'),
]
for q, h, kw in followups:
    t0 = time.time()
    resolved = brain.resolve_standalone(q, history=h)
    dt = time.time() - t0
    ok = (kw in resolved)
    print(f'  "{q}" → "{resolved}"')
    check(f'resolve "{q}"', ok, f'({dt:.1f}s) keyword "{kw}" {"found" if ok else "MISSING"}')


print('\n=== TEST 3: Course alias detection ===')
content = open('/home/ubuntu/smart_study_agent/bot/telegram_bot.py', encoding='utf-8').read()
m = re.search(r'_COURSE_ALIASES = \{(.*?)\n    \}', content, re.DOTALL)
aliases = {}
if m:
    for pair in re.findall(r"'([^']+)':\s*'([^']+)'", m.group(1)):
        aliases[pair[0]] = pair[1]

print(f'  (loaded {len(aliases)} aliases)')
alias_cases = [
    ('מדמח',       'intro_to_cs'),
    ('חשבון',      'calculus_2'),
    ('אלגברה',     'linear_algebra_2'),
    ('בדידה',      'discrete_math_2'),
    ('מיקרו',      'microeconomics'),
    ('cs',         'intro_to_cs'),
    ('מטריצות',    'linear_algebra_2'),
    ('אינטגרלים', 'calculus_2'),
    ('מבוא',       'intro_to_cs'),
    ('מורחב',      'intro_to_cs'),
    ('לינארית',    'linear_algebra_2'),
    ('חדוא',       'calculus_2'),
]
for alias, expected in alias_cases:
    actual = aliases.get(alias)
    ok = (actual == expected)
    check(f'alias "{alias}"', ok, f'got {actual!r} expected {expected!r}')


print('\n=== TEST 4: File search simulation ===')
STOP = {'של','עם','את','אני','הוא','היא','מ','ב','ל','ו','ה','כל','all',
        'אחרון','אחרונה','האחרון','האחרונה','latest','last',
        'הרצאה','הרצאת','הרצאות','תרגול','תרגולים','תרגיל','תרגילים',
        'רצאה','רצאות','רצאת','רגול','רגולים','רגיל','רגילים','רצא','רגו',
        'מה','שלמדנו','שנלמד','שעשינו','ילמד','נלמד','למדנו',
        'שלמד','שיש','יש','קיים','קיימת','חומר','חומרים',
        'ממה','שיעור','בשיעור','בקורס','בכל','כל',
        'סיכום','סיכומים','הסיכום','הסיכומים',
        'מסמך','מסמכים','תיק','תיקים','דף','דפים',
        'תוכן','תכנים','גרסה','גרסת','גרסאות',
        'summary','summaries','document','doc',
        'יכום','יכומים'}

def strip_he(w, alias_set=None):
    """Strip prefixes, stop on alias hit (mirrors live code)."""
    if alias_set and w in alias_set:
        return w
    while len(w) > 2 and w[0] in 'הבלמשכו':
        w = w[1:]
        if alias_set and w in alias_set:
            return w
    return w

# Load teacher map too
m2 = re.search(r'_TEACHER_TO_COURSE = \{(.*?)\n    \}', content, re.DOTALL)
teachers = {}
if m2:
    for pair in re.findall(r"'([^']+)':\s*'([^']+)'", m2.group(1)):
        teachers[pair[0]] = pair[1]
print(f'  (loaded {len(teachers)} teacher mappings)')

alias_set = set(aliases.keys())

search_cases = [
    ('הסיכום האחרון במבוא מורחב למדמח', 'intro_to_cs'),
    ('הסיכום האחרון בחשבון',             'calculus_2'),
    ('הרצאה אחרונה מרצה_כ',                'calculus_2'),       # via teacher
    ('תרגול מרצה_י',                       'calculus_2'),       # via teacher
    ('כל ההרצאות של בדידה',                'discrete_math_2'),
    ('שלח לי לינארית',                      'linear_algebra_2'),
    ('שלח לי הרצאה של מרצה_צ',               'linear_algebra_2'), # via teacher
    ('סיכום של מרצה_ע',                     'discrete_math_2'),  # via teacher
]
for q, exp_course in search_cases:
    ql = re.sub(r'^\s*(תשלח|שלח|תן|תוריד|הורד)\s+(לי\s+)?(את\s+)?', '', q).strip()
    raw_tokens = [t for t in re.split(r'\s+', ql.lower()) if t]
    course = None
    clean = []
    for t in raw_tokens:
        ts = strip_he(t, alias_set=alias_set)
        if ts in aliases:
            course = aliases[ts]; continue
        if t in aliases:
            course = aliases[t]; continue
        if ts in teachers:
            if not course: course = teachers[ts]
            clean.append(ts); continue
        if t in teachers:
            if not course: course = teachers[t]
            clean.append(t); continue
        if t in STOP or ts in STOP: continue
        if len(ts) <= 1: continue
        clean.append(ts)
    ok = (course == exp_course)
    info = f'tokens={clean}'
    print(f'  "{q}" → course={course} tokens={clean}')
    check(f'search "{q[:40]}"', ok, info)


print(f'\n=== SUMMARY: {passed} passed, {failed} failed ===')
if fail_details:
    print('\nFailures:')
    for d in fail_details:
        print(f'  - {d}')
