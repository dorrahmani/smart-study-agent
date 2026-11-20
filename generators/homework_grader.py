"""generators/homework_grader.py — Grade a homework solution step by step.

Takes one or more images (or a multipage PDF) of a solved exercise and returns
structured Hebrew feedback:
  • הצהרת הבעיה (the problem statement, i.e. what was identified)
  • סקירת הצעדים (step review, ✓/✗ for each)
  • שגיאות שזוהו (errors identified) with an explanation of *why*
  • רמז להמשך (a hint going forward; not a full solution, unless user_wants_solution=True)
  • ציון משוער (estimated grade, 0-100)

Uses gemini-2.5-pro for quality (homework grading is a high-stakes use case).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent


GRADER_SYSTEM = """אתה עוזר הוראה (Teaching Assistant) שבודק תרגיל פתור של סטודנט באוניברסיטה.
המטרה: לתת *משוב חינוכי* — לא רק "נכון/לא נכון", אלא הסבר מדוע ואיך לתקן.

═══════════════════════════════════
פורמט המשוב (חובה):
═══════════════════════════════════

📝 *מה זיהיתי שאתה פותר:*
<משפט אחד-שניים: מה השאלה / איזה סוג תרגיל>

🔍 *סקירת הצעדים:*
<עבור כל צעד מספרי או לוגי בפתרון של הסטודנט — שורה אחת:>
  ✅ צעד 1: <תיאור> — תקין
  ✅ צעד 2: <תיאור> — תקין
  ❌ צעד 3: <תיאור> — שגיאה. <משפט הסבר ספציפי>
  ⚠️ צעד 4: <תיאור> — מתבסס על שגיאה מצעד 3
  ...

(אם הכל תקין — סמן את כולם ✅)

💡 *השגיאה העיקרית:*
<אם זוהתה שגיאה: הסבר *מדוע* הסטודנט טעה — מה ההיגיון השגוי, איזה תנאי פוספס, איזה חוק הופעל לא נכון. 2-4 משפטים.>
<אם הכל תקין: דלג על הסעיף.>

🎯 *רמז להמשך:*
<אם יש שגיאה — תן רמז של *כיוון* (לא פתרון מלא!) לאן ללכת. הסטודנט אמור לחשוב בעצמו.>
<אם הכל תקין — דלג.>

📊 *הערכה כללית:*
ציון: <0-100>
<משפט שתמצת: רמת הבנה, איכות כתיבה, מה לעבוד עליו>

═══════════════════════════════════
כללי משוב:
═══════════════════════════════════

1. **קצר וענייני** — אל תפלט.
2. **בעברית.**
3. **אסור LaTeX מסובך בטלגרם** — השתמש ב-Unicode (λ, ⊆, ², √, etc.). אם מטריצה:
   ```
   [1 0]
   [0 1]
   ```
4. **כן** תפנה לחוקים ספציפיים: "לפי משפט קיילי-המילטון", "ההגדרה אומרת ש..."
5. **אל תיתן את הפתרון המלא** אלא אם המשתמש ביקש זאת במפורש (caption כולל "תפתור" / "תן פתרון מלא").
6. **אם התמונה לא ברורה** — אמור זאת בכנות, בקש שיצלם שוב.
7. **אם זה לא תרגיל פתור** (למשל רק שאלה, או לוח של מרצה) — אמור שאינך רואה פתרון לבדוק.
"""


def grade_homework(
    images: Iterable[bytes | str | Path],
    course_hint: str = '',
    user_caption: str = '',
    want_full_solution: bool = False,
    model: str = 'gemini-2.5-pro',
) -> str:
    """Grade a homework solution. Returns Hebrew markdown ready for Telegram.

    Args:
        images: iterable of image bytes (jpg/png) OR file paths. Multiple → multipage.
        course_hint: e.g. 'linear_algebra_2' or 'חדו"א 2' — helps Gemini ground the topic.
        user_caption: free-text from the user's Telegram caption (instructions).
        want_full_solution: if True, also include the full correct solution.
"""
    from google import genai
    from google.genai import types as genai_types

    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        return '⚠️ אין מפתח Gemini.'

    # Build prompt
    extra = []
    if course_hint:
        extra.append(f"קורס: {course_hint}")
    if user_caption:
        extra.append(f"הוראות המשתמש: {user_caption}")
    if want_full_solution:
        extra.append("המשתמש ביקש פתרון מלא — אחרי המשוב, הוסף סעיף 📘 *הפתרון המלא:* עם פתרון צעד-אחר-צעד.")

    instruction_block = '\n'.join(extra) if extra else 'אין הקשר נוסף — נסה לזהות את הנושא מהתמונה.'

    parts: list = []
    # Add system + instruction block as text first
    parts.append(GRADER_SYSTEM + '\n\n---\n\n# הקשר:\n' + instruction_block +
                 '\n\n---\n\n# הפתרון של הסטודנט:\n(התמונות מצורפות)')

    # Add images
    for img in images:
        if isinstance(img, (str, Path)):
            p = Path(img)
            if not p.exists(): continue
            data = p.read_bytes()
            ext = p.suffix.lower().lstrip('.') or 'jpeg'
            mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                    'pdf': 'application/pdf', 'webp': 'image/webp'}.get(ext, 'image/jpeg')
            parts.append(genai_types.Part.from_bytes(data=data, mime_type=mime))
        elif isinstance(img, (bytes, bytearray)):
            # Default to JPEG (Telegram photos are usually JPEG)
            parts.append(genai_types.Part.from_bytes(data=bytes(img), mime_type='image/jpeg'))

    if len(parts) < 2:  # only the prompt, no images
        return '⚠️ לא קיבלתי תמונה. שלח צילום של התרגיל הפתור.'

    client = genai.Client(api_key=key)
    try:
        resp = client.models.generate_content(
            model=model,
            contents=parts,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=3000,
                temperature=0.2,
            ),
        )
        try:
            from utils.usage_tracker import log_gemini
            log_gemini(resp, model, op='homework.grade')
        except Exception: pass
        out = (resp.text or '').strip()
        if not out:
            return '⚠️ Gemini החזיר תשובה ריקה. נסה לצלם שוב בבהירות גבוהה.'
        return out
    except Exception as e:
        return f'⚠️ שגיאה בבדיקה: {e}'


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('usage: python -m generators.homework_grader <image1> [image2 ...]')
        sys.exit(1)
    print(grade_homework(sys.argv[1:]))
