# 🛠️ הקמה — צעד אחרי צעד

הדרך הקלה: לפתוח את Claude Code (או עוזר קוד אחר) בתיקייה, ולבקש: "תקרא את README.md ואת SETUP.md ותעבור איתי על ההקמה צעד אחרי צעד. את הסיסמאות והמפתחות אני מקליד בעצמי ב-.env, לא בצ'אט."

אפשר גם לבד. אחרי כל שלב: `python scripts/setup_check.py`.

---

## 0. איפה זה ירוץ

| אפשרות | מתאים ל | הערות |
|---|---|---|
| **שרת** (מומלץ): Oracle Cloud Always Free, ‏Ubuntu 22.04 ARM | הכול, כולל בוט הוואטסאפ 24/7 | חינם. `docs/RUNBOOK.md` §3 = בנייה מאפס |
| **המחשב שלך** (Windows/Mac/Linux) | סיכומים, דשבורד, NotebookLM | הבוט עובד רק כשהמחשב דלוק |

- **Python 3.12** (לפחות 3.11).
- **ffmpeg** (אודיו וקריאת לוח): `sudo apt install ffmpeg` / `winget install Gyan.FFmpeg`. אם הוא לא ב-PATH, `FFMPEG_PATH` ב-.env.
- **git**.

```bash
git clone <your-private-repo> smart_study_agent && cd smart_study_agent
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt          # בשרת: requirements.lock.txt (גרסאות נעולות)
venv/bin/playwright install --with-deps chromium
python scripts/setup_check.py --init               # יוצר .env, config/courses.yaml, config/professors.yaml
```

## 1. מפתחות ל-.env

כל משתנה מוסבר ב-`.env.example`. החשובים:

| משתנה | מאיפה |
|---|---|
| `TAU_USERNAME`, `TAU_ID`, `TAU_PASSWORD` | פרטי ההתחברות שלך לאוניברסיטה (אותם פרטים של מודל). אוניברסיטה אחרת: ראו §6 |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey. להפעיל חיוב, ובאותו פרויקט ב-Google Cloud → Billing → Budgets להגדיר התראה |
| `OPENAI_API_KEY` (רשות) | https://platform.openai.com/api-keys. רק לתמלול כשאין כתוביות. בלי זה, הרצאות בלי כתוביות מדולגות |
| `DASHBOARD_PASSWORD` | מחרוזת אקראית ארוכה (`python -c "import secrets;print(secrets.token_urlsafe(24))"`) |
| `WA_OWNER`, `WA_OWNER_NAME`, `WA_PARTNER_NAME`, `WA_BOT_NUMBER` | §3 |
| `GMAIL_CREDENTIALS_JSON`, `RECIPIENT_EMAIL` | §4 |
| `SSA_ACTIVE_COURSES` | המפתחות של הקורסים של הסמסטר הזה מתוך `config/courses.yaml` |

> ⚠️ את `.env` לא שולחים לאף אחד, לא מדביקים בצ'אט ולא מעלים ל-git. בשרת: `chmod 600 .env`.

## 2. מערכת השעות: `config/courses.yaml`

הקובץ שנוצר הוא דוגמה. לכל קורס:

1. **`name` ו-`course_code`:** כמו ב-my.tau.
2. **`my_sessions`:** השיעורים **של הקבוצה שלך**: `type` (lecture/tutorial), `day` (sun..thu), `start`, `end`, `group`, `location`. לפי זה המערכת לוקחת רק את ההקלטות שלך.
3. **`sources.panopto.folder_id`:** אחרי שה-.env מלא: `python scripts/find_panopto_folders.py` מדפיס את התיקיות שלך. התיקייה מופיעה רק אחרי שעלתה ההקלטה הראשונה.
4. **`sources.moodle.course_id`:** המספר בכתובת `moodle.tau.ac.il/course/view.php?id=…`.
5. **`settings.schedule.semester_start` / `semester_end`:** מחוץ לחלון הזה הצינור לא אוסף ולא שולח. `no_class_dates` לימים בלי לימודים.

אחר כך: `SSA_ACTIVE_COURSES=key1,key2,…` ב-.env, ו-`COURSE_NAMES` / `COURSE_ALT_NAMES` ב-`config/paths.py` אם הוספת קורס שאין שם (שם התצוגה ושמות חלופיים שהבוט מזהה).

בדיקה: `python dry_run.py` (בלי שליחה), ואז `python scripts/dress_rehearsal.py --no-email` על הרצאה אחת.

