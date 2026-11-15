"""
Homework Checker - בודק תרגילים ומזהה שימוש בשיטות שלא נלמדו עדיין
"""

import os
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

import re
import base64
import json
from datetime import date as _date
from pathlib import Path
from typing import Optional

# LLM = Gemini only (The owner's rule). The Claude/Anthropic paths that used to live
# here (check fallback + Claude Vision OCR) were replaced by Gemini in 09/2026.
# OpenAI stays only as an explicit manual choice in the dashboard picker.
import yaml

# ── Pricing per million tokens (input, output) — same table as the summarizer;
# order matters: 'flash-lite' must match before 'flash'.
_PRICING = [
    ('flash-lite',       (0.10, 0.40)),
    ('gemini-3.1-pro',   (3.50, 10.50)),
    ('gemini-3-pro',     (3.50, 10.50)),
    ('gemini-3.8-flash', (0.75, 3.75)),   # promo price until 31.12.2026, then 1.50/7.50
    ('gemini-3-flash',   (0.50, 3.00)),
    ('gemini-2.5-pro',   (1.25, 10.00)),
    ('gemini-2.5-flash', (0.075, 0.30)),
    ('gpt-4o-mini',      (0.15, 0.60)),
    ('gpt-4o',           (2.50, 10.00)),
]


def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    for key, (inp_price, out_price) in _PRICING:
        if key in (model or ''):
            return (input_tokens * inp_price + output_tokens * out_price) / 1_000_000
    return 0.0


