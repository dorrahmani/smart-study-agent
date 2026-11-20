"""
agent/exam_plan.py — "על מה לשים את הכסף בהסתברות?" / "מה חוזר במבחנים בתוכנה 1?" answered from the course's past exams.

The owner's improvement 7 (17/11): per course, from every past exam we hold —
    • the share of questions per topic (all years, and the last 5 years),
    • what comes back almost every year (topics, kinds of question, near-identical questions) with the years,
    • how many questions an exam has and its usual structure,
    • the topics that are not relevant: cut / not found in this semester's summaries, or excluded by a clarification.

Deterministic code only, zero API cost:
    probability, microeconomics_2   the question tags made once when the exams were read (data/exam_topics/<ck>.json)
    calculus_2 … microeconomics     the topic each OCR'd question already carries ("## שאלה N (נושא: …)", 89 exams,
                                    data/exams_text) → the course's topic list by keyword rules
    software_1                      the exam PDFs on Moodle are digital: pdftotext (free) → questions / items →
                                    keyword rules on the Java in them. Cached in data/exams_text/software_1/
    macroeconomics                  no past exams on Moodle → says so
What no rule can place is "לא מסווג" and counted, never guessed.

    python -m agent.exam_plan            build every course: data/exam_plan/<ck>.json + a note per course in the bot's own
                                         vault area (👩‍💼 העוזר/🎓 לימודים/📊 מבחנים/), never in the owner's notes
    answer(text) → str | None            the short WhatsApp version + the note's link. Only when he asks; it offers no
                                         practice question (feedback_no_unsolicited_study).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = 'exam_plan'
NOTE_DIR = '👩‍💼 העוזר/🎓 לימודים/📊 מבחנים'
UNCLASSIFIED = 'לא מסווג'

CURRENT = ('probability', 'software_1', 'microeconomics_2', 'macroeconomics')
NAMES = {'probability': 'הסתברות', 'software_1': 'תוכנה 1', 'microeconomics_2': 'מיקרו כלכלה ב׳',
         'macroeconomics': 'מאקרו כלכלה', 'calculus_2': 'חדו"א 2ב', 'linear_algebra_2': 'אלגברה לינארית 2ב',
         'discrete_math_2': 'מתמטיקה בדידה 2', 'intro_to_cs': 'מבוא מורחב למדמ"ח', 'microeconomics': 'מיקרו כלכלה א׳'}
try:                                                     # your semester (SSA_ACTIVE_COURSES + courses.yaml); TAU's courses are the example
    from config.paths import ACTIVE_COURSES as _ACTIVE, COURSE_NAMES as _CN
    CURRENT = tuple(_ACTIVE) or CURRENT
    NAMES = {**NAMES, **{k: v for k, v in _CN.items() if v}}
except Exception:
    pass
COURSE_RX = [  # what the owner calls the course → key (the more specific first)
    ('microeconomics', r'מיקרו\s*(?:כלכלה\s*)?א[׳\'"]?(?![א-ת])'), ('microeconomics_2', r'מיקרו'),
    ('macroeconomics', r'מאקרו|מקרו'), ('probability', r'הסתברות|הסתב'), ('software_1', r'תוכנה|הנדסת תוכנה|ג׳אווה|ג\'אווה|java'),
    ('calculus_2', r'חדו"?א|חדוא|אינפי|חשבון דיפרנציאלי'), ('linear_algebra_2', r'לינארית|ליניארית|אלגברה'),
    ('discrete_math_2', r'בדידה'), ('intro_to_cs', r'מבוא מורחב|מדמ"?ח|מבוא למדעי המחשב'),
]

# ── the course's topic list → keyword rules (built from the curriculum and the OCR's own topic words) ─────────────
# Each topic: regexes. A question gets every topic whose rules hit its OCR topic line; nothing there → its text.
RULES: dict[str, dict[str, list[str]]] = {
    'calculus_2': {
        'סדרות וטורי פונקציות, התכנסות במידה שווה': [r'במידה שווה', r'סדר(?:ו)?ת(?: של)? פונקציות', r'טור(?:י)? פונקציות', r'מבחן M', r'ויירשטראס M'],
        'טורי חזקות וטיילור': [r'טור(?:י)? חזקות', r'רדיוס התכנסות', r'טיילור'],
        'טורי פורייה': [r'פורי(?:י)?ה', r'fourier'],
        'אינטגרלים לא אמיתיים ואינטגרביליות רימן': [r'לא אמיתי', r'רימן', r'דיריכלה', r'אינטגרביל', r'פונקציות אינטגרליות'],
        'דיפרנציאביליות, נגזרת כיוונית וכלל השרשרת': [r'דיפרנציאבי', r'נגזרת כיוונית', r'גרדיאנט', r'כלל השרשרת', r'נגזר(?:ו)?ת חלקי',
                                                    r'הערך הממוצע', r'נגזרת מסילתית', r'פרמה'],
        'פונקציות סתומות והפוכות': [r'סתומ', r'פונקציות הפיכות', r'הפונקציה ההפוכה', r'העתקה הפתוחה', r'חד-חד-ערכי'],
        'נקודות קריטיות וקיצון מקומי': [r'נקוד(?:ו)?ת קריטי', r'אוכף', r'הסיאן', r'מקומי'],
        'קיצון מוחלט ותחת אילוצים (כופלי לגרנז׳)': [r'לגרנז', r'אילוצ', r'מוחלט', r'סגורה וחסומה', r'אקסטרמום', r'(?:מינימום|מקסימום) (?:ו|ומ)',
                                                    r'ערכי קיצון', r'קיצון', r'אופטימיזציה', r'ויירשטראס ו'],
        'אינטגרלים מרובים, שינוי משתנים ונפח': [r'אינטגרל(?:ים)? (?:כפול|משולש|מרוב)', r'נפח', r'קואורדינטות', r'יעקוביאן',
                                               r'שינוי משתנים', r'פוביני', r'זניח'],
        'רציפות וטופולוגיה במרחב': [r'רציפות במידה שווה', r'קבוצ(?:ה|ות) פתוח', r'קומפקט', r'רציפות'],
        'מסילות ואינטגרלים קוויים': [r'מסיל', r'אינטגרל קווי', r'גרין'],
    },
    'linear_algebra_2': {
        'ערכים עצמיים ולכסון': [r'ערכים עצמיים', r'ערך עצמי', r'ע"ע', r'וקטור(?:ים)? עצמי', r'(?<!ל)לכסינ(?!ות אורתו|ות יונ)', r'לכסון (?:מטריצ|סימולטני)',
                                r'ריבוי (?:אלגברי|גיאומטרי)', r'מרחב(?:ים)? עצמי', r'דמיון מטריצות', r'תנאים לכסינות'],
        'פולינום אופייני, מינימלי וקיילי-המילטון': [r'פולינום (?:אופייני|מינימלי)', r'קיילי', r'פולינומים'],
        'תת-מרחבים אינווריאנטיים, סכום ישר ושילוש': [r'אינווריאנט', r'סכום ישר', r'שילוש', r'תמ"ו', r'משולשי'],
        'צורת ז׳ורדן ונילפוטנטיות': [r"ז['׳]ורדן", r"ג['׳]ורדן", r'נילפוטנט'],
        'מכפלה פנימית, גרם-שמידט והטלות': [r'מכפלה פנימית', r'גרם', r'אורתונורמל', r'הטל(?:ה|ות)', r'ריבועים פחותים', r'QR', r'קירוב ריבועי'],
        'אופרטורים צמודים, הרמיטיים, יוניטריים ונורמליים': [r'צמוד', r'הרמיטי', r'יוניטרי(?!ת)', r'נורמלי', r'אופרטור', r'סימטרי'],
        'משפט הפירוק הספקטרלי ולכסון אורתוגונלי/יוניטרי': [r'לכסון (?:אורתוגונלי|יוניטרי)', r'לכסינות (?:אורתוגונלית|יוניטרית)',
                                                         r'ספקטרל', r'דמיון (?:יוניטרי|אורתוגונלי)', r'שילוש אורתוגונלי'],
        'מטריצות חיוביות, פירוק פולארי ושורשים': [r'חיובי', r'פולארי', r'SVD', r'סינגולרי', r'שורש (?:ריבועי|מעוקב)', r'אי[- ]שלילי'],
        'אקספוננט של מטריצה': [r'אקספוננט'],
        'העתקות לינאריות (גרעין, תמונה)': [r'העתק(?:ות|ה) (?:לינארי|סקלרי|יוניטרי|נילפוטנט)', r'גרעינים', r'תמונות'],
        'אי-שוויונים ונורמות': [r'אי-שוויון', r'אי שוויון', r'חסומים'],
    },
    'discrete_math_2': {
        'קומבינטוריקה: סידורים, בחירות וחלוקות': [r'קומבינטוריקה(?! - (?:זהויות|נוסחאות))', r'סידור', r'הושבה', r'בחירה', r'ספירה',
                                                    r'תתי קבוצות', r'חלוקות', r'דיופנטי', r'פונקציות(?! יוצרות)', r'סדרות', r'מחרוזות',
                                                    r'קבוצות', r'מולטיס', r'וקטורים', r'גשרים'],
        'בינום וזהויות קומבינטוריות': [r'בינום', r'זהויות', r'הוכחה קומבינטורית'],
        'עקרון ההכלה וההדחה': [r'הכלה וה(?:הדחה|הפרדה)'],
        'עקרון שובך היונים ומספרי רמזי': [r'שובך', r'רמזי'],
        'נוסחאות נסיגה': [r'נסיגה', r'רקורסיבי', r'האנוי', r'פיבונא'],
        'פונקציות יוצרות': [r'יוצרות', r'יוצרת'],
        'מספרי קטלן, סטירלינג ובל': [r'קטל[אן]', r'סטירלינג', r'מספרי בל'],
        'תורת הגרפים': [r'גרפ', r'גרף', r'דרגות', r'מסלולים', r'מעגלים', r'צביעה'],
        'עצים ועצים פורשים': [r'עצ(?:ים|י)', r'עץ', r'פורש', r'קירכהוף'],
        'תמורות': [r'תמור'],
        'הסתברות בדידה והילוכים': [r'הסתברות', r'הילוכים'],
        'מטריצות בינאריות': [r'מטריצות'],
    },
    'intro_to_cs': {
        'סיבוכיות זמן וזיכרון': [r'סיבוכיות', r'זמן ריצה'],
        'רקורסיה וממואיזציה': [r'רקורסי', r'ממואיזציה', r'האנוי', r'חזקות'],
        'תכנון דינמי (LCS, LIS, Subset Sum)': [r'תכנון דינמי', r'LCS', r'LIS', r'Subset', r'תת-סדרות', r'תת-מחרוזת'],
        'מיון וחיפוש בינארי': [r'מיון', r'חיפוש בינארי'],
        'רשימות מקושרות': [r'רשימות', r'רשימה מקושרת'],
        'עצים ועצי חיפוש בינאריים': [r'עצ(?:י|ים)', r'עץ'],
        'טבלאות ופונקציות גיבוב': [r'גיבוב', r'hash'],
        'קידוד ודחיסה (האפמן, למפל-זיו)': [r'האפמן', r'הופמן', r'LZ', r'למפל', r'דחיס', r'קידוד'],
        'קודים לתיקון שגיאות': [r'תיקון שגיאות'],
        'ייצוג מספרים ובסיסים': [r'ייצוג (?:בינארי|מספרים)', r'בסיסי ספירה'],
        'תורת המספרים וקריפטוגרפיה (GCD, ראשוניים)': [r'GCD', r'ראשוני', r'קריפטו', r'תורת המספרים', r'חלוקות מספרים', r'פיבונא'],
        'גנרטורים': [r'גנרטור'],
        'עיבוד תמונה': [r'עיבוד תמונה', r'תמונה'],
        'גרפים ו-PageRank': [r'PageRank', r'גרפים'],
        'דקדוקים חסרי הקשר': [r'דקדוק'],
        'מחרוזות ודמיון מילים': [r'דמיון מילים', r'מחרוז'],
        'מבני נתונים, פייתון וניתוח קוד': [r'מבני נתונים', r'פייתון', r'באגים', r'ניתוח קוד', r'שאלות קצרות', r'שונות'],
        'גיאומטריה חישובית': [r'גיאומטריה'],
    },
    'microeconomics': {
        'העדפות ופונקציות תועלת': [r'העדפות', r'פונקציי?(?:ת|ות) תועלת'],
        'בחירת הצרכן וביקוש': [r'סל אופטימלי', r'ביקוש', r'קו תקציב', r'בחירת צרכן', r'נחותים', r'מותרות', r'שעת שמחה', r'תקציב'],
        'אפקט תחלופה והכנסה, פיצוי סלוצקי והיקס': [r'תחלופה', r'פיצוי', r'סלוצקי', r'היקס'],
        'היצע עבודה (עבודה ופנאי) ומיסוי': [r'פנאי', r'היצע עבודה', r'מיסוי', r'מיסים'],
        'אי-ודאות ותוחלת תועלת': [r'תוחלת תועלת', r'סיכון', r'אי-ודאות', r'VNM', r'מצבי טבע', r'שווה ערך וודאי'],
        'תורת הפירמה: ייצור, עלויות, רווח והיצע': [r'ייצור', r'פירמה', r'רווח', r'עלות', r'היצע(?! עבודה)'],
        'סובסידיה ומענק': [r'סובסיד', r'מענק'],
    },
    'software_1': {
        'מחלקות, אובייקטים, static ובנאים': [r'\bstatic\b', r'בנאי', r'constructor', r'כימוס', r'encapsulat'],
        'מודל הזיכרון, == מול equals ו-hashCode': [r'\bequals\b', r'hashCode', r'\bheap\b', r'\bstack\b', r'aliasing', r'by value', r'==\s*\w'],
        'ממשקים (interface, default)': [r'\binterface\b', r'\bimplements\b', r'\bdefault\b', r'מנשק', r'ממשק'],
        'ירושה, דריסה ופולימורפיזם': [r'\bextends\b', r'@Override', r'\bsuper\b', r'\babstract\b', r'אבסטרקט', r'instanceof', r'ClassCastException',
                                      r'דריס', r'ירוש', r'פולימורפ', r'טיפוס (?:דינמי|סטטי)', r'casting', r'\boverrid'],
        'חוזים ו-LSP': [r'@pre', r'@post', r'@inv', r'precondition', r'postcondition', r'invariant', r'תנאי (?:קדם|בתר|אחר)', r'חוזה',
                        r'ליסקוב', r'\bLSP\b'],
        'חריגים (Exceptions)': [r'Exception\b', r'\bthrows?\b', r'\btry\b', r'\bcatch\b', r'\bfinally\b', r'חריג'],
        'Generics': [r'[<>]\s*T\s*[<>]', r'[<>]\s*[TEKV]\s*,', r'\?\s*(?:extends|super)\b', r'generic', r'גנרי', r'type erasure'],
        'אוספים (Collections) ו-Iterator': [r'\b(?:Array|Linked)List\b', r'\b(?:Hash|Tree|Linked)(?:Map|Set)\b', r'\bMap\s*[<>]', r'\bSet\s*[<>]',
                                           r'\bList\s*[<>]', r'\bCollection', r'\bIterator\b', r'\bIterable\b', r'hasNext', r'Comparator',
                                           r'Comparable', r'compareTo', r'\bQueue\b', r'\bDeque\b'],
        'למבדות, ממשקים פונקציונליים ו-Streams': [r'-\s*>\s*[\w{(]', r'\bstream\b', r'\bStream\b', r'\bFunction\b', r'Predicate', r'Consumer',
                                                    r'Supplier', r'lambda', r'למבד', r'::\s*\w', r'\.filter\b', r'\.collect\b', r'\bOptional\b',
                                                    r'@FunctionalInterface'],
        'מחלקות פנימיות ואנונימיות': [r'inner class', r'מחלקה פנימית', r'מחלקות פנימיות', r'anonymous', r'אנונימי', r'\bnested\b',
                                      r'מקוננ'],
        'enum': [r'\benum\b'],
        'תבניות עיצוב (Design Patterns)': [r'Singleton', r'Factory', r'Observer', r'Strategy', r'Decorator', r'Visitor', r'Composite',
                                            r'Adapter', r'\bMVC\b', r'Builder', r'design pattern', r'תבני(?:ת|ות) עיצוב'],
        'מקביליות (Threads)': [r'\bThread\b', r'synchronized', r'Runnable', r'concurren'],
        'קלט/פלט וקבצים': [r'BufferedReader', r'FileReader', r'\bScanner\b', r'InputStream', r'\bFile\b', r'קובץ', r'קבצים'],
        'בדיקות (JUnit)': [r'@Test', r'JUnit', r'assertEquals'],
    },
    'probability': {   # for coverage and clarifications only: its questions were tagged when the exams were read
        'קומבינטוריקה וספירה': [r'קומבינטורי', r'ספירה'], 'מרחבי הסתברות סימטריים': [r'סימטרי'],
        'עקרון ההכלה וההפרדה': [r'הכלה וה'], 'הסתברות מותנה, הסתברות שלמה ובייס': [r'מותנ', r'בייס', r'הסתברות שלמה'],
        'אי-תלות': [r'אי-תלות', r'בלתי תלוי'], 'משתנים מקריים בדידים (בינומי, גאומטרי, פואסון, היפרגאומטרי)': [r'בינומ', r'גאומטרי', r'גיאומטרי', r'פואסון'],
        'משתנים מקריים רציפים (אחיד, מעריכי, נורמלי)': [r'רציף', r'מעריכי', r'צפיפות', r'נורמלי'],
        'התפלגות משותפת ומשתנים דו-ממדיים': [r'משותפת', r'דו-ממד'], 'תוחלת (לינאריות, אינדיקטורים)': [r'תוחלת', r'אינדיקטור'],
        'שונות, שונות משותפת ומתאם': [r'שונות', r'מתאם'], "אי-שוויונות מרקוב, צ'בישב וצ'רנוף": [r'מרקוב', r"צ['׳]בישב", r"צ['׳]רנוף"],
        'משפט הגבול המרכזי וקירוב נורמלי': [r'הגבול המרכזי', r'קירוב נורמלי'], 'חוקי המספרים הגדולים וסוגי התכנסות': [r'המספרים הגדולים', r'התכנסות'],
        'הלמה של בורל-קנטלי': [r'בורל', r'קנטלי'], 'פונקציות יוצרות מומנטים': [r'יוצרת מומנטים', r'יוצרות מומנטים', r'מומנט'],
        'הילוכים מקריים ובעיות מיוחדות': [r'הילוך', r'הילוכים'],
    },
    'microeconomics_2': {
        'יעילות בכלכלת חליפין (פארטו, קופסת אדג׳וורת׳)': [r'אדג', r'חליפין', r'קו החוזה', r'פארטו'],
        'יעילות בתנאי אי-ודאות': [r'אי-ודאות', r'אי ודאות', r'סיכון'], 'יעילות בכלכלה עם ייצור': [r'עקומת התמורה', r'ייצור'],
        'שיווי משקל תחרותי בכלכלת חליפין': [r'שיווי משקל תחרותי', r'וולרא'], 'שיווי משקל תחרותי בתנאי אי-ודאות': [r'נכסים', r'ביטוח'],
        'שיווי משקל תחרותי בכלכלה עם ייצור': [r'פירמ'], 'משפטי הרווחה': [r'משפט(?:י)? הרווחה'], 'מיסים וסובסידיות': [r'מיס', r'סובסיד'],
        'השפעות חיצוניות': [r'חיצוני', r'פיגו', r'קואז'], 'מוצרים ציבוריים': [r'ציבורי', r'סמואלסון', r'טרמפיסט'],
    },
}
GENERAL_FOR_TEXT = {  # too broad to place a question from its whole text (fine on the OCR's own topic line)
    'microeconomics': {'תורת הפירמה: ייצור, עלויות, רווח והיצע'}, 'linear_algebra_2': {'העתקות לינאריות (גרעין, תמונה)'},
    'intro_to_cs': {'מבני נתונים, פייתון וניתוח קוד', 'עיבוד תמונה'}, 'calculus_2': {'רציפות וטופולוגיה במרחב'},
}

BIDI = re.compile('[‎‏‪-‮⁦-⁩­]')
HEB_WORD = re.compile(r'[א-ת]{3,}|[A-Za-z_]{3,}\w*')
STOP = set('של את על עם או אם כי לא זה זו הוא היא כל גם אך רק בין לכל מתוך אחד אחת שני שתי נתון נתונה נתונים הבא הבאה '
           'הוכיחו הראו חשבו מצאו האם יהי תהי עבור כאשר אשר מספר יש אין שאלה סעיף נקודות נק הקוד הבא'.split())
EXCLUDE_RX = re.compile(r'(?:לא\s+(?:יופיע|יופיעו|יהיה|יהיו|ייכלל|ייכללו|ייבחן|ייבחנו|יבחן|נכלל(?:ים|ת)?)\s+(?:ב|ל)(?:ה)?(?:מבחן|בחינה)|'
                        r'לא\s+(?:למבחן|במבחן|בבחינה|לבחינה)|ירד(?:ו)?\s+מ(?:ה)?חומר|מחוץ\s+ל(?:חומר|מבחן)|'
                        r'הוצא(?:ו)?\s+מ(?:ה)?חומר|אינו\s+(?:חומר|נכלל)\s+ל?(?:מבחן|בחינה))')
EXAM_WORD = re.compile(r'מבחן|מבחנ|בחינ')


def _paths(root: Path | None = None):
    r = Path(root) if root else ROOT
    return r, r / 'data'


# ── loading: one record per exam: {'id','label','year','questions':[{'q','units':[{'text','topics'}],'points','bonus'}]} ──

def _year(s: str) -> int:
    m = re.search(r'(20\d\d|19\d\d)', s or '')
    return int(m.group(1)) if m else 0


def classify(ck: str, topic_line: str, body: str = '') -> list[str]:
    """Topics by the course's keyword rules: the OCR's own topic line first; nothing there → the question's text."""
    rules = RULES.get(ck) or {}
    hits = [t for t, rxs in rules.items() if any(re.search(rx, topic_line or '', re.I) for rx in rxs)]
    if hits or not body:
        return hits
    broad = GENERAL_FOR_TEXT.get(ck, set())
    score = {t: sum(len(re.findall(rx, body, re.I)) for rx in rxs) for t, rxs in rules.items() if t not in broad}
    best = max(score.values(), default=0)
    if best < 2:
        return []
    return [t for t, s in sorted(score.items(), key=lambda kv: -kv[1]) if s >= max(2, best * 0.5)][:3]