## 3. בוט הוואטסאפ (המספר שלך)

הבוט הוא **מכשיר מקושר** (כמו WhatsApp Web) של מספר וואטסאפ. מומלץ מאוד **מספר נפרד לבוט** (סים שני / eSIM זול): זו ספרייה לא רשמית (neonize), ומספר שנחסם צריך להיות רק המספר של הבוט.

1. ב-.env:
   - `WA_TRANSPORT=bridge`
   - `WA_BOT_NUMBER` = המספר של הבוט (ספרות בלבד, ‎9725XXXXXXXX)
   - `WA_OWNER` = **המספר שלך**, שממנו תכתוב לבוט
   - `WA_ALLOWED` = מספרים נוספים שמותר להם לדבר עם הבוט (למשל בן/בת זוג). כל השאר מתעלמים בשקט.
   - `WA_OWNER_NAME`, `WA_PARTNER_NAME` = איך הבוט קורא לך ולבן/בת הזוג.
2. סביבה נפרדת לגשר (בכוונה בלי גישה ל-.env ולמפתחות):
   ```bash
   python3.12 -m venv venv_wa && venv_wa/bin/pip install neonize
   WA_BOT_NUMBER=9725XXXXXXXX venv_wa/bin/python whatsapp/bridge.py
   ```
3. בהרצה הראשונה נכתב קוד צימוד ל-`data/wa_bridge/pair_code.txt`. בטלפון של הבוט: וואטסאפ ← מכשירים מקושרים ← קישור מכשיר ← "קישור עם מספר טלפון" ← להקליד את הקוד.
4. שירותים בשרת (`deploy/systemd/`, לעדכן נתיבים אם המשתמש אינו `ubuntu`):
   ```bash
   sudo cp deploy/systemd/smart-study-wa-bridge.service deploy/systemd/smart-study-wa-worker.service /etc/systemd/system/
   sudo mkdir -p /etc/systemd/system/smart-study-wa-bridge.service.d && sudo cp deploy/systemd/hardening/smart-study-wa-bridge.conf /etc/systemd/system/smart-study-wa-bridge.service.d/
   sudo systemctl daemon-reload && sudo systemctl enable --now smart-study-wa-bridge smart-study-wa-worker
   ```
5. בדיקה: לשלוח "היי" לבוט מהמספר שלך.

(חלופה רשמית: Meta WhatsApp Cloud API: `WA_TOKEN` / `WA_PHONE_NUMBER_ID`, ראו `docs/RUNBOOK.md` §7.)

## 4. Google: מייל, יומן, Drive

1. https://console.cloud.google.com → פרויקט חדש → APIs: **Gmail API**, **Google Calendar API** (ו-Drive אם רוצים גיבוי).
2. OAuth consent screen: External, להוסיף את עצמך כ-test user, ואחר כך **In production** (במצב Testing הטוקן מת אחרי 7 ימים).
3. Credentials → OAuth client ID → **Desktop app** → להוריד JSON.
4. מייל: את תוכן ה-JSON בשורה אחת ל-`GMAIL_CREDENTIALS_JSON`, ואז `python scripts/gmail_reauth.py` (פותח דפדפן, שומר `config/gmail_token.pickle`).
5. יומן: `python scripts/gcal_auth.py <client.json>` (שומר `config/gcal_token.json`).
6. במייל האוניברסיטאי (רשות, קריאה בלבד): `python scripts/tau_mail_auth.py`.
7. שרת: מתחברים במחשב ומעתיקים את קבצי הטוקן לשרת ב-`scp`, ואז `chmod 600`.

## 5. NotebookLM

אין API רשמי; הספרייה `notebooklm-py` משתמשת בהתחברות Google רגילה.

```bash
python -m notebooklm login            # במחשב, פותח דפדפן
python scripts/sync_notebooklm_all.py # מחברת לכל קורס + העלאת מה שחדש
```

בשרת: `scripts/vnc_notebooklm_login.sh` (התחברות דרך VNC בשרת עצמו, הכי יציב), או העתקה של `~/.notebooklm/profiles/default/storage_state.json` מהמחשב. Google מנתקת לפעמים התחברות שהועתקה; `scripts/nlm_keepalive.py` בודק ומתריע. אם NotebookLM נופל, הסיכומים והמיילים ממשיכים כרגיל.

## 6. מודל ופנופטו (אוניברסיטה אחרת?)