class HomeworkChecker:
    MODEL_GEMINI_OCR_FAST   = 'gemini-2.5-flash'                 # OCR לסריקה מודפסת (כמו בסיכומים)
    MODEL_GEMINI_FLASH_LITE = 'gemini-3.1-flash-lite-preview'  # הכי זול — קוד Python
    MODEL_GEMINI_PRO        = 'gemini-3.1-pro-preview'          # הכי חזק — מתמטיקה

    # מיפוי קורס → (provider, model)
    # 'gemini' → Google Genai SDK  |  'openai' → רק בבחירה ידנית בדשבורד
    _COURSE_CHECK_MODEL = {
        'intro_to_cs':      ('gemini', 'gemini-3.1-flash-lite-preview'),  # Python → Flash-Lite (הכי זול)
        'calculus':         ('gemini', 'gemini-3.1-pro-preview'),          # חדו"א → Pro
        'linear_algebra':   ('gemini', 'gemini-3.1-pro-preview'),          # אלגברה → Pro
        'linear_algebra_2': ('gemini', 'gemini-3.1-pro-preview'),          # אלגברה 2 → Pro
        'discrete_math':    ('gemini', 'gemini-3.1-pro-preview'),          # מתמטיקה דיסקרטית → Pro
        # סמסטר א' תשפ"ז
        'probability':      ('gemini', 'gemini-3.1-pro-preview'),          # הסתברות → Pro
        'software_1':       ('gemini', 'gemini-3.8-flash'),          # Java (נבדק כטקסט) → Flash
        'microeconomics_2': ('gemini', 'gemini-3.8-flash'),
        'macroeconomics':   ('gemini', 'gemini-3.8-flash'),
        # כל שאר הקורסים → Gemini Pro (ברירת מחדל)
    }

    # OCR settings — Gemini Vision (Pro לכתב יד, Flash לסריקה מודפסת)
    OCR_DPI         = 150
    OCR_JPEG_QUAL   = 80
    OCR_BATCH_SIZE  = 4     # pages per vision call
    OCR_MAX_PAGES   = 30

    def __init__(self):
        # Gemini client — the checker's only default LLM
        self._gemini_client = None
        gemini_key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
        if gemini_key:
            try:
                from google import genai as _genai
                self._gemini_client = _genai.Client(api_key=gemini_key)
            except ImportError:
                print('⚠️  google-genai לא מותקן — pip install google-genai')

        # OpenAI client (אופציונלי — רק אם יש מפתח)
        self._openai_client = None
        openai_key = os.getenv('OPENAI_API_KEY')
        if openai_key:
            try:
                import openai as _openai
                self._openai_client = _openai.OpenAI(api_key=openai_key)
            except ImportError:
                print('⚠️  openai לא מותקן — pip install openai')

        config_path = Path(__file__).parent.parent / 'config' / 'professors.yaml'
        if not config_path.exists():                       # fresh clone: fall back to the shipped example
            config_path = config_path.with_name('professors.example.yaml')
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            self.professors: dict = data.get('professors', {})
        except FileNotFoundError:
            print(f'⚠️  professors.yaml לא נמצא ב-{config_path} — ממשיך ללא פרסונת פרופסור')
            self.professors = {}

        courses_path = Path(__file__).parent.parent / 'config' / 'courses.yaml'
        try:
            with open(courses_path, 'r', encoding='utf-8') as f:
                cdata = yaml.safe_load(f)
            self.courses: dict = cdata.get('courses', {})
        except FileNotFoundError:
            self.courses = {}

    # ── Main public method ────────────────────────────────────────────────────

    def check_homework(
        self,
        exercise_pdf_path: str,
        solution_pdf_path: Optional[str],
        course_key: str,
        solution_code_path: Optional[str] = None,   # .py קובץ קוד (מדמ"ח)
        code_filename: str = '',                      # שם הקובץ המקורי לבדיקת פורמט
        theory_filename: str = '',
        model_override: Optional[tuple] = None,      # (provider, model) לדריסת ברירת המחדל
        target_questions: Optional[list] = None,     # [str] — בדוק רק שאלות אלו (תיקון)
        previous_report_md: Optional[str] = None,    # תוכן דוח קודם — לבדיקת תיקונים
    ) -> dict:
        """
        Main method. Supports:
          - Single-file submissions: exercise PDF + solution file
          - Dual-file submissions (מדמ"ח): exercise PDF + code .py + theory PDF

        Returns structured feedback dict:
        {
            'overall_score': str,
            'summary': str,
            'unauthorized_methods': list,
            'per_question': list,
            'main_mistakes': list,
            'strengths': list,
            'filename_issues': list,   # בעיות פורמט שם קובץ
            'static_issues': list,     # בעיות סטטיות בקוד
            'cost_usd': float,
            'tokens_in': int,
            'tokens_out': int,
        }
        """
        # ── Validate inputs ──────────────────────────────────────────────────
        ex_path = Path(exercise_pdf_path)
        if not ex_path.exists():
            raise FileNotFoundError(f'❌ קובץ שאלות לא נמצא: {exercise_pdf_path}')

        sol_path  = Path(solution_pdf_path)  if solution_pdf_path  else None
        code_path = Path(solution_code_path) if solution_code_path else None

        if sol_path and not sol_path.exists():
            raise FileNotFoundError(f'❌ קובץ פתרון PDF לא נמצא: {solution_pdf_path}')
        if code_path and not code_path.exists():
            raise FileNotFoundError(f'❌ קובץ קוד לא נמצא: {solution_code_path}')
        if not sol_path and not code_path:
            raise ValueError('❌ יש לספק לפחות קובץ פתרון אחד (PDF / .py / .java)')

        # ── Course context ───────────────────────────────────────────────────
        course_info = self.courses.get(course_key, {})
        course_name = course_info.get('name', course_key)
        professor_persona = self.professors.get(course_key, {})
        hw_fmt = professor_persona.get('homework_format', {}) if professor_persona else {}

        # ── Static pre-checks (filename, code analysis) ──────────────────────
        filename_issues = []
        static_issues   = []

        if hw_fmt:
            # בדוק פורמט שמות קבצים
            fn_issues = self._check_filenames(
                code_filename or (code_path.name if code_path else ''),
                theory_filename or (sol_path.name if sol_path else ''),
                hw_fmt,
            )
            filename_issues.extend(fn_issues)

        static_hints: list[str] = []   # רמזים לקלוד (לא שגיאות ודאיות)
        is_java = bool(code_path) and (code_path.is_dir() or code_path.suffix.lower() == '.java')
        if code_path and not is_java:
            # ניתוח סטטי של קוד Python
            static_issues, static_hints = self._static_check_python(str(code_path), hw_fmt)

        # ── Read exercise ────────────────────────────────────────────────────
        print('📝 קורא תרגיל...')
        exercise_text, ex_tokens_in, ex_tokens_out, ex_model = self._read_any_file(
            str(ex_path), is_handwriting=False
        )
        print(f'   ✅ תרגיל: {len(exercise_text)} תווים')

        # ── Read solutions ───────────────────────────────────────────────────
        sol_tokens_in = sol_tokens_out = 0
        sol_model     = 'none'
        solution_theory_text = ''
        solution_code_text   = ''

        if code_path:
            print('☕ קורא קוד Java...' if is_java else '🐍 קורא קוד Python...')
            solution_code_text = self._read_code_sources(code_path)
            print(f'   ✅ קוד: {len(solution_code_text)} תווים')

        if sol_path:
            print('✍️  קורא פתרון תיאוריה...')
            solution_theory_text, sol_tokens_in, sol_tokens_out, sol_model = \
                self._read_any_file(str(sol_path), is_handwriting=True)
            print(f'   ✅ תיאוריה: {len(solution_theory_text)} תווים')

        # ── Course knowledge ─────────────────────────────────────────────────
        print('📚 טוען ידע קורס...')
        course_knowledge = self._get_course_knowledge(course_key)
        print(f'   ✅ {len(course_knowledge)} תווים של נושאים שנלמדו')

        # ── Edge-case execution (for code-heavy courses, e.g. intro_to_cs) ──
        edge_test_results = ''
        if code_path and is_java:
            print('☕ מקמפל ומריץ Java בסביבה מבודדת...')
            edge_test_results = self._run_java_edge_tests(code_path, exercise_text, solution_code_text)
        elif code_path and course_key == 'intro_to_cs':
            print('🧪 מריץ מקרי קצה אמיתיים...')
            edge_test_results = self._run_python_edge_tests(
                code_path, exercise_text, solution_code_text
            )
            if edge_test_results:
                print(f'   ✅ נכתב פלט הרצה ({len(edge_test_results)} תווים)')

        # ── Build prompt & call the model ────────────────────────────────────
        print('🔍 בודק פתרון...')
        prompt = self._build_check_prompt(
            exercise_text=exercise_text,
            solution_code_text=solution_code_text,
            solution_theory_text=solution_theory_text,
            static_issues=static_issues,
            static_hints=static_hints,
            filename_issues=filename_issues,
            course_knowledge=course_knowledge,
            professor_persona=professor_persona,
            course_name=course_name,
            target_questions=target_questions,
            edge_test_results=edge_test_results,
            previous_report_md=previous_report_md,
        )

        # בחר provider ומודל — override ידני > ברירת מחדל לפי קורס
        if model_override is not None:
            provider, check_model = model_override
            print(f'🎛️  מודל נבחר ידנית: {provider}/{check_model}')
        else:
            provider, check_model = self._COURSE_CHECK_MODEL.get(
                course_key, ('gemini', self.MODEL_GEMINI_PRO)
            )
        # Gemini-only: a stale 'claude' choice (old dashboard state) maps to Gemini Pro,
        # and so does an OpenAI choice when no OpenAI key is configured.
        if provider not in ('gemini', 'openai') or \
                (provider == 'openai' and self._openai_client is None):
            print(f'⚠️  {provider} לא זמין/לא מותר — עובר ל-Gemini Pro')
            provider, check_model = 'gemini', self.MODEL_GEMINI_PRO
        if provider == 'gemini' and self._gemini_client is None:
            raise RuntimeError('אין GEMINI_API_KEY — בודק שיעורי הבית עובד רק דרך Gemini')
        print(f'🤖 מודל בדיקה: {provider.upper()} / {check_model}')

        if provider == 'gemini':
            # ── Gemini Flash (google-genai SDK החדש) ─────────────────────────
            from google.genai import types as _gtypes
            gemini_resp = self._gemini_client.models.generate_content(
                model=check_model,
                contents=prompt,
                config=_gtypes.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=8000,
                ),
            )
            from utils.usage_tracker import log_gemini as _lg; _lg(gemini_resp, check_model, op='homework.check')
            raw_text = gemini_resp.text
            try:
                check_tokens_in  = gemini_resp.usage_metadata.prompt_token_count
                check_tokens_out = gemini_resp.usage_metadata.candidates_token_count
            except Exception:
                check_tokens_in  = len(prompt) // 4
                check_tokens_out = len(raw_text) // 4
            check_cost = _calc_cost(check_model, check_tokens_in, check_tokens_out)
        else:
            # ── OpenAI (GPT-4o / GPT-4o-mini) ────────────────────────────────
            oai_resp = self._openai_client.chat.completions.create(
                model=check_model,
                max_tokens=8000,
                temperature=0.2,
                messages=[{'role': 'user', 'content': prompt}],
            )
            raw_text         = oai_resp.choices[0].message.content
            check_tokens_in  = oai_resp.usage.prompt_tokens
            check_tokens_out = oai_resp.usage.completion_tokens
            check_cost = _calc_cost(check_model, check_tokens_in, check_tokens_out)

        # ── Aggregate token usage & cost ─────────────────────────────────────
        total_in  = ex_tokens_in  + sol_tokens_in  + check_tokens_in
        total_out = ex_tokens_out + sol_tokens_out + check_tokens_out

        ocr_cost  = _calc_cost(ex_model,  ex_tokens_in,  ex_tokens_out)
        ocr_cost += _calc_cost(sol_model, sol_tokens_in, sol_tokens_out)
        total_cost = ocr_cost + check_cost

        print(
            f'💰 עלות: OCR=${ocr_cost:.4f} + בדיקה=${check_cost:.4f} '
            f'= ${total_cost:.4f} סה"כ'
        )

        # ── Parse the model's structured response ────────────────────────────
        result = self._parse_response(raw_text)
        result['filename_issues'] = filename_issues
        result['static_issues']   = static_issues
        result['cost_usd']   = round(total_cost, 6)
        result['tokens_in']  = total_in
        result['tokens_out'] = total_out
        self._record_learning(course_key, result)

        return result

    @staticmethod
    def _record_learning(course_key: str, result: dict) -> None:
        """Feed the mastery map (every graded question) and the mistake notebook
        (every mandatory question that wasn't fully right). Never raises."""
        try:
            from utils.mastery import record as mastery
            from utils.mistakes import record as mistake
            for q in result.get('per_question') or []:
                try:
                    pct = float(str(q.get('score_pct') or '').strip().rstrip('%'))
                except ValueError:
                    continue                                   # optional / not submitted
                topic = (q.get('topic') or '').strip() or f"שיעורי בית — שאלה {q.get('q_num', '?')}"
                mastery(course_key, topic, pct / 100, 'homework')
                if pct < 100 and not q.get('optional') and (q.get('feedback') or '').strip():
                    mistake(course_key, 'homework', topic=topic,
                            question=f"שאלה {q.get('q_num', '?')} בשיעורי הבית ({topic}): "
                                     f"מה הייתה הטעות ואיך עושים נכון?",
                            wrong=q.get('status', ''), correct=q['feedback'])
        except Exception as e:
            print(f'  ⚠️ learning record failed: {e}')

    # ── File reading unified ─────────────────────────────────────────────────

    def _read_any_file(
        self,
        file_path: str,
        is_handwriting: bool = False,
    ) -> tuple[str, int, int, str]:
        """Reads .py as plain text; reads .pdf via PDF reader / OCR. Returns (text, tok_in, tok_out, model)."""
        p = Path(file_path)
        if p.suffix.lower() == '.py':
            return p.read_text(encoding='utf-8', errors='replace'), 0, 0, 'none'
        return self._read_pdf_content(file_path, is_handwriting=is_handwriting)

    # ── Filename format validation ────────────────────────────────────────────

    @staticmethod
    def _check_filenames(code_name: str, theory_name: str, hw_fmt: dict) -> list[str]:
        """
        Checks that filenames match hwN_XXXXXXXXX.py / .pdf pattern.
        Returns list of issue strings (empty = all OK).
        """
        issues = []
        pattern = re.compile(r'^hw\d+_\d{9}\.(py|pdf)$', re.IGNORECASE)

        if code_name and not pattern.match(code_name):
            issues.append(
                f'שם קובץ קוד לא תקין: "{code_name}" — הפורמט הנדרש: hwN_XXXXXXXXX.py'
                f' (N=מספר תרגיל, X=9 ספרות ת"ז)'
            )
        if theory_name and not pattern.match(theory_name):
            issues.append(
                f'שם קובץ תיאוריה לא תקין: "{theory_name}" — הפורמט הנדרש: hwN_XXXXXXXXX.pdf'
            )

        # Cross-check: both files should have same N and ID
        if code_name and theory_name:
            m_code   = re.match(r'^(hw\d+_\d{9})', code_name,   re.IGNORECASE)
            m_theory = re.match(r'^(hw\d+_\d{9})', theory_name, re.IGNORECASE)
            if m_code and m_theory and m_code.group(1).lower() != m_theory.group(1).lower():
                issues.append(
                    f'מספר תרגיל/ת"ז לא תואמים בין קבצים: '
                    f'"{m_code.group(1)}" vs "{m_theory.group(1)}" — שני הקבצים חייבים להתאים'
                )

        return issues

    # ── Static Python code analysis ───────────────────────────────────────────

    @staticmethod
    def _static_check_python(code_path: str, hw_fmt: dict) -> tuple[list[str], list[str]]:
        """
        Runs quick static checks on the submitted .py file.
        Returns (hard_issues, hints_for_claude):
          hard_issues   — בעיות ודאיות שאין עליהן ויכוח (syntax, test() call)
          hints_for_claude — מידע לקלוד שיפסוק אם זו בעיה (pass בפונקציה, print ללא return)
        """
        import ast

        hard_issues: list[str] = []
        hints: list[str] = []

        try:
            source = Path(code_path).read_text(encoding='utf-8', errors='replace')
        except Exception:
            return (['לא ניתן לקרוא את קובץ הקוד'], [])

        # 1. Syntax errors — ודאי תמיד
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            hard_issues.append(f'שגיאת syntax בשורה {e.lineno}: {e.msg}')
            return (hard_issues, hints)   # אין יותר מה לנתח

        # 2. test()/check() ברמת הקובץ — ודאי: גורם לריצה בייבוא
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                func = stmt.value.func
                name = (func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute) else '')
                if name.lower() in ('test', 'check', 'run_tests', 'main_test'):
                    hard_issues.append(
                        f'שורה {stmt.lineno}: קריאה ל-`{name}()` ברמת הקובץ הראשי — '
                        f'יש להסיר לפני הגשה כי הקוד ירוץ אוטומטית בכל ייבוא'
                    )

        # 3. פונקציות עם pass — רמז לקלוד (אולי שאלת רשות!)
        funcs_with_pass: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in node.body:
                    if isinstance(child, ast.Pass):
                        funcs_with_pass.append(f'`{node.name}` (שורה {node.lineno})')
                        break
        if funcs_with_pass:
            hints.append(
                f'פונקציות עם pass (שלד לא ממומש): {", ".join(funcs_with_pass)}. '
                f'כלל: אם הפונקציה שייכת לשאלת רשות שלא הוגשה — pass מותר לחלוטין, אל תציין זאת כבעיה כלל. '
                f'רק אם הפונקציה שייכת לשאלת חובה — יש לסמן כבעיה.'
            )

        # 4. פונקציות עם print בלבד ללא return — רמז לקלוד
        #    (יתכן שהפונקציה אמורה להדפיס, כמו "ציור" פירמידה)
        funcs_print_no_return: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith(('print_', 'draw_', 'display_', 'show_', 'main')):
                    continue  # שמות שמרמזים על הדפסה — סביר
                has_return = any(
                    isinstance(n, ast.Return) and n.value is not None
                    for n in ast.walk(node)
                )
                has_print = any(
                    isinstance(n, ast.Call) and
                    isinstance(getattr(n, 'func', None), ast.Name) and
                    n.func.id == 'print'
                    for n in ast.walk(node)
                )
                if has_print and not has_return:
                    funcs_print_no_return.append(f'`{node.name}` (שורה {node.lineno})')

        if funcs_print_no_return:
            hints.append(
                f'פונקציות עם print ללא return: {", ".join(funcs_print_no_return)}. '
                f'בדוק לפי התרגיל — אם הפונקציה אמורה להדפיס (ולא להחזיר), זה תקין.'
            )

        return (hard_issues, hints)

    # ── Course knowledge extraction ──────────────────────────────────────────

    def _get_course_knowledge(self, course_key: str) -> str:
        """
        Reads all summaries for this course, extracts ## and ### headers only.
        Returns formatted string of topics learned so far.
        """
        base = Path(__file__).parent.parent / 'data' / 'summaries' / course_key
        headers: list[str] = []

        for subfolder in ('lectures', 'tutorials'):
            folder = base / subfolder
            if not folder.exists():
                continue
            for md_file in sorted(folder.glob('*.md')):
                try:
                    text = md_file.read_text(encoding='utf-8')
                    # Strip metadata header (everything before first ---)
                    parts = text.split('---', 1)
                    content = parts[1] if len(parts) > 1 else text
                    # Extract ## and ### headers
                    for line in content.splitlines():
                        stripped = line.strip()
                        if stripped.startswith('## ') or stripped.startswith('### '):
                            header_text = re.sub(r'^#{2,3}\s+', '', stripped).strip()
                            if header_text and len(header_text) < 120:
                                headers.append(header_text)
                except Exception:
                    pass

        if not headers:
            return 'לא נמצאו סיכומים קודמים לקורס זה.'

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for h in headers:
            if h not in seen:
                seen.add(h)
                unique.append(h)

        lines = ['נושאים שנלמדו עד כה:'] + [f'- {h}' for h in unique]
        return '\n'.join(lines)

    # ── PDF reading ───────────────────────────────────────────────────────────

    def _read_pdf_content(
        self,
        pdf_path: str,
        is_handwriting: bool = False,
    ) -> tuple[str, int, int, str]:
        """
        Returns (text, tokens_in, tokens_out, model_used).
        Tries text extraction first; falls back to Vision OCR.
        Handwriting uses Gemini Pro; printed scans use Gemini Flash.
        """
        text = self._extract_pdf_text(pdf_path)

        # Determine page count
        page_count = self._get_page_count(pdf_path)

        # Heuristic: if < 80 chars per page on average → scanned/handwriting
        needs_ocr = page_count > 0 and len(text.strip()) < page_count * 80

        if needs_ocr or (is_handwriting and len(text.strip()) < 200):
            # OCR needs Gemini Vision. Without a key, return whatever text
            # pdfplumber extracted (degraded gracefully — better than crash).
            if self._gemini_client is None:
                print(f'   ⚠️ PDF נראה סרוק אבל Gemini לא זמין — '
                      f'ממשיך עם {len(text.strip())} תווי טקסט גולמי.')
                return text, 0, 0, 'pdfplumber-only'
            ocr_model = self.MODEL_GEMINI_PRO if is_handwriting else self.MODEL_GEMINI_OCR_FAST
            label = 'Pro (כתב יד)' if is_handwriting else 'Flash (סרוק)'
            print(
                f'   📷 PDF סרוק ({page_count} עמ\', {len(text.strip())} תווים) '
                f'— OCR עם Gemini Vision ({label})'
            )
            text, tok_in, tok_out = self._ocr_pdf_with_gemini(
                pdf_path, page_count, model=ocr_model, is_handwriting=is_handwriting
            )
            return text, tok_in, tok_out, ocr_model

        return text, 0, 0, 'none'

    def _extract_pdf_text(self, pdf_path: str) -> str:
        """Plain text extraction via pdfplumber."""
        text = ''
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    text += (page.extract_text() or '') + '\n\n'
        except Exception as e:
            print(f'   ⚠️  pdfplumber נכשל ({e}) — מנסה pypdf')
            try:
                import pypdf
                with open(pdf_path, 'rb') as f:
                    reader = pypdf.PdfReader(f)
                    for page in reader.pages:
                        text += (page.extract_text() or '') + '\n\n'
            except Exception as e2:
                print(f'   ❌ שגיאה בקריאת PDF: {e2}')
        return text

    def _get_page_count(self, pdf_path: str) -> int:
        """Returns number of pages, trying fitz then pdfplumber."""
        try:
            import fitz
            return fitz.open(pdf_path).page_count
        except Exception:
            pass
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                return len(pdf.pages)
        except Exception:
            return 1

    def _ocr_pdf_with_gemini(
        self,
        pdf_path: str,
        total_pages: int,
        model: str,
        is_handwriting: bool,
    ) -> tuple[str, int, int]:
        """
        Converts PDF pages to images and sends them to Gemini Vision.
        Returns (text, total_tokens_in, total_tokens_out).
        """
        pages_to_ocr = min(total_pages, self.OCR_MAX_PAGES)
        all_parts: list[str] = []
        total_in = total_out = 0

        # Try fitz (PyMuPDF) first, fall back to pdfplumber image rendering
        use_fitz = False
        try:
            import fitz
            doc = fitz.open(pdf_path)
            use_fitz = True
        except ImportError:
            doc = None

        try:
            for batch_start in range(0, pages_to_ocr, self.OCR_BATCH_SIZE):
                batch_end = min(batch_start + self.OCR_BATCH_SIZE, pages_to_ocr)
                print(f'   OCR עמודים {batch_start + 1}-{batch_end}...', end=' ', flush=True)

                from google.genai import types as _gtypes
                image_contents = []

                for i in range(batch_start, batch_end):
                    b64, media_type = self._page_to_base64(doc, pdf_path, i, use_fitz)
                    image_contents.append(_gtypes.Part.from_bytes(
                        data=base64.b64decode(b64), mime_type=media_type))

                if is_handwriting:
                    ocr_instruction = (
                        f'אלו עמודים {batch_start + 1}-{batch_end} של פתרון תרגיל בכתב יד בעברית. '
                        'חלץ את כל הטקסט המוקלד והכתוב בדיוק מרבי. '
                        'שמור על מבנה השאלות והתשובות. '
                        'כתב נוסחאות מתמטיות בסינטקס LaTeX ($...$). '
                        'אם טקסט לא ברור — ציין [לא ברור] אך אל תדלג.'
                    )
                else:
                    ocr_instruction = (
                        f'אלו עמודים {batch_start + 1}-{batch_end} של תרגיל. '
                        'חלץ את כל הטקסט כולל שאלות, הוראות ונוסחאות. '
                        'שמור על מספור שאלות.'
                    )

                image_contents.append(_gtypes.Part.from_text(text=ocr_instruction))

                try:
                    resp = self._gemini_client.models.generate_content(
                        model=model,
                        contents=image_contents,
                        config=_gtypes.GenerateContentConfig(max_output_tokens=4000),
                    )
                    from utils.usage_tracker import log_gemini as _lg; _lg(resp, model, op='homework.ocr')
                    batch_text = resp.text or ''
                    all_parts.append(batch_text)
                    try:
                        total_in  += resp.usage_metadata.prompt_token_count or 0
                        total_out += resp.usage_metadata.candidates_token_count or 0
                    except Exception:
                        pass
                    print(f'✅ {len(batch_text)} תווים')
                except Exception as e:
                    print(f'❌ batch נכשל: {e}')

        finally:
            if use_fitz and doc:
                doc.close()

        combined = '\n\n'.join(all_parts)
        print(f'   ✅ OCR הושלם: {len(combined)} תווים סה"כ')
        return combined, total_in, total_out

    def _page_to_base64(
        self,
        fitz_doc,
        pdf_path: str,
        page_idx: int,
        use_fitz: bool,
    ) -> tuple[str, str]:
        """Returns (base64_string, media_type) for a single page."""
        if use_fitz:
            import fitz
            page = fitz_doc[page_idx]
            scale = self.OCR_DPI / 72
            mat = fitz.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes('jpeg', jpg_quality=self.OCR_JPEG_QUAL)
            return base64.standard_b64encode(img_bytes).decode(), 'image/jpeg'

        # Fallback: pdfplumber page.to_image()
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                page = pdf.pages[page_idx]
                img = page.to_image(resolution=self.OCR_DPI)
                import io
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                return base64.standard_b64encode(buf.getvalue()).decode(), 'image/png'
        except Exception as e:
            raise RuntimeError(
                f'לא ניתן להמיר עמוד {page_idx + 1} לתמונה. '
                f'התקינו PyMuPDF: pip install pymupdf\nשגיאה: {e}'
            )

    # ── Edge-case execution (NEW) ─────────────────────────────────────────────

    def _run_python_edge_tests(
        self, code_path: Path, exercise_text: str, code_text: str
    ) -> str:
        """Generate edge-case tests via LLM, run the student's code with them
        in a sandboxed subprocess, return formatted results for the prompt."""
        import ast as _ast
        import tempfile, subprocess
        import re as _re

        # 1. Parse functions from student code
        try:
            tree = _ast.parse(code_text)
        except SyntaxError as e:
            return (
                "## 🧪 הרצת מקרי קצה — נכשלה\n"
                f"קוד הסטודנט מכיל שגיאת syntax: `{e.msg}` בשורה {e.lineno}.\n"
                "לא ניתן להריץ עד שיתוקן.\n"
            )

        functions = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                args = [a.arg for a in node.args.args]
                functions.append({'name': node.name, 'args': args, 'lineno': node.lineno})
        if not functions:
            return ''  # nothing to test

        # 2. Ask Gemini Flash-Lite to generate test code (cheap, fast)
        if self._gemini_client is None:
            return ''
        try:
            from google.genai import types as _gtypes
        except ImportError:
            return ''

        test_gen_prompt = f"""You generate edge-case Python tests for student homework.

Exercise (Hebrew):
{exercise_text[:6000]}

Student's functions:
{json.dumps(functions, ensure_ascii=False, indent=2)}

Student's code (preview):
```python
{code_text[:15000]}
```

Write a SINGLE Python script that:
1. Imports the student's module as `import student_code as sc`
2. Defines a `run_tests()` function with 5-8 edge-case tests targeting:
   - Boundary values (0, -1, very large, empty)
   - Off-by-one mistakes
   - Type edge cases (None, empty list/string)
   - Common student bugs (mutating input, wrong return type)
3. For each test, print exactly:
   `TEST: <short_name> | INPUT: <input_repr> | EXPECTED: <expected> | GOT: <actual> | PASS` (or FAIL/ERROR)
4. Wrap each test in try/except — print `TEST: <name> | ERROR: <exception class>: <message>` on exception
5. At bottom: `if __name__ == "__main__": run_tests()`

Rules:
- stdlib only, no pip installs, no file I/O, no network
- Each test individual try/except — never let one failure stop the others
- Keep total runtime under 5 seconds

Return ONLY the Python code (no markdown fences, no explanation)."""

        try:
            resp = self._gemini_client.models.generate_content(
                model='gemini-3.1-flash-lite-preview',
                contents=test_gen_prompt,
                config=_gtypes.GenerateContentConfig(
                    max_output_tokens=4000, temperature=0.0
                ),
            )
            from utils.usage_tracker import log_gemini as _lg; _lg(resp, 'gemini-3.1-flash-lite-preview', op='homework.edge_tests')
            test_code = (resp.text or '').strip()
            # Strip markdown fences if present
            m = _re.search(r'```(?:python)?\s*\n(.*?)```', test_code, _re.DOTALL)
            if m:
                test_code = m.group(1).strip()
            if not test_code or len(test_code) < 50:
                return ''
        except Exception as e:
            return f"## 🧪 הרצת מקרי קצה — נכשלה ביצירת מבחנים\n`{e}`\n"

        # 3. Run in the sandbox (no network, no /home, no API keys in env)
        from utils.sandbox import run_sandboxed, python_interpreter
        with tempfile.TemporaryDirectory(prefix='hw_test_') as tmp:
            tmp_p = Path(tmp)
            (tmp_p / 'student_code.py').write_text(code_text, encoding='utf-8')
            (tmp_p / 'tests.py').write_text(test_code, encoding='utf-8')
            try:
                r = run_sandboxed([python_interpreter(), 'tests.py'], tmp_p, timeout=15)
            except Exception as e:
                return f"## 🧪 הרצת מקרי קצה — שגיאת הרצה\n`{e}`\n"
            if r.timed_out:
                return (
                    "## 🧪 הרצת מקרי קצה — Timeout\n"
                    "הקוד רץ יותר מ-15 שניות — ייתכן לולאה אינסופית או רקורסיה לא תקינה.\n"
                )
            stdout = r.stdout[:3500]
            stderr = r.stderr[:1500]
            rc = r.rc

        # 4. Format for prompt
        # Count PASS/FAIL/ERROR
        pass_n = stdout.count(' | PASS')
        fail_n = stdout.count(' | FAIL')
        err_n  = stdout.count(' | ERROR')
        summary = f"PASS={pass_n}, FAIL={fail_n}, ERROR={err_n}"

        result = (
            "## 🧪 הרצת מקרי קצה אמיתיים\n"
            f"Gemini Flash-Lite יצר מבחנים, הרצתי את הקוד של הסטודנט עליהם.\n"
            f"**סיכום:** {summary} (return_code={rc})\n\n"
            "### תוצאות פר-מקרה:\n"
            "```\n"
            f"{stdout if stdout else '(אין פלט)'}\n"
            "```\n"
        )
        if stderr:
            result += f"\n### שגיאות (stderr):\n```\n{stderr}\n```\n"
        result += (
            "\n**חשוב לבדיקה:** אם יש כשלים — צטט את ה-TEST הכושל ב-feedback. "
            "אלה מקרים אמיתיים שהרצתי, לא ניתוח טקסטואלי.\n"
        )
        return result

    # ── Java (Software 1): compile + run edge tests in the sandbox ───────────

    _JAVA_PUBLIC_TYPE = re.compile(
        r'^\s*public\s+(?:(?:final|abstract|sealed|non-sealed|strictfp)\s+)*'
        r'(?:class|interface|enum|record)\s+(\w+)', re.M)
    _JAVA_PACKAGE = re.compile(r'^\s*package\s+([\w.]+)\s*;', re.M)

    @staticmethod
    def _java_files(code_path: Path) -> list[Path]:
        if code_path.is_dir():
            return sorted(p for p in code_path.rglob('*.java') if p.is_file())
        return [code_path]

    @classmethod
    def _read_code_sources(cls, code_path: Path) -> str:
        """Code text for the prompt. A folder of .java files → one listing with a header per file."""
        if not code_path.is_dir():
            return code_path.read_text(encoding='utf-8', errors='replace')
        parts = []
        for p in cls._java_files(code_path):
            parts.append(f'// ===== {p.relative_to(code_path)} =====\n'
                         + p.read_text(encoding='utf-8', errors='replace'))
        return '\n\n'.join(parts)

    @classmethod
    def _stage_java(cls, code_path: Path, src: Path) -> list[str]:
        """Copy the student's .java files into src/, each named after its public type
        (javac demands it; uploads arrive as 'solution_code.java' etc.). Returns file names."""
        src.mkdir(parents=True, exist_ok=True)
        names = []
        for i, p in enumerate(cls._java_files(code_path)):
            text = p.read_text(encoding='utf-8', errors='replace')
            m = cls._JAVA_PUBLIC_TYPE.search(text)
            name = f'{m.group(1)}.java' if m else (p.name if p.suffix == '.java' else f'Part{i}.java')
            while name in names:
                name = f'_{name}'
            (src / name).write_text(text, encoding='utf-8')
            names.append(name)
        return names

    def _run_java_edge_tests(self, code_path: Path, exercise_text: str, code_text: str) -> str:
        """Compile the student's Java, generate edge-case tests with Gemini, run them
        (and the student's main, if any) in the sandbox. Returns a prompt section."""
        import tempfile
        from utils.sandbox import run_sandboxed
        import shutil as _sh
        if not _sh.which('javac'):
            return ''
        with tempfile.TemporaryDirectory(prefix='hw_java_') as tmp:
            w = Path(tmp)
            names = self._stage_java(code_path, w / 'src')
            if not names:
                return ''
            r = run_sandboxed(['javac', '-J-Xmx384m', '-encoding', 'UTF-8', '-nowarn', '-d', 'out']
                              + [f'src/{n}' for n in names], w, timeout=60)
            if not r.ok:
                return ("## 🧪 קומפילציה (javac) — נכשלה\n"
                        "הקוד של הסטודנט **לא מתקמפל**. זו בעיה קריטית — בבודק האוטומטי של הקורס זה 0.\n"
                        f"```\n{(r.stderr or r.stdout)[:3500]}\n```\n"
                        "צטט את השגיאות הראשונות והסבר איך לתקן.\n")
            sections = ["## 🧪 הרצת Java אמיתית (javac + java, בסביבה מבודדת)\n",
                        f"✅ הקוד מתקמפל ({len(names)} קבצים: {', '.join(names)}).\n"]

            packages = sorted(set(self._JAVA_PACKAGE.findall(code_text)))
            mains = []
            for n in names:
                t = (w / 'src' / n).read_text(encoding='utf-8')
                if re.search(r'public\s+static\s+void\s+main\s*\(', t):
                    pk = self._JAVA_PACKAGE.search(t)
                    mains.append((f'{pk.group(1)}.' if pk else '') + n[:-5].lstrip('_'))
            java = ['java', '-Xmx256m', '-XX:+UseSerialGC', '-XX:TieredStopAtLevel=1', '-cp', 'out']

            # the student's own main (what the course's examples print)
            for cls_name in mains[:2]:
                rm = run_sandboxed(java + [cls_name], w, timeout=15, stdin='')
                tag = 'Timeout (לולאה אינסופית? המתנה לקלט?)' if rm.timed_out else f'exit={rm.rc}'
                sections.append(f"\n### הרצת main של `{cls_name}` — {tag}\n```\n{rm.stdout[:1500] or '(אין פלט)'}\n```\n"
                                + (f"stderr:\n```\n{rm.stderr[:1000]}\n```\n" if rm.stderr.strip() else ''))

            tests = self._generate_java_tests(exercise_text, code_text, packages)
            if not tests:
                return ''.join(sections)
            (w / 'tests').mkdir()
            (w / 'tests' / 'EdgeTests.java').write_text(tests, encoding='utf-8')
            rc = run_sandboxed(['javac', '-J-Xmx384m', '-encoding', 'UTF-8', '-nowarn', '-cp', 'out', '-d', 'out',
                                'tests/EdgeTests.java'], w, timeout=60)
            if not rc.ok:      # our generated test is broken — not the student's fault
                sections.append("\n_(מבחני הקצה שנוצרו אוטומטית לא התקמפלו — לא נכללו. זו לא בעיה בקוד הסטודנט.)_\n")
                return ''.join(sections)
            rt = run_sandboxed(java + ['EdgeTests'], w, timeout=20)
            if rt.timed_out:
                sections.append("\n### מבחני קצה — Timeout\nהקוד רץ יותר מ-20 שניות על מקרי הקצה — "
                                "ייתכן לולאה אינסופית או רקורסיה בלי תנאי עצירה.\n"
                                f"```\n{rt.stdout[-1500:]}\n```\n")
                return ''.join(sections)
            out = rt.stdout[:3500]
            summary = f"PASS={out.count(' | PASS')}, FAIL={out.count(' | FAIL')}, ERROR={out.count(' | ERROR')}"
            sections.append(f"\n### מבחני קצה — {summary}\n```\n{out or '(אין פלט)'}\n```\n")
            if rt.stderr.strip():
                sections.append(f"stderr:\n```\n{rt.stderr[:1500]}\n```\n")
            sections.append("\n**חשוב:** אלה הרצות אמיתיות. FAIL/ERROR — צטט ב-feedback את המקרה. "
                            "אבל ה-EXPECTED חושב אוטומטית: לפני שאתה קובע שהסטודנט טעה, ודא בעצמך מול "
                            "דרישות התרגיל שהציפייה נכונה.\n")
            return ''.join(sections)

    def _generate_java_tests(self, exercise_text: str, code_text: str, packages: list[str]) -> str:
        if self._gemini_client is None:
            return ''
        try:
            from google.genai import types as _gtypes
        except ImportError:
            return ''
        imports = '\n'.join(f'import {p}.*;' for p in packages)
        prompt = f"""You write edge-case tests for a student's Java homework (Tel Aviv University, "Software 1").

Exercise (Hebrew):
{exercise_text[:8000]}

Student's code:
```java
{code_text[:20000]}
```

Write ONE Java file: a class `EdgeTests` in the DEFAULT package (no package line), Java 21, java.* only.
{('It must start with these imports (the student classes are in packages):' + chr(10) + imports) if imports else ''}
- `public static void main(String[] args)` runs 6-10 tests aimed at the exercise's requirements:
  boundary values (0, negative, empty array/string, null where the spec allows it), off-by-one,
  large inputs, and the exact output/return format the exercise asks for.
- Call only methods/constructors that really exist in the student's code (same names and parameter types).
- For each test print exactly ONE line:
  `TEST: <short_name> | INPUT: <input> | EXPECTED: <expected> | GOT: <actual> | PASS` (or `| FAIL`)
  Compare arrays with java.util.Arrays.toString / deepToString, doubles with a 1e-9 tolerance.
- Wrap every test in its own try/catch(Throwable t) and print
  `TEST: <short_name> | ERROR: <t.getClass().getSimpleName()>: <t.getMessage()>` — never let one test stop the rest.
- No file/network I/O, no System.exit, no reading System.in. Total runtime well under 5 seconds.
Return ONLY the Java code."""
        try:
            resp = self._gemini_client.models.generate_content(
                model='gemini-3.8-flash', contents=prompt,
                config=_gtypes.GenerateContentConfig(
                    max_output_tokens=8000, temperature=0.0,
                    thinking_config=_gtypes.ThinkingConfig(thinking_level='low')))
            from utils.usage_tracker import log_gemini as _lg; _lg(resp, 'gemini-3.8-flash', op='homework.java_tests')
            code = (resp.text or '').strip()
        except Exception as e:
            print(f'   ⚠️ יצירת מבחני Java נכשלה: {e}')
            return ''
        m = re.search(r'```(?:java)?\s*\n(.*?)```', code, re.S)
        if m:
            code = m.group(1).strip()
        code = re.sub(r'^\s*package\s+[\w.]+\s*;\s*', '', code, flags=re.M)   # must be default package
        return code if 'class EdgeTests' in code else ''

    # ── Prompt building ───────────────────────────────────────────────────────

    def _build_check_prompt(
        self,
        exercise_text: str,
        solution_code_text: str,
        solution_theory_text: str,
        static_issues: list,
        filename_issues: list,
        course_knowledge: str,
        professor_persona: dict,
        course_name: str,
        static_hints: list | None = None,
        target_questions: list | None = None,
        edge_test_results: str = '',
        previous_report_md: str | None = None,
    ) -> str:
        """Builds the checking prompt with structured XML output instructions."""

        static_hints = static_hints or []

        # Previous-report context (if this is a re-submission with corrections)
        prev_report_block = ''
        if previous_report_md and previous_report_md.strip():
            # Cap at 8K chars to leave room for the rest of the prompt
            prev_text = previous_report_md.strip()[:8000]
            prev_report_block = f"""
## 🔁 בדיקת תיקון — הגשה חוזרת

זוהי **הגשה חוזרת** של תרגיל שכבר נבדק. הסטודנט קיבל את הדוח הבא ועכשיו מגיש גרסה מתוקנת:

```
{prev_text}
```

**בבדיקה הנוכחית, התמקד ב:**
- ✅ אילו טעויות מהדוח הקודם **תוקנו**? פרט במפורש לכל אחת.
- ❌ אילו טעויות **עדיין קיימות** או חוזרות? סמן בדגש.
- ⚠️ האם נוצרו **בעיות חדשות** שלא היו בקודם?
- 🎯 **פסיקה כללית:** האם הסטודנט שיפר? נכשל? עדיין צריך עוד תיקון?

הוסף סעיף בסוף הפידבק בכותרת "## 🔁 השוואה לבדיקה קודמת" עם הפירוט הזה.
"""

        # Professor context block
        prof_block = ''
        if professor_persona:
            prof_name    = professor_persona.get('name', '')
            emphasis     = professor_persona.get('emphasis', '')
            mistakes     = professor_persona.get('common_mistakes', [])
            mistakes_str = '\n'.join(f'- {m}' for m in mistakes) if mistakes else ''
            prof_block = f"""
## הקשר הפרופסור
פרופסור: {prof_name}
דגשים: {emphasis}
טעויות נפוצות בקורס:
{mistakes_str}
"""

        # Course-specific homework format block
        hw_format_block = ''
        hw_fmt = professor_persona.get('homework_format', {}) if professor_persona else {}
        if hw_fmt:
            code_checks   = '\n'.join(f'- {c}' for c in hw_fmt.get('code_checklist', []))
            theory_checks = '\n'.join(f'- {c}' for c in hw_fmt.get('theory_checklist', []))
            ai_policy     = hw_fmt.get('ai_tools_policy', '')
            hw_format_block = f"""
## כללי הגשה ספציפיים לקורס זה
{f"בדיקות קוד (.py):{chr(10)}{code_checks}" if code_checks else ""}
{f"בדיקות תיאוריה (.pdf):{chr(10)}{theory_checks}" if theory_checks else ""}
{f"מדיניות כלי AI: {ai_policy}" if ai_policy else ""}
"""

        # Hard issues (ודאיות): שגיאות syntax, קריאת test() ברמת הקובץ
        pre_checks_block = ''
        all_pre_issues = filename_issues + static_issues
        if all_pre_issues:
            pre_list = '\n'.join(f'- {iss}' for iss in all_pre_issues)
            pre_checks_block = f"""
## ⚠️ בעיות ודאיות שאותרו אוטומטית (חובה לתקן)
{pre_list}
שלב בעיות אלו בתוצאות הבדיקה ובטעויות העיקריות.
"""

        # Hints (רמזים): pass, print ללא return — קלוד יחליט
        hints_block = ''
        if static_hints:
            hints_list = '\n'.join(f'- {h}' for h in static_hints)
            hints_block = f"""
## 📋 ממצאי ניתוח קוד — לשיפוטך
הממצאים הבאים נמצאו בקוד. **לא בהכרח שגיאות** — שפוט לפי שאלות התרגיל:
{hints_list}
כלל: אם הפונקציה שייכת לשאלת רשות שלא הוגשה — אל תסמן כבעיה.
כלל: אם הפונקציה אמורה להדפיס output (לא להחזיר ערך) — print ללא return הוא תקין.
כלל: כשמציין בעיה בקוד — ציין תמיד מספר שורה ושם הפונקציה.
"""

        # Solution sections
        solution_block = ''
        # Java signals only — a Python "class Node:" must not match
        _is_java = bool(re.search(r'\b(?:class|interface|enum|record)\s+\w+[^:\n]*\{|^\s*import\s+java\.|System\.out\.',
                                  solution_code_text or '', re.M))
        _lang, _ext, _fence = ('Java', '.java', 'java') if _is_java else ('Python', '.py', 'python')
        if solution_code_text and solution_theory_text:
            solution_block = f"""## פתרון הסטודנט — קוד {_lang} ({_ext})
```{_fence}
{solution_code_text[:50000]}
```

---

## פתרון הסטודנט — תיאוריה (.pdf)
{solution_theory_text[:20000]}"""
        elif solution_code_text:
            solution_block = f"""## פתרון הסטודנט — קוד {_lang} ({_ext})
```{_fence}
{solution_code_text[:50000]}
```"""
        else:
            solution_block = f"""## פתרון הסטודנט
{solution_theory_text[:25000]}"""

        # Instruction customization based on submission type
        dual_mode = bool(solution_code_text and solution_theory_text)
        if dual_mode:
            check_instructions = """**בדיקת קוד (.py)**:
1. האם הקוד רץ ללא שגיאות syntax/runtime (סמולצי בראשך)
2. האם כל פונקציה מחזירה (return) את הערך הנכון — לא מדפיסה אותו
3. בדוק כל פונקציה מול הדוגמאות בתרגיל
4. זיהוי שיטות/imports שלא נלמדו עדיין

**בדיקת תיאוריה (.pdf)**:
1. נכונות הוכחות/תשובות תיאורטיות
2. ניסוח הגדרות ומשפטים
3. השאלות שאינן בקוד — ענה עליהן כאן

**לכל שאלה** — זהה האם היא שאלת קוד, תיאוריה, או שניהם, ובדוק את הפתרון המתאים."""
        else:
            check_instructions = """1. נכונות כל שאלה — חשב/פתור בעצמך ובדוק
2. טעויות חישוב, אלגבראיות או לוגיות
3. ניסוח לא מדויק של הגדרות/משפטים
4. חוזקות — מה נעשה טוב"""

        # Build target-questions block separately (avoids nested f-string,
        # which Python 3.10 doesn't support cleanly).
        if target_questions:
            _qs = ', '.join(str(q) for q in target_questions)
            target_block = (
                f"\n### ⚡ מצב בדיקת תיקון — בדוק רק שאלות: {_qs}\n"
                f"הסטודנט מגיש תיקון לשאלות {_qs} בלבד.\n"
                f"- **שאלות אחרות** — אל תבדוק, אל תגרע נקודות, ציין כ-`לא נבדק (תיקון ממוקד)`.\n"
                f"- **הציון הכולל** — חשב רק לפי שאלות {_qs}.\n"
                f"- **הסבר לסטודנט** — ציין במפורש אם התיקון עבר בכל שאלה שנבדקה.\n"
            )
        else:
            target_block = ""

        prompt = f"""אתה בודק תרגילי בית בקורס "{course_name}".

{prof_block}
{hw_format_block}
{pre_checks_block}
{hints_block}

## נושאים שנלמדו בקורס עד כה
{course_knowledge}

---

## שאלות התרגיל
{exercise_text}

---

{solution_block}

---

{edge_test_results}

---

{prev_report_block}

---

## הוראות לבדיקה
{target_block}
### זיהוי שאלות חובה מול רשות
**ראשית** — סרוק את התרגיל וזהה אילו שאלות הן **רשות** (מסומנות בדרך כלל כ"רשות", "bonus", "אופציונלי", "לא חובה", "extra credit" וכדומה).
שאלות רשות שהסטודנט **לא הגיש** — סמן כ-`רשות - לא הוגש` ואל תחסיר נקודות בגינן.
שאלות חובה שהסטודנט לא הגיש — סמן כ-`חסר`.

### משימה ראשית — שיטות לא מורשות

**⚠️ קרא תחילה את מדיניות התרגיל לגבי שימוש בכלים.**
לפעמים התרגיל עצמו מגדיר את הכלל — למשל: *"כל עוד לא אסרנו שימוש בכלי מסוים, ניתן לפתור בכל דרך"*. אם כלל כזה כתוב בתרגיל — הוא גובר על רשימת הנושאים שנלמדו.

**שני סוגי מדיניות אפשריים — זהה מה כתוב בתרגיל:**

1. **מדיניות "רשימה לבנה" (whitelist):** רק מה שנלמד מפורשות מותר. חובה לציין שיטות שלא נלמדו.
2. **מדיניות "רשימה שחורה" (blacklist):** הכל מותר אלא אם נאסר מפורשות (למשל: `import` אסור). במקרה זה — **אל תסמן שיטה כ"לא מורשת" רק כי לא נלמדה** — אלא אם היא `import` או נאסרה במפורש.

**אם התרגיל אומר משהו כמו:** *"כל עוד לא אסרנו = מותר"* — זו מדיניות blacklist. במקרה זה:
- `endswith`, `startswith`, `count`, `find` וכדומה — **מותרים** (אין צורך ב-import)
- `import` (ספריות חיצוניות) — **אסור**
- אל תסמן אותם כ"שיטות לא מורשות" — לכל היותר הזכר אותם כ**המלצה** לפתרון עם slicing

**שלב 1:** קרא את ה"כללים" / "מדיניות" שמופיעים בתרגיל (לרוב בתחילתו).
**שלב 2:** פרש לפי המדיניות שנמצאה — whitelist או blacklist.
**שלב 3:** רק אם מדיניות whitelist — השווה מול "נושאים שנלמדו עד כה".

### כלל הפידבק
- **שאלה נכונה** (✅): רשום רק `נכון` בסטטוס, score_pct=100, feedback ריק לחלוטין — אל תוסיף שבחים או הסברים מיותרים.
- **שאלה שגויה/חלקית** (❌/🔶): פירוט מלא — מה בדיוק שגוי, החישוב/הגיון הנכון, כיצד לתקן.
- **שאלת רשות שלא הוגשה**: status=`רשות - לא הוגש`, score_pct ריק, feedback=`שאלת רשות, לא הוגש — בסדר גמור.`

### כללי pass בקוד
- `pass` בפונקציה של **שאלת רשות שלא הוגשה** — **תקין לחלוטין, אל תזכיר אותו בכלל**
- `pass` בפונקציה של **שאלת חובה** — בעיה, יש לציין
- הכלל הזה גובר על כל הנחיית הגשה כללית שאומרת "הסר pass"

### המלצות לשיפור (recommendations)
לאחר הבדיקה — תן **2-4 המלצות ספציפיות** שנגזרות מהחומר שנלמד. אלו **לא** טעויות — אלו עצות לשדרוג הקוד/פתרון:
- שיפורי סגנון/קריאות לפי עקרונות שנלמדו (שמות משתנים, הערות, מבנה)
- שימוש בכלים שנלמדו שהפתרון לא ניצל (List Comprehension במקום לולאה, short-circuit, slicing וכד')
- אם הסטודנט השתמש ב-built-in שלא נלמד (כגון `endswith`) אך מותר לפי מדיניות הקורס — הצע כ**המלצה** את הפתרון עם הכלים שנלמדו (slicing וכד'), בלי לסמן כטעות
- נקודות מחשבה לעתיד — קשרים לחומר שעוד יגיע בקורס (ציין רק אם רלוונטי)
- תובנות על יעילות/ביצועים (מהיר/איטי, כמות פעולות)
**כלל:** המלצה חייבת להתייחס לכלי/עיקרון **שנלמד עד כה** בקורס. אל תמליץ על דברים שלא נלמדו.

{check_instructions}

ציון כולל מוצע (מ-100) — **אל תכלול שאלות רשות שלא הוגשו בחישוב הציון**.

---

## פורמט תשובה — חובה להחזיר XML מדויק

<feedback>

<overall_score>ציון כאן — מספר/100 (ללא שאלות רשות שלא הוגשו)</overall_score>

<summary>2-3 משפטים בעברית: הערכה כוללת — רמה כללית ומה עיקר הבעיות. אל תשבח שאלות שנכונות — רק ציין מה בעייתי ומה הרמה הכללית.</summary>

<unauthorized_methods>
<!-- כלול כאן רק שיטות שאסורות לפי מדיניות התרגיל:
     - אם מדיניות whitelist: שיטה שלא נלמדה ולא הותרה מפורשות
     - אם מדיניות blacklist: רק import/ספרייה חיצונית, או מה שנאסר במפורש
     - אל תכלול built-in methods (endswith, startswith וכד') במדיניות blacklist!
     - אם אין — כתוב <item>לא נמצאו שיטות לא מורשות</item> -->
<item>שם השיטה + באיזו שאלה + מדוע אסורה לפי מדיניות התרגיל</item>
</unauthorized_methods>

<per_question>
<!-- שאלה אחת לכל <question> — אסור להשמיט שאלה, כולל רשות -->
<question>
  <q_num>1</q_num>
  <topic>נושא השאלה בקורס, 2-5 מילים</topic>
  <optional>false</optional>  <!-- true אם שאלת רשות -->
  <status>נכון / שגוי חלקי / שגוי / חסר / רשות - לא הוגש</status>
  <score_pct>85</score_pct>  <!-- ריק אם רשות-לא הוגש -->
  <feedback>פירוט רק אם שגוי/חלקי/חסר. אם נכון — השאר ריק לחלוטין.</feedback>
</question>
</per_question>

<main_mistakes>
<!-- 3-5 טעויות עיקריות בשאלות חובה שגויות בלבד, מהחשובה לפחות -->
<!-- אם אין טעויות — <item>לא נמצאו טעויות משמעותיות</item> -->
<item>תיאור הטעות + כיצד לתקן</item>
</main_mistakes>

<recommendations>
<!-- 2-4 המלצות לשיפור — רק מחומר שנלמד עד כה. לא טעויות — שדרוגים אפשריים -->
<!-- אם אין המלצות משמעותיות — <item>הפתרון ממצה היטב את הכלים שנלמדו</item> -->
<item>תיאור ההמלצה + דוגמת קוד קצרה אם רלוונטי + מדוע זה שיפור</item>
</recommendations>

</feedback>

כתוב את כל הפידבק בעברית. היה ספציפי ובונה. אסור להשמיט שאלות מ-per_question."""

        return prompt

    # ── Response parsing ──────────────────────────────────────────────────────

    def _parse_response(self, raw_text: str) -> dict:
        """Parses the model's XML-structured response into a dict."""

        def _tag_content(tag: str, text: str) -> str:
            m = re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
            return m.group(1).strip() if m else ''

        def _tag_items(parent_tag: str, item_tag: str, text: str) -> list[str]:
            parent_m = re.search(rf'<{parent_tag}>(.*?)</{parent_tag}>', text, re.DOTALL)
            if not parent_m:
                return []
            parent_content = parent_m.group(1)
            return [
                m.strip()
                for m in re.findall(rf'<{item_tag}>(.*?)</{item_tag}>', parent_content, re.DOTALL)
                if m.strip()
            ]

        def _parse_questions(text: str) -> list[dict]:
            pq_m = re.search(r'<per_question>(.*?)</per_question>', text, re.DOTALL)
            if not pq_m:
                return []
            questions = []
            for q_match in re.finditer(r'<question>(.*?)</question>', pq_m.group(1), re.DOTALL):
                q_text   = q_match.group(1)
                optional = _tag_content('optional', q_text).lower() in ('true', 'yes', '1')
                status   = _tag_content('status', q_text)
                feedback = _tag_content('feedback', q_text)
                # Normalize: if status is "נכון" wipe feedback
                if status == 'נכון':
                    feedback = ''
                questions.append({
                    'q_num':     _tag_content('q_num', q_text),
                    'topic':     _tag_content('topic', q_text),
                    'optional':  optional,
                    'status':    status,
                    'score_pct': _tag_content('score_pct', q_text),
                    'feedback':  feedback,
                })
            return questions

        # If no XML tags found, return raw text as summary
        if '<feedback>' not in raw_text:
            return {
                'overall_score':        'לא זמין',
                'summary':              raw_text.strip(),
                'unauthorized_methods': [],
                'per_question':         [],
                'main_mistakes':        [],
                'strengths':            [],
            }

        recommendations = _tag_items('recommendations', 'item', raw_text)
        # סנן "אין המלצות" מהרשימה
        recommendations = [r for r in recommendations
                           if 'ממצה היטב' not in r and 'אין המלצות' not in r]

        return {
            'overall_score':        _tag_content('overall_score', raw_text),
            'summary':              _tag_content('summary', raw_text),
            'unauthorized_methods': _tag_items('unauthorized_methods', 'item', raw_text),
            'per_question':         _parse_questions(raw_text),
            'main_mistakes':        _tag_items('main_mistakes', 'item', raw_text),
            'recommendations':      recommendations,
            'strengths':            [],
        }

    # ── Markdown report formatting ────────────────────────────────────────────

    def format_feedback_md(self, result: dict, course_name: str, hw_title: str) -> str:
        """Formats the check result as a Hebrew Markdown report."""
        today = _date.today().strftime('%d/%m/%Y')

        score = result.get('overall_score', '—')
        summary = result.get('summary', '')
        unauthorized = result.get('unauthorized_methods', [])
        per_q = result.get('per_question', [])
        mistakes = result.get('main_mistakes', [])
        strengths = result.get('strengths', [])
        filename_issues = result.get('filename_issues', [])
        static_issues   = result.get('static_issues', [])
        cost = result.get('cost_usd', 0.0)
        tok_in  = result.get('tokens_in', 0)
        tok_out = result.get('tokens_out', 0)

        lines: list[str] = []

        # Header
        lines.append(f'# בדיקת תרגיל — {hw_title}')
        lines.append(f'**קורס:** {course_name}  ')
        lines.append(f'**תאריך בדיקה:** {today}  ')
        lines.append(f'**ציון:** `{score}`')
        lines.append('')
        lines.append('---')
        lines.append('')

        # Filename / static issues (pre-submission checks)
        all_pre = filename_issues + static_issues
        if all_pre:
            lines.append('## 🔴 בעיות הגשה — לתקן לפני הגשה!')
            for iss in all_pre:
                lines.append(f'- {iss}')
            lines.append('')

        # Summary
        lines.append('## סיכום כולל')
        lines.append(summary)
        lines.append('')

        # Unauthorized methods — highlighted prominently
        lines.append('## ⚠️ שיטות לא מורשות (לא נלמדו עדיין)')
        if unauthorized and unauthorized != ['לא נמצאו שיטות לא מורשות']:
            for item in unauthorized:
                lines.append(f'- {item}')
        else:
            lines.append('לא נמצאו שיטות לא מורשות. הסטודנט השתמש רק בחומר שנלמד.')
        lines.append('')

        # Per-question breakdown
        if per_q:
            lines.append('## פירוט לפי שאלות')
            lines.append('')
            for q in per_q:
                q_num     = q.get('q_num', '?')
                status    = q.get('status', '')
                optional  = q.get('optional', False)
                score_pct = q.get('score_pct', '')
                feedback  = q.get('feedback', '').strip()

                opt_tag = ' (רשות)' if optional else ''

                if status == 'נכון' and not feedback:
                    # שאלה נכונה — שורה קצרה בלי פירוט
                    lines.append(f'**שאלה {q_num}{opt_tag}** — ✅ נכון')
                elif 'רשות' in status and 'לא הוגש' in status:
                    lines.append(f'**שאלה {q_num} (רשות)** — ⚪ לא הוגש (בסדר גמור)')
                else:
                    status_icon = (
                        '🔶' if 'חלקי' in status
                        else '❌' if 'שגוי' in status or 'חסר' in status
                        else '⬜'
                    )
                    score_badge = f' ({score_pct}%)' if score_pct else ''
                    lines.append(f'### שאלה {q_num}{opt_tag} — {status_icon} {status}{score_badge}')
                    if feedback:
                        lines.append(feedback)
                lines.append('')

        # Main mistakes
        lines.append('## טעויות עיקריות לתיקון')
        if mistakes and mistakes != ['לא נמצאו טעויות משמעותיות']:
            for i, m in enumerate(mistakes, 1):
                lines.append(f'{i}. {m}')
        else:
            lines.append('לא זוהו טעויות משמעותיות.')
        lines.append('')

        # Recommendations
        recommendations = result.get('recommendations', [])
        if recommendations:
            lines.append('## 💡 המלצות לשיפור')
            lines.append('*מבוסס על הנושאים שנלמדו בקורס עד כה*')
            lines.append('')
            for i, r in enumerate(recommendations, 1):
                lines.append(f'**{i}.** {r}')
                lines.append('')

        lines.append('---')
        lines.append('')

        # Cost footer
        lines.append('## עלות API')
        lines.append(f'- **Tokens:** {tok_in:,} input + {tok_out:,} output')
        lines.append(f'- **עלות:** ${cost:.4f}')
        lines.append('')
        lines.append(f'*נוצר אוטומטית ב-{today} על ידי Smart Study Agent*')

        return '\n'.join(lines)

    # ── Save helper ───────────────────────────────────────────────────────────

    def save_feedback(
        self,
        result: dict,
        course_key: str,
        course_name: str,
        hw_title: str,
        user_id: str = 'admin',
    ) -> str:
        """
        Saves the formatted feedback to
        data/homework/{user_id}/{course_key}/{date}_{hw_title}_feedback.md
        Returns the saved file path. user_id keeps multi-user feedback isolated.
        """
        safe_title = re.sub(r'[<>:"/\\|?*]', '', hw_title).strip().replace(' ', '_')
        date_str   = _date.today().strftime('%Y-%m-%d')
        filename   = f'{date_str}_{safe_title}_feedback.md'

        out_dir = Path(__file__).parent.parent / 'data' / 'homework' / user_id / course_key
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / filename

        md_content = self.format_feedback_md(result, course_name, hw_title)
        out_path.write_text(md_content, encoding='utf-8')

        print(f'💾 פידבק נשמר: {out_path}')
        return str(out_path)

    # ── Practice exercise generation ─────────────────────────────────────────

    def generate_practice_exercise(
        self,
        course_key: str,
        course_name: str,
        feedback_mds: list[str],
    ) -> dict:
        """
        מנתח את כל דוחות הפידבק של קורס → מזהה נושאים חלשים →
        מייצר תרגיל מתקן ממוקד (2-3 שאלות).

        Returns:
            {
              'weak_topics': list[str],
              'exercise_md': str,          # תרגיל בפורמט Markdown
              'target_questions': list[str], # שאלות מספר שעל הסטודנט לתרגל
              'cost_usd': float,
            }
        """
        # קרא את כל הפידבקים
        feedback_texts = []
        for path in feedback_mds:
            try:
                feedback_texts.append(Path(path).read_text(encoding='utf-8'))
            except Exception:
                pass

        if not feedback_texts:
            return {
                'weak_topics': [],
                'exercise_md': '# לא נמצאו דוחות בדיקה קודמים',
                'target_questions': [],
                'cost_usd': 0.0,
            }

        all_feedback = '\n\n---\n\n'.join(feedback_texts)
        course_knowledge = self._get_course_knowledge(course_key)

        prompt = f"""אתה מורה מומחה ב-{course_name}.
להלן דוחות בדיקה של כל שיעורי הבית שהסטודנט הגיש עד כה:

<feedback_reports>
{all_feedback}
</feedback_reports>

חומר הקורס שנלמד עד כה:
<course_knowledge>
{course_knowledge}
</course_knowledge>

משימתך:
1. זהה את 2-3 הנושאים/שאלות שבהם הסטודנט ביצע הכי הרבה טעויות.
2. צור תרגיל מתקן ממוקד עם 2-3 שאלות הממוקדות בדיוק בנקודות החולשה.
3. השאלות צריכות להיות ברמה דומה לתרגילים המקוריים אך לא זהות.
4. כתוב הסבר קצר לסטודנט למה הוא צריך לפתור את התרגיל הזה.

ענה בפורמט XML מדויק:

<weak_topics>
  <topic>נושא חלש 1</topic>
  <topic>נושא חלש 2</topic>
</weak_topics>

<exercise_intro>
הסבר קצר לסטודנט (2-3 משפטים) למה נבחרו השאלות האלו
</exercise_intro>

<exercise_md>
# תרגיל מתקן — {course_name}

## שאלה 1: [כותרת]
[תוכן השאלה המלא, כולל דרישות ברורות]

## שאלה 2: [כותרת]
[תוכן השאלה המלא]

## שאלה 3: [כותרת — אופציונלי]
[תוכן השאלה המלא]
</exercise_md>

<target_question_nums>
  <q>1</q>
  <q>2</q>
</target_question_nums>
"""

        raw = self._call_api(prompt, course_key)
        tok_in  = len(prompt) // 4
        tok_out = len(raw) // 4
        cost    = self._estimate_cost(tok_in, tok_out, course_key)

        def _items(tag, raw):
            import re as _re
            block = _re.search(rf'<{tag}>(.*?)</{tag}>', raw, _re.DOTALL)
            if not block:
                return []
            return [m.strip() for m in _re.findall(r'<\w+>(.*?)</\w+>', block.group(1), _re.DOTALL)]

        def _block(tag, raw):
            import re as _re
            m = _re.search(rf'<{tag}>(.*?)</{tag}>', raw, _re.DOTALL)
            return m.group(1).strip() if m else ''

        exercise_md   = _block('exercise_md', raw)
        exercise_intro = _block('exercise_intro', raw)
        if exercise_intro and not exercise_md.startswith('# תרגיל'):
            exercise_md = f"> {exercise_intro}\n\n{exercise_md}"

        return {
            'weak_topics':       _items('weak_topics', raw),
            'exercise_md':       exercise_md,
            'target_questions':  _items('target_question_nums', raw),
            'exercise_intro':    exercise_intro,
            'cost_usd':          cost,
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='בודק תרגיל בית ומזהה שיטות שלא נלמדו עדיין'
    )
    parser.add_argument('exercise_pdf',  help='נתיב ל-PDF של שאלות התרגיל')
    parser.add_argument('solution_pdf',  help='נתיב ל-PDF של פתרון הסטודנט')
    parser.add_argument('course_key',    help='מפתח הקורס (למשל: calculus_2)')
    parser.add_argument('--title',       default='תרגיל', help='כותרת התרגיל (ברירת מחדל: תרגיל)')
    parser.add_argument('--no-save',     action='store_true', help='אל תשמור קובץ פידבק')

    args = parser.parse_args()

    if not os.getenv('ANTHROPIC_API_KEY'):
        print('❌ משתנה סביבה ANTHROPIC_API_KEY לא מוגדר')
        sys.exit(1)

    checker = HomeworkChecker()

    try:
        result = checker.check_homework(
            exercise_pdf_path=args.exercise_pdf,
            solution_pdf_path=args.solution_pdf,
            course_key=args.course_key,
        )
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)

    # Print summary to console
    print()
    print('=' * 60)
    print(f'ציון כולל: {result["overall_score"]}')
    print(f'סיכום:     {result["summary"]}')

    if result['unauthorized_methods']:
        print()
        print('⚠️  שיטות לא מורשות:')
        for item in result['unauthorized_methods']:
            if item != 'לא נמצאו שיטות לא מורשות':
                print(f'   • {item}')

    if result['main_mistakes']:
        print()
        print('טעויות עיקריות:')
        for i, m in enumerate(result['main_mistakes'], 1):
            print(f'  {i}. {m}')

    print()
    print(
        f'💰 עלות: ${result["cost_usd"]:.4f}  '
        f'({result["tokens_in"]:,} in + {result["tokens_out"]:,} out tokens)'
    )
    print('=' * 60)

    if not args.no_save:
        course_info = checker.courses.get(args.course_key, {})
        course_name = course_info.get('name', args.course_key)
        saved_path  = checker.save_feedback(result, args.course_key, course_name, args.title)
        print(f'\n📄 דוח מלא: {saved_path}')


if __name__ == '__main__':
    main()
