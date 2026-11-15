"""
Professor Agent - מוסיף טיפים מנקודת-מבט הפרופסור לסיכומים
"""

import os
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

import re
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types as genai_types
import yaml


class ProfessorAgent:
    # Pricing per million tokens
    _PRICING = {
        'gemini-2.5-flash': (0.30, 2.50),
        'sonnet': (3.0, 15.0),
        'haiku':  (0.80, 4.0),
    }

    def __init__(self):
        key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
        self.client = genai.Client(api_key=key)
        self.model = 'gemini-2.5-flash'

        config_path = Path(__file__).parent.parent / 'config' / 'professors.yaml'
        if not config_path.exists():                       # fresh clone: fall back to the shipped example
            config_path = config_path.with_name('professors.example.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        self.professors: dict = data.get('professors', {})

    # ── public API ──────────────────────────────────────────────────────────

    def add_tips_to_summary(self, summary_md_path: str, course_key: str) -> dict:
        """
        קורא את הסיכום, מייצר טיפים מנקודת-מבט הפרופסור,
        מצרף אותם לקובץ MD וממיר מחדש ל-PDF.

        Returns: {'tips': str, 'cost_usd': float, 'tokens_in': int, 'tokens_out': int}
        """
        md_path = Path(summary_md_path)
        if not md_path.exists():
            print(f'❌ קובץ לא נמצא: {summary_md_path}')
            return {'tips': '', 'cost_usd': 0.0, 'tokens_in': 0, 'tokens_out': 0}

        persona = self.professors.get(course_key)
        if not persona:
            print(f'⚠️  אין פרופסור מוגדר עבור קורס: {course_key}')
            return {'tips': '', 'cost_usd': 0.0, 'tokens_in': 0, 'tokens_out': 0}

        # בדוק שאין כבר טיפים
        if self.get_tips_section(summary_md_path):
            print(f'ℹ️  טיפי הפרופסור כבר קיימים ב: {md_path.name}')
            return {'tips': '', 'cost_usd': 0.0, 'tokens_in': 0, 'tokens_out': 0}

        print(f'\n👨‍🏫 מייצר טיפי פרופסור עבור: {md_path.name}')

        # קרא תוכן הסיכום (ללא מטא-דאטה)
        full_md = md_path.read_text(encoding='utf-8')
        summary_content = self._strip_metadata(full_md)

        # בנה זיכרון קורס (כותרות מהרצאות קודמות)
        memory = self._get_course_memory(course_key, exclude_path=str(md_path))

        # בנה שם קורס אנושי לפי הפרופסור
        course_name = persona.get('name', course_key)

        # צור prompt ושלח ל-Claude
        prompt = self._build_tips_prompt(summary_content, memory, persona, course_name)

        print('🧠 שולח ל-Gemini לייצור טיפים...')
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=2000, temperature=0.4,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0)),
            )
            try:
                from utils.usage_tracker import log_gemini
                log_gemini(response, self.model, op='professor.tips')
            except Exception:
                pass
        except Exception as e:
            print(f'❌ שגיאה ב-API: {e}')
            return {'tips': '', 'cost_usd': 0.0, 'tokens_in': 0, 'tokens_out': 0}

        tips = (response.text or '').strip()
        _um = getattr(response, 'usage_metadata', None)
        tokens_in = getattr(_um, 'prompt_token_count', 0) or 0
        tokens_out = getattr(_um, 'candidates_token_count', 0) or 0
        cost = self._calc_cost(self.model, tokens_in, tokens_out)

        print(f'💰 פרופסור: {tokens_in:,} in + {tokens_out:,} out = ${cost:.4f}')

        # צרף לקובץ MD
        self._append_tips_to_file(
            md_path=str(md_path),
            tips=tips,
            cost_info={'tokens_in': tokens_in, 'tokens_out': tokens_out, 'cost_usd': cost}
        )

        # מחולל PDF מחדש
        try:
            from generators.pdf_generator import markdown_to_pdf
            updated_md = md_path.read_text(encoding='utf-8')
            clean_md = self._strip_metadata(updated_md)
            pdf_path = str(md_path).replace('.md', '.pdf')
            markdown_to_pdf(clean_md, pdf_path)
            print(f'📄 PDF עודכן: {Path(pdf_path).name}')
        except Exception as e:
            print(f'⚠️  שגיאה בעדכון PDF: {e}')

        print(f'✅ טיפי פרופסור נוספו ({len(tips)} תווים)')
        return {
            'tips': tips,
            'cost_usd': cost,
            'tokens_in': tokens_in,
            'tokens_out': tokens_out,
        }

    # ── memory ───────────────────────────────────────────────────────────────

    def _get_course_memory(self, course_key: str, exclude_path: str) -> str:
        """
        קורא כותרות ## ו-### מסיכומים קודמים בקורס (לא את הנוכחי).
        מחזיר מחרוזת בפורמט:
          הרצאה 1 (14/04/2026):
          - נושא א
          - נושא ב
        """
        exclude = Path(exclude_path).resolve()
        summaries_dir = Path(f'data/summaries/{course_key}/lectures')
        if not summaries_dir.exists():
            summaries_dir = Path(__file__).parent.parent / f'data/summaries/{course_key}/lectures'
        if not summaries_dir.exists():
            return ''

        files = sorted(summaries_dir.glob('*.md'))
        parts = []

        for f in files:
            if f.resolve() == exclude:
                continue

            stem = f.stem  # "2026-04-14 - מרצה_כ - הרצאה 1"
            # חלץ תאריך מהשם
            date_match = re.match(r'^(\d{4}-\d{2}-\d{2})', stem)
            date_str = ''
            if date_match:
                raw = date_match.group(1)
                # YYYY-MM-DD → DD/MM/YYYY
                parts_d = raw.split('-')
                date_str = f'{parts_d[2]}/{parts_d[1]}/{parts_d[0]}'

            # שם תצוגה: אחרי התאריך
            label_parts = stem.split(' - ', 1)
            label = label_parts[1] if len(label_parts) > 1 else stem
            if date_str:
                label = f'{label} ({date_str})'

            try:
                text = f.read_text(encoding='utf-8')
                # חלץ רק שורות ## ו-### (כותרות נושאים)
                headers = []
                for line in text.splitlines():
                    if line.startswith('## ') or line.startswith('### '):
                        # הסר סימני # ורווח
                        clean = re.sub(r'^#{2,3}\s+', '', line).strip()
                        # דלג על כותרות מטא-דאטה שחוזרות
                        if clean in ('חומרים שנסרקו לסיכום זה', 'עלות API',
                                     'לוח הכיתה', 'קישור להרצאות קודמות',
                                     'בדיקת רצף ממסמכי הקורס', '👨‍🏫 הערות הפרופסור'):
                            continue
                        headers.append(f'- {clean}')

                if headers:
                    block = '\n'.join(headers[:20])  # עד 20 כותרות לכל הרצאה
                    parts.append(f'{label}:\n{block}')
            except Exception:
                parts.append(f'{label}:\n- (לא זמין)')

        if not parts:
            return ''

        return '\n\n'.join(parts)

    # ── prompt ───────────────────────────────────────────────────────────────

    def _build_tips_prompt(
        self,
        summary_content: str,
        memory: str,
        persona: dict,
        course_name: str
    ) -> str:
        """בונה את ה-prompt לייצור טיפי פרופסור."""

        memory_section = ''
        if memory.strip():
            memory_section = f"""
## נושאים שנלמדו בהרצאות קודמות:
{memory}

"""

        common_exam = '\n'.join(f'- {t}' for t in persona.get('common_exam_topics', []))
        common_mistakes = '\n'.join(f'- {m}' for m in persona.get('common_mistakes', []))

        prompt = f"""אתה {persona.get('name', 'פרופסור')}, המרצה של קורס {course_name}.

## אופי התקשורת שלך:
{persona.get('personality', '')}

## מה אתה מדגיש:
{persona.get('emphasis', '')}

## נושאים שמופיעים לעיתים קרובות במבחנים בקורס:
{common_exam}

## טעויות נפוצות של סטודנטים:
{common_mistakes}

## סגנון הטיפים שלך:
{persona.get('tips_style', '')}

---
{memory_section}## סיכום השיעור הנוכחי:
{summary_content[:6000]}

---

## המשימה:
כתוב הערות פרופסור בעברית עבור סיכום זה. הכלל:

1. **טיפים למבחן** — מה מתוך חומר השיעור הזה סביר שיופיע במבחן ובאיזו צורה
2. **טעויות נפוצות לנושאי השיעור הזה** — הטעויות הספציפיות שסטודנטים עושים *בנושאים שנלמדו בשיעור זה*
3. **קשרים לחומר קודם** — כיצד השיעור הזה מתחבר לנושאים שנלמדו קודם (אם יש זיכרון קורס למעלה)
4. **מה לשנן מול מה להבין** — אילו נוסחאות/הגדרות חייבים לזכור בע"פ, ואילו אפשר להסיק

כתוב בגוף ראשון כאילו אתה הפרופסור מדבר ישירות לסטודנט.
השתמש בסגנון הטיפים שלך כפי שתואר למעלה.
אורך מומלץ: 250–400 מילים.
כתוב בעברית בלבד.
"""
        return prompt

    # ── file manipulation ────────────────────────────────────────────────────

    def _append_tips_to_file(self, md_path: str, tips: str, cost_info: dict):
        """
        מוסיף את סעיף הטיפים לסוף קובץ ה-MD ומעדכן את סעיף עלות API בכותרת.
        """
        path = Path(md_path)
        existing = path.read_text(encoding='utf-8')

        tokens_in  = cost_info.get('tokens_in', 0)
        tokens_out = cost_info.get('tokens_out', 0)
        cost_usd   = cost_info.get('cost_usd', 0.0)

        # ── עדכן סעיף עלות API בכותרת ──────────────────────────────────────────
        # מחפש את שורת הסה"כ ומוסיף לפניה שורת פרופסור + מעדכן הסה"כ
        def _update_cost_section(text: str) -> str:
            # מצא את שורת הסה"כ הקיימת
            total_m = re.search(r'- \*\*סה"כ\*\*: \$([0-9.]+)', text)
            if not total_m:
                return text  # לא נמצא — לא מעדכן
            existing_total = float(total_m.group(1))
            new_total = existing_total + cost_usd

            prof_line = (
                f'- **פרופסור** (Sonnet): '
                f'{tokens_in:,} input + {tokens_out:,} output = ${cost_usd:.4f}'
            )
            updated_total = f'- **סה"כ**: ${new_total:.4f}'

            # הוסף שורת פרופסור לפני שורת הסה"כ ועדכן את הסה"כ
            text = re.sub(
                r'(- \*\*סה"כ\*\*: \$[0-9.]+)',
                prof_line + '\n' + updated_total,
                text,
                count=1
            )
            return text

        updated_existing = _update_cost_section(existing)

        # ── הוסף בלוק טיפים לסוף הקובץ ─────────────────────────────────────────
        tips_block = f'\n---\n## 👨\u200d🏫 הערות הפרופסור\n\n{tips}\n'
        updated = updated_existing.rstrip('\n') + '\n' + tips_block
        path.write_text(updated, encoding='utf-8')
        print(f'💾 הערות פרופסור נכתבו ל: {path.name}')

    def get_tips_section(self, md_path: str) -> str:
        """
        מחזיר את סעיף הערות הפרופסור הקיים בקובץ, או מחרוזת ריקה אם אין.
        """
        path = Path(md_path)
        if not path.exists():
            return ''
        try:
            text = path.read_text(encoding='utf-8')
        except Exception:
            return ''

        match = re.search(
            r'##\s*👨\u200d🏫\s*הערות הפרופסור\s*\n(.*?)(?=\n##\s*|\Z)',
            text,
            re.DOTALL
        )
        if match:
            return match.group(1).strip()
        return ''

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_metadata(md_text: str) -> str:
        """מסיר את בלוק המטא-דאטה (חומרים + עלות) מעל קו ה--- הראשון."""
        parts = re.split(r'\n---\n', md_text, maxsplit=1)
        if len(parts) == 2:
            return parts[1].lstrip('\n')
        return md_text

    @classmethod
    def _calc_cost(cls, model: str, input_tokens: int, output_tokens: int) -> float:
        """מחשב עלות בדולרים לפי מחירון המודל."""
        key = model if model in cls._PRICING else ('sonnet' if 'sonnet' in model else 'haiku')
        inp_price, out_price = cls._PRICING[key]
        return (input_tokens * inp_price + output_tokens * out_price) / 1_000_000


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='הוסף טיפי פרופסור לסיכום')
    parser.add_argument('summary_path', help='נתיב לקובץ MD של הסיכום')
    parser.add_argument('course_key', help='מפתח הקורס (למשל: calculus_2)')
    args = parser.parse_args()

    agent = ProfessorAgent()
    result = agent.add_tips_to_summary(args.summary_path, args.course_key)
    if result['tips']:
        print(f'\n✅ הושלם. עלות: ${result["cost_usd"]:.4f}')
    else:
        print('\nℹ️  לא נוספו טיפים חדשים.')
