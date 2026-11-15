"""
quiz_generator.py — יוצר חידון PDF נפרד ושולח כמייל נפרד מהסיכום.

שימוש:
    gen = QuizGenerator()
    pdf_path = gen.generate(summary_md_path, course_name, course_key)
    gen.send(pdf_path, course_name, recipients)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / '.env', override=True)
except ImportError:
    pass

ROOT = Path(__file__).parent.parent
QUIZ_DIR = ROOT / 'data' / 'quizzes'
QUIZ_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Claude API — MCQ generation
# ─────────────────────────────────────────────────────────────────────────────

QUIZ_SYSTEM = """אתה מומחה לכתיבת שאלות בחינה. צור שאלות אמריקאיות (Multiple Choice) ברמה אקדמית מתאימה.
הנחיות:
- השאלות בעברית (או באנגלית אם החומר באנגלית)
- 4 תשובות לכל שאלה (א/ב/ג/ד)
- תשובה אחת נכונה בלבד
- ההסבר קצר וברור (1-2 משפטים)
- השאלות מגוונות: הגדרה, יישום, השוואה, ניתוח
- אל תחזור על אותו מושג פעמיים
החזר JSON בלבד, ללא טקסט נוסף."""

QUIZ_PROMPT = """מהסיכום הבא, צור {n} שאלות אמריקאיות.

סיכום:
{summary}

פורמט JSON מדויק:
{{
  "title": "חידון — שם הנושא",
  "questions": [
    {{
      "q": "שאלה",
      "options": {{"א": "...", "ב": "...", "ג": "...", "ד": "..."}},
      "answer": "א",
      "explanation": "הסבר קצר"
    }}
  ]
}}"""


def _call_claude(summary_text: str, n_questions: int = 6) -> dict:
    """קורא ל-Gemini API וממיר תשובה ל-dict (שם הפונקציה נשמר לתאימות)."""
    from google import genai
    from google.genai import types as _gt
    key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
    client = genai.Client(api_key=key)

    # חתוך סיכום ארוך מדי
    if len(summary_text) > 18_000:
        summary_text = summary_text[:18_000] + '\n...[נחתך]'

    prompt = QUIZ_PROMPT.format(n=n_questions, summary=summary_text)

    resp = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=QUIZ_SYSTEM + '\n\n' + prompt,
        config=_gt.GenerateContentConfig(
            max_output_tokens=3000, temperature=0.4,
            thinking_config=_gt.ThinkingConfig(thinking_budget=0),
            response_mime_type='application/json'),
    )
    try:
        from utils.usage_tracker import log_gemini
        log_gemini(resp, 'gemini-2.5-flash', op='quiz.generate')
    except Exception:
        pass
    raw = (resp.text or '').strip()

    # נקה JSON מעטיפות markdown אם יש
    raw = re.sub(r'^```json\s*', '', raw)
    raw = re.sub(r'^```\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)

    data = json.loads(raw)
    return data


# ─────────────────────────────────────────────────────────────────────────────
# PDF generation
# ─────────────────────────────────────────────────────────────────────────────

_QUIZ_PDF_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@400;600;700&family=Roboto+Mono:wght@400;500&display=swap');

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Heebo', Arial, sans-serif;
  direction: rtl;
  background: #0e0c14;
  color: #f2eee4;
  padding: 40px 48px;
  font-size: 13.5px;
  line-height: 1.65;
}

.header {
  border-bottom: 2px solid #f5b041;
  padding-bottom: 18px;
  margin-bottom: 32px;
}

.header-label {
  font-size: 10px;
  letter-spacing: 1.2px;
  text-transform: uppercase;
  color: #f5b041;
  font-family: 'Roboto Mono', monospace;
  margin-bottom: 6px;
}

.header-title {
  font-size: 22px;
  font-weight: 700;
  color: #f2eee4;
  margin-bottom: 4px;
}

.header-meta {
  font-size: 11px;
  color: #6b6580;
}

.question-block {
  background: #17141f;
  border: 1px solid #25222f;
  border-radius: 10px;
  padding: 20px 22px;
  margin-bottom: 18px;
  page-break-inside: avoid;
}

.question-num {
  font-size: 10px;
  font-family: 'Roboto Mono', monospace;
  color: #f5b041;
  letter-spacing: .8px;
  margin-bottom: 8px;
}

.question-text {
  font-size: 14.5px;
  font-weight: 600;
  color: #f2eee4;
  margin-bottom: 14px;
  line-height: 1.5;
}

.options {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.option {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 9px 13px;
  border: 1px solid #25222f;
  border-radius: 7px;
  background: #100f15;
}

.option-letter {
  font-family: 'Roboto Mono', monospace;
  font-size: 11px;
  font-weight: 600;
  color: #6b6580;
  min-width: 18px;
  padding-top: 1px;
}

.option-text {
  font-size: 13px;
  color: #c8c2b8;
  line-height: 1.45;
}

.answer-box {
  margin-top: 28px;
  border-top: 1px solid #25222f;
  padding-top: 18px;
}

.answer-box-title {
  font-size: 10px;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: #6b6580;
  font-family: 'Roboto Mono', monospace;
  margin-bottom: 12px;
}

.answer-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.answer-pill {
  background: #1d1b28;
  border: 1px solid #25222f;
  border-radius: 20px;
  padding: 4px 14px;
  font-size: 12px;
  color: #f2eee4;
  font-family: 'Roboto Mono', monospace;
}

.answer-pill .correct {
  color: #48c774;
  font-weight: 700;
}

.explanation {
  background: #1a1400;
  border: 1px solid #3d3000;
  border-radius: 7px;
  padding: 10px 14px;
  margin-top: 6px;
  font-size: 12px;
  color: #c8b870;
  line-height: 1.5;
}

.exp-label {
  font-size: 10px;
  color: #f5b041;
  font-family: 'Roboto Mono', monospace;
  letter-spacing: .6px;
  margin-bottom: 4px;
}

.footer {
  margin-top: 36px;
  padding-top: 14px;
  border-top: 1px solid #25222f;
  font-size: 10px;
  color: #3d3a4a;
  font-family: 'Roboto Mono', monospace;
  text-align: center;
}
"""


