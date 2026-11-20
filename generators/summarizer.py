"""
AI Summarizer - מייצר סיכומים ממוקדים באמצעות Google Gemini API
"""

import os
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass
import io
import re
import json
from pathlib import Path

from typing import List, Dict, Optional
import google.genai as genai
from google.genai import types as genai_types
import pypdf  # maintained successor of PyPDF2 (unmaintained, known CVE)
import pdfplumber


class AISummarizer:
    def __init__(self):
        # Use GEMINI_API_KEY for access to Gemini 3.1 Pro
        api_key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
        # Remove GOOGLE_API_KEY temporarily so genai uses GEMINI_API_KEY
        _gk = os.environ.pop('GOOGLE_API_KEY', None)
        self.client = genai.Client(api_key=api_key)
        if _gk:
            os.environ['GOOGLE_API_KEY'] = _gk
        # Primary model for summaries. 02/10/2026 A/B on a real lecture (same
        # prompt): gemini-3.8-flash $0.053 vs 3.1-pro $0.157, and the Flash
        # summary was MORE complete (TL;DR, more tables, a proof Pro skipped).
        # The owner approved the switch. Pro stays first in the fallback chain.
        # Revert with one env var:  SSA_SUMMARY_MODEL=gemini-3.1-pro-preview
        self.model = os.getenv("SSA_SUMMARY_MODEL", "gemini-3.8-flash")
        # Fast model for board reading + missing-topics check (small queries, doesn't need pro)
        self.model_fast = "gemini-2.5-flash"
    
    def read_caption_file(self, caption_path: str) -> str:
        """קורא קובץ כתוביות SRT ומחזיר טקסט עם timestamps"""
        with open(caption_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # פרסר פשוט של SRT
        lines = content.split('\n')
        
        parsed_captions = []
        current_time = ""
        current_text = []
        
        for line in lines:
            line = line.strip()
            
            # זה שורת זמן?
            if '-->' in line:
                if current_text:
                    parsed_captions.append({
                        'time': current_time,
                        'text': ' '.join(current_text)
                    })
                    current_text = []
                
                # חלץ זמן התחלה
                current_time = line.split('-->')[0].strip()
            
            # זה טקסט?
            elif line and not line.isdigit():
                current_text.append(line)
        
        # הוסף את האחרון
        if current_text:
            parsed_captions.append({
                'time': current_time,
                'text': ' '.join(current_text)
            })
        
        # צור טקסט מעוצב
        formatted_text = "\n".join([
            f"[{cap['time']}] {cap['text']}"
            for cap in parsed_captions
        ])
        
        return formatted_text
    
    # Max pages per OCR batch (to stay within API limits)
    OCR_MAX_PAGES = 20
    OCR_BATCH_SIZE = 5  # pages per API call
    TEXT_SUFFIXES = ('.java', '.py', '.c', '.h', '.cpp', '.txt', '.md', '.csv', '.json', '.xml', '.sql')

    def read_pdf(self, pdf_path: str) -> str:
        """קורא PDF ומחזיר טקסט. אם הוא סרוק → OCR עם Claude Vision."""
        # Skip non-existent or inaccessible files gracefully
        from pathlib import Path as _Path
        if not _Path(pdf_path).exists():
            print(f"⚠️ PDF לא נמצא, מדלג: {pdf_path}")
            return ""
        # 10/11: Moodle hands us a lecture's .java / .ppt as "material"; each run read them as PDFs and logged
        # "❌ שגיאה בקריאת PDF: Stream has ended unexpectedly", which the health report then sent as a failure.
        # Code/text goes in as text (Software 1's tester class is real material); other non-PDFs are skipped quietly.
        try:
            with open(pdf_path, 'rb') as _f:
                head = _f.read(1024)
        except OSError:
            head = b''
        if not head.lstrip().startswith(b'%PDF'):
            if _Path(pdf_path).suffix.lower() in self.TEXT_SUFFIXES and b'\x00' not in head:
                try:
                    return _Path(pdf_path).read_text(encoding='utf-8', errors='replace')[:20000]
                except OSError:
                    return ""
            print(f"ℹ️ לא PDF, מדלג: {_Path(pdf_path).name}")
            return ""

        text = self._extract_pdf_text(pdf_path)

        # If less than 100 chars per page on average → scanned PDF, use OCR
        try:
            import fitz  # pymupdf
            doc = fitz.open(pdf_path)
            page_count = doc.page_count
        except Exception as e:
            print(f"⚠️ לא ניתן לפתוח PDF ({e.__class__.__name__}), מדלג: {_Path(pdf_path).name}")
            return text  # return whatever text we extracted earlier

        if page_count > 0 and len(text.strip()) < page_count * 100:
            print(f"📷 PDF סרוק ({page_count} עמודים, {len(text.strip())} תווים) — מפעיל OCR עם Gemini Vision")
            try:
                text = self._ocr_pdf_with_claude(pdf_path, page_count)
            except Exception as e:
                print(f"⚠️ OCR נכשל ({e.__class__.__name__}), ממשיך ללא: {_Path(pdf_path).name}")

        return text

    def _extract_pdf_text(self, pdf_path: str) -> str:
        """חילוץ טקסט רגיל מ-PDF (לא סרוק)."""
        text = ""
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text() or ""
                    text += page_text + "\n\n"
        except Exception:
            try:
                with open(pdf_path, 'rb') as f:
                    reader = pypdf.PdfReader(f)
                    for page in reader.pages:
                        text += (page.extract_text() or "") + "\n\n"
            except Exception as e:
                print(f"❌ שגיאה בקריאת PDF: {e}")
        return text

    def _ocr_pdf_with_claude(self, pdf_path: str, total_pages: int) -> str:
        """ממיר עמודי PDF לתמונות ושולח ל-Gemini Vision לחילוץ טקסט."""
        import fitz  # pymupdf

        pages_to_ocr = min(total_pages, self.OCR_MAX_PAGES)
        print(f"  OCR: {pages_to_ocr}/{total_pages} עמודים")

        doc = fitz.open(pdf_path)
        all_text_parts = []

        # Process in small batches to stay within API limits
        for batch_start in range(0, pages_to_ocr, self.OCR_BATCH_SIZE):
            batch_end = min(batch_start + self.OCR_BATCH_SIZE, pages_to_ocr)
            print(f"  OCR batch: עמודים {batch_start+1}-{batch_end}")

            parts = []
            for i in range(batch_start, batch_end):
                page = doc[i]
                mat = fitz.Matrix(120/72, 120/72)  # 120 DPI
                pix = page.get_pixmap(matrix=mat)
                img_bytes = pix.tobytes("jpeg", jpg_quality=75)
                parts.append(genai_types.Part.from_bytes(
                    data=img_bytes, mime_type="image/jpeg"
                ))

            parts.append(genai_types.Part.from_text(
                text=f"אלו עמודים {batch_start+1}-{batch_end} מ-PDF של שקפי הרצאה. חלץ את כל הטקסט מכל עמוד כפי שהוא — כולל נוסחאות, הגדרות ורשימות."
            ))

            try:
                response = self.client.models.generate_content(
                    model=self.model_fast,
                    contents=parts,
                    config=genai_types.GenerateContentConfig(max_output_tokens=4000, thinking_config=genai_types.ThinkingConfig(thinking_budget=0)),  # 2.5-flash thinking ate the output budget (truncation) and was billed
                )
                batch_text = response.text or ""
                all_text_parts.append(batch_text)
                try:
                    from utils.usage_tracker import log_gemini
                    log_gemini(response, self.model_fast, op='summarizer.ocr_pdf')
                except Exception: pass
                print(f"    ✅ {len(batch_text)} תווים")
            except Exception as e:
                print(f"    ❌ OCR batch נכשל: {e}")

        doc.close()
        ocr_text = "\n\n".join(all_text_parts)
        print(f"  ✅ OCR הושלם: {len(ocr_text)} תווים")
        return ocr_text
    
    def extract_tricks_and_tips(self, transcript: str) -> List[str]:
        """מזהה "טריקים" והערות חשובות בתמלול"""
        keywords = [
            'חשוב למבחן',
            'טעות נפוצה',
            'שימו לב',
            'מקרה קצה',
            'זכרו',
            'תזכרו',
            'לא לשכוח',
            'כדאי לזכור',
            'בבחינה',
            'זה יהיה במבחן',
            'זה חשוב',
            'זה קריטי'
        ]
        
        tricks = []
        lines = transcript.split('\n')
        
        for i, line in enumerate(lines):
            for keyword in keywords:
                if keyword in line.lower():
                    # קח את המשפט המלא + שורה אחרי
                    context = line
                    if i + 1 < len(lines):
                        context += " " + lines[i + 1]
                    
                    tricks.append({
                        'keyword': keyword,
                        'context': context.strip()
                    })
        
        return tricks
    
    def get_previous_summary_recap(self, course_key: str, kind: str,
                                     current_date: str = "", max_chars: int = 2000) -> str:
        """
        מוצא את הסיכום הקודם של אותו קורס ואותו סוג (הרצאה/תרגול),
        ומחזיר תקציר של הנושאים שנלמדו בו (עד max_chars תווים).
        current_date: YYYY-MM-DD — לא כולל סיכומים מאותו תאריך ואילך.
        """
        kind_dir_map = {'lecture': 'lectures', 'tutorial': 'tutorials'}
        kind_dir = kind_dir_map.get(kind, 'lectures')
        summary_dir = Path(f'data/summaries/{course_key}/{kind_dir}')
        if not summary_dir.exists():
            return ""

        # מצא את כל קבצי .md לפי שם (שמתחיל ב-YYYY-MM-DD)
        files = sorted(summary_dir.glob('*.md'))
        if not files:
            return ""

        # סנן: רק לפני התאריך הנוכחי
        if current_date:
            files = [f for f in files if f.name[:10] < current_date]
        if not files:
            return ""

        # קח את האחרון
        prev_file = files[-1]
        try:
            content = prev_file.read_text(encoding='utf-8')
        except Exception:
            return ""

        # חלץ שם הקובץ (ללא תאריך וסיומת) בתור כותרת
        prev_title = prev_file.stem[11:].strip(' -') if len(prev_file.stem) > 10 else prev_file.stem
        prev_date = prev_file.stem[:10]

        # נסה לחלץ את חלק "מפת השיעור" ו-"נוסחאות ומשפטים"
        sections = {}
        current_section = None
        section_buf = []

        for line in content.split('\n'):
            if line.startswith('## '):
                if current_section and section_buf:
                    sections[current_section] = '\n'.join(section_buf).strip()
                current_section = line[3:].strip()
                section_buf = []
            elif current_section:
                section_buf.append(line)
        if current_section and section_buf:
            sections[current_section] = '\n'.join(section_buf).strip()

        # בחר את החלקים הכי שימושיים לתזכורת
        recap_parts = []
        for sec_name in ['מפת השיעור', 'נוסחאות ומשפטים — דף עזר', 'מושגים חדשים שנלמדו']:
            for k, v in sections.items():
                if sec_name in k and v:
                    recap_parts.append(f"**{k}:**\n{v[:600]}")
                    break

        if not recap_parts:
            # fallback: טקסט ראשון בלי frontmatter
            text = '\n'.join(
                l for l in content.split('\n')
                if l.strip() and not l.startswith('---') and not l.startswith('#!')
            )
            recap_parts.append(text[:800])

        recap = '\n\n'.join(recap_parts)[:max_chars]
        return f"📅 תאריך: {prev_date} | כותרת: {prev_title}\n\n{recap}"

    @staticmethod
    def _extract_relevant_pdf_section(pdf_content: str, lecture_title: str,
                                       max_chars: int = 12000) -> str:
        """
        מחלץ את הקטע הרלוונטי מה-PDF לפי מספר ההרצאה שבכותרת.
        אם לא מצא — מחזיר את 12K התווים הראשונים.
        """
        import re as _re

        # חלץ מספר הרצאה מהכותרת
        num_match = _re.search(r'(?:הרצאה|lecture|שיעור)\s*(\d+)', lecture_title, _re.IGNORECASE)
        if not num_match:
            num_match = _re.search(r'\b(\d{1,2})\b', lecture_title)
        if not num_match:
            return pdf_content[:max_chars]

        lec_num = num_match.group(1)

        # חפש "הרצאה N" / "Lecture N" בגוף ה-PDF
        patterns = [
            rf'הרצאה\s*{lec_num}\b',
            rf'Lecture\s*{lec_num}\b',
            rf'שיעור\s*{lec_num}\b',
            rf'^\s*{lec_num}[.\)]\s',
        ]

        best_pos = -1
        for pat in patterns:
            m = _re.search(pat, pdf_content, _re.IGNORECASE | _re.MULTILINE)
            if m:
                best_pos = m.start()
                break

        if best_pos == -1:
            return pdf_content[:max_chars]

        # חלץ חלון סביב הסעיף שנמצא (עד הסעיף הבא או max_chars)
        section_text = pdf_content[best_pos:]

        # נסה לחתוך בסוף הסעיף (לפני הרצאה N+1)
        next_lec_num = str(int(lec_num) + 1)
        next_patterns = [
            rf'הרצאה\s*{next_lec_num}\b',
            rf'Lecture\s*{next_lec_num}\b',
            rf'שיעור\s*{next_lec_num}\b',
        ]
        end_pos = len(section_text)
        for pat in next_patterns:
            m = _re.search(pat, section_text, _re.IGNORECASE | _re.MULTILINE)
            if m and m.start() > 200:  # לא לחתוך מיד בהתחלה
                end_pos = min(end_pos, m.start())
                break

        section_text = section_text[:end_pos]

        # אם הסעיף קצר מדי, הוסף גם הקשר לפני
        if len(section_text) < 2000 and best_pos > 0:
            prefix = pdf_content[max(0, best_pos - 2000):best_pos]
            section_text = prefix + section_text

        return section_text[:max_chars]

    @staticmethod
    def course_style(course_key: str) -> str:
        """Per-course summary guidance from config/summary_styles.yaml ('' if none).
        A missing/broken file must never block a summary — it just adds nothing."""
        if not course_key:
            return ''
        try:
            import yaml
            p = Path(__file__).resolve().parent.parent / 'config' / 'summary_styles.yaml'
            data = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
            return str((data.get(course_key) or {}).get('focus') or '')
        except Exception as e:
            print(f"⚠️  summary_styles.yaml לא נקרא ({e}) — ממשיך בלי הנחיות קורס")
            return ''

    def create_summary_prompt(
        self,
        course_name: str,
        lecture_title: str,
        transcript: str,
        pdf_content: str,
        tricks: List[str],
        board_text: str = "",
        course_key: str = "",
        user_feedback: str = "",
        previous_recap: str = "",
        lecturer_notes: str = "",
        slides: str = "",
        covered: str = ""
    ) -> str:
        """יוצר prompt לסיכום"""

        board_section = ""
        if board_text.strip():
            board_section = (
                f"\n## תוכן הלוח — צילומי מסך + נוסחאות שנכתבו על הלוח:\n"
                f"(כל שורה [HH:MM:SS] מלווה בצילום מסך אמיתי מההרצאה — השתמש בהם!)\n"
                f"{board_text[:12000]}\n"
            )

        relevant_pdf = self._extract_relevant_pdf_section(pdf_content, lecture_title)

        # הקשר בין-הרצאתי — קרא תוכן מפות השיעור מהרצאות קודמות
        prev_topics = self._get_previous_topics(course_key or '', lecture_title)
        prev_section = f"\n\n{prev_topics}\n" if prev_topics else ""

        # הנחיות לפי קורס (config/summary_styles.yaml) — Java בתוכנה 1, נוסחאות
        # ודוגמאות פתורות בהסתברות, גרפים מתוארים במילים בכלכלה
        course_style_instruction = ""
        style = self.course_style(course_key)
        if style:
            course_style_instruction = f"""
## הנחיות ספציפיות לקורס הזה (חובה ליישם)
{style.strip()}
"""

        # הערות משתמש מריצה קודמת
        feedback_instruction = ""
        if user_feedback and user_feedback.strip():
            feedback_instruction = f"""
## הערות מהמשתמש מסיכום קודם (חובה להתייחס!)
המשתמש בדק את הסיכום הקודם ורשם את ההערות הבאות — **חובה לשפר בהתאם**:
{user_feedback.strip()}
"""

        continuity_instruction = ""
        if prev_topics:
            continuity_instruction = """
## בדיקת רצף נושאים (חשוב!)
בסוף הסיכום, הוסף סעיף קצר:
### קישור להרצאות קודמות
- אילו נושאים מהרצאה זו מרחיבים/ממשיכים הרצאות קודמות?
- האם יש **קפיצה או דילוג** על נושא שהיה צפוי לפי הרצף? (ציין אם כן)
- קשרים עם חומר קודם שכדאי לזכור
(אם אין ממשכים ברורים — ציין "הרצאה ראשונה בנושא חדש")
"""

        # Build previous recap block for the document
        prev_recap_block = ""
        if previous_recap:
            prev_recap_block = f"""
## תזכורת — מה נלמד בשיעור הקודם
> סיכום קצר של השיעור הקודם כדי להמשיך בצורה רציפה:

{previous_recap}

---
"""

        covered_block = ""
        if covered.strip():                               # 02/11 the owner: "אתה מסכם לי כמה הקצאות כמה פעמים"
            covered_block = (
                "\n## מה כבר סוכם בקורס הזה בסיכומים קודמים (כותרות) — לא ללמד שוב\n"
                "נושא שכבר הוסבר באחד מהסיכומים האלה: שורה אחת שמזכירה אותו + הפניה (\"ראה סיכום 19/10\"), "
                "ובלי להסביר מחדש את ההגדרות, הנוסחאות או הדוגמאות שלו. את העומק שמור למה שחדש בשיעור הזה: "
                "הרחבה, מקרה חדש, דוגמה חדשה, ובתרגול — התרגילים והפתרונות עצמם, לא התאוריה מההרצאה.\n"
                f"{covered[:5000]}\n")
        try:                                              # 18/10: every topic marked by its weight in his exams
            from agent.exam_weight import prompt_block
            exam_weight_block = prompt_block(course_key) if course_key else ""
        except Exception:
            exam_weight_block = ""
        slides_block = ""
        if slides.strip():                                # 19/10 the owner: "תשתמש גם במצגת" — every course
            slides_block = (
                "\n## המצגת / השקפים של השיעור הזה (מהמודל) — חובה לשלב\n"
                "אלה השקפים שהמרצה העלה לשיעור. עבור עליהם: כל הגדרה, נוסחה, דוגמה וקוד שבשקפים חייבים להופיע "
                "בסיכום במקום המתאים (סמן 🖥️ מהמצגת), כולל מה שהמרצה דילג עליו בעל-פה.\n"
                f"{slides[:40000]}\n")
        lecturer_block = ""
        if lecturer_notes.strip():                        # 18/10 the owner: one unified summary — the class + his notes
            lecturer_block = (
                "\n## סיכומי המרצה עצמו לנושאי השבוע (מהאתר שלו) — חובה לשלב\n"
                "זה הסיכום הרשמי של המרצה לאותם נושאים. צור *מסמך אחד מאוחד*: כל הגדרה, משפט ודוגמה מהסיכומים "
                "שלו חייבים להופיע במסמך (סמן 📘 מסיכומי המרצה), משולבים במקום המתאים בסדר ההרצאה; "
                "ואם יש סתירה בין התמלול לסיכום שלו — הסיכום שלו קובע, וציין זאת.\n"
                f"{lecturer_notes[:40000]}\n")

        prompt = f"""אתה עוזר לימודי אקדמי ברמת מצוין. המשימה שלך: ליצור **מסמך לימוד עצמאי ומקיף** — מסמך שסטודנט יכול ללמוד ממנו את כל החומר **בלי לצפות בהרצאה**. לא סיכום קצר, אלא ספר לימוד פרטי לשיעור הזה.

# עקרון מנחה
המבחן האולטימטיבי לסיכום: סטודנט שהחמיץ את ההרצאה — האם הוא יכול ללמוד הכל מהמסמך הזה? אם התשובה לא, הסיכום חסר.

# פרטי השיעור
קורס: {course_name}
נושא: {lecture_title}{prev_section}

# חומרי המקור:

## תמלול השיעור (עם timestamps):
{transcript}

## חומר כתוב (מצגות / שקפים / רשימות המרצה / סילבוס):
{relevant_pdf}
{lecturer_block}{slides_block}{exam_weight_block}{covered_block}
{board_section}
## הערות חשובות שזוהו בתמלול:
{json.dumps(tricks, ensure_ascii=False, indent=2)}

---

# חוקי כתיבה מרכזיים (חובה!)

## 1. הרחבה אקטיבית מ-PDFs
המרצה לעיתים קרובות **מקצר, מדלג, או רומז** על נושאים שמופיעים בהרחבה בחומר הכתוב.
**חובה**: כשמושג מוזכר קצרות בהרצאה אבל מופיע בהרחבה ב-PDFs — **הרחב מהחומר הכתוב**. סמן זאת כך:
> 📖 **השלמה מהחומר (עמוד N בשקפים):** [תוכן שלא נאמר במלואו בהרצאה אבל חיוני להבנה]

זה ערך מוסף עצום — סטודנט שלא קרא את החומר מקבל את ההקשר המלא.

**🔢 מספרי עמודי שקפים — חובה תמיד.**
כל הפניה לחומר הכתוב — ציין מספר עמוד אם ידוע (שקפים ממוספרים מ-1).
דוגמאות:
- `> 📖 **השלמה מהחומר (עמוד 12):** ...`
- ב-"מפת השיעור": `- [00:15:30] 🚨 ערכים עצמיים  ← עמוד 12 בשקפים`
- ב-"דף עזר נוסחאות": `**משפט קיילי-המילטון** (עמוד 18): A^n = ...`

אם אין מידע על עמודים — דלג, אל תמציא.

## 2. רמת פירוט = ספר לימוד, לא סיכום
- כל הוכחה — מלאה, שורה אחר שורה, עם הצדקה לכל מעבר
- כל דוגמה — פתרון מלא + **למה** הצעד הזה נבחר (לא רק "מחשבים X")
- כל הגדרה — פורמלית + תנאים מלאים + מקרים גבוליים
- כל משפט — הנחות + מסקנה + הוכחה (לפחות סקיצה) + דוגמה ליישום

## 3. תיוג עדיפות לבחינה
לכל נושא מרכזי, סמן אחד מאלה בכותרת:
- 🚨 **חובה למבחן** — המרצה אמר במפורש "יהיה במבחן" / "תשאלו על זה" / הדגיש מאוד
- ⭐ **חשוב** — מושג מרכזי בשיעור, סביר במבחן
- 📖 **רקע** — חשוב להבנה, פחות סביר כשאלה ישירה

## 4. ציטוטים מדויקים בטיפי המרצה
טיפים שיוחסו למרצה **חייבים** להיות בציטוט ישיר (או פרפראזה קרובה מאוד) **עם timestamp**. בלי המצאות.

# מבנה המסמך (Markdown — שמור על הסדר):
{prev_recap_block}
## ⚡ TL;DR — סיכום בפסקה
**חובה — לפני הכל.** 3-4 משפטים שמתמצתים את כל ההרצאה:
- מה הנושא המרכזי?
- מה המשפט/הרעיון העיקרי שעוסקים בו?
- מה הכלי/הטכניקה החדשה שלמדנו?
- למה זה חשוב/לאן זה מוביל?

זה הסעיף הראשון שהקורא רואה בסקירה חוזרת — כתוב אותו כאילו זה הדבר היחיד שיקרא.

**פורמט (העתק את המבנה הזה):**
```
> 📝 **TL;DR:** [פסקה זורמת של 3-4 משפטים, ללא רשימות, ללא נוסחאות מסובכות. שפה פשוטה ורגועה.]
```

## מפת השיעור
- רשימת כל הנושאים עם timestamps וסימן עדיפות (🚨/⭐/📖)
- פורמט: `- [00:15:30] 🚨 שם הנושא  ← עמוד 12 בשקפים`
- **תוסיף הפנייה לעמוד ב-PDF אם קיים** — עזר חזק בסקירה ערב המבחן

## תוכן השיעור — כרונולוגי
**עקוב אחרי סדר ההרצאה בדיוק.** אל תארגן מחדש לפי נושאים. הסיכום צריך להיות עוקב-בזמן עם השיעור.

### מערכת האייקונים (השתמש בעקביות):
- 📌 **הגדרה פורמלית** — מדויקת עם תנאים
- 🔢 **משפט / טענה** — הנחות + מסקנה
- 🛠️ **הוכחה** — שורה אחר שורה
- 💡 **אינטואיציה** — "למה זה נכון", "איך לחשוב על זה"
- ✏️ **דוגמה** — מלאה עם הצדקות
- ⚠️ **דגש המרצה** — ציטוט + timestamp
- 🔗 **קישור** — להרצאה/מושג קודם
- 📖 **השלמה מהחומר** — מ-PDFs (עם מספר עמוד אם זמין)
- 🚫 **טעות נפוצה** — מה לא לעשות ולמה
- 🔑 **לזכור** — נקודה קריטית
- 🌳 **זרימת הוכחה / היררכיה** — כשלבים ממוספרים + חצים לוגיים ($\\Rightarrow$), לא כדיאגרמה
---

### תבנית ליחידת תוכן

#### [HH:MM:SS] {{🚨/⭐/📖}} שם הנושא

📌 **הגדרה:** ...משפט פורמלי ב-LaTeX, עם כל התנאים...

💡 **אינטואיציה:** ...בלשון פשוטה — "מה זה אומר במציאות"...

🔢 **משפט (אם יש):** $$\\text{{משפט מלא}}$$
- **הנחות:** ...
- **מסקנה:** ...

🛠️ **הוכחה:**
1. **שלב 1:** ... — *הצדקה: למה זה תקף*
2. **שלב 2:** ... — *הצדקה: ...*
3. ∴ **מסקנה:** ...

✏️ **דוגמה {{🟢/🟡/🔴}}:** [שם/תיאור]
*(דרג כל דוגמה: 🟢 קל — בסיסי | 🟡 בינוני — רמת בחינה | 🔴 קשה — מאתגר)*
- **נתון:** ...
- **דרוש:** ...
- **אסטרטגיה:** *למה בוחרים בגישה הזו?*
- **פתרון:**
  1. ... — *מדוע*
  2. ... — *מדוע*
- **תוצאה:** ...
- 🚫 **טעות נפוצה:** ...

🚫 **איך לא לחשוב על זה (טעות נפוצה ספציפית למושג):**
- **חושבים בטעות:** ...
- **נכון:** ...

⚠️ **דגש המרצה [HH:MM:SS]:** *"ציטוט מהמרצה"* — למה זה חשוב

🔗 **קישור:** ...אם רלוונטי, עם wikilink לסיכום קודם: `[[2026-05-12 - הרצאה X]]`

📖 **השלמה מהחומר הכתוב:** ...

---

## 🎓 דוגמת זהב — איך נראה סעיף מצוין vs חלש

**✅ סעיף מצוין** (העתק את הסגנון הזה):

#### [00:15:30] 🚨 ערכים עצמיים — הגדרה ומציאה

📌 **הגדרה:** וקטור $v \\in \\mathbb{{R}}^n$, $v \\neq 0$, נקרא **וקטור עצמי** של $A \\in M_n(\\mathbb{{R}})$ אם קיים $\\lambda \\in \\mathbb{{R}}$ (ערך עצמי) כך ש: $Av = \\lambda v$.
*תנאים: $v$ חייב להיות שונה מאפס (אחרת המשוואה מתקיימת תמיד). $\\lambda$ יכול להיות 0.*

💡 **אינטואיציה:** הטרנספורמציה $A$ מותחת/מכווצת את $v$ בפקטור $\\lambda$ — בלי לסובב. זה הכיוון "המועדף" של $A$.

🛠️ **איך מוצאים?** $Av = \\lambda v \\Rightarrow (A - \\lambda I)v = 0$. כדי שיהיה פתרון לא-טריוויאלי: $\\det(A - \\lambda I) = 0$ (הפולינום האופייני).

✏️ **דוגמה 🟢:** $A = \\begin{{pmatrix}} 2 & 0 \\\\ 0 & 3 \\end{{pmatrix}}$, ערכים עצמיים: $\\lambda_1=2, \\lambda_2=3$.

**❌ סעיף חלש** (לא לעשות):

#### ערכים עצמיים
זה כשמתקיים $Av = \\lambda v$. רואים את זה דרך הפולינום האופייני.

*הבדל: המצוין נותן הגדרה מלאה עם תנאים, אינטואיציה, איך, דוגמה — והחלש רק זורק נוסחה.*

---

## טבלאות השוואה
**מותר ורצוי** להשתמש בטבלאות markdown להשוואות (לדוגמה: שיטה A vs שיטה B, מקרה רגיל vs מקרה מנוון). השתמש בהן כשהן עוזרות.

## נוסחאות חזותיות — בלי ASCII art
- **אל** תצייר תרשימים ב-ASCII (לא רנדר טוב ב-PDF).
- **כן** השתמש ב-LaTeX לציור מטריצות:
$$M = \\begin{{pmatrix}} a & b \\\\ c & d \\end{{pmatrix}}$$
- **כן** השתמש ברצף לוגי עם חצים: $A \\Rightarrow B \\Rightarrow C$
- **כן** השתמש ברשימות מובנות לגרפים/יחסים: "צומת A מחובר לצמתים B,C; צומת B..."

## 🚫 בלי דיאגרמות Mermaid — לעולם
ה-PDF שנשלח במייל **אינו** מרנדר Mermaid — בלוק ```` ```mermaid ```` יופיע כקוד גולמי שבור ומכוער (עברית + LaTeX מעורבבים). לכן **אסור להשתמש ב-Mermaid בכלל.**
במקום דיאגרמה, להצגת זרימת הוכחה / היררכיה / החלטה השתמש באחד מאלה — כולם נרנדרים מצוין גם ב-Obsidian וגם ב-PDF:
1. **שלבים ממוספרים** עם הצדקה לכל שלב (כמו בתבנית ההוכחה למעלה) — זו ברירת המחדל לזרימת הוכחה.
2. **שרשרת חצים לוגית** ב-LaTeX בתוך שורה: $A \\Rightarrow B \\Rightarrow C$.
3. **רשימות מקוננות** (תת-סעיפים) להיררכיית מושגים / טקסונומיה.
4. **טבלת markdown** לתרשים החלטה ("מתי שיטה A מול שיטה B").

## קופסאות זיכרון
> 🔑 **לזכור:** ...הנוסחה/הרעיון הקריטי בקטע הזה...

## בדיקות עצמיות בתוך הסיכום
אחרי קטע מורכב, הוסף:
> 🤔 **בדוק את עצמך:** שאלה אחת קצרה לוודא שהבנת.

---

# סעיפים בסוף המסמך (לפי הסדר):

## 📋 נוסחאות ומשפטים — דף עזר מרוכז
כל הנוסחאות, ההגדרות והמשפטים מהשיעור — לעיון מהיר ערב המבחן.
לכל אחד: שם → ניסוח ב-LaTeX → הקשר/מקרה שימוש.

## 🆕 מושגים חדשים שהוצגו
לכל מושג חדש:
- **שם** (עברית + אנגלית אם רלוונטי)
- **הגדרה קצרה** (משפט אחד-שניים)
- **דוגמה מינימלית**
- **מתי משתמשים בו**

## 💎 טיפי הזהב של המרצה
**רק ציטוטים אמיתיים עם timestamps.** אל תמציא.
פורמט:
- **[HH:MM:SS]** *"ציטוט מילולי או פרפראזה צמודה"* — למה זה חשוב

קטגוריות (קבץ לפי):
1. 🚨 **למבחן** — "זה יהיה בבחינה", "תזכרו את זה"
2. 🚫 **טעויות נפוצות** — "סטודנטים בדרך כלל טועים ב..."
3. 🎯 **טריקים** — "השיטה הקצרה היא..."
4. 🧠 **תובנות** — הסברים מיוחדים שהמרצה נתן

## 🧠 העמקה — מעבר לשיעור
כאן אתה מוסיף ערך **מעבר** למה שנאמר:
- **תבניות חוזרות:** "בכל פעם שרואים X — עושים Y"
- **קישורים לחומר קודם בקורס**
- **מלכודות עדינות** שהמרצה לא הזכיר אבל סטודנטים נופלים בהן
- **למה זה עובד?** — אינטואיציה עמוקה שלא הוצגה

## 🎯 חידון הבנה עצמית — 3 רמות
צור **6 שאלות** בשלוש רמות:

### רמה 1: שליטה (2 שאלות)
שאלות מילון: "הגדר X", "מהו משפט Y"
- **שאלה 1:** ...
  **תשובה:** ...

### רמה 2: יישום (2 שאלות)
"חשב", "פתור", "הוכח דוגמה ספציפית"
- **שאלה 3:** ...
  **תשובה:** ... (פתרון מלא)

### רמה 3: סינתזה (2 שאלות)
"מדוע", "השווה", "מה היה קורה אם...", "קשר בין X ל-Y"
- **שאלה 5:** ...
  **תשובה:** ...

## 💎 3 הדברים החשובים ביותר לזכור

אם תזכור רק 3 רעיונות מהשיעור הזה — אלה הם:

1. **[רעיון מרכזי 1]** — משפט/הגדרה/תובנה. למה זה חיוני?
2. **[רעיון מרכזי 2]** — ...
3. **[רעיון מרכזי 3]** — ...

*כתוב את הדחיסה האולטימטיבית — מה שהיית אומר לסטודנט שיש לו 60 שניות לבחינה.*

## 🌐 גלוסר עברית ↔ אנגלית

טבלה של כל המושגים החדשים בעברית עם המקבילה האנגלית — חיוני לחיפוש ב-StackExchange/Wikipedia/MathOverflow ולשיפור ה-RAG (אפשר לשאול גם באנגלית):

| עברית | English | מתי בשיעור |
|-------|---------|------------|
| ערך עצמי | eigenvalue | [00:15:30] |
| וקטור עצמי | eigenvector | [00:18:00] |
| פולינום אופייני | characteristic polynomial | [00:22:15] |
| ... | ... | ... |

**חוקים:**
- רק מושגים *חדשים* בשיעור — לא תרגום של מילים בסיסיות
- שורה אחת לכל מושג מרכזי
- אם אין מקבילה אנגלית מקובלת — דלג

## ⏱️ זמני קריאה ותכנון לימוד

```
📊 אומדן זמני קריאה:
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   קריאה מלאה ומעמיקה        ~XX דק׳
   רק 🚨 חובה למבחן            ~XX דק׳   ← לערב הבחינה
   רק נוסחאות + 3 הדברים       ~XX דק׳   ← לסקירה מהירה
   רק החידון                   ~XX דק׳   ← לבדוק שזכרת
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

חשב את הזמנים על בסיס: ~250 מילים/דקה לטקסט עברי, הוכחות 2x זמן (חצי המהירות), דוגמאות 1.5x.

---

# 🚫 כללי כתיבה — אסור לעשות

- **"כפי שראינו"** — אם מצטטים — ציין הרצאה/תאריך/מקור ספציפי
- **"ברור ש..." / "מהגדרה"** — תוכיח, אל תניח. תצטט את ההגדרה.
- **"פשוט מחשבים"** — תסביר את הצעד, מדוע נבחר
- **דילוג שלבים בחישוב** — כל שלב כתוב במפורש
- **לסכם נושא בלי הוכחה** — לפחות סקיצה של ההוכחה
- **טבלאות בלי הקשר** — הסבר תחילה למה ההשוואה רלוונטית

---

# כללי פורמט (חובה!)

- **עברית** לכל אורך המסמך
- **כלול הכל** — לא משמיטים נושא שנלמד
- **צילומי לוח** — כשיש שורת `![📷 ...]({{path}})` בחומר הלוח, **העתק אותה ישירות** לפני ההסבר הרלוונטי. אסור למחוק/לשנות נתיב.
- **חובה לסיים כל סעיף** — אסור לעצור באמצע משפט/הוכחה/דוגמה
- **נוסחאות**: LaTeX עם $...$ או $$...$$ — תמיד. לעולם לא backticks (`) למתמטיקה.
- **טבלאות**: מותרות להשוואות (markdown tables עם |). אל תשתמש בהן לתוכן רגיל.
- **כותרות**: ללא מספור בתחילה ("## נושא", לא "## 1. נושא")
- **Timestamps**: `[HH:MM:SS]` תמיד
- המסמך **שלם מתחילתו ועד סופו** — כולל החידון בסוף

# בדיקת איכות חובה (לפני סיום)

עבור על כל המסמך ובדוק:

1. **דיוק מתמטי:** כל נוסחה, סימן, כיוון אי-שוויון, גבולות אינטגרל — נכונים. חישבת בעצמך?
2. **שלמות תנאים:** כל משפט כולל את כל ההנחות. כל הגדרה כולל את כל המקרים.
3. **הוכחות:** כל הוכחה מלאה, ללא קפיצות. הצדקה לכל מעבר.
4. **דוגמאות:** כל פתרון נכון. בדקת את התוצאה?
5. **ציטוטים:** כל ציטוט שיוחס למרצה הוא **אמיתי** (מופיע בתמלול). לא המצאת.
6. **כיסוי:** כל נושא שעלה בהרצאה — כוסה במסמך.
7. **ערך מוסף:** הוספת השלמות מ-PDFs, תבניות, טעויות נפוצות — לא רק תיעוד מילולי.

**אם משהו לא וודאי — אל תכתוב.** עדיף לרשום "המרצה לא פירט; ראה מקור: [שם PDF]" מאשר לכתוב שגיאה.
{continuity_instruction}
{course_style_instruction}
{feedback_instruction}
התחל לכתוב את המסמך עכשיו:
"""

        return prompt
    
    def _compute_input_hash(self, caption_path: Optional[str],
                            pdf_path, video_path: Optional[str]) -> str:
        """Hash of all inputs that affect the summary output.
        Used by smart cache to skip re-summarization when inputs unchanged."""
        import hashlib
        h = hashlib.sha256()
        # Caption: hash content (since SRT may be regenerated)
        if caption_path and Path(caption_path).exists():
            try:
                h.update(b'CAP:')
                h.update(Path(caption_path).read_bytes())
            except Exception: pass
        # PDFs: hash file sizes + mtimes (avoid reading large PDFs every check)
        if pdf_path:
            paths = pdf_path if isinstance(pdf_path, list) else [pdf_path]
            for p in paths:
                if p and Path(p).exists():
                    st = Path(p).stat()
                    h.update(f'PDF:{p}:{st.st_size}:{int(st.st_mtime)};'.encode())
        # Video: only mtime (don't read it)
        if video_path and not video_path.startswith('http'):
            if Path(video_path).exists():
                st = Path(video_path).stat()
                h.update(f'VID:{int(st.st_mtime)};'.encode())
        return h.hexdigest()[:16]

    def _check_cache(self, course_key: str, content_type: str,
                     lecture_title: str, input_hash: str) -> Optional[str]:
        """Return existing summary path if its cached hash matches input_hash.
        We embed the hash in the .md as an HTML comment: <!-- input_hash: XXXX -->.
        """
        try:
            md_path = self.build_summary_path(course_key, content_type, lecture_title)
            if not md_path or not Path(md_path).exists(): return None
            head = Path(md_path).read_text(encoding='utf-8', errors='replace')[:2000]
            import re
            m = re.search(r'<!--\s*input_hash:\s*([0-9a-f]+)\s*-->', head)
            if m and m.group(1) == input_hash:
                return str(md_path)
        except Exception:
            pass
        return None

    def _check_budget_lock(self) -> Optional[str]:
        """Lock reason for the current month, else None (see utils/budget.py)."""
        from utils.budget import budget_lock_reason
        return budget_lock_reason()

    @staticmethod
    def covered_before(course_key: str, lecture_date: str, root: Path | None = None) -> str:
        """The headings of this semester's earlier summaries of the course — what's already been taught."""
        if not course_key or not lecture_date:
            return ""
        try:
            from collectors.course_files import SEMESTER_START
            start = SEMESTER_START.isoformat()
        except Exception:
            start = ""
        d = (root or Path('data') / 'summaries') / course_key
        out = []
        for md in sorted(d.glob('*/*.md')) if d.exists() else []:
            day = md.name[:10]
            if md.parent.name not in ('lectures', 'tutorials') or not (start <= day < lecture_date):
                continue
            try:
                heads = re.findall(r'^#{2,3}\s+(.+)$', md.read_text(encoding='utf-8'), re.M)
            except OSError:
                continue
            skip = re.compile(r'תזכורת|חומרים שנסרקו|גלוסר|זמני קריאה|קישור להרצאות|בדיקת|מפת|שאלות לתרגול|טעויות נפוצות')
            heads = [re.sub(r'[*`]|\[\d{2}:\d{2}:\d{2}\]', '', h).strip() for h in heads if not skip.search(h)]
            kind = 'תרגול' if md.parent.name == 'tutorials' else 'הרצאה'
            out.append(f"• {kind} {day[8:10]}/{day[5:7]}: " + ' · '.join(heads[:14]))
        return '\n'.join(out)

    SUB_SECONDS = 600
    SUB_MIN_CHARS = 6000

    @classmethod
    def _summary_on_subscription(cls, prompt: str) -> str | None:
        """The summary on the subscription, or None (off, resting, failed, or too short / not a document)."""
        if os.getenv('SSA_SUMMARY_VIA_SUB', '1') == '0' or not isinstance(prompt, str):
            return None
        try:
            from utils import gemini_sub
            print(f"🧠 מנסה קודם על מנוי Gemini Pro (agy)...")
            txt = gemini_sub.ask(prompt, timeout=cls.SUB_SECONDS)
        except Exception as e:
            print(f"⚠️ המנוי לא זמין לסיכום: {str(e)[:120]}")
            return None
        if not txt:
            return None
        if len(txt) < cls.SUB_MIN_CHARS or txt.count('#') < 3:
            print(f"⚠️ הסיכום מהמנוי קצר מדי ({len(txt)} תווים) — עובר ל-API")
            return None
        try:
            from utils.usage_tracker import log_usage
            log_usage(service='gemini-subscription', model=f'agy-{gemini_sub.MODEL}', op='summarizer.main.sub',
                      extra={'chars_in': len(prompt), 'chars_out': len(txt)})
        except Exception:
            pass
        return txt

    @staticmethod
    def _was_cut(resp) -> bool:
        try:
            return 'MAX_TOKENS' in str(resp.candidates[0].finish_reason)
        except (AttributeError, IndexError, TypeError):
            return False

    def _continue_if_cut(self, model: str, prompt: str, cfg: dict, resp, txt: str, rounds: int = 2) -> str:
        """29/10: half of the semester's summaries ended mid-sentence ("**אקסיומות קול", "וחייבים אתח", a formula
        cut inside a fraction) — a 32K-character Hebrew summary hits the 16K-token output cap and was saved as is.
        Cut by the cap → ask the same model to go on from exactly there (up to `rounds` times)."""
        for _ in range(rounds):
            if not self._was_cut(resp):
                break
            print(f"✂️ הסיכום נחתך במגבלת האורך ({len(txt)} תווים) — ממשיך מאותה נקודה")
            try:
                resp = self.client.models.generate_content(
                    model=model,
                    contents=[
                        genai_types.Content(role='user', parts=[genai_types.Part(text=prompt)]),
                        genai_types.Content(role='model', parts=[genai_types.Part(text=txt)]),
                        genai_types.Content(role='user', parts=[genai_types.Part(text=(
                            'נעצרת באמצע בגלל מגבלת האורך. המשך בדיוק מהתו שבו נעצרת — בלי לחזור על שום דבר, '
                            'בלי פתיח ובלי כותרת חדשה אם היית באמצע סעיף — עד סוף המסמך.'))]),
                    ],
                    config=genai_types.GenerateContentConfig(**cfg),
                )
                try:
                    from utils.usage_tracker import log_gemini
                    log_gemini(resp, model, op='summarizer.continue')
                except Exception:
                    pass
                more = resp.text or ''
            except Exception as e:
                print(f"⚠️ המשך הסיכום נכשל: {str(e)[:120]}")
                break
            if not more.strip():
                break
            txt += more
        return txt

    def generate_summary(
        self,
        course_name: str,
        lecture_title: str,
        caption_path: Optional[str] = None,
        pdf_path: Optional[str] = None,
        pdf_source_type: Optional[str] = None,
        video_path: Optional[str] = None,
        course_key: str = "",
        notebook_offset: int = 0,
        user_feedback: str = "",
        force_regenerate: bool = False,
    ) -> tuple:
        # Budget guard — abort if user hit the hard monthly cap
        lock_reason = self._check_budget_lock()
        if lock_reason:
            raise RuntimeError(
                f'⛔ Budget locked: {lock_reason}\n'
                f'Delete data/budget_lock.json or send "בטל מגבלת תקציב" via bot.'
            )
        """
        מייצר סיכום מקיף.
        מחזיר (summary_text, sources_dict) כדי שניתן לשמור כותרת מקורות.

        Smart cache: אם כל הקלטים (SRT, PDF, video) לא השתנו מאז סיכום קודם —
        נחזיר את הסיכום הקיים בלי לקרוא ל-Gemini. force_regenerate=True עוקף.
        """

        print(f"\n🤖 מייצר סיכום עבור: {lecture_title}")

        # Smart cache check (skip if forced or no course_key)
        input_hash = self._compute_input_hash(caption_path, pdf_path, video_path)
        if course_key and not force_regenerate:
            content_type = 'tutorials' if 'תרגול' in lecture_title or 'תרגיל' in lecture_title else 'lectures'
            cached = self._check_cache(course_key, content_type, lecture_title, input_hash)
            if cached:
                print(f"💾 Smart cache hit — קלטים זהים, מחזיר סיכום קיים: {cached}")
                try:
                    cached_text = Path(cached).read_text(encoding='utf-8')
                    return cached_text, {'cached': True, 'path': cached, 'input_hash': input_hash}
                except Exception:
                    pass  # fall through to regenerate

        self._current_input_hash = input_hash  # used by save_summary to embed

        # קרא תמלול
        transcript = ""
        if caption_path and Path(caption_path).exists():
            transcript = self.read_caption_file(caption_path)
            print(f"✅ נקרא תמלול ({len(transcript)} תווים)")

        # קרא PDF (תומך ב-path אחד או רשימה)
        lecturer_parts: list = []
        slide_parts: list = []
        pdf_content = ""
        if pdf_path:
            paths = pdf_path if isinstance(pdf_path, list) else [pdf_path]
            parts = []
            for p in paths:
                if p and Path(p).exists():
                    md = Path(p).with_suffix('.md')            # a lecturer PDF already read cleanly by Gemini (18/10)
                    if ('lecturer' in Path(p).parts or 'files' in Path(p).parts) and md.exists():
                        text = md.read_text(encoding='utf-8')
                    else:
                        text = self.read_pdf(p)
                    if text:
                        if 'lecturer' in Path(p).parts:        # the lecturer's own summaries for this week
                            lecturer_parts.append(f'### {Path(p).stem}\n{text}')
                        elif 'files' in Path(p).parts:         # 19/10: this lecture's slides from Moodle
                            slide_parts.append(f'### {Path(p).stem}\n{text}')
                        else:
                            parts.append(text)
                        print(f"✅ נקרא PDF ({len(text)} תווים): {Path(p).name}")
            pdf_content = "\n\n---\n\n".join(parts)
            if len(parts) > 1:
                print(f"  📚 שולב {len(parts)} PDFs ({len(pdf_content)} תווים סה\"כ)")

        # קרא תוכן לוח מוידאו (אם סופק) — תומך גם ב-URL וגם בנתיב מקומי
        board_text = ""
        board_frame_paths: list = []
        board_usage = {}
        if video_path and (video_path.startswith('http') or Path(video_path).exists()):
            try:
                from collectors.board_reader import extract_board_transcript

                # קבע תיקיית שמירת frames לפי course_key + תאריך
                _date_for_frames = ""
                if caption_path:
                    import re as _re2
                    _dm = _re2.search(r'(\d{4}-\d{2}-\d{2})', str(caption_path))
                    if _dm:
                        _date_for_frames = _dm.group(1)
                frames_dir = None
                if course_key and _date_for_frames:
                    frames_dir = f"data/frames/{course_key}/{_date_for_frames}"

                board_text, board_frame_paths, board_usage = extract_board_transcript(
                    video_path, course_name,
                    use_sonnet=False,
                    save_frames_dir=frames_dir,
                )
                if board_text:
                    print(f"✅ נקרא תוכן לוח ({len(board_text)} תווים, {len(board_frame_paths)} frames שמורים)")
            except Exception as e:
                print(f"⚠️  שגיאה בקריאת לוח: {e}")

        # זהה טריקים
        tricks = self.extract_tricks_and_tips(transcript)
        print(f"✅ זוהו {len(tricks)} הערות חשובות")

        # חלץ תקציר מהשיעור הקודם (לתזכורת בראש הסיכום)
        lecture_date = ""
        if caption_path:
            import re as _re
            m = _re.search(r'(\d{4}-\d{2}-\d{2})', str(caption_path))
            if m:
                lecture_date = m.group(1)
        kind = 'tutorial' if any(k in lecture_title.lower() for k in ['תרגול', 'tutorial', 'recitation', 'תרגיל']) else 'lecture'
        previous_recap = self.get_previous_summary_recap(course_key or '', kind, lecture_date)
        covered = self.covered_before(course_key or '', lecture_date)
        if previous_recap:
            print(f"📎 נמצא סיכום קודם לתזכורת ({len(previous_recap)} תווים)")

        # משוב מהדשבורד/טלגרם על סיכומים קודמים בקורס (👍/👎 + הערות) — עד 10/2026
        # רק main.py קרא אותו, והצינור הלילי התעלם ממנו
        _used_pending_feedback = False
        if not (user_feedback or '').strip() and course_key:
            try:
                from utils.summary_feedback import pending_feedback
                user_feedback = pending_feedback(course_key)
                _used_pending_feedback = bool(user_feedback)
                if _used_pending_feedback:
                    print(f"📝 משוב קודם על הקורס נכנס לסיכום ({user_feedback.count(chr(10)) + 1} הערות)")
            except Exception as _fe:
                print(f"⚠️  קריאת משוב נכשלה: {_fe}")

        # צור prompt
        prompt = self.create_summary_prompt(
            course_name,
            lecture_title,
            transcript,
            pdf_content,
            tricks,
            board_text,
            course_key=course_key,
            user_feedback=user_feedback,
            previous_recap=previous_recap,
            lecturer_notes="\n\n".join(lecturer_parts),
            slides="\n\n".join(slide_parts),
            covered=covered,
        )
        
        # שלח ל-Gemini עם streaming
        print(f"🧠 שולח ל-Gemini API ({self.model})...")

        # Multi-model fallback chain. Verified via direct API test (2026-05-21):
        #   ✅ gemini-3.1-pro-preview, gemini-3-pro-preview, gemini-2.5-pro,
        #      gemini-3-flash-preview, gemini-2.5-flash
        #   ❌ gemini-3-pro / gemini-3.1-pro / gemini-2.5-pro-preview (404 NOT_FOUND)
        FALLBACK_MODELS = [
            self.model,                    # primary (gemini-3.8-flash, see __init__)
            'gemini-3.1-pro-preview',      # previous primary — quality fallback
            'gemini-3-pro-preview',        # other 3-series pro
            'gemini-2.5-pro',              # established 2.5 pro
            'gemini-3.8-flash',            # newest flash; 3-flash-preview is billed as 3.5-flash ($1.5/$9)
            'gemini-2.5-flash',            # cheap last-resort
        ]
        # De-dup while preserving order
        seen = set()
        FALLBACK_MODELS = [m for m in FALLBACK_MODELS if not (m in seen or seen.add(m))]

        import time
        summary = None
        summary_usage = {}
        used_model = self.model
        last_err = None

        # 19/11 the owner: "$50 זה הרבה". The summary goes to his Gemini Pro subscription first (agy, $0; measured on the
        # server: a 64K-char transcript → a 32K-char summary in 106s); the paid models below only when it can't answer
        # or the answer is too short to be the whole document.
        sub = self._summary_on_subscription(prompt)
        if sub:
            summary, used_model = sub, 'agy-subscription'
        for model_idx, _model_name in enumerate([] if summary else FALLBACK_MODELS):
            for attempt in range(2):   # 2 tries per model
                try:
                    if model_idx > 0 or attempt > 0:
                        print(f"🔄 ניסיון {attempt+1}/2 עם {_model_name}...")
                    # max_output_tokens 32K caused 400 Bad Request on 3.1-pro-preview
                    # with long prompts. 16K is safe across all current models.
                    # thinking_budget=0 is REJECTED by all -pro models (require
                    # thinking). Only set it for flash variants (speed boost there).
                    _cfg = dict(
                        max_output_tokens=16000,
                        temperature=0.3,
                        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                    )
                    if 'flash' in _model_name.lower():
                        _cfg['thinking_config'] = genai_types.ThinkingConfig(thinking_budget=0)
                    _resp = self.client.models.generate_content(
                        model=_model_name,
                        contents=prompt,
                        config=genai_types.GenerateContentConfig(**_cfg),
                    )
                    txt = _resp.text or ""
                    if not txt or len(txt) < 500:
                        raise RuntimeError(f"תגובה ריקה/קצרה מדי ({len(txt)} תווים)")
                    txt = self._continue_if_cut(_model_name, prompt, _cfg, _resp, txt)
                    summary = txt
                    used_model = _model_name
                    if hasattr(_resp, "usage_metadata") and _resp.usage_metadata:
                        um = _resp.usage_metadata
                        summary_usage = {
                            "model": _model_name,
                            "input_tokens": getattr(um, "prompt_token_count", 0) or 0,
                            "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
                        }
                    try:
                        from utils.usage_tracker import log_gemini
                        log_gemini(_resp, _model_name, op='summarizer.main')
                    except Exception: pass
                    break
                except Exception as e:
                    last_err = e
                    err_str = str(e)
                    is_overload = '503' in err_str or 'UNAVAILABLE' in err_str or 'overloaded' in err_str.lower()
                    is_quota = '429' in err_str or 'quota' in err_str.lower() or 'RESOURCE_EXHAUSTED' in err_str
                    print(f"⚠️ {_model_name} ניסיון {attempt+1}: {err_str[:100]}")
                    if is_overload:
                        # Wait briefly then try next model
                        if attempt == 0:
                            time.sleep(10)
                        break  # move to next model
                    elif is_quota:
                        # Quota hit → skip to next model immediately
                        break
                    else:
                        # Other error — quick retry on same model
                        if attempt == 0:
                            time.sleep(5)
            if summary:
                break

        if not summary:
            return f"# שגיאה ביצירת סיכום\n\nכל המודלים נכשלו. אחרון: {last_err}", {}

        print(f"✅ סיכום הופק! ({len(summary)} תווים) [model={used_model}]")

        # בדיקת רצף ודילוגים עם Haiku (רק אם יש PDF של כל הסמסטר)
        lec_num = self._extract_lecture_number(lecture_title)
        # Apply notebook_offset: Panopto lecture N = notebook chapter N + offset
        actual_lec_num = (lec_num + notebook_offset) if lec_num else None
        syllabus_section = (
            self._get_syllabus_section(pdf_content, actual_lec_num, window=1)
            if actual_lec_num and pdf_content
            else self._extract_relevant_pdf_section(pdf_content, lecture_title)
        )
        missing, haiku_usage = self._check_missing_topics(
            summary, syllabus_section, transcript[:5000],
            lecture_title=lecture_title
        )
        if missing:
            print(f"📋 נוספו נושאים חסרים ({len(missing)} תווים)")
            summary += missing

        # הדפס סיכום שימוש
        total_cost = 0.0
        for component, u in [('סיכום ראשי', summary_usage),
                              ('קריאת לוח', board_usage),
                              ('בדיקת רצף', haiku_usage)]:
            if u:
                cost = self._calc_cost(u.get('model', ''), u.get('input_tokens', 0), u.get('output_tokens', 0))
                total_cost += cost
                print(f"  💰 {component}: {u.get('input_tokens', 0):,} in + {u.get('output_tokens', 0):,} out = ${cost:.4f}")
        if total_cost > 0:
            print(f"  💰 סה\"כ: ${total_cost:.4f}")

        if _used_pending_feedback and summary and len(summary) > 2000:
            try:
                from utils.summary_feedback import mark_consumed
                mark_consumed(course_key)
            except Exception:
                pass

        # formula fact-check against transcript + board (appends a short section)
        if summary and len(summary) > 2000 and os.getenv('SSA_FORMULA_CHECK', '1') != '0':
            try:
                from generators.formula_check import check_formulas
                _fc = check_formulas(summary, transcript, board_text, self.client)
                if _fc:
                    summary += _fc
                    print("🔎 בדיקת נוסחאות: " + ("נמצאו נוסחאות לבדיקה" if "⚠️" in _fc else "תקין"))
            except Exception as _fce:
                print(f"⚠️  בדיקת נוסחאות דולגה: {_fce}")

        # clickable timestamps → open the Panopto recording at that second
        try:
            from utils.panopto_links import video_id_for_srt, linkify_timestamps
            _vid = video_id_for_srt(caption_path or '')
            if _vid:
                summary = linkify_timestamps(summary, _vid)
                print("🔗 חותמות הזמן בסיכום מקושרות להקלטה בפנופטו")
        except Exception as _le:
            print(f"⚠️  קישור חותמות זמן נכשל ({_le}) — הסיכום נשמר בלי קישורים")

        sources = {
            'transcript': transcript,
            'pdf_content': pdf_content,
            'board_text': board_text,
            'caption_path': caption_path,
            'pdf_path': pdf_path,
            'pdf_source_type': pdf_source_type,
            'video_path': video_path,
            'usage': {
                'summary': summary_usage,
                'board': board_usage,
                'haiku_check': haiku_usage,
            }
        }
        return summary, sources
    
    @staticmethod
    def build_summary_path(course_key: str, content_type: str, title: str,
                           date: str = "") -> str:
        """
        בונה נתיב סיכום לפי המבנה:
        data/summaries/{course_key}/{lectures|tutorials}/{YYYY-MM-DD} - {title}.md
        content_type: 'lecture' או 'tutorial'
        date: 'YYYY-MM-DD' — אם ריק, משתמש בתאריך היום
        """
        import re as _re
        from datetime import date as _date
        folder = 'lectures' if content_type == 'lecture' else 'tutorials'
        safe = _re.sub(r'[<>:"/\\|?*]', '', title).strip()
        date_str = date[:10] if date else str(_date.today())
        return f"data/summaries/{course_key}/{folder}/{date_str} - {safe}.md"

    def _get_previous_topics(self, course_key: str, current_title: str) -> str:
        """
        קורא את סעיף "מפת השיעור" מכל סיכום קודם בקורס.
        מחזיר טקסט קומפקטי עם הנושאים שכבר נלמדו — כדי שהסיכום החדש יוכל
        לקשר להם ולזהות אם חסר נושא צפוי ברצף.
        """
        import re as _re
        summaries_dir = Path(f"data/summaries/{course_key}/lectures")
        if not summaries_dir.exists():
            return ""

        files = sorted(summaries_dir.glob("*.md"))
        parts = []

        for f in files:
            stem = f.stem  # e.g. "2026-04-12 - מרצה_כ - הרצאה 1"
            if current_title in stem:
                continue  # דלג על ההרצאה הנוכחית

            # חלץ שם הרצאה (אחרי התאריך)
            label_parts = stem.split(' - ', 1)
            label = label_parts[1] if len(label_parts) > 1 else stem

            try:
                text = f.read_text(encoding='utf-8')
                # חפש סעיף "מפת השיעור" — מכיל רשימת נושאים עם timestamps
                map_match = _re.search(
                    r'##\s*מפת השיעור\s*\n(.*?)(?=\n##|\Z)',
                    text, _re.DOTALL
                )
                if map_match:
                    topics_raw = map_match.group(1).strip()
                    # קח רק את שמות הנושאים (בלי timestamps)
                    topics = _re.sub(r'\[[\d:]+\]\s*', '', topics_raw)
                    # קצץ לעד 600 תווים לכל הרצאה
                    topics = topics[:600].strip()
                    parts.append(f"**{label}**: {topics}")
                else:
                    # אם אין מפה — קח שורות ראשונות של הסיכום
                    first_lines = '\n'.join(text.splitlines()[5:15]).strip()[:300]
                    parts.append(f"**{label}**: {first_lines}")
            except Exception:
                parts.append(f"**{label}**: (לא זמין)")

        if not parts:
            return ""

        return "## נושאים שנלמדו בהרצאות קודמות:\n" + "\n\n".join(parts)

    @staticmethod
    def _extract_lecture_number(title: str) -> Optional[int]:
        """מחלץ מספר הרצאה מהכותרת."""
        import re as _re
        m = _re.search(r'(?:הרצאה|lecture|שיעור)\s*(\d+)', title, _re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = _re.search(r'\b(\d{1,2})\b', title)
        return int(m.group(1)) if m else None

    def _get_syllabus_section(self, pdf_content: str, lec_num: int,
                               window: int = 1) -> str:
        """
        מחלץ את הרשימות של הרצאות lec_num-window עד lec_num מה-PDF.
        window=1 → מחזיר גם את ההרצאה הקודמת כהקשר.
        """
        import re as _re
        parts = []
        for n in range(max(1, lec_num - window), lec_num + 1):
            patterns = [
                rf'(?:הרצאה|שיעור)\s*{n}\b',
                rf'Lecture\s*{n}\b',
            ]
            best = -1
            for pat in patterns:
                m = _re.search(pat, pdf_content, _re.IGNORECASE | _re.MULTILINE)
                if m:
                    best = m.start()
                    break
            if best == -1:
                continue
            next_lec = n + 1
            end = len(pdf_content)
            for pat in [rf'(?:הרצאה|שיעור)\s*{next_lec}\b', rf'Lecture\s*{next_lec}\b']:
                m2 = _re.search(pat, pdf_content[best:], _re.IGNORECASE | _re.MULTILINE)
                if m2 and m2.start() > 100:
                    end = best + m2.start()
                    break
            parts.append(pdf_content[best:min(end, best + 5000)])
        return "\n\n".join(parts)

    def _check_missing_topics(self, summary: str, pdf_section: str,
                               transcript_sample: str,
                               lecture_title: str = "") -> tuple:
        """
        בדיקת Gemini Flash:
        1. אילו נושאים מרשימות ההרצאה הנוכחית חסרים בסיכום
        2. האם המרצה דילג על נושאים שהיו צפויים לפי הרצף ברשימות
        מחזיר (טקסט קצר לצרף לסיכום, usage_dict).
        """
        if not pdf_section.strip():
            return "", {}
        try:
            prompt_text = f"""אתה בודק תוכן הרצאה אקדמית.

=== רשימות הרצאה מהחוברת (מה שהיה צריך להילמד) ===
{pdf_section[:4000]}

=== מה שנאמר בפועל בהרצאה (תמלול) ===
{transcript_sample[:2000]}

=== הסיכום שנוצר ===
{summary[:4000]}

ענה על שתי שאלות בלבד:
1. **נושאים שדולגו**: האם ישנם נושאים ברשימות שהמרצה לא הזכיר כלל בהרצאה?
2. **דילוג בין שיעורים**: האם נראה שהמרצה קפץ על חלק מהחוברת?

אם הכל תקין — כתוב: תקין
אם יש בעיה ברורה — כתוב בקצרה מה חסר (עד 3 שורות)."""

            resp = self.client.models.generate_content(
                model=self.model_fast,
                contents=prompt_text,
                config=genai_types.GenerateContentConfig(max_output_tokens=700, thinking_config=genai_types.ThinkingConfig(thinking_budget=0)),  # 2.5-flash thinking ate the output budget (truncation) and was billed
            )
            usage = {}
            if hasattr(resp, 'usage_metadata') and resp.usage_metadata:
                um = resp.usage_metadata
                usage = {
                    'model': self.model_fast,
                    'input_tokens': getattr(um, 'prompt_token_count', 0) or 0,
                    'output_tokens': getattr(um, 'candidates_token_count', 0) or 0,
                }
            result = (resp.text or "").strip()
            if 'תקין' in result and len(result) < 50:
                return "", usage
            return f"\n\n## בדיקת רצף ממסמכי הקורס\n{result}", usage
        except Exception:
            return "", {}

    @staticmethod
    def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
        """מחשב עלות ב-$ לפי מחירון Gemini (per million tokens)."""
        # Prices per million tokens: (input, output)
        pricing = {
            'gemini-3.1-pro':   (3.50, 10.50),
            'gemini-3-pro':     (3.50, 10.50),
            'gemini-3.8-flash': (0.75,  3.75),   # promo until 31.12.2026
            'gemini-3-flash':   (0.50,  3.00),
            'gemini-3.1-flash': (0.30,  1.25),
            'gemini-2.5-pro':   (1.25, 10.00),
            'gemini-2.5-flash': (0.075, 0.30),
        }
        for key, prices in pricing.items():
            if key in model:
                return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
        return 0.0

    @staticmethod
    def build_sources_header(transcript: str, pdf_content: str, board_text: str,
                              caption_path: Optional[str] = None,
                              pdf_path: Optional[str] = None,
                              pdf_source_type: Optional[str] = None,
                              video_path: Optional[str] = None,
                              usage: Optional[dict] = None) -> str:
        """בונה כותרת מקורות בראש הסיכום."""
        from datetime import date as _date
        lines = [f"*נוצר ב-{_date.today().strftime('%d/%m/%Y')}*\n"]
        lines.append("## חומרים שנסרקו לסיכום זה\n")

        # ── תמלול ──
        if transcript and caption_path:
            is_whisper = caption_path.endswith("_whisper.srt")
            how = "OpenAI Whisper (הוקלד אוטומטית — אין כתוביות מקוריות)" if is_whisper else "כתוביות Panopto (מקוריות)"
            lines.append(f"- **הרצאה מוקלטת (קול)** — {how} · {len(transcript):,} תווים")
        elif transcript:
            lines.append(f"- **הרצאה מוקלטת (קול)** — תמלול · {len(transcript):,} תווים")

        # ── PDF ──
        if pdf_content and pdf_path:
            if isinstance(pdf_path, list):
                pdf_name = " + ".join(Path(p).name for p in pdf_path)
            else:
                pdf_name = Path(pdf_path).name
            src_label = {
                'moodle':     'הורד מ-Moodle',
                'wikidot':    'הורד מאתר הקורס (wikidot)',
                'static_pdf': 'רשימות הרצאות (PDF מקומי)',
            }.get(pdf_source_type or '', 'PDF')
            lines.append(f"- **חומר כתוב** — `{pdf_name}` · {src_label} · {len(pdf_content):,} תווים")

        # ── צילומי מסך (board) ──
        if board_text and usage and usage.get('board'):
            bu = usage['board']
            n_frames = bu.get('frame_count', '?')
            interval = bu.get('frame_interval_sec', 45)
            model_lbl = 'Sonnet' if 'sonnet' in bu.get('model', '') else 'Haiku'
            lines.append(
                f"- **לוח הכיתה** — {n_frames} צילומי מסך (כל {interval} שניות) · "
                f"נקראו עם Claude Vision ({model_lbl}) · {len(board_text):,} תווים"
            )
        elif board_text:
            lines.append(f"- **לוח הכיתה** — צילומי מסך · {len(board_text):,} תווים")

        if not transcript and not pdf_content and not board_text:
            lines.append("- *(אין מקורות זמינים)*")

        lines.append("\n---\n")
        return "\n".join(lines)

    def save_summary(self, summary: str, output_path: str, course_name: str = "",
                     lecture_title: str = "", sources: dict = None) -> str:
        """שומר סיכום כ-Markdown וגם כ-PDF. מחזיר נתיב ה-PDF."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        s = sources or {}
        sources_header = self.build_sources_header(
            s.get('transcript', ''), s.get('pdf_content', ''), s.get('board_text', ''),
            caption_path=s.get('caption_path'), pdf_path=s.get('pdf_path'),
            pdf_source_type=s.get('pdf_source_type'),
            video_path=s.get('video_path'),
            usage=s.get('usage')
        )
        # Smart-cache hash marker — invisible in render, used by _check_cache
        input_hash = getattr(self, '_current_input_hash', None) or s.get('input_hash')
        cache_marker = f'<!-- input_hash: {input_hash} -->\n' if input_hash else ''
        full_content = cache_marker + sources_header + summary

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(full_content)

        # צור PDF לצד ה-MD
        pdf_path = output_path.replace('.md', '.pdf')
        try:
            from generators.pdf_generator import markdown_to_pdf
            markdown_to_pdf(summary, pdf_path, title=lecture_title, course_name=course_name)
            print(f"📄 PDF נוצר: {pdf_path}")
        except Exception as e:
            print(f"⚠️  שגיאה ביצירת PDF: {e}")
            pdf_path = None

        return pdf_path
        
        print(f"💾 הסיכום נשמר ב: {output_path}")


if __name__ == "__main__":
    # טסט
    summarizer = AISummarizer()
    
    # דוגמה
    summary = summarizer.generate_summary(
        course_name="חדו\"א 2ב",
        lecture_title="אינטגרלים - הרצאה 5",
        caption_path="data/panopto/example_lecture.srt",
        pdf_path="data/moodle/calculus_2/lecture5_slides.pdf"
    )
    
    # שמור
    summarizer.save_summary(summary, "data/summaries/calculus_2_lecture5.md")
