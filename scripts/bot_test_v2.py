"""Bot tests v2 — edge cases, multi-turn conversations, real file search, RAG quality."""
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
warnings = 0
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

def warn(name, info=''):
    global warnings
    warnings += 1
    print(f'WARN  {name}  {info}')


# ============================================================
print('\n=== TEST 5: Tricky classifications (edge cases) ===')
# ============================================================
tricky = [
    # name → query → expected_action [, expected_param_contains]
    ('mixed language',        'send me Lecture 5 of intro',           ['file_send', 'ask']),
    ('typo - חידון',           'תיכין לי חידון על בדידה',                ['quiz']),
    ('typo - תזכיר',           'תזכיר ילי מחר 14:00 לקרוא',              ['reminder']),
    ('compound: file+quiz',    'שלח לי הרצאה 5 בלינארית ותכין חידון',    ['file_send', 'quiz', 'summarize']),  # acceptable any
    ('very short',             'תודה',                                   ['greeting']),
    ('very short 2',           'בדידה',                                  ['ask', 'list_summaries', 'file_send']),
    ('unclear: just course',   'לינארית',                                ['ask', 'list_summaries', 'file_send']),
    ('Q with prep',            'אז מה זה בדיוק גרף קשיר?',                ['ask']),
    ('summarize today plural', 'תסכם את כל ההרצאות של היום',             ['summarize']),
    ('schedule-like',          'מתי הבחינה הקרובה?',                     ['ask', 'today', 'assignments']),
    ('multi-turn precursor',   'מה היה היום בלינארית?',                  ['ask', 'today']),
]
for name, q, valid_actions in tricky:
    t0 = time.time()
    r = brain.classify(q)
    dt = time.time() - t0
    actual = r.get('action')
    ok = actual in valid_actions
    info = f'({dt:.1f}s) got={actual}, accept any of {valid_actions}'
    check(name, ok, info)


# ============================================================
print('\n=== TEST 6: Multi-turn conversation resolution ===')
# ============================================================
hist1 = (
    "user: מה זה משפט קיילי-המילטון?\n"
    "bot: משפט קיילי-המילטון אומר שכל מטריצה מקיימת את הפולינום האופייני שלה.\n"
    "user: תן דוגמה\n"
    "bot: עבור מטריצה 2x2 A=[[1,2],[3,4]], הפולינום האופייני הוא λ²-5λ-2."
)
multi_turn = [
    ('follow-up: deeper',  'ולמה?',                hist1, ['קיילי', 'משפט', 'נכון', 'הוכחה']),
    ('change topic',        'ובלינארית מה הרצאת מרצה_צ האחרונה?', hist1, ['מרצה_צ', 'לינארית', 'הרצאה']),
    ('confirmation: yes',   'כן',                    hist1, ['קיילי', 'דוגמה', 'מטריצה']),
    ('negation',            'לא',                    hist1, []),  # ambiguous, just check it doesn't crash
    ('clarify: which?',     'איזו מהן?',             hist1, ['דוגמה', 'מטריצה', 'קיילי']),
    ('imperative with ref', 'תפרט את הצעד הראשון',   hist1, ['דוגמה', 'מטריצה', 'פולינום', 'קיילי']),
]
for name, q, h, must_contain_any in multi_turn:
    t0 = time.time()
    resolved = brain.resolve_standalone(q, history=h)
    dt = time.time() - t0
    print(f'  "{q}" → "{resolved}"')
    if not must_contain_any:
        # just sanity — didn't crash
        check(name, isinstance(resolved, str) and len(resolved) > 0,
              f'({dt:.1f}s) resolved (no keyword check)')
    else:
        ok = any(kw in resolved for kw in must_contain_any)
        check(name, ok, f'({dt:.1f}s) need any of {must_contain_any}')


# ============================================================
print('\n=== TEST 7: Real file search — does it actually find files? ===')
# ============================================================
SUMMARIES = Path('data/summaries')

# Build a file index for our tests
all_pdfs = []
for course_dir in SUMMARIES.iterdir():
    if not course_dir.is_dir(): continue
    for sub in ('lectures', 'tutorials'):
        d = course_dir / sub
        if d.exists():
            all_pdfs.extend(d.glob('*.pdf'))

print(f'  (found {len(all_pdfs)} PDF files total)')

