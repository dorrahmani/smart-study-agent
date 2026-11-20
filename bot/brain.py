"""
bot/brain.py — Smart natural-language router.
Takes user message → asks Gemini to classify intent → returns action+params.
"""
from __future__ import annotations
import json, os, re
from typing import Any

INTENT_PROMPT = """אתה router חכם לבוט עוזר לימוד אקדמי. קבל הודעת משתמש (עברית/אנגלית) והחזר JSON המתאר את הפעולה.

{history_block}הודעת המשתמש (זו ההודעה לסיווג): «{message}»

================================================================
חוקי-זהב לסיווג (חשוב!)
================================================================
1. כל הודעה מסווגת לאחת מהפעולות למטה. אם **בכלל לא בטוח** → "ask".
2. שאלות (מה/איך/למה/איפה/מתי/האם/הסבר/תסביר/תפרט) → כמעט תמיד "ask".
3. בקשת פעולה כבדה (יצירת סיכום/חידון/וידאו/פודקאסט) — רק אם המשתמש אמר במפורש "תכין/תייצר/תסכם/תכתוב".
4. בקשת מידע/סקירה על חומר קיים → "ask" (גם אם המילה "סיכום" נאמרה).
5. הודעות קצרות מאוד / לא ברורות / שיחת חולין → "greeting" או "ask".
6. **תסכם לי את הנושאים** = ask (סקירת נושאים מחומר קיים).
   **תסכם לי את ההרצאה** = summarize (יצירת קובץ חדש).

================================================================
הפעולות:
================================================================

— "ask" — שאלה כללית, הסבר, סקירה, חיפוש מושג, "מה למדנו"
  ביטויים: "מה זה X?", "תסביר", "תפרט", "סכם לי את הנושאים", "מה למדנו ב-X", "תן סקירה",
            "מה החומר ל-Y", "איך עושים", "מה ההבדל בין", "תזכיר לי מה זה"
  params: {{}}

— "file_send" — לשלוח קובץ קיים בצ'אט (PDF של הרצאה/תרגיל/סיכום)
  ביטויים: "תשלח לי", "שלח", "תן לי את הקובץ"
  לא כולל: בקשת מייל (זה email_send).
  params: {{ "query": "<תיאור הקובץ>" }}

— "email_send" — לשלוח קובץ/קבצים **למייל** (לא לצ'אט)
  ביטויים: "תשלח לי למייל את כל הסיכומים במדמ"ח", "שלח במייל",
            "מייל לי את ההרצאה", "שלח לי לאימייל",
            "תשלח לו ל-X@gmail.com", "עכשיו תשלח את זה לכתובת אחרת"
  params: {{
    "query": "<תיאור הקבצים, או 'אותו דבר' אם המשך לפעולה קודמת>",
    "recipient": "<כתובת מייל אם מופיעה בהודעה, אחרת השמט>",
    "file_type": "<'pdf' אם המשתמש אמר 'רק PDF' / 'רק סיכומי PDF' / 'PDFs בלבד', 'md' אם 'רק MD', אחרת השמט>"
  }}
  **חשוב:** אם המשתמש כותב כתובת מייל בהודעה (X@Y.com) — חלץ אותה ל-recipient.
  אם אומר "רק PDF" או "סיכומי PDF" — file_type="pdf".

— "zip_send" — לארוז ולשלוח קבוצת קבצים כקובץ ZIP בודד (לטלגרם)
  ביטויים: "שלח לי את כל הסיכומים בקובץ אחד", "תארוז ושלח",
            "תן לי zip של כל ההרצאות בלינארית"
  params: {{ "query": "<תיאור הקבצים>" }}

— "summarize" — ליצור סיכום חדש מהרצאה (SRT → MD + PDF). פעולה כבדה ~3-5 דק'.
  ביטויים: "תסכם את ההרצאה", "תכין סיכום להרצאה", "סכם את ההקלטה",
            "תסכם הרצאות חדשות", "תייצר סיכום"
  לא: "תסכם נושאים/חומר/קורס" — זה ask!
  params: {{
    "course": "<course_key אופציונלי>",
    "scope": "today|latest|recent",
    "date": "<YYYY-MM-DD אופציונלי>"
  }}

— "ical_send" — לשלוח קובץ לוח שנה (.ics) עם כל הדדליינים. אחרי שמוסיפים את הקובץ למכשיר — האייפון מסנכרן את הדדליינים אוטומטית.
  ביטויים: "תן לי קלנדר", "ייצא דדליינים ללוח שנה", "תכין קובץ ics",
            "תוסיף לי את הדדליינים ללוח שנה", "תייצר קובץ של לוח שנה"
  params: {{}}

— "homework_grade" — לבדוק שיעורי בית של המשתמש. דורש שיצרף תמונה/PDF.
  **חשוב:** אם ההודעה כוללת רק טקסט (בלי קובץ מצורף עכשיו), זה כנראה בקשה לא שלמה —
  ענה ש"צריך לצרף צילום של התרגיל הפתור".
  ביטויים: "תבדוק את שיעורי הבית", "תבדוק את הפתרון", "תבדוק את התרגיל הזה",
            "תגיד אם נכון", "תעבור על מה שעשיתי", "תוודא שהפתרון תקין",
            "תן ציון", "תבדוק האם פתרתי נכון"
  params: {{
    "course": "<course_key אם המשתמש ציין>",
    "want_full_solution": <true/false — true רק אם המשתמש ביקש "תן פתרון מלא">
  }}

— "study_plan" — לבנות תוכנית לימוד יומית למבחן.
  ביטויים: "תכנן לי לימוד למבחן בלינארית בעוד 12 ימים",
            "תוכנית לימוד למבחן", "תכנן לי שבועיים לפני המבחן בחדואא",
            "איך ללמוד למבחן בעוד 5 ימים"
  params: {{
    "course": "<course_key — חובה>",
    "days": <int — מספר ימים עד המבחן, חובה>,
    "exam_date": "<YYYY-MM-DD אופציונלי>"
  }}

— "ask_pdf" — לענות על שאלה ולשלוח את התשובה כקובץ PDF (לא רק טקסט בצ'אט).
  **מתי?** המשתמש מבקש במפורש PDF: "תן לי PDF", "תייצא לPDF", "תשובה כקובץ",
  "תן לי את התשובה כPDF", "PDF של זה", "אני רוצה לקבל PDF".
  ביטויים: "תסביר על X ותן לי PDF", "X — PDF", "PDF של מטריצות מ-NotebookLM",
            "תייצא תשובה לPDF", "תן לי את זה כקובץ"
  params: {{
    "question": "<השאלה ללא המילה PDF>",
    "course": "<course_key אופציונלי לסינון>"
  }}

— "cheatsheet" — לשלוח דף עזר מרוכז של קורס (PDF דף-אחד עם כל הנוסחאות/משפטים).
  ביטויים: "תן לי דף עזר", "cheatsheet של לינארית", "דף נוסחאות של חדואא",
            "תן לי דף נוסחאות לפני המבחן"
  params: {{ "course": "<course_key — חובה>" }}

— "course_summary" — סיכום-על מקיף של קורס (PDF 3-5 עמודים, כל המשפטים+הגדרות+דוגמאות+טיפי זהב).
  שונה מ-cheatsheet: יותר מקיף, עם פסקאות מבוא לכל נושא, ודוגמאות.
  **קריטי:** אם המשתמש כתב "סיכום על" / "סיכום-על" / "סיכום מקיף" + שם קורס —
  זה תמיד `course_summary`, **לעולם לא** `ask`. גם ל-"תן לי סיכום של X" כשX זה קורס.
  ביטויים: "תן לי סיכום של כל הקורס", "סיכום-על של לינארית",
            "מסמך לימוד מלא של חדואא", "סיכום מקיף של הקורס",
            "כל המשפטים החשובים של [קורס]",
            "סיכום על מבוא", "סיכום על חדואא", "תן לי סיכום של בדידה",
            "סיכום של הקורס", "PDF של הקורס", "מסמך לימוד"
  params: {{ "course": "<course_key — חובה. הסק מהשם: 'מבוא'/'מדמח' → intro_to_cs; 'חדואא'/'חדו\"א' → calculus_2; 'בדידה' → discrete_math_2; 'לינארית' → linear_algebra_2; 'הסתברות' → probability; 'תוכנה 1'/'ג׳אווה' → software_1; 'מיקרו'/'כלכלה' → microeconomics_2; 'מאקרו' → macroeconomics; 'מיקרו א׳' → microeconomics>" }}

— "notebooklm_ask" — שאלת NotebookLM של גוגל על הסרטונים/PDFs האינדקסים של קורס.
  **מתי לבחור בזה במקום ask (RAG טקסטואלי)?**
  1. המשתמש *מבקש במפורש*: "תשאל את NotebookLM", "תבדוק בקלטות"
  2. השאלה מזכירה הקלטות/וידאו/סרטון: "בהקלטה", "בסרטון", "בהרצאה ההיא ש...", "מה המרצה אמר ב..."
  3. השאלה לגבי איך/למה המרצה הסביר משהו ספציפי (סגנון, נימה, דוגמה ספציפית בלוח)
  4. השאלה מציינת timestamp או דקה ספציפית בהרצאה
  אחרת — תעדיף `ask` (RAG על הסיכומים, זול ומהיר).
  ביטויים: "תשאל את NotebookLM", "תשאל את הסרטונים", "מה אמרו בהקלטות על X",
            "תבדוק בקלטות של [קורס]", "מהוידאו של לינארית",
            "איך המרצה הסביר", "מה אמר המרצה ב..."
  params: {{
    "course": "<course_key — חובה>",
    "scope": "lectures|tutorials  (ברירת מחדל lectures)",
    "question": "<השאלה במלואה>"
  }}

— "nlm_reset" — לאפס שיחת NotebookLM הנוכחית (מתחיל הקשר שיחתי חדש).
  ביטויים: "נקה שיחה", "התחל שיחה חדשה", "תאפס", "reset conversation"
  params: {{}}

— "nlm_study_guide" — לייצר מדריך לימוד מקיף לקורס ב-NotebookLM (מבוסס על הקלטות + PDFs).
  ביטויים: "תייצר מדריך לימוד של לינארית", "study guide של חדואא",
            "מדריך לימוד מ-NotebookLM", "תכין לי study guide למבחן"
  params: {{
    "course": "<course_key — חובה>",
    "scope": "lectures|tutorials (ברירת מחדל lectures)"
  }}

— "nlm_faq" — לייצר מסמך שאלות-נפוצות (FAQ) של קורס מ-NotebookLM.
  ביטויים: "תייצר FAQ של לינארית", "שאלות נפוצות של חדואא",
            "תן לי FAQ", "מסמך שאלות ותשובות"
  params: {{ "course": "...", "scope": "lectures|tutorials" }}

— "nlm_timeline" — לייצר ציר זמן של נושאי הקורס מ-NotebookLM.
  ביטויים: "ציר הזמן של מבוא מורחב", "תייצר timeline",
            "סדר הנושאים בקורס", "מתי למדנו כל נושא"
  params: {{ "course": "...", "scope": "lectures|tutorials" }}

— "nlm_mindmap" — לייצר מפת חשיבה (mind map) של נושאי הקורס מ-NotebookLM.
  ביטויים: "מפת מושגים", "mind map", "תייצר תרשים נושאים",
            "תכין mindmap של הקורס"
  params: {{ "course": "...", "scope": "lectures|tutorials" }}

— "nlm_compare" — להשוות מושג בין שני קורסים (multi-notebook synthesis).
  ביטויים: "מה ההבדל בין X בלינארית ל-Y במבוא",
            "השווה את X בקורס A ל-B בקורס C",
            "איך זה שונה בקורס X לעומת Y"
  params: {{
    "course_a": "<course_key ראשון>",
    "course_b": "<course_key שני>",
    "question": "<השאלה המקורית>"
  }}

— "anki_export" — לייצא את כרטיסיות הלימוד כקובץ Anki (.apkg).
  ביטויים: "תייצא ל-Anki", "תן לי anki deck", "ייצוא כרטיסיות", "אנקי",
            "תן לי את הכרטיסיות לאנקי"
  params: {{ "course": "<course_key אופציונלי לקורס אחד>" }}

— "budget_unlock" — שחרור נעילת תקציב (אחרי שעברת מגבלה חודשית).
  ביטויים: "בטל מגבלת תקציב", "תשחרר את התקציב", "אישור — תמשיך לסכם",
            "כן, להמשיך עם כסף", "remove budget lock"
  params: {{}}

— "costs" — הצגת סיכום הוצאות API (Gemini / Whisper / Embeddings).
  ביטויים: "כמה הוצאתי החודש?", "תראה לי עלויות", "כמה כסף הלך על AI",
            "סיכום עלויות", "מה ההוצאות שלי", "עלויות gemini"
  params: {{ "since": "<30d|7d|24h|today|month, ברירת מחדל 30d>" }}

— "image" — לייצר תמונה/דיאגרמה
  ביטויים: "תצייר", "תכין תמונה", "תייצר דיאגרמה", "תראה לי תמונה של"
  params: {{ "prompt": "<תיאור התמונה>" }}

— "video" — לייצר סרטון מ-NotebookLM
  ביטויים: "תכין סרטון", "תייצר וידאו"
  params: {{ "course": "..." }}

— "podcast" — לייצר פודקאסט
  ביטויים: "פודקאסט", "audio overview", "תכין הקלטה לשמיעה"
  params: {{ "course": "..." }}

— "quiz" — לייצר חידון מלא (NotebookLM, ארוך, ל-PDF)
  ביטויים: "תכין חידון מלא", "חידון PDF", "צור לי בחינת תרגול"
  params: {{ "course": "..." }}

— "quiz_chat" — חידון אינטראקטיבי בצ'אט (שאלה-שאלה, אמריקאיות, מהיר)
  ביטויים: "בחן אותי", "בחן אותי בבדידה", "תשאל אותי שאלה", "אמריקאית",
            "שאלת תרגול", "תן שאלה", "נסה אותי"
  params: {{ "course": "..." }}

— "flashcards" — לייצר פלאשקארדים
  ביטויים: "פלאשקארדים", "כרטיסיות זיכרון", "flashcards"
  params: {{ "course": "..." }}

— "studyguide" — לייצר מדריך לימוד
  ביטויים: "מדריך לימוד", "study guide", "תכין לי דף לימוד"
  params: {{ "course": "..." }}

— "teach_back" — מצב פיינמן: המשתמש רוצה להסביר מושג בעצמו ולקבל ביקורת על ההסבר.
  ביטויים: "אני אסביר לך", "תן לי להסביר", "בדוק את ההסבר שלי", "מצב פיינמן",
            "אני רוצה ללמד אותך את X", "בוא אלמד אותך", "תבחן את ההבנה שלי ב-X"
  params: {{ "concept": "<המושג שהמשתמש רוצה להסביר>", "course": "<course_key אם ידוע>" }}

— "exam_report" — דוח מרוכז של *כל מה שהמרצה/מתרגל אמר על המבחן* בקורס (מה יבוא, דגשים, טעויות נפוצות, טיפים).
  ביטויים: "דוח מבחן של מיקרו", "מה המרצה אמר על המבחן בלינארית",
            "תיצור דוח של כל מה שאמרו על המבחן", "כל הדגשים למבחן בחדוא",
            "מה אמרו על המבחן בכל הקורסים", "דוח למבחן"
  params: {{ "course": "<course_key אם צוין; השמט אם המשתמש אמר 'בכל מקצוע/כל הקורסים'>" }}

— "topic_guide" — מדריך *מאפס* ללימוד נושא ספציפי מהיסוד עד רמת מבחן.
  שונה מ-ask (תשובה קצרה) — זה מדריך לימוד מלא ומדורג לנושא.
  ביטויים: "תכין לי מדריך מאפס לטורי פוריה", "תלמד אותי X מאפס",
            "מדריך ל-Y מההתחלה", "הסבר לי הכל על Z מאפס", "מדריך מלא ל-W"
  params: {{ "topic": "<הנושא>", "course": "<course_key אם אפשר להסיק>" }}

— "practice_set" — סט *תרגילים לתרגול עם פתרונות מלאים* (קובץ PDF), מתוך החומר שנלמד בקורס.
  שונה מ-quiz_chat (שאלה-שאלה בצ'אט) ומ-topic_guide (הסבר/מדריך) — כאן המשתמש רוצה תרגילים לפתור לבד.
  ביטויים: "תן לי תרגילים בהסתברות", "סט תרגול במיקרו", "תרגול על התפלגות בינומית",
            "תכין לי 8 תרגילים בתוכנה 1", "תרגילים עם פתרונות", "בא לי לתרגל מאקרו",
            "תרגול לקראת המטלה בהסתברות", "תרגילים שיעזרו לי בשיעורי הבית במיקרו"
  params: {{ "course": "<course_key — חובה; הסק מהשם>", "topic": "<נושא אם צוין, אחרת השמט>", "n": <מספר תרגילים אם צוין, אחרת השמט>,
            "homework": <true אם ביקש תרגול לקראת המטלה/שיעורי הבית, אחרת השמט> }}
  (בלי topic הסט מכוון אוטומטית למטלה הפתוחה הקרובה בקורס, אם יש.)

— "mistakes" — מחברת הטעויות של הקורס (PDF): כל מה שטעיתי בו בסטי תרגול, שיעורי בית וחידון ועוד לא תוקן.
  ביטויים: "מחברת טעויות בהסתברות", "במה טעיתי במיקרו", "תראה לי את הטעויות שלי בתוכנה 1"
  params: {{ "course": "<course_key — חובה>" }}

— "mastery_map" — מפת שליטה: באילו נושאים אני שולט / חלש, לפי כל התשובות שלי.
  ביטויים: "מפת שליטה", "במה אני חלש בהסתברות", "איפה אני עומד במאקרו", "מה אני יודע ומה לא"
  params: {{ "course": "<course_key; השמט אם ביקש את כל הקורסים>" }}

— "coverage_map" — מפת כיסוי *מלאה* של הקורס: אילו נושאים מהתכנית נלמדו ואילו ירדו (בגלל המלחמה).
  שונה מ-coverage_check (נושא בודד) — זה סורק את *כל* נושאי הקורס ומחזיר טבלת ✅/❌.
  ביטויים: "מפת כיסוי של מיקרו", "אילו נושאים ירדו בלינארית", "מה נלמד ומה לא בקורס",
            "מה ירד מהחומר בחדוא", "אילו נושאים לא הספקנו", "מה רלוונטי למבחן", "coverage map"
  params: {{ "course": "<course_key — חובה. הסק מהשם כמו ב-course_summary>" }}

— "coverage_check" — לבדוק אם נושא מסוים *נלמד בפועל* בקורס (מה שבאמת כוסה, לא מה זה).
  **קריטי:** בחר בזה כשהמשתמש שואל אם משהו *נלמד / עברנו עליו / נכלל / נכנס לחומר* —
  לא כשהוא שואל *מה זה* (זה ask). רקע: הסמסטר ירד חומר בגלל המלחמה.
  ביטויים: "האם למדנו X", "עברנו על Y?", "האם X נכלל בחומר", "למדנו את Z השנה?",
            "יש את X בקורס?", "האם דיברנו על W", "X בכלל נלמד?", "עשינו את V?"
  params: {{ "topic": "<הנושא לבדיקה>", "course": "<course_key אם צוין>" }}

— "reminder" — להוסיף תזכורת (הודעה מהבוט בזמן מסוים)
  ביטויים: "תזכיר לי", "תיצור תזכורת", "תזכורת"
  params: {{ "raw": "<טקסט מקורי כולל תאריך>" }}

— "calendar_add" — להוסיף אירוע ליומן גוגל (פגישה, מבחן, שיעור פרטי, יום הולדת...).
  ביטויים: "תוסיף ליומן", "שים ביומן", "קבע לי ביומן", "תכניס ליומן פגישה עם חבר_א ביום ג׳ ב-18:00",
            "יש לי מבחן ב-12/1 תוסיף", "תרשום ביומן"
  params: {{ "raw": "<הטקסט המקורי>" }}

— "web_search" — שאלה על העולם שבחוץ שדורשת מידע עדכני מהאינטרנט ואינה על חומר הקורסים או היומן:
  מזג אוויר, חדשות, מחירים, טיסות ומלונות, שעות פתיחה, "מתי נפתחת ההרשמה", תוצאות.
  ביטויים: "מה מזג האוויר מחר", "כמה עולה טיסה לבודפשט", "מה קרה היום בחדשות", "עד מתי פתוח הסופר"
  params: {{}}

— "calendar_list" — מה יש ביומן (קריאה בלבד).
  ביטויים: "מה יש לי מחר ביומן", "מה ביומן היום", "אני פנוי ביום רביעי?", "מה התוכניות שלי מחר",
            "מה יש לי מחר?", "מה יש לי ביום חמישי", "אני פנוי ב-25 לאוקטובר?"
  params: {{ "date": "<YYYY-MM-DD אם צוין תאריך מפורש, אחרת השמט>" }}

— "assignments" — להציג מטלות פתוחות / ציונים / דדליינים
  ביטויים: "מטלות", "ציונים", "דדליינים", "מה צריך להגיש", "מה פתוח", "תרגילים פתוחים"
  params: {{}}

— "today" — מה חדש מאתמול (סיכומים/הרצאות/מטלות חדשים)
  ביטויים: "מה חדש?", "מה היום?", "מה הוסיפו?", "מה התעדכן"
  params: {{}}

— "status" — מצב המערכת (מה רץ, מתי הריצה האחרונה, האם הקרון רץ)
  ביטויים: "מה הסטטוס?", "מה רץ?", "האם הקרון רץ?", "סטטוס", "תראה לוג"
  params: {{}}

— "list_summaries" — להציג את הסיכומים הקיימים
  ביטויים: "מה יש לי בסיכומים", "תראה לי את הסיכומים", "אילו סיכומים יש לי",
            "רשימה של הסיכומים", "מה כבר סיכמת"
  params: {{ "course": "<אופציונלי>" }}

— "greeting" — ברכה, שיחת חולין, "תודה", "בסדר"
  ביטויים: "היי", "שלום", "מה נשמע", "תודה", "אחלה", "סבבה", "ok"
  params: {{}}

— "sync" — לרענן ולשלוף נתונים חדשים (פייפליין כבד)
  ביטויים: "תרענן", "תסנכרן", "תבדוק עכשיו", "תריץ את הפייפליין"
  params: {{}}

— "no_class" — **סימון** ימים בלי לימודים (פקודה, לא שאלה!).
  **חוק קריטי:** אם ההודעה מתחילה ב"אין לימודים" או "אין שיעור" או
  "אין לי לימודים" + יום/תאריך — זה תמיד `no_class`, **לעולם לא** `ask`.
  גם אם זה נראה כמו אמירה כללית — המשתמש מצפה לפעולה.
  ביטויים: "אין לימודים ראשון שני", "השבוע אין לימודים",
            "מחר אין לימודים", "אין לימודים 25/05",
            "ביום ראשון אין לימודים", "אין לי שיעור היום",
            "אני לא לומד מחר", "תרשום שאין לי לימודים השבוע"
  params: {{
    "raw": "<טקסט מקורי כולל ימים/תאריכים>",
    "action_type": "add"  // או "remove" אם המשתמש רוצה לבטל
  }}

— "list_no_class" — הצגת רשימת ימים שסומנו "ללא לימודים"
  ביטויים: "אילו ימים אין לימודים?", "מה הימים שסימנתי", "תראה לי ימי חופש"
  params: {{}}

================================================================
מיפוי מילים לקורסים:
================================================================
  מבוא / מורחב / מדמ"ח / מדמח / cs / מדעי המחשב / פייתון → intro_to_cs
  חדואא / חדוא / חדו"א / חשבון / קלקולוס / אינטגרלים / דיפרנציאלים → calculus_2
  בדידה / בדיד / מתמטיקה בדידה / קומבינטוריקה → discrete_math_2
  לינארית / לינארי / אלגברה לינארית / מטריצות / וקטורים → linear_algebra_2
  מיקרו א' / מיקרו 1 → microeconomics   (סמסטר קודם)
  --- סמסטר נוכחי (א' תשפ"ז) — ברירת המחדל כשלא צוין אחרת ---
  הסתברות / סטטיסטיקה / הסתב → probability
  תוכנה 1 / תוכנה / ג'אווה / java → software_1
  מיקרו / מיקרו ב' / מיקרו 2 / כלכלה / מיקרו-כלכלה / micro → microeconomics_2
  מאקרו / מקרו / macro → macroeconomics

================================================================
מיפוי scope לזמן:
================================================================
  "היום" / "עכשיו" → today
  "אתמול" → date=YYYY-MM-DD (חשב אתמול)
  "אחרון" / "האחרונה" / "הכי חדש" → latest
  "השבוע" / "השבוע האחרון" / "ימים אחרונים" → recent

================================================================
החזר JSON שורה אחת בלבד. אם לא בטוח — "ask" עם params ריקים.

דוגמאות:
{{"action": "file_send", "params": {{"query": "ההרצאה האחרונה של מרצה_כ"}}}}
{{"action": "ask", "params": {{}}}}
{{"action": "summarize", "params": {{"scope": "today"}}}}
{{"action": "summarize", "params": {{"course": "discrete_math_2", "scope": "latest"}}}}
{{"action": "quiz", "params": {{"course": "discrete_math_2"}}}}
{{"action": "reminder", "params": {{"raw": "מחר 14:00 לקרוא חדו\\"א פרק 5"}}}}
{{"action": "status", "params": {{}}}}
{{"action": "list_summaries", "params": {{"course": "linear_algebra_2"}}}}
{{"action": "today", "params": {{}}}}
{{"action": "greeting", "params": {{}}}}
{{"action": "no_class", "params": {{"raw": "אין לימודים ראשון שני", "action_type": "add"}}}}
{{"action": "no_class", "params": {{"raw": "אין לימודים מחר", "action_type": "add"}}}}
{{"action": "list_no_class", "params": {{}}}}
"""