def _build_quiz_html(quiz_data: dict, course_name: str, date_str: str, include_answers: bool) -> str:
    title = quiz_data.get('title', f'חידון — {course_name}')
    questions = quiz_data.get('questions', [])

    q_blocks = ''
    for i, q in enumerate(questions, 1):
        opts_html = ''
        for letter, text in q.get('options', {}).items():
            opts_html += (
                f'<div class="option">'
                f'<span class="option-letter">{letter}</span>'
                f'<span class="option-text">{text}</span>'
                f'</div>'
            )

        q_blocks += (
            f'<div class="question-block">'
            f'<div class="question-num">שאלה {i} / {len(questions)}</div>'
            f'<div class="question-text">{q.get("q", "")}</div>'
            f'<div class="options">{opts_html}</div>'
            f'</div>'
        )

    # תשובות + הסברים — בקובץ נפרד או בתחתית
    if include_answers:
        answer_rows = ''
        explanations = ''
        for i, q in enumerate(questions, 1):
            answer = q.get('answer', '?')
            answer_rows += (
                f'<div class="answer-pill">'
                f'{i}. <span class="correct">{answer}</span>'
                f'</div>'
            )
            explanations += (
                f'<div style="margin-bottom:10px">'
                f'<div class="exp-label">שאלה {i}</div>'
                f'<div class="explanation">{q.get("explanation", "")}</div>'
                f'</div>'
            )

        answers_section = (
            f'<div class="answer-box">'
            f'<div class="answer-box-title">תשובות נכונות</div>'
            f'<div class="answer-row">{answer_rows}</div>'
            f'{explanations}'
            f'</div>'
        )
    else:
        answers_section = ''

    return f"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
<meta charset="utf-8">
<style>{_QUIZ_PDF_CSS}</style>
</head>
<body>
<div class="header">
  <div class="header-label">✏️ חידון הבנה</div>
  <div class="header-title">{title}</div>
  <div class="header-meta">{course_name} &nbsp;·&nbsp; {date_str} &nbsp;·&nbsp; {len(questions)} שאלות</div>