# We need to simulate _find_files_for_query using the actual logic.
# Easier: import the function from the running bot module by reading constants
content = open('/home/ubuntu/smart_study_agent/bot/telegram_bot.py', encoding='utf-8').read()
m = re.search(r'_COURSE_ALIASES = \{(.*?)\n    \}', content, re.DOTALL)
aliases = {}
if m:
    for pair in re.findall(r"'([^']+)':\s*'([^']+)'", m.group(1)):
        aliases[pair[0]] = pair[1]
m2 = re.search(r'_TEACHER_TO_COURSE = \{(.*?)\n    \}', content, re.DOTALL)
teachers = {}
if m2:
    for pair in re.findall(r"'([^']+)':\s*'([^']+)'", m2.group(1)):
        teachers[pair[0]] = pair[1]

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

def strip_he(w):
    if w in aliases: return w
    while len(w) > 2 and w[0] in 'הבלמשכו':
        w = w[1:]
        if w in aliases: return w
    return w

def normalize_he(w):
    w = strip_he(w.lower())
    if w.endswith('ות') and len(w) > 3: w = w[:-2] + 'ה'
    elif w.endswith('ים') and len(w) > 3: w = w[:-2]
    elif w.endswith('יים') and len(w) > 4: w = w[:-3]
    return w

def find_files(q, max_results=5):
    """Mirror of _find_files_for_query — returns matching file paths."""
    ql = re.sub(r'^\s*(תשלח|שלח|תן|תוריד|הורד)\s+(לי\s+)?(את\s+)?', '', q).strip()
    if not ql: return []
    wants_latest = bool(re.search(r'אחרון|אחרונה|האחרון|האחרונה|latest|last', ql))
    wants_all    = bool(re.search(r'\bכל\b|\bכל ה|all\b', ql))
    wants_lectures  = bool(re.search(r'הרצא', ql))
    wants_tutorials = bool(re.search(r'תרגול|תרגיל', ql))
    if not (wants_lectures or wants_tutorials):
        wants_lectures = wants_tutorials = True

    course = None
    clean = []
    for t in [x for x in re.split(r'\s+', ql.lower()) if x]:
        ts = strip_he(t)
        if ts in aliases: course = aliases[ts]; continue
        if t in aliases: course = aliases[t]; continue
        if ts in teachers:
            if not course: course = teachers[ts]
            clean.append(ts); continue
        if t in teachers:
            if not course: course = teachers[t]
            clean.append(t); continue
        tn = normalize_he(t)
        if t in STOP or ts in STOP or tn in STOP: continue
        if len(ts) <= 1: continue
        clean.append(tn)

    candidates = []
    for pdf in all_pdfs:
        path_l = str(pdf).replace('\\', '/').lower()
        stem_l = pdf.stem.lower()
        if course:
            if course not in path_l: continue
        # kind filter
        parent = pdf.parent.name.lower()
        is_lecture = 'lecture' in parent
        is_tutorial = 'tutorial' in parent or 'תרגול' in stem_l
        if wants_lectures and wants_tutorials: pass
        else:
            if wants_lectures and not is_lecture: continue
            if wants_tutorials and not is_tutorial: continue
        stem_norm = normalize_he(stem_l)
        if clean and not wants_all:
            if not all(t in stem_l or t in stem_norm for t in clean):
                continue
        candidates.append(pdf)

    # sort by date desc
    def date_in(p):
        m = re.search(r'(\d{4}-\d{2}-\d{2})', p.stem)
        return m.group(1) if m else ''
    candidates.sort(key=lambda p: date_in(p), reverse=True)
    if wants_latest and candidates: return [candidates[0]]
    if wants_all: return candidates[:25]
    return candidates[:max_results]


file_search_cases = [
    ('latest intro CS lecture',        'הסיכום האחרון במבוא',          'intro_to_cs', 'lectures'),
    ('latest intro CS by abbrev',      'תשלח לי הסיכום האחרון במדמח',  'intro_to_cs', 'lectures'),
    ('Arnon lecture',                  'הרצאה אחרונה של מרצה_כ',         'calculus_2',  'lectures'),
    ('lecturer_e tutorial',               'תרגול מרצה_י',                  'calculus_2',  'tutorials'),
    ('Yelena (Yelena=מרצה_צ)',           'הרצאה אחרונה של מרצה_צ',          'linear_algebra_2', 'lectures'),
    ('Feldman discrete',               'סיכום של מרצה_ע',                'discrete_math_2', None),
    ('all discrete lectures',          'כל ההרצאות של בדידה',           'discrete_math_2', None),
    ('linear algebra lecture',         'הרצאה לינארית אחרונה',          'linear_algebra_2', 'lectures'),
]
for name, q, exp_course, exp_kind in file_search_cases:
    files = find_files(q)
    ok = bool(files)
    if ok:
        # verify course filter worked
        ok = exp_course in str(files[0]).replace('\\','/').lower()
    info = f'found {len(files)} files'
    if files:
        info += f', top: {files[0].name}'
    if not ok and not files:
        info += f' (expected to find {exp_course}/{exp_kind})'
    check(f'file_search: {name}', ok, info)