RESOLVE_PROMPT = """אתה עוזר שמנסח מחדש שאלות. קבל שיחה אחרונה והודעת משתמש חדשה, והחזר את ההודעה כשאלה עצמאית ברורה (standalone), בלי להישען על "זה"/"הם"/"ההוא" שדורש הקשר.

כללים:
- אם ההודעה כבר עצמאית — החזר אותה כמו שהיא.
- אם ההודעה היא המשך ("תפרט", "ולמה?", "תן דוגמה", "ועוד?", "כן/לא") — שלב את ההקשר. למשל אם דיברנו על "משפט קיילי-המילטון" והמשתמש כתב "תן דוגמה" → "תן דוגמה למשפט קיילי-המילטון".
- אם המשתמש מאשר/מבטל ("בסדר", "כן", "תעשה") — שלב את הפעולה שדיברו עליה.
- שמור על השפה (עברית).
- החזר רק את השאלה החדשה — בלי הסברים, בלי "התשובה:".

{history_block}הודעה נוכחית: «{message}»

שאלה עצמאית מנוסחת:"""



# ── the WhatsApp agent's router (08/10/26) ─────────────────────────────────
# INTENT_PROMPT above serves the Telegram bot: ~60 actions (NotebookLM, podcasts, zips…) ≈ 7.6k tokens per
# message. The WhatsApp agent (agent/core.py run_intent) only acts on the ~18 below, and everything that isn't a
# study question is handled by agent/assistant.py — so it gets this compact prompt (~1k tokens). Static text
# first, the message last (lets Gemini reuse the cached prefix). Chosen by SSA_ROUTER=agent (set by
# whatsapp/worker.py), so the Telegram bot in its own process is unchanged.
AGENT_PROMPT = """סווג הודעת וואטסאפ של סטודנט לפעולה אחת. החזר JSON בלבד: {{"action": "...", "params": {{...}}}}

פעולות (params אופציונליים):
calendar_add — להוסיף ליומן ("תוסיף ליומן", "קבעתי", "יש לי X ב-Y" כשזה אירוע)
calendar_list {{date: YYYY-MM-DD}} — מה יש ביומן / האם פנוי
today — מה יש לי היום בלימודים (הרצאות/משימות של היום)
assignments — מטלות/הגשות קרובות
practice_set {{course, topic, n}} — "תן לי תרגילים / סט תרגול / בא לי לתרגל"
quiz_chat {{course}} — "בחן אותי", שאלה אמריקאית
mistakes {{course}} — מחברת הטעויות
mastery_map {{course}} — מפת שליטה / איפה אני חלש
course_summary {{course}} — לשלוח את הסיכום האחרון של קורס
topic_guide {{course, topic}} — מדריך/הסבר מעמיק לנושא שלם
coverage_check {{course, topic}} — "האם למדנו X"
coverage_map {{course}} — מה נכלל בחומר / מפת כיסוי
exam_report {{course}} — ניתוח מבחנים קודמים / מה חוזר במבחנים
no_class {{action_type: add|remove}} — "אין שיעור ב…"; list_no_class — אילו ימים בלי שיעור
web_search — מידע עדכני מהאינטרנט (מזג אוויר, חדשות, מחירים, שעות פתיחה)
greeting — רק "היי"/"תודה"/"בוקר טוב" בלי בקשה
ask {{course}} — שאלה על חומר הלימוד, וגם כל דבר אחר / לא ברור (יטופל בהמשך)

course (רק אם ברור): {courses}
כללים: שאלה על מושג/חומר → ask. משפט אישי, רגש, מידע שהוא מוסר, ביקורת, בקשה לשפר את הבוט → ask. ספק → ask.
שאלה ממוקדת על פרט אחד (איפה / מי מלמד / באיזה חדר / מתי בדיוק — שיעור, תרגול, מבחן, מרצה) → ask, לא today/calendar_list.
תשובה במילים לשאלה שהבוט שאל בשיחה ("לא צריך", "אני לא ניגש", "עזוב") → ask.

{history_block}ההודעה: «{message}»"""