</div>
{q_blocks}
{answers_section}
<div class="footer">Smart Study Agent · נוצר אוטומטית · {date_str}</div>
</body>
</html>"""


def _html_to_pdf(html: str, out_path: Path) -> bool:
    """ממיר HTML ל-PDF באמצעות Playwright (אותו מנגנון כמו pdf_generator.py)."""
    import tempfile
    try:
        from playwright.sync_api import sync_playwright
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', suffix='.html', delete=False
        ) as f:
            f.write(html)
            tmp_path = f.name
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(f"file:///{tmp_path.replace(chr(92), '/')}")
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(800)
                page.pdf(
                    path=str(out_path),
                    format='A4',
                    margin={'top': '20mm', 'bottom': '20mm',
                            'left': '18mm', 'right': '18mm'},
                    print_background=True,
                )
                browser.close()
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"  ⚠️ PDF error: {e}")
        out_path.with_suffix('.html').write_text(html, encoding='utf-8')
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

class QuizGenerator:

    def generate(
        self,
        summary_md_path: str,
        course_name: str,
        course_key: str,
        n_questions: int = 6,
    ) -> dict:
        """
        יוצר חידון מסיכום MD.
        מחזיר:
          {
            questions_pdf: Path,   ← רק שאלות (ללא תשובות) — זה מה שנשלח
            answers_pdf:   Path,   ← שאלות + תשובות + הסברים (נשמר מקומית)
            quiz_json:     Path,   ← JSON גולמי
            data:          dict,   ← dict החידון
          }
        """
        md_path = Path(summary_md_path)
        summary_text = md_path.read_text(encoding='utf-8', errors='replace')

        date_str = datetime.now().strftime('%d/%m/%Y')
        stem = md_path.stem  # "2026-04-26 - מיקרו א - פתרון תרגיל 1"

        out_dir = QUIZ_DIR / course_key
        out_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n✏️  מייצר חידון עבור: {stem}")
        quiz_data = _call_claude(summary_text, n_questions)

        # שמור JSON
        json_path = out_dir / f"{stem}_quiz.json"
        json_path.write_text(
            json.dumps(quiz_data, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )

        # PDF שאלות בלבד (מה שנשלח)
        q_html = _build_quiz_html(quiz_data, course_name, date_str, include_answers=False)
        q_pdf  = out_dir / f"{stem}_quiz_questions.pdf"
        _html_to_pdf(q_html, q_pdf)
        print(f"  📄 שאלות PDF: {q_pdf.name}")

        # PDF שאלות + תשובות (נשמר מקומית)
        a_html = _build_quiz_html(quiz_data, course_name, date_str, include_answers=True)
        a_pdf  = out_dir / f"{stem}_quiz_answers.pdf"
        _html_to_pdf(a_html, a_pdf)
        print(f"  📄 תשובות PDF: {a_pdf.name}")

        return {
            'questions_pdf': q_pdf,
            'answers_pdf':   a_pdf,
            'quiz_json':     json_path,
            'data':          quiz_data,
            'stem':          stem,
        }

    def send(
        self,
        quiz_result: dict,
        course_name: str,
        recipients: list[str],
    ) -> bool:
        """שולח את קובץ השאלות בלבד כמייל נפרד לכל נמען."""
        import os as _os
        sys.path.insert(0, str(ROOT))
        from generators.gmail_sender import GmailSender

        q_pdf = quiz_result['questions_pdf']
        title = quiz_result['data'].get('title', f'חידון — {course_name}')
        date_str = datetime.now().strftime('%d/%m/%Y')
        n = len(quiz_result['data'].get('questions', []))

        body_html = f"""
<html dir="rtl" lang="he">
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;direction:rtl;background:#f5f5f5;padding:20px">
<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;
            border:1px solid #e0e0e0;padding:28px 32px">
  <div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;
              color:#f5b041;font-family:monospace;margin-bottom:8px">✏️ חידון הבנה</div>
  <h2 style="font-size:20px;color:#1a1a2e;margin:0 0 6px">{title}</h2>
  <p style="color:#888;font-size:13px;margin:0 0 20px">{course_name} · {date_str}</p>
  <p style="font-size:14px;color:#333;line-height:1.6">
    מצורף חידון עם <strong>{n} שאלות</strong> לבדיקת הבנה מההרצאה/תרגול.<br>
    ענה על השאלות ובדוק את עצמך — <em>התשובות יישלחו בנפרד לבקשה.</em>
  </p>
  <div style="margin-top:20px;padding:14px 16px;background:#fafafa;
              border-radius:8px;border:1px solid #eee;font-size:12px;color:#666">
    💡 ענה על השאלות לפני שתסתכל בסיכום — זה עוזר לזכרון לטווח ארוך.
  </div>
</div>
</body>
</html>"""

        orig = _os.getcwd()
        _os.chdir(str(ROOT))
        try:
            mailer = GmailSender()
            ok_count = 0
            for addr in recipients:
                ok = mailer.send_email(
                    to=addr,
                    subject=f"✏️ חידון: {title} ({date_str})",
                    body_html=body_html,
                    attachment_path=str(q_pdf) if q_pdf.exists() else None,
                )
                if ok:
                    ok_count += 1
            return ok_count > 0
        finally:
            _os.chdir(orig)
