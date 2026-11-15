"""
scripts/enhance_obsidian.py
===========================
משדרג את הוולט ל-7 תחומים:
  1. graph.json — צבעי קבוצות, MOC זהוב, מושגים סגול, חולשות אדומות
  2. _index.md לכל קורס — MOC עשיר עם בר התקדמות + 5 מושגים מובילים + מטלות פתוחות
  3. 🏠 בית.md — דשבורד עם "מה חדש" + מטלות + ציונים + מושגים פעילים
  4. 🌉 גשרים.md — hub למושגים שמופיעים ביותר מקורס אחד
  5. מושגים/_ALL.md — אינדקס מלא של כל המושגים ממוין לפי כמות references
  6. שבועות — רענון של weekly digests עם נתונים מלאים
  7. concept duplicates report — דו"ח טקסט עם הצעות מיזוג ידני

הכל idempotent — אפשר להריץ שוב ושוב בלי לשבור כלום.
"""
import sys, os, re, json, yaml
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict, Counter

sys.path.insert(0, '.')
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except: pass

from utils.env import _load_env_dotfile
_load_env_dotfile()

VAULT_ROOT = Path(os.environ.get('OBSIDIAN_VAULT_PATH', str(Path.home() / 'Documents' / 'Obsidian Vault')))
STUDY = VAULT_ROOT / 'Smart Study Agent'
CONCEPTS_DIR = STUDY / 'מושגים'
WEEKS_DIR = STUDY / '📅 שבועות'

# קונפיג קורסים — מהקובץ הראשי
config = yaml.safe_load(open('config/courses.yaml', encoding='utf-8').read())
courses_cfg = config.get('courses', config)

COURSE_FOLDER = {
    'intro_to_cs': 'מבוא מורחב למדמח',
    'calculus_2': 'חשבון דיפרנציאלי ואינטגרלי 2ב',
    'discrete_math_2': 'מתמטיקה בדידה 2',
    'linear_algebra_2': 'אלגברה לינארית 2ב',
    'microeconomics': "מיקרו כלכלה א'",
}
COURSE_EMOJI = {
    'intro_to_cs': '💻',
    'calculus_2': '∫',
    'discrete_math_2': '🧩',
    'linear_algebra_2': '🔢',
    'microeconomics': '📈',
}
COURSE_COLOR_HEX = {
    'intro_to_cs': '#5C7CFA',       # blue
    'calculus_2': '#FFA726',        # orange
    'discrete_math_2': '#66BB6A',   # green
    'linear_algebra_2': '#AB47BC',  # purple
    'microeconomics': '#EF5350',    # red
}

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)

def read_fm(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding='utf-8')
    except Exception:
        return {}, ''
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    return fm, text[m.end():]

def hex_to_int(h: str) -> int:
    return int(h.lstrip('#'), 16)

def safe_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')

