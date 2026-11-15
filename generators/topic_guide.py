# -*- coding: utf-8 -*-
"""generators/topic_guide.py — "Teach me X from scratch" guide.

Builds a structured, from-zero learning guide for a single topic (e.g. "טורי
פוריה"), grounded in the course's own summaries (RAG) so it matches what was
taught, augmented by Gemini's knowledge. Gemini-only.
"""
from __future__ import annotations
import os

ROOT = None


def build_topic_guide(topic: str, course: str | None = None) -> str:
    from pathlib import Path
    global ROOT
    ROOT = Path(__file__).resolve().parent.parent
    topic = (topic or '').strip()
    if not topic:
        return 'איזה נושא ללמד מאפס? למשל: "תכין לי מדריך מאפס לטורי פוריה".'

    # ground in the course's own material
    ctx = ''
    try:
        from rag.vault_indexer import search as _search
        chunks = _search(topic, k=8, course_filter=course or None)
        parts, seen = [], set()
        for c in chunks:
            src = c.get('stem') or c.get('title') or ''
            parts.append(f"[{src}]\n{(c.get('text') or '')[:1000]}")
            seen.add(src)
        ctx = '\n\n'.join(parts)[:11000]
    except Exception:
        pass

    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        return '⚠️ אין מפתח Gemini זמין.'
    ctx_block = (f'חומר מהקורס שלך (השתמש בו כדי להתאים לניסוח, לסימונים ולרמת העומק שנלמדה; '
                 f'העדף אותו על ידע כללי כשיש סתירה):\n{ctx}' if len(ctx) > 200
                 else '(לא נמצא חומר ספציפי מהקורס — בנה לפי ידע אקדמי סטנדרטי.)')
    prompt = (
        f'אתה מורה פרטי מצוין. בנה *מדריך מאפס* לנושא *"{topic}"* לסטודנט שרוצה '
        f'להבין את הנושא מהיסוד עד רמת מבחן. כתוב בעברית, ברור ומדורג.\n\n'
        f'{ctx_block}\n\n'
        f'מבנה המדריך:\n'
        f'## 🎯 מה זה ולמה זה חשוב\n'
        f'אינטואיציה בשפה פשוטה — מה הבעיה שהנושא פותר, לפני נוסחאות.\n\n'
        f'## 📖 הבסיס — הגדרות ומושגי יסוד\n'
        f'הגדרות פורמליות עם תנאים, בהדרגה. כל מושג חדש — הסבר במילים ואז פורמלית.\n\n'
        f'## 🔢 המשפטים/הנוסחאות המרכזיים\n'
        f'עם הנחות ומסקנות, ומתי משתמשים בכל אחד.\n\n'
        f'## 💡 דוגמה פתורה צעד-אחר-צעד\n'
        f'דוגמה מייצגת אחת לפחות, עם כל השלבים.\n\n'
        f'## ⚠️ טעויות נפוצות ומלכודות\n\n'
        f'## 📝 איך זה מופיע במבחן (החלק הכי חשוב!)\n'
        f'- אילו *סוגי שאלות* נשאלים על הנושא הזה במבחנים.\n'
        f'- מה בדיוק צריך לדעת לעשות (חישוב/הוכחה/זיהוי).\n'
        f'- שלבי פתרון טיפוסיים לשאלת מבחן בנושא.\n'
        f'- מלכודות ספציפיות שמפילות במבחן.\n\n'
        f'## 🧠 איך זוכרים / איך מזהים במבחן\n'
        f'טריקים לזיהוי איזה כלי להפעיל.\n\n'
        f'## ✅ בדיקה עצמית\n'
        f'2-3 שאלות בסגנון מבחן (בלי פתרון) לבדוק הבנה.\n\n'
        f'כללים: *ממוקד-מבחן* — הדגש מה שצריך כדי לפתור שאלות במבחן, לא רק תיאוריה. '
        f'הדרגתי (מהפשוט למורכב), עם אינטואיציה לפני פורמליזם. '
        f'בלי LaTeX מסובך — Unicode מתמטי (∑ ∫ π θ ² ₙ →). תכליתי אבל מלא.'
    )
    try:
        from google import genai
        from google.genai import types as gt
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=gt.GenerateContentConfig(
                max_output_tokens=5000, temperature=0.3,
                thinking_config=gt.ThinkingConfig(thinking_budget=0)))
        try:
            from utils.usage_tracker import log_gemini
            log_gemini(resp, 'gemini-2.5-flash', op='topic_guide')
        except Exception:
            pass
        out = (resp.text or '').strip()
        if not out:
            return '⚠️ לא הצלחתי לבנות את המדריך.'
        tail = '' if len(ctx) > 200 else '\n\n_(מבוסס על ידע אקדמי כללי — לא נמצא חומר ספציפי מהקורס לנושא הזה)_'
        return f'📘 *מדריך מאפס: {topic}*\n\n{out}{tail}'
    except Exception as e:
        return f'⚠️ שגיאת Gemini: {e}'


if __name__ == '__main__':
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from utils.env import _load_env_dotfile
        _load_env_dotfile()
    except Exception:
        pass
    t = sys.argv[1] if len(sys.argv) > 1 else 'טורי פוריה'
    c = sys.argv[2] if len(sys.argv) > 2 else None
    print(build_topic_guide(t, c))
