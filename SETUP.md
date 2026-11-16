# 🛠️ הקמה, צעד אחרי צעד

הדרך הקלה: לפתוח את Claude Code (או עוזר קוד אחר) בתיקייה, ולבקש: "תקרא את README.md ואת SETUP.md ותעבור איתי על ההקמה צעד אחרי צעד, מסלול א׳. את הסיסמאות והמפתחות אני מקליד בעצמי ב-.env, לא בצ'אט."

אפשר גם לבד. אחרי כל שלב: `python scripts/setup_check.py`. הוא יודע באיזה מסלול אתה (`SSA_STUDY`) ובודק רק את מה שצריך בו.

| מסלול | מה צריך | זמן |
|---|---|---|
| **א׳: רק העוזר** | מפתח Gemini, וואטסאפ עם המספר שלך, יומן גוגל | בערך שעה |
| **ב׳: + לימודים** (רשות, אפשר גם אחר כך) | התחברות לאוניברסיטה, `config/courses.yaml`, מודל ופנופטו, מייל, NotebookLM | עוד שעה-שעתיים |

---

## 0. איפה זה ירוץ

| אפשרות | מתאים ל | הערות |
|---|---|---|
| **שרת** (מומלץ): Oracle Cloud Always Free, ‏Ubuntu 22.04 ARM | עוזר זמין 24/7 | חינם. `docs/RUNBOOK.md` §3 = בנייה מאפס |
| **המחשב שלך** (Windows/Mac/Linux) | לנסות, לפתח | הבוט עונה רק כשהמחשב דלוק |

צריך **Python 3.12** (לפחות 3.11) ו-**git**.

# חלק א׳: רק העוזר

## א1. התקנה

```bash
git clone <your-private-repo> assistant && cd assistant
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt          # בשרת: requirements.lock.txt (גרסאות נעולות)
venv/bin/playwright install --with-deps chromium  # רק אם רוצים "תרשום אותי ל<קישור>" (הרשמה באתרים)
python scripts/setup_check.py --init               # יוצר .env מהדוגמה, עם SSA_STUDY=0
```

## א2. המפתחות ב-.env

כל משתנה מוסבר ב-`.env.example` (חלק A). המינימום:

| משתנה | מאיפה |
|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey. להפעיל חיוב, ובאותו פרויקט ב-Google Cloud: Billing → Budgets, להגדיר התראה |
| `WA_OWNER` | **המספר שלך**, ספרות בלבד (‎9725XXXXXXXX). זה המספר היחיד שהבוט מציית לו |
| `WA_BOT_NUMBER` | המספר שהגשר מקושר אליו (§א3) |
| `WA_OWNER_NAME` | איך הבוט קורא לך |
| `SSA_STUDY` | נשאר `0` במסלול הזה |

> ⚠️ את `.env` לא שולחים לאף אחד, לא מדביקים בצ'אט ולא מעלים ל-git. בשרת: `chmod 600 .env`.

## א3. וואטסאפ (המספר שלך)

הבוט הוא **מכשיר מקושר** (כמו WhatsApp Web) של מספר וואטסאפ. מומלץ מאוד **מספר נפרד לבוט** (סים שני או eSIM זול). זו ספרייה לא רשמית (neonize), ואם מספר נחסם, עדיף שזה יהיה המספר של הבוט ולא שלך.

1. ב-.env: `WA_TRANSPORT=bridge`, `WA_BOT_NUMBER`, `WA_OWNER`.
2. סביבה נפרדת לגשר, בכוונה בלי גישה ל-.env ולמפתחות:
   ```bash
   python3.12 -m venv venv_wa && venv_wa/bin/pip install neonize
   WA_BOT_NUMBER=9725XXXXXXXX venv_wa/bin/python whatsapp/bridge.py
   ```