def _from_tags(ck: str, data_dir: Path) -> list[dict]:
    try:
        d = json.loads((data_dir / 'exam_topics' / f'{ck}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    exams = []
    for e in d.get('exams') or []:
        qs: dict[str, dict] = {}
        for q in e.get('questions') or []:
            main = re.match(r'\s*(\d+)', str(q.get('q')))
            key = main.group(1) if main else str(q.get('q'))
            qs.setdefault(key, {'q': key, 'units': [], 'points': None, 'bonus': False})['units'].append(
                {'text': q.get('gist') or '', 'topics': [t for t in q.get('topics') or []]})
        label = e.get('by') if e.get('by') and e.get('by') != 'אני' else ''
        exams.append({'id': e['date'], 'label': (label or 'מבחן') + f" ({e['date'][8:10]}/{e['date'][5:7]}/{e['date'][:4]})",
                      'year': _year(e['date']), 'date': e['date'], 'questions': list(qs.values())})
    return exams


SOL_RX = re.compile(r'SOL|פתרון|solution|_s$', re.I)
Q_HEAD = re.compile(r'^#{1,4}\s*\**\s*שאלה\s*(?:מספר\s*)?(\d+)\s*\**(.*)$', re.M)


def _ocr_key(stem: str) -> str:
    m = re.search(r'מועד\s*([אבג])', stem)
    if m and not re.search(r'20\d\d[AB]', stem):                     # Hebrew names: "מבחן מיקרו א- מועד א 2024-2", "פתרון …"
        return f"{_year(stem)} {m.group(1)}{'_' if '_' in stem else ''}"
    s = re.sub(r'SOL|\(פתרון\)|פתרון|מבחן\+|_מבחן\+פתרון|מבחן', ' ', stem, flags=re.I)
    s = re.sub(r'[_\-+]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _ocr_label(stem: str) -> tuple[str, int]:
    y = _year(stem)
    m = re.search(r'20\d\d([AB])(A|B|C|Sample|mid)', stem, re.I)
    if m:
        sem = 'א' if m.group(1).upper() == 'A' else 'ב'
        g = m.group(2)
        moed = {'a': 'מועד א', 'b': 'מועד ב', 'c': 'מועד ג', 'sample': 'מבחן לדוגמה', 'mid': 'אמצע'}[g.lower()]
        return f'{y} סמ׳ {sem} {moed}', y
    m = re.search(r'מועד\s*([אבג])', stem)
    return (f"{y} מועד {m.group(1)}" if m else stem[:30]) + ('' if '_' not in stem else ' (נוסח 2)'), y


def _from_ocr(ck: str, data_dir: Path) -> list[dict]:
    d = data_dir / 'exams_text' / ck
    if not d.exists():
        return []
    files = sorted(d.glob('*.md'))
    by_key: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        by_key[_ocr_key(f.stem)].append(f)
    exams, kept_words = [], []
    for key, fs in sorted(by_key.items()):
        qf = [f for f in fs if not SOL_RX.search(f.stem)] or fs          # the question paper; a solution only if alone
        f = qf[0]
        text = f.read_text(encoding='utf-8', errors='replace')
        if 'נוסחאות' in f.stem:
            continue
        ws = _words(text)
        if any(len(ws & k) / max(1, len(ws | k)) >= 0.8 for k in kept_words):   # the same paper under a second name
            continue
        kept_words.append(ws)
        heads = list(Q_HEAD.finditer(text))
        seen, qs = set(), []
        for i, h in enumerate(heads):
            n = h.group(1)
            body = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
            if n in seen:                                                    # the OCR repeated the paper: first copy only
                continue
            seen.add(n)
            line = h.group(2)
            tline = ' '.join(re.findall(r'נושא\s*:\s*([^)]*)', line)) or line
            pts = re.search(r'(\d+)\s*(?:נקודות|נק[\'׳])', line + body[:60].split('\n')[0])
            qs.append({'q': n, 'points': int(pts.group(1)) if pts else None, 'bonus': 'בונוס' in line or 'בונוס' in body[:120],
                       'units': [{'text': (tline + ' — ' + body[:1500]).strip(), 'topics': classify(ck, tline, body[:3000])}]})
        if not qs:
            continue
        label, y = _ocr_label(f.stem)
        exams.append({'id': f.stem, 'label': label, 'year': y, 'date': str(y), 'questions': qs})
    return exams


# software 1: the digital exam PDFs → text (pdftotext, free; cached) → questions → items
SW1_FILES = re.compile(r'exam_20\d\d[ab][ab]|טופס|בחינת', re.I)
SW1_SOL = re.compile(r'sol|solution|פתרון', re.I)


def _sw1_texts(data_dir: Path) -> dict[str, tuple[str, str]]:
    """{exam id: (label, text)} — one question paper per exam (Moodle's section name says which exam it is)."""
    src = data_dir / 'moodle' / 'software_1' / 'files'
    cache = data_dir / 'exams_text' / 'software_1'
    out: dict[str, tuple[str, str]] = {}
    pdfs = sorted(p for p in src.glob('*.pdf') if SW1_FILES.search(p.name)) if src.exists() else []
    for p in sorted(pdfs, key=lambda p: (bool(SW1_SOL.search(p.name.split('_', 1)[-1].split('.pdf')[0])), len(p.name))):
        m = re.search(r'מועד\s*([אב])\s*סמסטר\s*([אב])\s*(20\d\d)', p.name)
        if not m:
            continue
        eid = f'{m.group(3)}{"a" if m.group(2) == "א" else "b"}{"a" if m.group(1) == "א" else "b"}'
        if eid in out:
            continue
        own = p.name.split('.pdf')[0]
        if SW1_SOL.search(own) and any(SW1_FILES.search(q.name) and not SW1_SOL.search(q.name.split('.pdf')[0])
                                       and eid_of(q) == eid for q in pdfs):
            continue
        txt_path = cache / f'{eid}.txt'
        text = ''
        if txt_path.exists():
            text = txt_path.read_text(encoding='utf-8', errors='replace')
        elif shutil.which('pdftotext'):
            try:
                text = subprocess.run(['pdftotext', '-layout', str(p), '-'], capture_output=True, timeout=60).stdout.decode('utf-8', 'replace')
                if len(text) > 2000:
                    cache.mkdir(parents=True, exist_ok=True)
                    txt_path.write_text(text, encoding='utf-8')
            except (OSError, subprocess.SubprocessError):
                text = ''
        if len(text) > 2000:
            out[eid] = (f'{m.group(3)} סמ׳ {m.group(2)} מועד {m.group(1)}', text)
    return out


def eid_of(p: Path) -> str:
    m = re.search(r'מועד\s*([אב])\s*סמסטר\s*([אב])\s*(20\d\d)', p.name)
    return f'{m.group(3)}{"a" if m.group(2) == "א" else "b"}{"a" if m.group(1) == "א" else "b"}' if m else ''


def _sw1_split(text: str) -> list[dict]:
    """The question headers ("שאלה 2 (20 נק')", in whatever order pdftotext put the RTL words), then items ("סעיף ב'")."""
    lines = BIDI.sub('', text).splitlines()
    heads = []                                     # (line index, q number, points, design?)
    want = 1
    intro_design = bool(re.search(r'שאלת\s*\S*\s*Design|Design\S*\s*\(?\s*20', ' '.join(lines[:80])))
    for i, ln in enumerate(lines):
        s = re.sub(r'\s+', '', ln)                  # "ש א ל ה  5 2 ) 3נק '(" (letter-spaced) reads like the others
        if 'שאלה' not in s or len(s) > 45 or 'בשאלה' in s or 'סעיפ' in s:
            continue
        if re.search(r'B,C|זכאים', s):                                      # the extra question for groups B,C: not the exam
            continue
        nums = [int(x) for x in re.findall(r'\d+', s)]
        if not nums or 'נק' not in s:
            continue
        qn, pts = (min(nums), max(nums)) if len(nums) > 1 else (nums[0], None)
        if qn != want:
            continue
        nxt = ' '.join(lines[i:i + 8])
        heads.append((i, qn, pts, bool(re.search(r'Design', nxt)) or (intro_design and qn == 2 and pts == 20)))
        want += 1
    qs = []
    for k, (i, qn, pts, design) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        body = lines[i + 1:end]
        cut = [j for j, ln in enumerate(body) if re.search(r'סעיף\s*[א-ת][\'׳]?', ln) and len(ln.strip()) < 40]
        parts = [body[a:b] for a, b in zip(cut, cut[1:] + [len(body)])] if len(cut) >= 2 else [body]
        units = []
        for part in parts:
            t = '\n'.join(part)
            if len(t.strip()) < 30:
                continue
            tops = ['שאלת Design (תכנון מחלקות ומנשקים)'] if design else classify('software_1', '', t)
            units.append({'text': re.sub(r'\s+', ' ', t)[:1500], 'topics': tops})
        if units:
            qs.append({'q': str(qn), 'points': pts, 'bonus': False, 'units': units})
    return qs


def _from_sw1(data_dir: Path) -> list[dict]:
    exams = []
    for eid, (label, text) in sorted(_sw1_texts(data_dir).items()):
        qs = _sw1_split(text)
        if qs:
            exams.append({'id': eid, 'label': label, 'year': int(eid[:4]), 'date': eid, 'questions': qs})
    return exams


def load_exams(ck: str, root: Path | None = None) -> list[dict]:
    _, data_dir = _paths(root)
    if ck in ('probability', 'microeconomics_2'):
        return _from_tags(ck, data_dir)
    if ck == 'software_1':
        return _from_sw1(data_dir)
    if ck in RULES:
        return _from_ocr(ck, data_dir)
    return []


# ── coverage: taught this semester? excluded by a clarification? ─────────────────────────────────────────────────────

def _summaries(ck: str, data_dir: Path) -> list[tuple[str, str]]:
    out = []
    for sub in ('lectures', 'tutorials'):
        for p in sorted((data_dir / 'summaries' / ck / sub).glob('*.md')):
            try:
                out.append((p.stem, p.read_text(encoding='utf-8', errors='replace')))
            except OSError:
                pass
    return out


def coverage(ck: str, topics: list[str], root: Path | None = None, vault_dir: Path | None = None) -> dict:
    """{topic: {'state': taught|not_yet|cut|unknown, 'where': [...], 'excluded': [quote…]}} — words only, no model."""
    _, data_dir = _paths(root)
    sums = _summaries(ck, data_dir)
    mapped: set[str] = set()
    try:                                            # the course site's map links each taught topic to its exam units
        m = json.loads((data_dir / 'site_topics' / ck / 'map.json').read_text(encoding='utf-8'))
        for t in m.get('topics') or []:
            mapped.update(t.get('exam_topics') or [])
    except (OSError, ValueError):
        pass
    rules = RULES.get(ck) or {}
    res = {}
    for t in topics:
        rx = rules.get(t) or []
        if mapped:                                  # the map knows: only its links count (words alone over-match)
            where = []
        else:                                       # no map: the topic's words, at least 3 times in one summary
            where = [stem for stem, txt in sums if sum(len(re.findall(r, txt, re.I)) for r in rx) >= 3] if rx else []
        if t in mapped or where:
            state = 'taught'
        elif not sums or (not rx and not mapped):    # a format, not a topic (software 1's Design question)
            state = 'unknown'
        elif ck in CURRENT:
            state = 'not_yet'
        else:
            state = 'cut'
        res[t] = {'state': state, 'where': where[:3], 'excluded': []}
    # clarifications: "X לא יופיע במבחן" in this semester's summaries or the course's announcements
    texts = list(sums)
    vd = vault_dir
    if vd is None:
        try:
            from config.paths import VAULT_DIR as vd
        except Exception:
            vd = None
    if vd:
        for p in sorted((Path(vd) / '📢 הודעות' / ck).glob('*.md')):
            try:
                texts.append(('הודעה ' + p.stem, p.read_text(encoding='utf-8', errors='replace')))
            except OSError:
                pass
    general = []
    for stem, txt in texts:
        for sent in re.split(r'(?<=[.!?\n])\s+', txt):
            if len(sent) > 400 or not EXCLUDE_RX.search(sent) or not EXAM_WORD.search(sent):
                continue
            s = re.sub(r'[*_>#`]+', '', sent).strip()
            hit = [t for t in topics if any(re.search(r, s, re.I) for r in rules.get(t) or [])]
            for t in hit:
                if len(res[t]['excluded']) < 2:
                    res[t]['excluded'].append(f'{s[:220]} ({stem[:40]})')
            if not hit and len(general) < 5:
                general.append(f'{s[:220]} ({stem[:40]})')
    return {'topics': res, 'general': general, 'n_summaries': len(sums)}


# ── statistics ────────────────────────────────────────────────────────────────────────────────────────────────────

def _words(t: str) -> set[str]:
    return {w.lower() for w in HEB_WORD.findall(t or '') if w not in STOP}


def _clusters(units: list[tuple[dict, dict, dict]], min_exams: int = 2, thr: float = 0.5) -> list[list[int]]:
    """Near-identical questions across exams: a question joins a group only when it is that close (word-set overlap >= thr)
    to the group's first question, so a chain of loosely similar questions never becomes one group. Indices into units."""
    ws = [_words(u['text']) for _, _, u in units]
    groups: list[list[int]] = []
    for i in range(len(units)):
        if len(ws[i]) < 6:
            continue
        for g in groups:
            j = g[0]
            if units[i][0]['id'] not in {units[k][0]['id'] for k in g} and len(ws[i] & ws[j]) / len(ws[i] | ws[j]) >= thr:
                g.append(i)
                break
        else:
            groups.append([i])
    return [g for g in groups if len({units[i][0]['id'] for i in g}) >= min_exams]


def stats(ck: str, exams: list[dict]) -> dict:
    exams = sorted(exams, key=lambda e: (e.get('date') or '', e['id']))
    units = [(e, q, u) for e in exams for q in e['questions'] if not q.get('bonus') for u in q['units']]
    n_ex, n_u = len(exams), len(units)
    years = sorted({e['year'] for e in exams if e['year']})
    recent_from = (years[-1] - 4) if years else 0
    recent_ex = [e for e in exams if e['year'] >= recent_from]
    recent_u = [x for x in units if x[0]['year'] >= recent_from]
    topics = list(dict.fromkeys([t for _, _, u in units for t in u['topics']]))
    rows = []
    for t in topics:
        tu = [x for x in units if t in x[2]['topics']]
        te = {x[0]['id'] for x in tu}
        ty = sorted({x[0]['year'] for x in tu if x[0]['year']})
        rows.append({'topic': t, 'units': len(tu), 'pct': round(100 * len(tu) / n_u) if n_u else 0,
                     'exams': len(te), 'exam_pct': round(100 * len(te) / n_ex) if n_ex else 0,
                     'recent_pct': round(100 * sum(1 for x in recent_u if t in x[2]['topics']) / len(recent_u)) if recent_u else 0,
                     'recent_exams': len({x[0]['id'] for x in recent_u if t in x[2]['topics']}),
                     'years': ty, 'last': max((x[0].get('date') or '' for x in tu), default='')})
    rows.sort(key=lambda r: (-r['recent_pct'], -r['pct'], r['topic']))
    unclassified = [x for x in units if not x[2]['topics']]
    # what comes back: topics in (almost) every exam, kinds of question (topic sets) in many years, near-identical questions
    fixed_topics = [r for r in rows if n_ex >= 3 and r['exam_pct'] >= 75]
    kinds = defaultdict(lambda: {'years': set(), 'exams': set(), 'ex': ''})
    for e, q, u in units:
        if u['topics']:
            k = ' + '.join(sorted(u['topics']))
            kinds[k]['years'].add(e['year'])
            kinds[k]['exams'].add(e['id'])
            kinds[k]['ex'] = kinds[k]['ex'] or (u['text'] if ck in ('probability', 'microeconomics_2') else '')
    fixed_kinds = sorted(([k, sorted(v['years']), len(v['exams']), v['ex']] for k, v in kinds.items()
                          if len(years) >= 3 and len(v['years']) >= max(3, round(0.5 * len(years)))),
                         key=lambda x: (-len(x[1]), -x[2]))[:6]
    near = []
    for g in _clusters(units):
        ex = units[g[0]]
        gist = ex[2]['text']
        m = re.search(r'סעיף\s*[א-ת][\'׳]?\s*[:?]?', gist[:40])
        gist = re.sub(r'^[\s:?.()\'׳]+', '', gist[m.end():] if m else gist)
        near.append({'years': sorted({units[i][0]['year'] for i in g}), 'exams': sorted({units[i][0]['label'] for i in g}),
                     'topics': sorted({t for i in g for t in units[i][2]['topics']}), 'q': ex[1]['q'],
                     'gist': re.sub(r'\s+', ' ', gist)[:160]})
    near.sort(key=lambda c: (-len(c['years']), -len(c['exams'])))
    # structure: questions per exam, items, points
    per = [{'label': e['label'], 'questions': sum(1 for q in e['questions'] if not q.get('bonus')),
            'items': sum(len(q['units']) for q in e['questions'] if not q.get('bonus')),
            'points': [q['points'] for q in e['questions'] if not q.get('bonus')],
            'bonus': sum(1 for q in e['questions'] if q.get('bonus'))} for e in exams]
    qc = Counter(p['questions'] for p in per)
    rc = Counter(p['questions'] for p in per if any(e['label'] == p['label'] and e['year'] >= recent_from for e in exams))
    typical = (rc or qc).most_common(1)[0][0] if per else 0
    pts_rows = [tuple(p['points']) for p in per if p['questions'] == typical and all(p['points'])]
    return {'course': ck, 'name': NAMES.get(ck, ck), 'n_exams': n_ex, 'n_units': n_u, 'years': years, 'recent_from': recent_from,
            'n_recent': len(recent_ex), 'rows': rows, 'fixed_topics': [r['topic'] for r in fixed_topics], 'fixed_kinds': fixed_kinds,
            'near': near[:8], 'per_exam': per, 'typical_questions': typical, 'q_range': [min(qc), max(qc)] if qc else [0, 0],
            'typical_points': list(Counter(pts_rows).most_common(1)[0][0]) if pts_rows else [],
            'unclassified': len(unclassified),
            'unclassified_examples': [f"{e['label']} ש׳ {q['q']}: {re.sub(chr(10), ' ', u['text'])[:90]}" for e, q, u in unclassified[:5]],
            'unit_word': 'סעיפים' if ck in ('software_1', 'microeconomics_2') else 'שאלות'}


# ── the note and the short answer ─────────────────────────────────────────────────────────────────────────────────

STATE_HE = {'taught': '✅ נלמד', 'not_yet': '⏳ עוד לא נלמד', 'cut': '❌ לא נמצא בסיכומים — כנראה ירד', 'unknown': '—'}


def _yrs(ys: list[int]) -> str:
    return ', '.join(str(y) for y in ys)


def note_rel(ck: str) -> str:
    safe = re.sub(r'[\\/:*?<>|#^]+', '', NAMES.get(ck, ck).replace('"', '״'))
    return f'{NOTE_DIR}/{safe}.md'


def hub_rel(ck: str, vault_dir: Path | None) -> str:
    """The course's hub note (vault_sync/maps), path without .md; '' when the course folder isn't there."""
    try:
        from config.paths import VAULT_COURSE_FOLDER_TO_KEY, COURSE_NAMES
        for f, k in VAULT_COURSE_FOLDER_TO_KEY.items():
            if k == ck and vault_dir and (Path(vault_dir) / f).is_dir():
                safe = re.sub(r'[\\/:*?"<>|#^]+', '', COURSE_NAMES.get(ck, f))
                return f'{f}/🧭 {safe} — מפה'
    except Exception:
        pass
    return ''


def note_link(ck: str) -> str:
    try:
        from config.paths import VAULT_ROOT, VAULT_DIR
        rel = f'{VAULT_DIR.name}/{note_rel(ck)}'
        return f'obsidian://open?vault={quote(VAULT_ROOT.name)}&file={quote(rel[:-3])}'
    except Exception:
        return ''


def render(s: dict, cov: dict | None, hub: str = '') -> str:
    ck, name = s['course'], s['name']
    back = f"↩️ [[{NOTE_DIR}/📊 מבחנים|📊 מבחנים]]" + (f" · [[{hub}|🧭 {name} — מפה]]" if hub else '')
    if not s['n_exams']:
        return (f"# 📊 {name} — על מה לשים את הכסף במבחן\n\n{back}\n\n"
                "אין לי עדיין מבחנים קודמים בקורס הזה (במודל אין תיקיית מבחנים, ולא קיבלתי מבחנים בדרך אחרת).\n"
                "כשיעלו מבחנים — אבנה כאן את אותה תמונה: אחוז השאלות לפי נושא, מה חוזר, ומבנה המבחן.\n")
    ct = (cov or {}).get('topics') or {}
    L = [f"# 📊 {name} — על מה לשים את הכסף במבחן", '', back, '',
         f"> מבוסס על *{s['n_exams']} מבחנים* ({s['years'][0]}–{s['years'][-1]}), {s['n_units']} {s['unit_word']}. "
         f"\"5 שנים אחרונות\" = {s['recent_from']} ואילך ({s['n_recent']} מבחנים). נבנה בקוד מהמבחנים עצמם, בלי AI. "
         f"שאלה יכולה להיות בכמה נושאים, לכן האחוזים לא מסתכמים ל-100."]
    if s['unclassified']:
        L.append(f"> {UNCLASSIFIED}: {s['unclassified']} מתוך {s['n_units']} {s['unit_word']} (פירוט בסוף).")
    top = [r for r in s['rows'] if ct.get(r['topic'], {}).get('state') not in ('cut',) and not ct.get(r['topic'], {}).get('excluded')][:3]
    L += ['', '## 🎯 בשורה אחת', '',
          'הכי הרבה כסף: ' + ' · '.join(f"*{r['topic']}* ({r['recent_pct']}% מה{s['unit_word']} בשנים האחרונות)" for r in top), '',
          '## 📈 אחוז השאלות לפי נושא', '',
          f"| נושא | % מה{s['unit_word']} (5 שנים) | % מה{s['unit_word']} (הכול) | במבחנים | לאחרונה | הסמסטר |",
          '|---|---|---|---|---|---|']
    for r in s['rows']:
        c = ct.get(r['topic'], {})
        st = '🚫 הבהרה: לא למבחן' if c.get('excluded') else STATE_HE.get(c.get('state'), '—')
        L.append(f"| {r['topic']} | {r['recent_pct']}% | {r['pct']}% | {r['exams']}/{s['n_exams']} | {str(r['last'])[:4]} | {st} |")
    L += ['', '## 🔁 מה חוזר כמעט כל שנה', '']
    if s['fixed_topics']:
        for t in s['fixed_topics']:
            r = next(x for x in s['rows'] if x['topic'] == t)
            L.append(f"- 🔥 *{t}* — ב-{r['exams']} מתוך {s['n_exams']} מבחנים. שנים: {_yrs(r['years'])}")
    else:
        L.append('- אין נושא שמופיע בכמעט כל מבחן.')
    if s['fixed_kinds']:
        L += ['', '*סוגי שאלה קבועים* (אותו שילוב נושאים, ברוב השנים):']
        for k, ys, ne, ex in s['fixed_kinds']:
            L.append(f"- {k} — {ne} פעמים, ב-{len(ys)} שנים ({_yrs(ys)})" + (f". למשל: {ex[:110]}" if ex else ''))
    if s['near']:
        L += ['', '*שאלות שחזרו כמעט כמו שהן* (ניסוח דומה מאוד):']
        for c in s['near']:
            L.append(f"- {', '.join(c['topics']) or UNCLASSIFIED} — {', '.join(c['exams'])}: \"{c['gist'][:120]}…\"")
    L += ['', '## 🧱 מבנה מבחן טיפוסי', '']
    rng = s['q_range']
    L.append(f"- בדרך כלל *{s['typical_questions']} שאלות* (בין {rng[0]} ל-{rng[1]} לאורך השנים)."
             + (f" ניקוד טיפוסי: {' / '.join(str(p) for p in s['typical_points'])}." if s['typical_points'] else ''))
    if ck == 'software_1':
        L.append('- התבנית הקבועה מ-2024: שאלה 1 — 7–8 שאלות אמריקאיות (4 נק׳ כל אחת), שאלה 2 — שאלת Design (20 נק׳), '
                 'שאלה 3 — שאלה פתוחה בכמה סעיפים (52–55 נק׳). ב-2023: שתי שאלות פתוחות ו-10 אמריקאיות.')
    L += ['', '| מבחן | שאלות | סעיפים | ניקוד |', '|---|---|---|---|']
    for p in reversed(s['per_exam']):
        pts = ' / '.join(str(x) for x in p['points'] if x) or '—'
        L.append(f"| {p['label']} | {p['questions']}{' (+בונוס)' if p['bonus'] else ''} | {p['items']} | {pts} |")
    L += ['', '## 🚫 מה לא רלוונטי', '']
    cut = [t for t, c in ct.items() if c['state'] == 'cut']
    nyet = [t for t, c in ct.items() if c['state'] == 'not_yet']
    excl = [(t, c['excluded']) for t, c in ct.items() if c['excluded']]
    if excl:
        for t, qs in excl:
            L.append(f"- 🚫 *{t}* — הבהרה: {qs[0]}")
    if cut:
        L.append('- ❌ לא נמצאו בסיכומי הסמסטר (כנראה ירדו בגלל המלחמה — לוודא מול המרצה): ' + ', '.join(cut))
    if nyet:
        L.append('- ⏳ עוד לא נלמדו הסמסטר (הסמסטר באמצע — ייתכן שיגיעו): ' + ', '.join(nyet))
    if not (excl or cut or nyet):
        L.append('- לא מצאתי נושא שירד או שהוצא מהחומר.')
    gen = (cov or {}).get('general') or []
    if gen:
        L += ['', '*הבהרות כלליות על החומר למבחן* (מהסיכומים / ההודעות, לבדוק לבד):'] + [f'- {g}' for g in gen[:4]]
    if s['unclassified']:
        L += ['', f"## ❓ {UNCLASSIFIED}", '',
              f"{s['unclassified']} {s['unit_word']} שהכללים לא שייכו לנושא (לא ניחשתי). דוגמאות:"]
        L += [f'- {x}' for x in s['unclassified_examples']]
    return '\n'.join(L) + '\n'


def build(ck: str, root: Path | None = None, vault_dir: Path | None = None) -> dict:
    exams = load_exams(ck, root)
    s = stats(ck, exams) if exams else {'course': ck, 'name': NAMES.get(ck, ck), 'n_exams': 0, 'n_units': 0, 'rows': [],
                                        'unclassified': 0, 'fixed_topics': [], 'fixed_kinds': [], 'near': []}
    topics = [r['topic'] for r in s['rows']]
    s['coverage'] = coverage(ck, topics, root, vault_dir) if topics else {'topics': {}, 'general': []}
    s['note'] = note_rel(ck)
    return s


def build_all(root: Path | None = None, vault_dir: Path | None = None, write_vault: bool = True) -> dict:
    _, data_dir = _paths(root)
    out = data_dir / OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    if vault_dir is None:
        from config.paths import VAULT_DIR as vault_dir
    from vault_sync.notewriter import Writer
    w = Writer(Path(vault_dir))
    res = {}
    for ck in list(CURRENT) + [c for c in NAMES if c not in CURRENT]:
        s = build(ck, root, vault_dir)
        (out / f'{ck}.json').write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding='utf-8')
        if write_vault and (s['n_exams'] or ck in CURRENT):
            w.note(note_rel(ck), {'type': 'exam_plan', 'title': f"{NAMES[ck]} — על מה לשים את הכסף", 'course_key': ck,
                                  'source': 'assistant', 'tags': ['מבחנים', 'סטטיסטיקה', ck]},
                   render(s, s['coverage'], hub_rel(ck, vault_dir)))
        res[ck] = {'exams': s['n_exams'], 'units': s['n_units'], 'unclassified': s['unclassified']}
    if write_vault:
        idx = ['# 📊 מבחנים — על מה לשים את הכסף', '', 'לכל קורס: אחוז השאלות לפי נושא, מה חוזר כל שנה, מבנה המבחן ומה לא רלוונטי. '
               'נבנה בקוד מהמבחנים הקודמים, בלי AI.', '', '## הסמסטר', '']
        for ck in CURRENT:
            idx.append(f"- [[{note_rel(ck)[:-3]}|{NAMES[ck]}]] — {res[ck]['exams']} מבחנים")
        idx += ['', '## קורסים קודמים', '']
        for ck in NAMES:
            if ck not in CURRENT and res.get(ck, {}).get('exams'):
                idx.append(f"- [[{note_rel(ck)[:-3]}|{NAMES[ck]}]] — {res[ck]['exams']} מבחנים")
        w.note(f'{NOTE_DIR}/📊 מבחנים.md', {'type': 'exam_plan_index', 'title': 'מבחנים — על מה לשים את הכסף',
                                            'source': 'assistant', 'tags': ['מבחנים', 'MOC']}, '\n'.join(idx))
    return res


# ── the bot: only when he asks ───────────────────────────────────────────────────────────────────────────────────────

ASK_RX = re.compile(r'(?:לשים|לשפוך)\s+(?:את\s+)?(?:ה)?כסף|שאל(?:ה|ות) קבוע|חוזר(?:ות|ים)? (?:כל שנה|במבח[נן])|'
                    r'סטטיסטיק\S* (?:של )?(?:ה)?מבח[נן]|(?:תוכנית|תכנית) (?:למידה|לימוד) לפי (?:ה)?מבח[נן]|'
                    r'(?:ה)?מבנה (?:של )?(?:ה)?מבח[נן]|כמה שאלות (?:יש )?(?:ב|יש ב)מבח[נן]|מה הכי (?:שואלים|נפוץ|שכיח|מופיע) במבח[נן]')
WEAK_RX = re.compile(r'על מה (?:לשים|להתמקד|להשקיע)|(?:מה|אילו|איזה)\s+(?:שאלות\s+|נושאים\s+)?(?:הכי\s+)?(?:חוזר|חוזרות|חוזרים|קבוע|קבועות)|'
                     r'אחוז(?:ים)? (?:מה)?שאלות')   # these only with a word about exams beside them


def course_of(text: str) -> str | None:
    for ck, rx in COURSE_RX:
        if re.search(rx, text or '', re.I):
            return ck
    return None


def load(ck: str, root: Path | None = None) -> dict:
    _, data_dir = _paths(root)
    try:
        return json.loads((data_dir / OUT_DIR / f'{ck}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def short(ck: str, root: Path | None = None) -> str:
    s = load(ck, root)
    name = NAMES.get(ck, ck)
    if not s:
        return f'עוד לא בניתי תמונת מבחנים ל{name}.'
    link = note_link(ck)
    tail = f"\n\n📎 הפירוט המלא: {s.get('note', note_rel(ck))[:-3]}" + (f'\n{link}' if link else '')
    if not s.get('n_exams'):
        return f'📊 *{name}*: אין לי עדיין מבחנים קודמים בקורס הזה, אז אין סטטיסטיקה. כשיעלו מבחנים — אבנה.' + tail
    ct = (s.get('coverage') or {}).get('topics') or {}
    ok = [r for r in s['rows'] if ct.get(r['topic'], {}).get('state') != 'cut' and not ct.get(r['topic'], {}).get('excluded')]
    uw = s.get('unit_word', 'שאלות')
    L = [f"📊 *{name} — על מה לשים את הכסף* ({s['n_exams']} מבחנים, {s['years'][0]}–{s['years'][-1]})", '']
    for r in ok[:5]:
        st = ct.get(r['topic'], {}).get('state')
        L.append(f"• *{r['topic']}* — {r['recent_pct']}% מה{uw} בשנים האחרונות, ב-{r['exams']}/{s['n_exams']} מבחנים"
                 + (' ⏳ עוד לא נלמד' if st == 'not_yet' else ''))
    if s.get('fixed_topics'):
        t = s['fixed_topics'][0]
        r = next(x for x in s['rows'] if x['topic'] == t)
        L += ['', f"🔁 *קבוע:* {', '.join(s['fixed_topics'][:3])} — כמעט בכל מבחן (שנים: {_yrs(r['years'][-6:])})"]
    elif s.get('fixed_kinds'):
        k, ys, ne, _ = s['fixed_kinds'][0]
        L += ['', f"🔁 *חוזר:* {k} — ב-{len(ys)} שנים ({_yrs(ys[-6:])})"]
    if s.get('near'):
        c = s['near'][0]
        L.append(f"♻️ שאלה שחזרה כמעט כמו שהיא: {', '.join(c['topics'][:2]) or UNCLASSIFIED} ({_yrs(c['years'])})")
    L.append(f"🧱 מבנה: בדרך כלל {s['typical_questions']} שאלות"
             + (f" ({' / '.join(str(p) for p in s['typical_points'])} נק׳)" if s.get('typical_points') else ''))
    cut = [t for t, c in ct.items() if c['state'] == 'cut' or c.get('excluded')]
    if cut:
        L.append(f"🚫 לא רלוונטי: {', '.join(cut[:4])}" + (f' ועוד {len(cut) - 4}' if len(cut) > 4 else ''))
    if s.get('unclassified'):
        L.append(f"❓ {UNCLASSIFIED}: {s['unclassified']} {uw}")
    return '\n'.join(L) + tail


def answer(text: str) -> str | None:
    """His own question about a course's exams → the short version. None = not this question."""
    if not (ASK_RX.search(text or '') or (WEAK_RX.search(text or '') and EXAM_WORD.search(text or ''))):
        return None
    ck = course_of(text)
    if ck:
        return short(ck)
    have = [c for c in CURRENT if load(c).get('n_exams')]
    return ('📊 על איזה קורס? יש לי תמונת מבחנים ל' + ', '.join(NAMES[c] for c in have)
            + '. למשל: "על מה לשים את הכסף בהסתברות?"') if have else None


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(ROOT))
    for ck, r in build_all().items():
        print(f"{ck:18s} exams={r['exams']:3d} units={r['units']:4d} unclassified={r['unclassified']}")