def _agent_courses() -> str:
    try:
        from config.paths import COURSE_NAMES
        return ', '.join(f'{k}={v}' for k, v in COURSE_NAMES.items())
    except Exception:
        return ''



def resolve_standalone(message: str, history: str = '') -> str:
    """Reformulate a message into a context-free standalone question.
    Returns the original message if no history or on any failure."""
    if not history or not history.strip():
        return message
    history_block = (
        f"שיחה אחרונה:\n{history.strip()}\n\n"
    )
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        return message
    try:
        from google import genai
        from google.genai import types as genai_types
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=RESOLVE_PROMPT.format(message=message, history_block=history_block),
            config=genai_types.GenerateContentConfig(
                max_output_tokens=200,
                temperature=0.0,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        from utils.usage_tracker import log_gemini as _lg; _lg(resp, 'gemini-2.5-flash', op='bot.brain')
        txt = (resp.text or '').strip()
        # sanity: not too long, not empty
        if txt and 3 < len(txt) < 500 and not txt.startswith('http'):
            return txt
    except Exception:
        pass
    return message


def _try_ollama(prompt: str, model: str = 'qwen2.5:7b', timeout: int = 45) -> str | None:
    """Try local Ollama. Returns response text or None on failure.
    qwen2.5:7b on ARM CPU: ~22s cold (first call), ~3s warm. Use as Gemini fallback."""
    try:
        import urllib.request as _ur
        import json as _json
        req = _ur.Request(
            'http://127.0.0.1:11434/api/generate',
            data=_json.dumps({
                'model': model,
                'prompt': prompt,
                'stream': False,
                'format': 'json',
                'options': {'temperature': 0.0, 'num_predict': 300},
            }).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        with _ur.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
            return data.get('response', '').strip() or None
    except Exception:
        return None


def classify(message: str, history: str = '') -> dict[str, Any]:
    """Returns {action: str, params: dict}. Falls back to {action:'ask'} on error.

    Strategy (cheap → expensive):
    1. Try local Ollama (free, ~5sec on CPU) — handles >90% of cases
    2. Fall back to Gemini API if Ollama unavailable or returns invalid JSON

    history: optional formatted string of recent conversation context (last few turns)
             — improves classification of follow-up messages like "ועוד?", "פרט", "כן".
"""
    fallback = {'action': 'ask', 'params': {}}
    history_block = ''
    if history and history.strip():
        history_block = (
            f"\n=== הקשר משיחה אחרונה (לעזרה בסיווג בלבד) ===\n"
            f"{history.strip()}\n"
            f"=========================================\n\n"
        )

    if os.environ.get('SSA_ROUTER') == 'agent':          # the WhatsApp agent: compact prompt (~1k tokens)
        hb = f'שיחה אחרונה:\n{history.strip()[-600:]}\n' if history and history.strip() else ''
        prompt = AGENT_PROMPT.format(courses=_agent_courses(), history_block=hb, message=message)
    else:
        prompt = INTENT_PROMPT.format(message=message, history_block=history_block)
    # Note: Gemini is fast (~500ms) and accurate. Ollama is slow (~20s on ARM CPU)
    # and less accurate on multi-class Hebrew JSON. So Gemini first, Ollama as
    # last-resort fallback when Gemini is completely unavailable.
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        return fallback
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        return fallback
    # Step 1: Gemini (primary — fast + accurate)
    try:
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=300,
                temperature=0.0,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                response_mime_type='application/json',
            ),
        )
        from utils.usage_tracker import log_gemini as _lg; _lg(resp, 'gemini-2.5-flash', op='bot.brain')
        txt = (resp.text or '').strip()
        m = re.search(r'\{.*\}', txt, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            if 'action' in data:
                data.setdefault('params', {})
                return data
    except Exception:
        pass  # fall through to Ollama

    # Step 2: Ollama fallback (when Gemini fails/quota/network)
    # Slow on ARM CPU but free and offline. Better than returning fallback.
    # 19/11: not inside a chat turn whose Gemini is out, and only when the model is loaded — a cold qwen on the VM's
    # CPU made the owner wait ~22-45 s for an 'ask' that then needed Gemini anyway (utils/llm_health.ollama_ok)
    from utils import llm_health
    ollama_txt = _try_ollama(prompt, timeout=15) if llm_health.ollama_ok() else None
    if ollama_txt:
        try:
            m = re.search(r'\{.*\}', ollama_txt, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                if 'action' in data:
                    data.setdefault('params', {})
                    return data
        except Exception:
            pass

    return fallback