3. בהרצה הראשונה נכתב קוד צימוד ל-`data/wa_bridge/pair_code.txt`. בטלפון של הבוט: וואטסאפ ← מכשירים מקושרים ← קישור מכשיר ← "קישור עם מספר טלפון" ← להקליד את הקוד.
4. שירותים בשרת (`deploy/systemd/`; לעדכן נתיבים אם המשתמש אינו `ubuntu`):
   ```bash
   sudo cp deploy/systemd/smart-study-wa-bridge.service deploy/systemd/smart-study-wa-worker.service /etc/systemd/system/
   sudo mkdir -p /etc/systemd/system/smart-study-wa-bridge.service.d && sudo cp deploy/systemd/hardening/smart-study-wa-bridge.conf /etc/systemd/system/smart-study-wa-bridge.service.d/
   sudo systemctl daemon-reload && sudo systemctl enable --now smart-study-wa-bridge smart-study-wa-worker
   ```

(חלופה רשמית: Meta WhatsApp Cloud API עם `WA_TOKEN` ו-`WA_PHONE_NUMBER_ID`, ראו `docs/RUNBOOK.md` §7.)

## א4. יומן גוגל

1. https://console.cloud.google.com → פרויקט חדש → APIs: **Google Calendar API**.
2. OAuth consent screen: ‏External, להוסיף את עצמך כ-test user, ואחר כך **In production** (במצב Testing הטוקן מת אחרי 7 ימים).
3. Credentials → OAuth client ID → **Desktop app** → להוריד JSON.
4. `python scripts/gcal_auth.py <client.json>`: הרשאה לאירועים בלבד, נשמר ב-`config/gcal_token.json`.
5. בשרת: מתחברים במחשב, מעתיקים את קובץ הטוקן לשרת ב-`scp`, ואז `chmod 600`.

## א5. תזמון

ה-worker עושה את העבודה החיה: תזכורות, מעקב כשלא עונים, משימות, התראות לפני אירועים, הודעת הבוקר. ב-cron רק תחזוקה ורשתות ביטחון:

```bash
crontab deploy/crontab.assistant.txt     # UTC. לעדכן נתיבים אם הם לא /home/ubuntu/smart_study_agent
```

יש בו: heartbeat, בדיקת תקציב, גיבוי מוצפן ל-Drive (צריך `BACKUP_GPG_RECIPIENT`), בדיקת הטוקנים, בדיקת האבטחה הלילית ולולאת השיפור (§א7).

## א6. בדיקה

```bash
PYTHONIOENCODING=utf-8 python -m tests.test_assistant_only   # בלי מודל, פנופטו, NotebookLM או courses.yaml
python scripts/setup_check.py                                # "✅ ready"
```

ואז לשלוח "היי" לבוט מהמספר שלך. לנסות: "תוסיף ליומן מחר ב-20 ארוחת ערב", "תזכיר לי בעוד שעה להתקשר לאמא", "מה מחכה לי".

## א7. תוספות (רשות)