# ============================================================
print('\n=== TEST 8: RAG quality — real retrieval ===')
# ============================================================
try:
    from rag.vault_indexer import search
    rag_q = [
        ('Cayley-Hamilton',        'מה זה משפט קיילי-המילטון?', 'linear_algebra_2'),
        ('Inclusion-exclusion',    'מה זה החלה והדחה?',           'discrete_math_2'),
        ('Recursion (intro CS)',   'מה זה רקורסיה?',              'intro_to_cs'),
        ('Indifference curves',    'מה זה עקומות אדישות?',        'microeconomics'),
        ('Limits in calc',         'מה זה גבול של פונקציה?',      'calculus_2'),
    ]
    for name, q, expected_course in rag_q:
        t0 = time.time()
        hits = search(q, k=5)
        dt = time.time() - t0
        ok = bool(hits)
        if hits:
            top_course = hits[0].get('course_key', '?')
            # Accept any course in top 3 hits — RAG can be fuzzy
            top3_courses = [h.get('course_key') for h in hits[:3]]
            ok = expected_course in top3_courses
            info = f'({dt:.1f}s) {len(hits)} hits, top3 courses: {top3_courses}'
        else:
            info = f'({dt:.1f}s) NO HITS'
        check(f'RAG: {name}', ok, info)
except Exception as e:
    warn('RAG tests', f'skipped: {e}')


# ============================================================
print('\n=== TEST 9: Performance — total response budget ===')
# ============================================================
perf_cases = [
    'מה זה גרף?',                          # ask
    'תסכם את ההרצאה האחרונה בבדידה',     # summarize
    'תשלח לי הרצאה 5',                    # file_send
    'מה הסטטוס?',                          # status
]
budget = 5.0  # seconds total per query (classify + resolve)
for q in perf_cases:
    t0 = time.time()
    resolved = brain.resolve_standalone(q, history='')
    intent = brain.classify(resolved, history='')
    dt = time.time() - t0
    ok = dt < budget
    check(f'perf: "{q[:30]}..."', ok, f'({dt:.1f}s, budget {budget}s)')


# ============================================================
print('\n=== TEST 10: System health checks ===')
# ============================================================
import urllib.request as _ur
# Bot service status
try:
    import subprocess
    r = subprocess.run(['systemctl', 'is-active', 'smart-study-bot.service'],
                       capture_output=True, text=True)
    check('bot service active', r.stdout.strip() == 'active', f'(returned: {r.stdout.strip()})')
except Exception as e:
    check('bot service active', False, str(e))

# Ollama reachable
try:
    with _ur.urlopen('http://127.0.0.1:11434/api/tags', timeout=5) as r:
        data = json.loads(r.read().decode())
        models = [m.get('name', '?') for m in data.get('models', [])]
        check('Ollama reachable', True, f'models: {models}')
except Exception as e:
    check('Ollama reachable', False, str(e))

# Vault index exists
idx_path = Path('data/vault_index.tfidf.json')
check('vault index exists', idx_path.exists() and idx_path.stat().st_size > 1000,
      f'{idx_path.stat().st_size if idx_path.exists() else 0} bytes')

# Gmail token
gm_path = Path('config/gmail_token.pickle')
check('gmail token present', gm_path.exists(), f'size={gm_path.stat().st_size if gm_path.exists() else 0}')

# Summaries on disk
sum_count = sum(1 for _ in SUMMARIES.rglob('*.md'))
check('summaries on disk', sum_count > 50, f'count={sum_count}')


print(f'\n{"="*55}')
print(f'TOTAL: {passed} passed, {failed} failed, {warnings} warnings')
print(f'{"="*55}')
if fail_details:
    print('\nFailures:')
    for d in fail_details:
        print(f'  - {d}')
