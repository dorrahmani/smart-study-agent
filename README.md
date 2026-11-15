# 📚 Smart Study Agent

מערכת לימוד אישית מלאה לסטודנטים (נבנתה ונבדקה באוניברסיטת תל אביב). היא רצה על שרת קטן או על המחשב שלך, ועושה בשבילך את העבודה השחורה של הסמסטר.

> **English, in short:** a full personal study system — lecture collection (Panopto/Moodle) → transcripts → Gemini summaries (PDF + e-mail), a course site, NotebookLM notebooks per course, a WhatsApp secretary bot on *your own* number, an obligations/deadlines tracker, a Streamlit dashboard, nightly security checks and a test-gated deploy. Everything personal lives in `.env` and `config/courses.yaml`, never in the code. Start with [SETUP.md](SETUP.md); run `python scripts/setup_check.py` to see what is missing and `python scripts/release_scan.py` before every push.

## מה היא עושה

- 🎥 **אוספת הרצאות** מפנופטו, רק של הקבוצה שלך (לפי היום והשעה שלך ב-`config/courses.yaml`).
- 📝 **תמלול וסיכום:** כתוביות פנופטו כשהן תקינות (ובדיקה שהן לא ג'יבריש בקירילית), אחרת תמלול. סיכום בעברית עם Gemini, כולל קריאת הלוח מהווידאו בקורסים מתמטיים.
- 📧 **שליחה:** סיכום PDF במייל ובוואטסאפ, אחרי כל שיעור.
- 🌐 **אתר קורסים** (`scripts/build_course_site.py`): כל הסיכומים, החומרים והמטלות לפי נושאים.
- 📓 **NotebookLM:** מחברת לכל קורס, שמתעדכנת לבד בסיכומים, בתמלולים ובחומרים ממודל.
- 💬 **מזכיר בוואטסאפ על המספר שלך:** "מה יש לי מחר?", "תוסיף ליומן…", "מתי מגישים את תרגיל 3?", "תזכיר לי…", קבצים וקישורים ("מה הדדליין פה?"), הודעות לבן/בת הזוג באישור שלך בלבד.
- ✅ **מעקב חובות:** מטלות, בחנים והגשות ממודל, עם תזכורות שיודעות אם כבר הגשת.
- 📊 **דשבורד** (Streamlit): היום, מטלות, סיכומים, עלויות, בריאות המערכת.
- 🛡️ **אבטחה:** בדיקת אבטחה לילית לשרת (`scripts/security_check.py`), ושער פריסה שמריץ 440+ בדיקות לפני כל עדכון בשרת (`deploy/hooks/`).
- 💰 **תקרת עלות חודשית קשיחה:** מעליה הכול נעצר ומקבלים הודעה.

## כמה זה עולה

רק API, לפי שימוש:

| מה | בערך |
|---|---|
| הרצאה עם כתוביות בפנופטו (רוב ההרצאות) | ‎$0.08 |
| הרצאה בלי כתוביות (תמלול OpenAI + סיכום) | ‎$0.40–0.50 |
| בוט הוואטסאפ (שימוש יומיומי) | עשרות סנטים עד דולר בחודש |
| שרת Oracle Cloud Always Free | ‎$0 |

סמסטר רגיל יוצא בערך ‎$5–15 בחודש. התקרה הקשיחה כברירת מחדל היא ‎$50 בחודש (`utils/budget.py`). חובה לשים גם התראת תקציב ב-Google Cloud Billing.

## הקמה

ראו **[SETUP.md](SETUP.md)**: צעד אחרי צעד, כשעה עד שעתיים. בקצרה:

```bash
python -m venv venv && venv/bin/pip install -r requirements.txt      # Windows: venv\Scripts\pip
venv/bin/playwright install chromium
python scripts/setup_check.py --init     # יוצר .env ו-config/courses.yaml מהדוגמאות
# ממלאים את .env ואת config/courses.yaml
python scripts/setup_check.py            # מה עוד חסר
python -m pytest tests/ -q               # הכול צריך לעבור
```

## מבנה

| תיקייה | מה |
|---|---|
| `collectors/` | פנופטו, מודל, הודעות, מטלות, אתרי מרצים, תמלול |
| `generators/` | סיכומים, PDF, חידונים, כרטיסיות, מעקב עלויות, NotebookLM |
| `pipelines/` | הריצה הלילית (`daily_full_update.py`) וסנכרונים |
| `agent/` | המוח של הבוט: הבנת הודעות, יומן, משימות, חובות, מזכיר |
| `whatsapp/` | הגשר (מכשיר מקושר), התור, ה-worker |
| `dashboard/` | הדשבורד |
| `integrations/` | יומן Google, מייל האוניברסיטה, my.tau, חיפוש |
| `deploy/` | systemd, crontab, שער הפריסה, הקשחת SSH, fail2ban |
| `scripts/` | כלים: `setup_check.py`, `release_scan.py`, `security_check.py`, ועוד |
| `tests/` | `test_safety_critical.py` (השער) ו-`conversation_evals.py` |
| `docs/RUNBOOK.md` | תקלות נפוצות ואיך מתקנים |

## אבטחה ופרטיות

- סודות רק ב-`.env` (לא נכנס ל-git). המערכת שלך ב-`config/courses.yaml` (גם הוא לא נכנס).
- לפני כל push: `python scripts/release_scan.py`. ראו [SECURITY.md](SECURITY.md).
- אם תשמרו את הקוד ב-GitHub: repo **פרטי**.