# ──────────────────────────────────────────────────────────────
# Step 1: Enhance graph.json
# ──────────────────────────────────────────────────────────────
def enhance_graph_json():
    print('\n[1/7] 🎨 משדרג graph.json...')
    gp = STUDY / '.obsidian' / 'graph.json'
    if not gp.exists():
        print(f'  ⚠️  לא נמצא: {gp}')
        return
    g = json.loads(gp.read_text(encoding='utf-8'))

    # קבוצות צבע — סדר הופך עדיפות (עליונים מנצחים)
    color_groups = [
        # MOCs (עדיפות עליונה — צבע זהב, גדול)
        {'query': 'path:"_index" OR path:"🏠"', 'color': {'a': 1.0, 'rgb': hex_to_int('#FFD700')}},
        # חולשות (אדום בולט)
        {'query': 'tag:#weakness', 'color': {'a': 1.0, 'rgb': hex_to_int('#E53935')}},
        # מושגים (סגול)
        {'query': 'path:"מושגים/"', 'color': {'a': 1.0, 'rgb': hex_to_int('#8E24AA')}},
        # שבועות (טורקיז)
        {'query': 'path:"שבועות/"', 'color': {'a': 1.0, 'rgb': hex_to_int('#26C6DA')}},
        # NotebookLM artifacts (כתום בהיר)
        {'query': 'path:"NotebookLM/"', 'color': {'a': 1.0, 'rgb': hex_to_int('#FFB74D')}},
        # מטלות (חום)
        {'query': 'path:"מטלות"', 'color': {'a': 1.0, 'rgb': hex_to_int('#8D6E63')}},
        # הודעות (אפור)
        {'query': 'path:"הודעות"', 'color': {'a': 1.0, 'rgb': hex_to_int('#9E9E9E')}},
        # קורסים — סדר ידוע
        {'query': 'tag:#intro_to_cs',      'color': {'a': 1.0, 'rgb': hex_to_int(COURSE_COLOR_HEX['intro_to_cs'])}},
        {'query': 'tag:#calculus_2',       'color': {'a': 1.0, 'rgb': hex_to_int(COURSE_COLOR_HEX['calculus_2'])}},
        {'query': 'tag:#discrete_math_2',  'color': {'a': 1.0, 'rgb': hex_to_int(COURSE_COLOR_HEX['discrete_math_2'])}},
        {'query': 'tag:#linear_algebra_2', 'color': {'a': 1.0, 'rgb': hex_to_int(COURSE_COLOR_HEX['linear_algebra_2'])}},
        {'query': 'tag:#microeconomics',   'color': {'a': 1.0, 'rgb': hex_to_int(COURSE_COLOR_HEX['microeconomics'])}},
    ]
    g['colorGroups'] = color_groups

    # תצוגה — ברירות מחדל טובות
    g.setdefault('showOrphans', True)
    g['showAttachments'] = False
    g['hideUnresolved'] = False
    g['showTags'] = False
    # פיזיקה — קצת יותר רך
    g['centerStrength'] = 0.5
    g['repelStrength'] = 12
    g['linkStrength'] = 1.0
    g['linkDistance'] = 200
    # גודל צמתים — לפי דרגה
    g['scale'] = 1.0
    g['close'] = True

    gp.write_text(json.dumps(g, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'  ✅ עודכן {gp.name} עם {len(color_groups)} color groups')

# ──────────────────────────────────────────────────────────────
# Step 2: Course MOC (_index.md)
# ──────────────────────────────────────────────────────────────
COURSE_MOC_TEMPLATE = """---
title: {course_name}
course_key: {course_key}
tags: [{course_key}, index, MOC]
type: index
---

# {emoji} {course_name}

> [!info] **המחברת ב-NotebookLM**
> [פתח ב-NotebookLM ↗](https://notebooklm.google.com/notebook/{notebook_id})

---

## 📊 סטטוס

```dataview
TABLE WITHOUT ID
  length(rows) AS "כמות",
  truncate(join(rows.file.link, ", "), 80) AS "פריטים"
FROM "Smart Study Agent/{folder_name}"
WHERE course_key = "{course_key}" AND type
GROUP BY type
```

**📈 התקדמות:** `= dv.pages('"Smart Study Agent/{folder_name}"').where(p => p.course_key == "{course_key}" && p.type == "הרצאה").length` הרצאות · `= dv.pages('"Smart Study Agent/{folder_name}"').where(p => p.course_key == "{course_key}" && p.type == "תרגול").length` תרגולים

---

## 📚 הרצאות אחרונות

```dataview
TABLE WITHOUT ID
  file.link AS "הרצאה",
  date AS "תאריך",
  topics AS "נושאים"
FROM "Smart Study Agent/{folder_name}"
WHERE course_key = "{course_key}" AND type = "הרצאה"
SORT date DESC
LIMIT 8
```

## ✏️ תרגולים אחרונים

```dataview
TABLE WITHOUT ID
  file.link AS "תרגול",
  date AS "תאריך",
  topics AS "נושאים"
FROM "Smart Study Agent/{folder_name}"
WHERE course_key = "{course_key}" AND type = "תרגול"
SORT date DESC
LIMIT 8
```

---

## 🧠 5 מושגים מובילים בקורס

```dataview
TABLE WITHOUT ID
  file.link AS "מושג",
  length(file.inlinks) AS "פעמים שהוזכר"
FROM "Smart Study Agent/מושגים"
WHERE contains(courses, "{course_key}") OR contains(course, "{course_name}") OR contains(file.name, "{course_key}")
SORT length(file.inlinks) DESC
LIMIT 5
```

---

## 📅 מטלות פתוחות

```dataview
TABLE WITHOUT ID
  file.link AS "מטלה",
  due_date AS "תאריך הגשה",
  status AS "סטטוס"
FROM "Smart Study Agent/📋 מטלות"
WHERE course_key = "{course_key}" AND status != "submitted"
SORT due_date ASC
```

## 🎯 חידונים

```dataview
TABLE WITHOUT ID
  file.link AS "חידון",
  date AS "תאריך"
FROM "Smart Study Agent/חידונים"
WHERE course_key = "{course_key}"
SORT date DESC
```

---

## 📢 הודעות אחרונות

```dataview
TABLE WITHOUT ID
  file.link AS "הודעה",
  date AS "תאריך"
FROM "Smart Study Agent/📢 הודעות"
WHERE course_key = "{course_key}"
SORT date DESC
LIMIT 5
```

---

> 🏠 [[🏠 בית|חזרה לדף הבית]] · 🌉 [[🌉 גשרי מושגים|מושגים בין קורסים]]
"""

def build_course_mocs(notebook_ids: dict[str, str]):
    print('\n[2/7] 📚 בונה MOCs לכל קורס...')
    for ck, folder in COURSE_FOLDER.items():
        course_dir = STUDY / folder
        if not course_dir.exists():
            print(f'  ⚠️  תיקייה חסרה: {folder}')
            continue
        index = course_dir / '_index.md'
        course_name = courses_cfg[ck]['name']
        nlm_id = notebook_ids.get(ck, {}).get('all', {}).get('notebook_id', '')
        content = COURSE_MOC_TEMPLATE.format(
            course_key=ck,
            course_name=course_name,
            folder_name=folder,
            emoji=COURSE_EMOJI[ck],
            notebook_id=nlm_id,
        )
        safe_write(index, content)
        print(f'  ✅ {folder}/_index.md')

# ──────────────────────────────────────────────────────────────
# Step 3: Home Dashboard
# ──────────────────────────────────────────────────────────────
HOME_TEMPLATE = """---
title: בית
tags: [home, MOC]
cssclasses: [home-page]
---

# 🎓 Smart Study Agent

> **{today}** · שבוע {iso_week}

---

## 🆕 מה חדש השבוע

```dataview
TABLE WITHOUT ID
  file.link AS "הערה",
  date AS "תאריך",
  course AS "קורס",
  type AS "סוג"
FROM "Smart Study Agent"
WHERE date >= date("{week_start}") AND date <= date("{today_iso}") AND course_key
SORT date DESC
LIMIT 15
```

---

## 📚 הקורסים שלי

{course_cards}

---

## ⚠️ מטלות פתוחות (לפי דחיפות)

```dataview
TABLE WITHOUT ID
  file.link AS "מטלה",
  course AS "קורס",
  due_date AS "הגשה",
  status AS "סטטוס"
FROM "Smart Study Agent/📋 מטלות"
WHERE status != "submitted" AND status != "graded"
SORT due_date ASC
```

---

## 🎯 5 ההערות שעודכנו לאחרונה

```dataview
TABLE WITHOUT ID
  file.link AS "הערה",
  file.mtime AS "עודכן",
  course AS "קורס"
FROM "Smart Study Agent"
WHERE course_key
SORT file.mtime DESC
LIMIT 5
```

---

## 🧠 10 המושגים החזקים ביותר

```dataview
TABLE WITHOUT ID
  file.link AS "מושג",
  length(file.inlinks) AS "הופעות",
  courses AS "קורסים"
FROM "Smart Study Agent/מושגים"
WHERE length(file.inlinks) > 1
SORT length(file.inlinks) DESC
LIMIT 10
```

---

## 🌉 מושגים בין-קורסיים

ראה: [[🌉 גשרי מושגים]] — מושגים שמופיעים ב-2+ קורסים שונים.

---

## 📅 ניווט

- 📆 [[📅 שבועות/{this_week}|שבוע נוכחי]]
- 📋 [[📅 מטלות|כל המטלות]]
- 🧠 [[מושגים/_ALL|כל המושגים]]
- 🎓 [[🎓 NotebookLM|NotebookLM hub]]

---

> _דף זה נוצר אוטומטית ב-{generated_at}_
"""

def build_home_dashboard():
    print('\n[3/7] 🏠 בונה דף בית...')
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    iso_year, iso_week, _ = today.isocalendar()
    this_week = f'{iso_year}-W{iso_week:02d}'

    cards = []
    for ck, folder in COURSE_FOLDER.items():
        course_dir = STUDY / folder
        if not course_dir.exists():
            continue
        course_name = courses_cfg[ck]['name']
        emoji = COURSE_EMOJI[ck]
        # ספירת קבצים
        notes = list(course_dir.glob('*.md'))
        n_lect = sum(1 for n in notes if 'הרצאה' in n.stem.lower() or 'lecture' in n.stem.lower())
        n_tut  = sum(1 for n in notes if 'תרגול' in n.stem.lower() or 'תרגיל' in n.stem.lower())
        # תאריך עדכון אחרון
        if notes:
            last = max(n.stat().st_mtime for n in notes)
            last_str = datetime.fromtimestamp(last).strftime('%d/%m/%Y')
        else:
            last_str = '—'
        cards.append(f"""> [!note]+ {emoji} {course_name}
>
> | | |
> |---|---|
> | 📖 הרצאות | {n_lect} |
> | ✏️ תרגולים | {n_tut} |
> | 🕐 עדכון אחרון | {last_str} |
>
> [[Smart Study Agent/{folder}/_index|📂 פתח קורס →]]
""")

    home = STUDY / '🏠 בית.md'
    content = HOME_TEMPLATE.format(
        today=today.strftime('%d/%m/%Y'),
        today_iso=today.isoformat(),
        iso_week=iso_week,
        week_start=week_start.isoformat(),
        course_cards='\n'.join(cards),
        this_week=this_week,
        generated_at=datetime.now().strftime('%d/%m/%Y %H:%M'),
    )
    safe_write(home, content)
    print(f'  ✅ {home.name}')

# ──────────────────────────────────────────────────────────────
# Step 4: Concept Bridges Hub
# ──────────────────────────────────────────────────────────────
def build_concept_bridges():
    print('\n[4/7] 🌉 בונה hub למושגים בין-קורסיים...')
    if not CONCEPTS_DIR.exists():
        print('  ⚠️  אין תיקיית מושגים')
        return

    # זיהוי גשרים אמיתיים: לאיזה מושג יש קישור (wikilink או concepts: בfrontmatter)
    # מהרצאות ב-2+ תיקיות קורסים?
    concept_to_courses = defaultdict(set)  # stem → {course_keys}
    stems = {f.stem for f in CONCEPTS_DIR.glob('*.md') if not f.stem.startswith('_')}

    folder_to_ck = {v: k for k, v in COURSE_FOLDER.items()}

    link_re = re.compile(r'\[\[([^\]|#^]+?)(?:\|[^\]]+)?\]\]')
    for folder, ck in folder_to_ck.items():
        course_dir = STUDY / folder
        if not course_dir.exists():
            continue
        for note in course_dir.rglob('*.md'):
            fm, body = read_fm(note)
            # 1. wikilinks בגוף
            for match in link_re.findall(body):
                target = match.strip().split('/')[-1]
                if target in stems:
                    concept_to_courses[target].add(ck)
            # 2. concepts: בfrontmatter
            for c in (fm.get('concepts') or []):
                if isinstance(c, str) and c.strip() in stems:
                    concept_to_courses[c.strip()].add(ck)

    # bridges = מושגים שיש להם קישורים מ-2+ קורסים
    bridges = []
    for stem, courses_set in concept_to_courses.items():
        if len(courses_set) >= 2:
            bridges.append({
                'stem': stem,
                'courses': sorted(courses_set),
                'n_courses': len(courses_set),
            })
    bridges.sort(key=lambda b: (-b['n_courses'], b['stem']))

    # סדר לפי כמות קורסים
    bridges.sort(key=lambda b: -len(b['courses']))

    lines = [
        '---',
        'title: גשרי מושגים',
        'tags: [bridges, MOC]',
        '---',
        '',
        '# 🌉 גשרי מושגים בין קורסים',
        '',
        f'> {len(bridges)} מושגים מופיעים ב-2+ קורסים שונים. אלה הצמתים הכי חשובים בגרף שלך.',
        '',
    ]
    if bridges:
        lines.append('| מושג | מספר קורסים | קורסים |')
        lines.append('|---|---|---|')
        for b in bridges[:80]:
            courses_str = ', '.join(b['courses'])
            lines.append(f'| [[{b["stem"]}]] | {b["n_courses"]} | {courses_str} |')
    else:
        lines.append('_עדיין לא נמצאו מושגים חוצי-קורסים._')

    lines += [
        '',
        '---',
        '## 🔍 כל המושגים',
        '',
        'ראה: [[מושגים/_ALL|אינדקס מלא של מושגים]]',
        '',
        '> 🏠 [[🏠 בית|חזרה לדף הבית]]',
    ]

    bridge_path = STUDY / '🌉 גשרי מושגים.md'
    safe_write(bridge_path, '\n'.join(lines))
    print(f'  ✅ {bridge_path.name} עם {len(bridges)} bridges')

# ──────────────────────────────────────────────────────────────
# Step 5: Concept Index (_ALL.md)
# ──────────────────────────────────────────────────────────────
def build_concept_index():
    print('\n[5/7] 🧠 בונה אינדקס מושגים מלא...')
    if not CONCEPTS_DIR.exists():
        return
    files = sorted(CONCEPTS_DIR.glob('*.md'))
    files = [f for f in files if not f.stem.startswith('_')]

    lines = [
        '---',
        'title: אינדקס מושגים',
        'tags: [concepts, index]',
        '---',
        '',
        '# 🧠 כל המושגים',
        '',
        f'> סה"כ **{len(files)}** מושגים בוולט. ממוין לפי שם.',
        '',
        '## דירוג לפי כמות הזכרות',
        '',
        '```dataview',
        'TABLE WITHOUT ID',
        '  file.link AS "מושג",',
        '  length(file.inlinks) AS "הופעות",',
        '  courses AS "קורסים"',
        'FROM "Smart Study Agent/מושגים"',
        'WHERE !startswith(file.name, "_")',
        'SORT length(file.inlinks) DESC',
        '```',
        '',
        '---',
        '',
        '## רשימה אלפבתית',
        '',
    ]
    # קבץ לפי אות ראשונה (מספר/אות)
    by_letter = defaultdict(list)
    for f in files:
        first = f.stem[0].upper() if f.stem else '?'
        by_letter[first].append(f)
    for letter in sorted(by_letter.keys()):
        lines.append(f'### {letter}')
        for f in by_letter[letter]:
            lines.append(f'- [[{f.stem}]]')
        lines.append('')
    lines.append('---')
    lines.append('> 🏠 [[🏠 בית|חזרה לדף הבית]] · 🌉 [[🌉 גשרי מושגים|גשרים]]')

    out = CONCEPTS_DIR / '_ALL.md'
    safe_write(out, '\n'.join(lines))
    print(f'  ✅ {out.relative_to(STUDY)} ({len(files)} מושגים)')

# ──────────────────────────────────────────────────────────────
# Step 6: Concept Duplicates Report
# ──────────────────────────────────────────────────────────────
def detect_concept_duplicates():
    print('\n[6/7] 🔍 מזהה מושגים כפולים...')
    if not CONCEPTS_DIR.exists():
        return
    files = [f for f in CONCEPTS_DIR.glob('*.md') if not f.stem.startswith('_')]

    # קריטריון 1: שמות שנגישים בהסרת suffix
    base_to = defaultdict(list)
    for f in files:
        base = re.sub(r'__\w+$', '', f.stem.lower())
        base_to[base].append(f.stem)

    # קריטריון 2: מילה אחת מהשני (substring)
    likely_dups = []
    stems = [f.stem for f in files]
    for i, a in enumerate(stems):
        for b in stems[i+1:]:
            la, lb = a.lower(), b.lower()
            # אם אחד substring של השני וההפרש קטן
            if la != lb and (la in lb or lb in la):
                if abs(len(la) - len(lb)) < 12:
                    likely_dups.append((a, b))

    lines = [
        '---',
        'title: דו"ח מושגים כפולים',
        'tags: [maintenance, concepts]',
        '---',
        '',
        '# 🔍 דו"ח מושגים שייתכן שהם כפילויות',
        '',
        '> **לקריאה ידנית**. אל תמחק אוטומטית. אם תרצה למזג — תגיד לי איזה לתוך איזה.',
        '',
        '## קבוצות לפי שם בסיס משותף',
        '',
    ]
    found = 0
    for base, group in sorted(base_to.items()):
        if len(group) >= 2:
            lines.append(f'- **{base}**:')
            for stem in group:
                lines.append(f'  - [[{stem}]]')
            found += 1
    if not found:
        lines.append('_אין קבוצות עם שם בסיס משותף._')

    lines += ['', '---', '', '## זוגות עם דמיון מילולי גבוה', '']
    if likely_dups:
        for a, b in likely_dups[:30]:
            lines.append(f'- [[{a}]] ↔ [[{b}]]')
    else:
        lines.append('_לא נמצאו זוגות חשודים._')

    lines += ['', '---', '> 🏠 [[🏠 בית|חזרה]]']

    out = STUDY / '🔍 דוח כפילויות.md'
    safe_write(out, '\n'.join(lines))
    print(f'  ✅ {out.name} ({found} קבוצות, {len(likely_dups)} זוגות חשודים)')

# ──────────────────────────────────────────────────────────────
# Step 7: Enrich existing weekly digests (only if minimal)
# ──────────────────────────────────────────────────────────────
WEEKLY_TEMPLATE = """---
title: שבוע {iso_year}-W{iso_week:02d}
type: weekly_digest
date: {monday_iso}
tags: [weekly]
---

# 📅 שבוע {iso_year}-W{iso_week:02d}

> {monday_he} – {sunday_he}

---

## 🆕 הרצאות ותרגולים השבוע

```dataview
TABLE WITHOUT ID
  file.link AS "הערה",
  date AS "תאריך",
  course AS "קורס",
  type AS "סוג"
FROM "Smart Study Agent"
WHERE date >= date("{monday_iso}") AND date <= date("{sunday_iso}") AND type
SORT date ASC
```

## 📋 מטלות שהוגשו / נפתחו השבוע

```dataview
TABLE WITHOUT ID
  file.link AS "מטלה",
  course AS "קורס",
  due_date AS "הגשה",
  status AS "סטטוס"
FROM "Smart Study Agent/📋 מטלות"
WHERE (created >= date("{monday_iso}") AND created <= date("{sunday_iso}")) OR (due_date >= date("{monday_iso}") AND due_date <= date("{sunday_iso}"))
SORT due_date ASC
```

## 📢 הודעות

```dataview
LIST date + " · " + summary
FROM "Smart Study Agent/📢 הודעות"
WHERE date >= date("{monday_iso}") AND date <= date("{sunday_iso}")
SORT date ASC
```

## 🧠 מושגים חדשים שהוזכרו השבוע

```dataview
TABLE WITHOUT ID
  file.link AS "מושג",
  length(file.inlinks) AS "הופעות"
FROM "Smart Study Agent/מושגים"
WHERE file.cday >= date("{monday_iso}") AND file.cday <= date("{sunday_iso}")
SORT length(file.inlinks) DESC
LIMIT 10
```

---

> 🏠 [[🏠 בית|חזרה לדף הבית]]
"""

def enrich_weekly_digests():
    print('\n[7/7] 📅 מרענן weekly digests...')
    WEEKS_DIR.mkdir(exist_ok=True)
    today = date.today()
    # 12 שבועות אחורה
    enriched = 0
    for weeks_back in range(12):
        target = today - timedelta(weeks=weeks_back)
        iso_year, iso_week, _ = target.isocalendar()
        # מצא יום שני של אותו שבוע
        monday = target - timedelta(days=target.weekday())
        sunday = monday + timedelta(days=6)
        path = WEEKS_DIR / f'{iso_year}-W{iso_week:02d}.md'
        # רק אם הקובץ קצר (פחות מ-1KB) או לא קיים — נחליף
        if path.exists() and path.stat().st_size > 1500:
            continue
        content = WEEKLY_TEMPLATE.format(
            iso_year=iso_year, iso_week=iso_week,
            monday_iso=monday.isoformat(),
            sunday_iso=sunday.isoformat(),
            monday_he=monday.strftime('%d/%m/%Y'),
            sunday_he=sunday.strftime('%d/%m/%Y'),
        )
        safe_write(path, content)
        enriched += 1
    print(f'  ✅ רוענו/נוצרו {enriched} digests')

# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────
CONCEPT_BLOCK_HEAD = '<!-- AUTO-CONCEPTS:BEGIN -->'
CONCEPT_BLOCK_TAIL = '<!-- AUTO-CONCEPTS:END -->'

def inject_concept_wikilinks_into_lectures():
    """
    בכל הערה שיש לה concepts: בfrontmatter — מוסיף בסוף הקובץ
    בלוק מסומן עם wikilinks מפורשים. הופך את הגרף לצפוף ולמושגים
    לניתנים לניווט. idempotent — מחליף בלוק קיים.
    """
    print('\n[8/8] 🔗 מזריק wikilinks למושגים בגוף הערות...')
    stems = {f.stem for f in CONCEPTS_DIR.glob('*.md') if not f.stem.startswith('_')}
    n_updated = 0
    n_links_total = 0
    for folder, ck in {v: k for k, v in COURSE_FOLDER.items()}.items():
        course_dir = STUDY / folder
        if not course_dir.exists():
            continue
        for note in course_dir.glob('*.md'):
            if note.stem == '_index':
                continue
            try:
                text = note.read_text(encoding='utf-8')
            except Exception:
                continue
            fm, _ = read_fm(text) if text.startswith('---') else (None, None)
            # אופציה: read_fm מחזיר tuple — נשתמש בparser שלי
            fm, body = read_fm(note)
            concepts = [c for c in (fm.get('concepts') or []) if isinstance(c, str) and c.strip() in stems]
            if not concepts:
                continue
            # בנה את הבלוק
            block = [CONCEPT_BLOCK_HEAD,
                     '## 🧠 מושגים מרכזיים',
                     '',
                     ' · '.join(f'[[{c.strip()}]]' for c in concepts),
                     CONCEPT_BLOCK_TAIL]
            block_text = '\n'.join(block)
            # החלף בלוק קיים או הוסף בסוף
            full = note.read_text(encoding='utf-8')
            if CONCEPT_BLOCK_HEAD in full:
                pat = re.compile(re.escape(CONCEPT_BLOCK_HEAD) + r'.*?' + re.escape(CONCEPT_BLOCK_TAIL),
                                  re.DOTALL)
                new_text = pat.sub(block_text, full)
            else:
                new_text = full.rstrip() + '\n\n' + block_text + '\n'
            if new_text != full:
                note.write_text(new_text, encoding='utf-8')
                n_updated += 1
                n_links_total += len(concepts)
    print(f'  ✅ עודכנו {n_updated} הערות עם {n_links_total} wikilinks חדשים לגרף')

def main():
    print(f'🌿 Vault: {STUDY}')
    if not STUDY.exists():
        print('❌ Vault not found')
        return

    # Load NotebookLM IDs (להזרים לתוך MOCs)
    nlm_path = Path('config/notebooklm_notebooks.json')
    notebook_ids = {}
    if nlm_path.exists():
        notebook_ids = json.loads(nlm_path.read_text(encoding='utf-8'))

    enhance_graph_json()
    build_course_mocs(notebook_ids)
    build_home_dashboard()
    inject_concept_wikilinks_into_lectures()   # קודם — כדי שיש wikilinks לגלות
    build_concept_bridges()
    build_concept_index()
    detect_concept_duplicates()
    enrich_weekly_digests()

    print('\n🎉 הושלם! פתח את הוולט עכשיו וראה את הקסם.')

if __name__ == '__main__':
    main()