- **בן/בת זוג:** `WA_ALLOWED` (המספר שלו/שלה) ו-`WA_PARTNER_NAME`. הוא/היא יכולים להציע אירועים ליומן ולקבל ממך הודעות, הכול באישור שלך.
- **יוזמה:** `SSA_PROACTIVE=1` מדליק את השכבה ש"שמה לב": עד 3 הודעות ביום, לפחות 90 דקות ביניהן, אף פעם לא בשעות השקט (`SSA_QUIET_HOURS`). "תפסיק לשאול על זה" משתיק נושא.
- **Telegram** כערוץ התראות שני: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_USER_ID` (בלי ה-ID הבוט עונה לכולם, אז חובה).
- **דשבורד** (Streamlit): `DASHBOARD_PASSWORD`, ואז `venv/bin/streamlit run dashboard/app.py --server.address 127.0.0.1 --server.port 8501`. הוא מאזין רק ל-127.0.0.1. מבחוץ דרך מנהרת SSH או Tailscale Serve (רק ב-tailnet, **אף פעם לא Funnel**).
- **לולאת השיפור ו"קלוד: …":** משימות שינוי רצות בסוכן קוד (Claude Code CLI) תחת משתמש מבודד, עם חומת אש ליציאה (`deploy/claude_agent/`). כל תיקון עובר את הבדיקות בענף נפרד ומחכה ל-✅. ההגדרה הזו דורשת חשבון של סוכן הקוד. בלעדיה "קלוד: …" לא יכול לשנות כלום, ושאר העוזר עובד כרגיל.
- **הקשחת שרת** (מומלץ): `deploy/ssh/00-hardening.conf`, `deploy/fail2ban/`, `deploy/systemd/hardening/` (פירוט ב-`docs/RUNBOOK.md` §2). בדיקת האבטחה הלילית: `scripts/security_check.py`. הקבועים בראש הקובץ (פורטים, sudoers, משתמשים) תואמים לשרת המקורי, אז בהרצה הראשונה `--dry` ולעדכן לשרת שלך. `SSA_AUTHORIZED_KEYS` = טביעת האצבע של המפתח שלך.
- **שער פריסה:** ‏bare repo בשרת + `deploy/hooks/` (ראו `deploy/hooks/README.md`). כל `git push` מריץ את `tests/test_safety_critical.py`, ופריסה שנכשלת נדחית.

# חלק ב׳: תחום הלימודים 🎓

אפשר להוסיף בכל שלב. נבנה לאוניברסיטת תל אביב. באוניברסיטה אחרת ראו §ב5.

## ב1. להדליק

```bash
python scripts/setup_check.py --init --student     # יוצר config/courses.yaml ו-config/professors.yaml מהדוגמאות
```

וב-.env (חלק B):

| משתנה | מאיפה |
|---|---|
| `SSA_STUDY=1` | מדליק את התחום |
| `TAU_USERNAME`, `TAU_ID`, `TAU_PASSWORD` | פרטי ההתחברות שלך לאוניברסיטה (אותם פרטים של מודל) |
| `SSA_ACTIVE_COURSES` | המפתחות של הקורסים של הסמסטר הזה מתוך `config/courses.yaml` |
| `OPENAI_API_KEY` (רשות) | רק לתמלול כשאין כתוביות. בלי זה, הרצאות בלי כתוביות מדולגות |
| `GMAIL_CREDENTIALS_JSON`, `RECIPIENT_EMAIL` (רשות) | סיכומים במייל (§ב3) |

עם `SSA_STUDY=1` נוספים לבוט: מעקב חובות, מייל האוניברסיטה, מודל, סיכומים, NotebookLM, חזרות ומבחנים.

## ב2. מערכת השעות: `config/courses.yaml`

הקובץ שנוצר הוא דוגמה. לכל קורס:

1. **`name` ו-`course_code`:** כמו ב-my.tau.
2. **`my_sessions`:** השיעורים **של הקבוצה שלך**: `type` (lecture/tutorial), `day` (sun..thu), `start`, `end`, `group`, `location`. לפי זה המערכת לוקחת רק את ההקלטות שלך, ומסננת עדכונים שנוגעים לקבוצות אחרות.
3. **`my_staff` / `other_staff`** (רשות): שמות המתרגלים של הקבוצה שלך ושל קבוצות אחרות, בעברית ובאנגלית. מסנן הרלוונטיות (`integrations/univ_relevance.py`) נעזר בהם.
4. **`sources.panopto.folder_id`:** אחרי שה-.env מלא, `python scripts/find_panopto_folders.py` מדפיס את התיקיות שלך. תיקייה מופיעה רק אחרי שעלתה ההקלטה הראשונה.
5. **`sources.moodle.course_id`:** המספר בכתובת `moodle.tau.ac.il/course/view.php?id=…`.
6. **`settings.schedule.semester_start` / `semester_end`:** מחוץ לחלון הזה הצינור לא אוסף ולא שולח. `no_class_dates` לימים בלי לימודים.

אחר כך `COURSE_NAMES` / `COURSE_ALT_NAMES` ב-`config/paths.py` אם הוספת קורס שאין שם (שם התצוגה ושמות חלופיים שהבוט מזהה).

בדיקה: `python dry_run.py` (בלי שליחה), ואז `python scripts/dress_rehearsal.py --no-email` על הרצאה אחת.

## ב3. מייל

1. באותו פרויקט ב-Google Cloud להפעיל גם **Gmail API** (ו-Drive אם רוצים גיבוי).
2. סיכומים במייל: את תוכן ה-JSON של ה-OAuth client בשורה אחת ל-`GMAIL_CREDENTIALS_JSON`, ואז `python scripts/gmail_reauth.py` (שומר `config/gmail_token.pickle`).
3. המייל האוניברסיטאי (קריאה בלבד): `python scripts/tau_mail_auth.py`.

## ב4. NotebookLM

אין API רשמי. הספרייה `notebooklm-py` משתמשת בהתחברות Google רגילה.

```bash
python -m notebooklm login            # במחשב, פותח דפדפן
python scripts/sync_notebooklm_all.py # מחברת לכל קורס + העלאה של מה שחדש
```

בשרת: `scripts/vnc_notebooklm_login.sh` (התחברות דרך VNC בשרת עצמו, הכי יציב), או העתקה של `~/.notebooklm/profiles/default/storage_state.json` מהמחשב. Google מנתקת לפעמים התחברות שהועתקה, ו-`scripts/nlm_keepalive.py` בודק ומתריע. אם NotebookLM נופל, הסיכומים והמיילים ממשיכים כרגיל.

## ב5. מודל ופנופטו (אוניברסיטה אחרת?)

- ההתחברות לת"א (SSO) נמצאת ב-`collectors/panopto_collector.py` ו-`collectors/moodle_collector.py`. הכתובות ב-`config/courses.yaml` → `settings.panopto.base_url` / `settings.moodle.base_url`.
- באוניברסיטה אחרת צריך להתאים את טופס ההתחברות (שמות השדות) ואת כתובות הבסיס. my.tau (`integrations/tau_portal.py`) והמייל האוניברסיטאי ספציפיים לת"א. בלעדיהם הבוט פשוט לא עונה על שעות מבחנים מהאתר.
- `scripts/panopto_cookie_probe.py` בודק כמה זמן מחזיקה ההתחברות.

## ב6. תזמון מלא

`deploy/crontab.txt` מכיל את כל מה שב-`crontab.assistant.txt` ועוד הצינור הלילי (`scripts/run_daily.sh`), הכנה לפני שיעור, מעקב הגשות, מייל האוניברסיטה, הודעות מודל ו-NotebookLM. למחוק שורות של מה שלא הפעלת, ואז `crontab deploy/crontab.txt`. במחשב Windows: `python scheduler/install_task.py` (Task Scheduler).

---

## בדיקות

```bash
PYTHONIOENCODING=utf-8 python -m tests.test_safety_critical    # השער: כל התחומים
PYTHONIOENCODING=utf-8 python -m tests.conversation_evals      # שיחות אמיתיות, בלי רשת
PYTHONIOENCODING=utf-8 python -m tests.test_assistant_only     # מסלול א׳ בלבד
```

בשיבוט נקי הבדיקות רצות מול `config/courses.example.yaml` (היא מועתקת ל-`config/courses.yaml` אם הוא חסר). הקובץ הזה לא מדליק את תחום הלימודים: רק `SSA_STUDY=1` מדליק. חלק מהבדיקות מכירות את מפתחות הקורסים שבדוגמה, אז אם מחקת קורס מהדוגמה ובדיקה נכשלת, זו הסיבה.

## מה עולה כסף, ואיך עוצרים

- Gemini (הבוט, קריאת קבצים, סיכומים), OpenAI (תמלול בלבד, רק בתחום הלימודים). כל קריאה נרשמת ב-`data/usage.jsonl` ומוצגת בדשבורד.
- תקרה קשיחה ‎$50 לחודש (`utils/budget.py`): מעליה הכול נעצר (`data/budget_lock.json`) ומגיעה הודעה. בנוסף לזה יש תקרה יומית לשיחות (`SSA_BRAIN_DAY_CAP`).
- לעצור את הבוט: `sudo systemctl stop smart-study-wa-worker`. לכבות את צינור הלימודים: `SMART_STUDY_DISABLE=1`. בלי תמלול בתשלום: `SMART_STUDY_DISABLE_WHISPER=1`.

## לפני שמעלים ל-GitHub

```bash
python scripts/install_git_hooks.py      # פעם אחת: כל push מריץ קודם את הסריקה ונחסם על כל ממצא
python scripts/release_scan.py           # חייב להיות "clean"
python scripts/release_scan.py --values ~/my_private_values.txt   # רשימה פרטית שלך (טלפון, מייל, ת"ז, IP), מחוץ לריפו
```

ראו [SECURITY.md](SECURITY.md).