- ההתחברות לת"א (SSO) נמצאת ב-`collectors/panopto_collector.py` ו-`collectors/moodle_collector.py`; הכתובות ב-`config/courses.yaml` → `settings.panopto.base_url` / `settings.moodle.base_url`.
- באוניברסיטה אחרת צריך להתאים את טופס ההתחברות (שמות השדות) ואת כתובות הבסיס. my.tau (`integrations/tau_portal.py`) והמייל האוניברסיטאי ספציפיים לת"א; בלעדיהם הבוט פשוט לא יענה על שעות מבחנים מהאתר.
- `scripts/panopto_cookie_probe.py` בודק כמה זמן מחזיקה ההתחברות.

## 7. תזמון (crontab)

- בשרת: `deploy/crontab.txt` הוא תבנית מלאה (UTC!). לעדכן נתיבים אם הם לא `/home/ubuntu/smart_study_agent`, למחוק שורות של דברים שלא הפעלת, ואז `crontab deploy/crontab.txt`.
- העיקר: `scripts/run_daily.sh` (הצינור הלילי), `scripts/pre_lecture_primer.py`, `scripts/deadline_watch.py`, `scripts/budget_check.py`, `scripts/backup_to_drive.sh` (צריך `BACKUP_GPG_RECIPIENT`), `scripts/security_check.py`.
- במחשב Windows: `python scheduler/install_task.py` (Task Scheduler).

## 8. דשבורד

```bash
venv/bin/streamlit run dashboard/app.py --server.address 127.0.0.1 --server.port 8501
```
מאזין רק ל-127.0.0.1. מבחוץ: מנהרת SSH (`ssh -L 8501:127.0.0.1:8501 ubuntu@<your-server>`) או Tailscale Serve (tailnet בלבד, **אף פעם לא Funnel**). שירות: `deploy/systemd/dashboard.service`.

## 9. שרת: הקשחה, בדיקת אבטחה, שער פריסה (רשות, מומלץ)

- הקשחה: `deploy/ssh/00-hardening.conf`, `deploy/fail2ban/`, `deploy/systemd/hardening/` (פירוט ב-`docs/RUNBOOK.md` §2).
- בדיקת אבטחה לילית: `scripts/security_check.py`. הקבועים בראש הקובץ (פורטים, sudoers, משתמשים) תואמים לשרת המקורי: בהרצה הראשונה `--dry`, ולעדכן לשרת שלך. `SSA_AUTHORIZED_KEYS` ב-.env = טביעת האצבע של המפתח שלך (`ssh-keygen -lf ~/.ssh/<key>.pub`).
- שער פריסה: bare repo בשרת + `deploy/hooks/` (ראו `deploy/hooks/README.md`). כל `git push` מריץ את `tests/test_safety_critical.py`, ופריסה שנכשלת נדחית.

## 10. בדיקות

```bash
python -m pytest tests/ -q
PYTHONIOENCODING=utf-8 python -m tests.test_safety_critical
PYTHONIOENCODING=utf-8 python -m tests.conversation_evals
```
בשיבוט נקי הבדיקות רצות מול `config/courses.example.yaml` (היא מועתקת ל-`config/courses.yaml` אם הוא חסר). כדאי להריץ אותן לפני שממלאים את הקורסים שלך. חלק מהבדיקות מכירות את מפתחות הקורסים שבדוגמה; אם מחקת קורס מהדוגמה ובדיקה נכשלת, זו הסיבה.

## 11. מה עולה כסף, ואיך עוצרים

- Gemini (סיכומים, בוט), OpenAI (תמלול בלבד). כל קריאה נרשמת ב-`data/usage.jsonl` ומוצגת בדשבורד.
- תקרה קשיחה ‎$50 לחודש (`utils/budget.py`): מעליה הכול נעצר (`data/budget_lock.json`) ומגיעה הודעה.
- כיבוי מיידי: `SMART_STUDY_DISABLE=1` ב-.env. בלי תמלול בתשלום: `SMART_STUDY_DISABLE_WHISPER=1`. הבוט: לעצור את `smart-study-wa-worker`.

## 12. לפני שמעלים ל-GitHub

```bash
python scripts/release_scan.py           # חייב להיות "clean"
python scripts/release_scan.py --values ~/my_private_values.txt   # רשימה פרטית שלך (טלפון, מייל, ת"ז, IP), מחוץ לריפו
```
ראו [SECURITY.md](SECURITY.md).
