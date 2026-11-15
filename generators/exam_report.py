# -*- coding: utf-8 -*-
"""generators/exam_report.py — "What did the lecturers/TAs say about the exam?"

Scans every lecture + tutorial summary of a course, pulls out everything the
lecturer/TA said that is relevant to the exam (explicit exam remarks + the
professor-tips / exam-tips sections the summarizer already extracts), and asks
Gemini to synthesize one clean, de-duplicated exam-prep report per course.
Gemini-only.
"""
from __future__ import annotations
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

COURSE_HE = {
    'intro_to_cs': 'מבוא מורחב למדעי המחשב', 'calculus_2': 'חשבון דיפרנציאלי ואינטגרלי 2',
    'discrete_math_2': 'מתמטיקה בדידה 2', 'linear_algebra_2': 'אלגברה לינארית 2',
    'microeconomics': 'מיקרו כלכלה א',
}

_LINE_KW = ('מבחן', 'למבחן', 'במבחן', 'לבחינה', 'בבחינה', 'הדגיש', 'יופיע', 'יבוא',
            'חשוב לזכור', 'טעות נפוצה', 'שימו לב', 'בטוח יבוא', 'נפוץ במבחן')
_SECTION_HDR = re.compile(r'^#{2,4}\s*.*(טיפ.*מרצה|טיפים למבחן|הדברים החשובים|הערות הפרופסור|'
                          r'דגשים.*בחינה|טיפי זהב|זהב.*מרצה|למבחן)')


def _extract_exam_bits(course_key: str, max_chars: int = 22000) -> str:
    base = ROOT / 'data' / 'summaries' / course_key
    out = []
    for sub in ('lectures', 'tutorials'):
        d = base / sub
        if not d.exists():
            continue
        for md in sorted(d.glob('*.md')):
            if md.stat().st_size < 2000:
                continue
            try:
                lines = md.read_text(encoding='utf-8', errors='replace').splitlines()
            except Exception:
                continue
            picked, in_section, sec_level = [], False, 0
            for ln in lines:
                s = ln.strip()
                h = re.match(r'^(#{1,6})\s', s)
                if h:
                    if _SECTION_HDR.match(s):
                        in_section, sec_level = True, len(h.group(1))
                        picked.append(s)
                        continue
                    elif in_section and len(h.group(1)) <= sec_level:
                        in_section = False
                if in_section and s:
                    picked.append(s)
                elif any(kw in s for kw in _LINE_KW) and len(s) > 8:
                    picked.append(s)
            if picked:
                kind = 'תרגול' if sub == 'tutorials' else 'הרצאה'
                out.append(f'\n### [{kind}] {md.stem}\n' + '\n'.join(picked[:60]))
    return '\n'.join(out)[:max_chars]


def build_exam_report(course_key: str) -> str:
    course_he = COURSE_HE.get(course_key, course_key)
    bits = _extract_exam_bits(course_key)
    if len(bits) < 150:
        return f'⚠️ לא מצאתי מספיק הערות-מבחן בסיכומים של {course_he}.'
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        return '⚠️ אין מפתח Gemini זמין.'
    prompt = (
        f'אתה מכין *דוח מבחן* לקורס *{course_he}* — ריכוז של *כל מה שהמרצים והמתרגלים אמרו '
        f'על המבחן* לאורך הסמסטר. המקור: קטעים שחולצו מסיכומי ההרצאות והתרגולים '
        f'(הערות ישירות על המבחן + סעיפי טיפים/דגשים של המרצה).\n\n'
        f'בנה דוח מסודר וממוקד-מבחן בעברית, לפי המבנה:\n\n'
        f'## 🎯 מה יבוא במבחן / הודגש\n'
        f'- נושאים ותתי-נושאים שהמרצה/מתרגל ציין במפורש שחשובים/יופיעו. צטט כשאפשר, וציין מאיזו הרצאה.\n\n'
        f'## ⚠️ טעויות נפוצות ומלכודות\n'
        f'- דברים שהזהירו מהם.\n\n'
        f'## 💡 טיפים וטריקים לפתרון\n'
        f'- שיטות/קיצורי-דרך שהמרצים נתנו.\n\n'
        f'## 📌 דגשים לפי נושא\n'
        f'- אם רלוונטי, קבץ הדגשים לפי נושא.\n\n'
        f'כללים: *אחד כפילויות* (אותה הערה מכמה שיעורים — פעם אחת). '
        f'תמציתי, ענייני, בלי LaTeX מסובך. אם משהו לא ברור מהמקור — אל תמציא.\n\n'
        f'=== ההערות שחולצו ===\n{bits}'
    )
    try:
        from google import genai
        from google.genai import types as gt
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=gt.GenerateContentConfig(
                max_output_tokens=4000, temperature=0.2,
                thinking_config=gt.ThinkingConfig(thinking_budget=0)))
        try:
            from utils.usage_tracker import log_gemini
            log_gemini(resp, 'gemini-2.5-flash', op='exam_report')
        except Exception:
            pass
        out = (resp.text or '').strip()
        if not out:
            return '⚠️ לא הצלחתי לבנות את הדוח.'
        return f'📝 *דוח מבחן — {course_he}*\n_(כל מה שהמרצים והמתרגלים אמרו על המבחן)_\n\n{out}'
    except Exception as e:
        return f'⚠️ שגיאת Gemini: {e}'


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(ROOT))
    try:
        from utils.env import _load_env_dotfile
        _load_env_dotfile()
    except Exception:
        pass
    ck = sys.argv[1] if len(sys.argv) > 1 else 'microeconomics'
    print(build_exam_report(ck))
