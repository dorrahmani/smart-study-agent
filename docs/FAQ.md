<div dir="rtl">

# ❓ שאלות נפוצות, תקלות ומילון

השאלות שחברים באמת שאלו בהקמה, עם תשובה קצרה ואיפה הפירוט. **English below.**

קודם כול, תמיד: `python scripts/setup_check.py`. הוא יודע באיזה מסלול אתה (`SSA_STUDY`), בודק רק את מה שצריך בו, ולא מדפיס אף ערך סודי.

## 🚀 הקמה

**מאיפה מתחילים?**
`python -m venv venv`, ‏`venv/bin/pip install -r requirements.txt`, ואז `python scripts/setup_check.py --init` (יוצר `.env` מ-`.env.example`). אחר כך `SETUP.md` לפי הסדר, ואחרי כל שלב שוב `python scripts/setup_check.py`. הכי קל: לפתוח את התיקייה ב-Claude Code ולבקש "help me set this up" (הוא עובד לפי `CLAUDE.md` ולא נוגע במפתחות).

**שרת או המחשב שלי?**
שרת מומלץ: Oracle Cloud Always Free (‏Ubuntu 22.04 ARM), ‏$0, והבוט זמין 24/7. על המחשב (Windows/Mac/Linux) אפשר לנסות ולפתח, אבל הבוט עונה רק כשהמחשב דלוק ומחובר (`SETUP.md` §0).

**זה עובד על Windows?**
כן, לניסיון. ב-Windows הפקודות הן `venv\Scripts\pip` ו-`venv\Scripts\python` במקום `venv/bin/...`, והגשר רץ ב-`venv_wa\Scripts\python whatsapp\bridge.py`. השירותים של `deploy/systemd/` הם ללינוקס בלבד, אז ב-Windows מריצים את הגשר וה-worker בחלונות פתוחים (או ב-WSL). תזמון של שכבת הלימודים ב-Windows: `python scheduler/install_task.py`. לעוזר שעונה תמיד: שרת.

**איזה Python?** ‏3.12 (לפחות 3.11). ‏`setup_check` כותב `need 3.11+` אם הגרסה ישנה.

**כמה זמן זה לוקח?** מסלול א׳ (רק העוזר): בערך שעה. מסלול ב׳ (לימודים): עוד שעה-שעתיים.

**מה ההבדל בין מסלול א׳ לב׳?**
א׳ = העוזר: מפתח Gemini, וואטסאפ, יומן גוגל. ‏`SSA_STUDY=0`. ב׳ = מוסיף את שכבת הלימודים (`SSA_STUDY=1`): התחברות לאוניברסיטה, `config/courses.yaml`, מודל ופנופטו, מייל, NotebookLM. אפשר להוסיף את ב׳ גם אחר כך.

**איך מעדכנים לגרסה חדשה של הפרויקט?**
```bash
git pull
venv/bin/pip install -r requirements.txt          # בשרת: requirements.lock.txt
PYTHONIOENCODING=utf-8 python -m tests.test_assistant_only
sudo systemctl restart smart-study-wa-worker smart-study-wa-bridge
```
אם הקמת שער פריסה (`deploy/hooks/README.md`), מעדכנים במחשב ועושים `git push` לשרת, והשער מריץ את הבדיקות לפני שהקוד עולה.

**איך עוצרים את הבוט?** ‏`sudo systemctl stop smart-study-wa-worker` (ו-`smart-study-wa-bridge` כדי לנתק את וואטסאפ). צינור הלימודים: `SMART_STUDY_DISABLE=1`.

## 📱 וואטסאפ

**צריך סים נוסף?**
כן. הבוט רץ כ**מכשיר מקושר** (כמו WhatsApp Web) של מספר וואטסאפ אחד (`WA_BOT_NUMBER`), ואתה מדבר איתו מהמספר שלך (`WA_OWNER`). הגשר מתעלם מהודעות שנשלחות מהחשבון שהוא מקושר אליו, אז על אותו מספר הוא לא ישמע אותך. צריך מספר שני לבוט: סים שני או eSIM זול. זה גם עדיף מבחינת חסימה (`SETUP.md` §א3).

**מה לשים ב-WA_OWNER?** המספר האישי שלך, בפורמט בינלאומי וספרות בלבד: `9725XXXXXXXX` (בלי `+`, בלי מקפים, בלי 0 בהתחלה). זה המספר היחיד שהבוט מציית לו. ‏`WA_BOT_NUMBER` = המספר של הבוט, באותו פורמט.

**איך מחברים את הבוט לוואטסאפ?**
1. ב-`.env`: ‏`WA_TRANSPORT=bridge`, ‏`WA_BOT_NUMBER`, ‏`WA_OWNER`.
2. `python3.12 -m venv venv_wa && venv_wa/bin/pip install neonize`, ואז `WA_BOT_NUMBER=9725XXXXXXXX venv_wa/bin/python whatsapp/bridge.py`.
3. קוד הצימוד נכתב ל-`data/wa_bridge/pair_code.txt`. בטלפון של הבוט: וואטסאפ ← מכשירים מקושרים ← קישור מכשיר ← "קישור עם מספר טלפון" ← הקוד.
4. בשרת: השירותים מ-`deploy/systemd/` (`SETUP.md` §א3 סעיף 4).

**הבוט לא עונה לי.** לפי הסדר:
1. `python scripts/setup_check.py`: ‏`WA_OWNER` נכון? יש "WhatsApp bridge session"?
2. `systemctl is-active smart-study-wa-bridge smart-study-wa-worker` (שניהם צריכים `active`), ו-`journalctl -u smart-study-wa-bridge -n 50`.
3. שלחת מהמספר של `WA_OWNER`, לא מהמספר של הבוט?
4. בטלפון של הבוט: וואטסאפ ← מכשירים מקושרים. אם המכשיר נעלם, מוחקים את `data/wa_bridge/session.sqlite3` ומצמדים מחדש.

**המספר יכול להיחסם?** כן, זה אפשרי: הגשר משתמש בספרייה לא רשמית (neonize). לכן מספר נפרד לבוט, ולא המספר הראשי שלך.

**הגשר לא רואה את ה-.env?** בכוונה. הוא רץ ב-`venv_wa` נפרד, בלי גישה למפתחות ובלי קוד הפרויקט, ורק מעביר הודעות בתיקיות `data/wa_spool` ו-`data/wa_outbox`.

**אפשר את ה-API הרשמי של מטא?** כן: WhatsApp Cloud API עם `WA_TOKEN` ו-`WA_PHONE_NUMBER_ID` (`docs/RUNBOOK.md` §7). דורש כתובת ציבורית ל-webhook.

**בן/בת זוג יכולים לדבר עם הבוט?** כן: `WA_ALLOWED` (המספר, ספרות) ו-`WA_PARTNER_NAME`. הם יכולים להציע אירועים ליומן ולקבל ממך הודעות, הכול באישור שלך (`SETUP.md` §א7).

**אפשר להכניס את הבוט לקבוצה?** כן: `SSA_GROUPS=1` ב-`.env`, ובקבוצה מתייגים את הבוט וכותבים "תפעיל כאן". הוא מגיב רק לתיוג שלך (`WA_OWNER`), לא לאחרים. "תכבה כאן" מכבה.

## 🔑 מפתחות ועלויות

**מאיפה מפתח Gemini?** https://aistudio.google.com/apikey. להפעיל חיוב (Billing) ובאותו פרויקט ב-Google Cloud: ‏Billing ← Budgets להגדיר התראה. המפתח מתחיל ב-`AIza`.

**איפה שמים מפתחות?** רק ב-`.env` (שם: `GEMINI_API_KEY=...`). לא בצ'אט, לא בקוד ולא בגיט. גם לא להדביק אותם ל-Claude Code: פותחים את `.env` ומדביקים בעצמך. ‏`setup_check` אומר רק אם המפתח קיים.

**צריך מפתח OpenAI?** לא חובה. הוא משמש רק לתמלול: **הודעות קוליות** לבוט, והרצאות בלי כתוביות בשכבת הלימודים. בלי `OPENAI_API_KEY` הודעות קוליות לא מתומללות (כותבים לבוט בטקסט), והרצאות בלי כתוביות מדולגות. `SMART_STUDY_DISABLE_WHISPER=1` = אף פעם לא לשלם על תמלול.

**כמה זה עולה?** בערך ‎$3–10 לחודש לעוזר בלבד, ‎$15–30 עם הלימודים. פירוט: [COSTS.md](COSTS.md).
| מה | בערך |
|---|---|
| הודעה למוח (Gemini) | ‎$0.005–0.015 |
| שאלות לוז, תזכורות, כפתורים, ביטול, "מה פספסתי" | ‎$0 (בלי מודל) |
| שאלה מהרשת | ‎$0 מובנית · ‎~$0.0005 כללית |
| שרת Oracle Always Free | ‎$0 |
| סיכום שיעור עם כתוביות או תמלול מקומי / תמלול בתשלום | ‎$0.08 / ‎$0.40–0.50 |

**איך מוזילים?** כתוביות קודם ותמלול מקומי בשרת (`SSA_LOCAL_ASR`), המסלול המהיר, חיפוש ברשת בפייתון, NotebookLM קודם לשאלות על החומר, ותקרות. כל אלה דולקים כברירת מחדל. מנוי Gemini אישי (`SSA_SUBSCRIPTION=1`) **כבוי** כברירת מחדל: תנאי המנוי חלים עליך, הוא איטי יותר, וטקסט של אחרים לעולם לא עובר אליו (`COSTS.md` §5).

**החשבונית גבוהה מהדשבורד?** זה קרה: עד שכל הקריאות נרשמו יחד עם טוקני החשיבה, החשבונית הייתה גבוהה מהיומן. לסמוך על החשבונית ולהגדיר התראת תקציב ב-Google Cloud.

**יש תקרה?** כן. תקרה קשיחה של ‎$50 לחודש (`utils/budget.py`, `SSA_MONTH_CAP_USD`), ואפשר תקרה אחרת לכל חודש ב-`config/budget_caps.json`. מעליה כל הקריאות בתשלום נעצרות (`data/budget_lock.json`), מגיעה הודעה, והבוט עובר למצב חירום ועונה מהנתונים. בנוסף `SSA_BRAIN_DAY_CAP` (ברירת מחדל ‎$1 ליום לשיחות) ו-`SSA_BRAIN_MAX_COST` (‏‎$0.10 להודעה). כל קריאה נרשמת ב-`data/usage.jsonl`.

**מפתח דלף לגיט?** להחליף אותו מיד (revoke ב-AI Studio ומפתח חדש). מחיקה ב-commit הבא לא מוחקת מההיסטוריה (`SECURITY.md`).

## 📅 יומן גוגל

**איך מחברים?** ‏console.cloud.google.com ← פרויקט ← להפעיל Google Calendar API ← OAuth consent screen (‏External, להוסיף את עצמך כ-test user, ואז **In production**) ← Credentials ← OAuth client ID מסוג **Desktop app** ← להוריד JSON ← `python scripts/gcal_auth.py <client.json>`. נשמר ב-`config/gcal_token.json` (`SETUP.md` §א4).

**הטוקן מת אחרי שבוע?** האפליקציה נשארה ב-Testing: שם הטוקן פג אחרי 7 ימים. להעביר ל-In production ולהריץ שוב את `gcal_auth.py`.

**בשרת?** מתחברים במחשב (צריך דפדפן), ואז `scp config/gcal_token.json` לשרת ו-`chmod 600`.

## 🛡️ אבטחה ופרטיות

**מי יכול לשלוח הודעה בשמי?** אף אחד בלי ✅ שלך. הבוט מציית רק ל-`WA_OWNER`. כל הודעה לאדם אחר, אירוע ביומן או הרשמה באתר מוצגים לך קודם, ויוצאים רק אחרי לחיצה על כפתור ✅. מילים כמו "כן" בצ'אט לא נחשבות אישור.

**יש לזה גישה לסיסמאות שלי?** לא. המפתחות שלך נמצאים רק ב-`.env` על השרת שלך. בהרשמה באתרים (`agent/web_agent.py`) הוא אף פעם לא מקליד סיסמה, אשראי, ת"ז או קוד SMS, ולא לוחץ "לתשלום". במסלול א׳ אין על המכונה בכלל פרטי אוניברסיטה. במסלול ב׳ `TAU_PASSWORD` נמצא ב-`.env` שלך בלבד.

**מישהו שולח לבוט מייל או הודעה עם הוראות?** טקסט ממייל, אתר, מסמך או אדם אחר הוא מידע, לא פקודה. הכלים של המודל רק קוראים, ופעולה עוברת רק אם בדיקה נפרדת, שרואה רק את ההודעות שלך, מאשרת שביקשת אותה (`SECURITY.md`).

**מישהו אחר רואה את ההודעות שלי?** לא. הכול רץ על השרת שלך עם המפתחות שלך. מי שנתן לך את הפרויקט לא רואה כלום.

**אפשר להעלות לגיטהאב?** כן, ל-repo **פרטי**. לפני כל push: ‏`python scripts/release_scan.py` (חייב "0 finding(s)"), ופעם אחת `python scripts/install_git_hooks.py` כדי שכל push יריץ את הסריקה. לעולם לא להעלות `.env`, קבצי טוקן או `data/`.

**הדשבורד חשוף?** לא. הוא מאזין רק ל-127.0.0.1 ודורש `DASHBOARD_PASSWORD`. מבחוץ: מנהרת SSH או Tailscale Serve (לא Funnel).

## 🎓 שכבת הלימודים (SSA_STUDY)

**מה זה SSA_STUDY?** הדגל של שכבת הלימודים. `0` (ברירת המחדל) = רק העוזר, בלי שום קוד לימודים. `1` = מעקב מטלות ובחנים ממודל (גם מטלה שהוזכרה במייל, תוך 15 דקות), סולם דדליינים, ציונים ומשוב הבודק, סטטיסטיקת מבחנים, עדכונים מהאוניברסיטה רק לקבוצה שלך, סיכומי שיעורים מפנופטו, אתר קורס, NotebookLM, מבחנים ומערכת שעות. הכול ב-[FEATURES.md](FEATURES.md) §2.

**אפשר שכל הדדליינים ייכנסו ליומן שלי לבד?** כן, אבל רק אם תבקש במפורש: `config/assistant.yaml` עם `auto_deadlines_to_calendar: true` (מהדוגמה `config/assistant.example.yaml`). רק האירועים האלה נכתבים בלי כרטיס, ושום דבר לא נמחק: מה שהוגש מסומן "✅ הוגש". כל פעולה אחרת ביומן עדיין מחכה ל-✅.

**אני לא בתל אביב.** העוזר (מסלול א׳) עובד בכל מקום. חלק מההתאמות הן בקבצים: `SSA_UNIVERSITY`, `config/university_pages.json`, `config/campus.json`, `config/student_lines.txt`. שכבת הלימודים נבנתה לת"א: באוניברסיטה אחרת צריך להתאים את טופס ההתחברות ב-`collectors/panopto_collector.py` ו-`collectors/moodle_collector.py` ואת `settings.panopto.base_url` / `settings.moodle.base_url` ב-`config/courses.yaml`. ‏my.tau והמייל האוניברסיטאי ספציפיים לת"א (`SETUP.md` §ב5).

**איך מוסיפים קורס?** ב-`config/courses.yaml`: ‏`name`, ‏`course_code`, ‏`my_sessions` (יום, שעה, קבוצה), ‏`sources.panopto.folder_id` (`python scripts/find_panopto_folders.py`), ‏`sources.moodle.course_id`. ואז את המפתח שלו ל-`SSA_ACTIVE_COURSES` (`SETUP.md` §ב2).

**NotebookLM לא מתחבר בשרת?** במחשב `python -m notebooklm login`, או בשרת `scripts/vnc_notebooklm_login.sh`, או להעתיק את `~/.notebooklm/profiles/default/storage_state.json`. ‏`scripts/nlm_keepalive.py` בודק ומתריע. בלי NotebookLM הסיכומים והמיילים ממשיכים.

## 🧩 הרחבה וקוד

**איך מוסיפים שכבה משלי?** דגל ב-`config/features.py`, מודול ב-`agent/`, הכלים שלה ב-`TOOLS` של `agent/mind.py` (מסוננים ב-`active_tools()`), והבדיקות התקופתיות ב-`tick_list()` של `whatsapp/worker.py`. שכבה כבויה לא נטענת. שכבת הלימודים בנויה כך, אז מעתיקים ממנה (`README.md` "בנוי בשכבות").

**איזה מודל?** רק Gemini (`GEMINI_API_KEY`). אין ספק LLM אחר בקוד, ואין להוסיף (`CLAUDE.md`). OpenAI משמש רק לתמלול קול.

**מה זה "קלוד: …"?** בקשה לשנות את הבוט עצמו. היא רצה בסוכן קוד (Claude Code CLI) תחת משתמש מבודד עם חומת אש (`deploy/claude_agent/`), בענף נפרד, עוברת את כל הבדיקות ומחכה ל-✅. בלי ההגדרה הזו "קלוד:" לא משנה כלום, והשאר עובד.

**איך מריצים בדיקות?** ‏`PYTHONIOENCODING=utf-8 python -m tests.test_assistant_only` (מסלול א׳), ‏`python -m tests.test_safety_critical` (השער), ‏`python -m tests.conversation_evals`, ‏`python -m pytest tests/ -q`.

**בדיקה נכשלת אחרי שמחקתי קורס מהדוגמה?** חלק מהבדיקות מכירות את מפתחות הקורסים של `config/courses.example.yaml`. להחזיר אותם לדוגמה (את הקורסים שלך שמים ב-`config/courses.yaml`).

## 🤖 מה העוזר יודע לעשות (ודוגמאות)

| תחום | לכתוב לו |
|---|---|
| 📅 יומן | "תוסיף ליומן מחר ב-20 ארוחת ערב" · "מה יש לי השבוע" · "תזיז את הפגישה למחר" |
| ⏰ תזכורות | "תזכיר לי בעוד שעה להתקשר לאמא" · "כל יום ב-8 לקחת ויטמין" · "אילו תזכורות יש לי" |
| ⏳ משימות | "מה מחכה לי" · "ביטלתי את המלון" (סוגר את הפריט) · "תבטל את מה שעשית" (ביטול הפעולה האחרונה של הבוט) · "מה פספסתי" |
| 🧳 נסיעות | "אני בים המלח מ-26 עד 29" · "מה אני צריך לסיים לפני ים המלח?" |
| 📎 הקבצים שלך | "מה מספר ההזמנה של המלון?" (מכל קובץ או קישור ששלחת) |
| 🌐 רשת | "מה מזג האוויר מחר בחיפה" · "כמה זה 100 דולר בשקלים" · "מתי נכנסת שבת" |
| 🛠️ על עצמו | "איך עובד הגיבוי?" · "מה העלות החודשית?" (רק לבעלים) |
| 🤝 הודעות ותיאומים | "תתאם עם יוסי פגישה בשבוע הבא" · "שלח לאמא ש…" · כרטיס איש קשר + "תיצור איתם קשר" (הכול עם ✅) |
| 🌐 הרשמה באתר | "תרשום אותי ל<קישור>": בדפדפן אמיתי, עד לפני התשלום (צריך `playwright install chromium`) |
| 🔗 קריאה | קישור, קובץ, צילום: "מה הדדליין פה?", "תסכם לי". הודעה קולית: עם `OPENAI_API_KEY` |
| 🧠 זיכרון | "תזכור ש…" · "מה אתה זוכר עליי" |
| 🔕 שקט | "תפסיק לשאול על זה" · שעות שקט ב-`SSA_QUIET_HOURS` (ברירת מחדל 23-7) |
| 🎓 לימודים (`SSA_STUDY=1`) | "מה חסר לי?" · "מה אני צריך להגיש?" · "יצא פתרון לתרגיל 3?" · "על מה אני צריך להתמקד?" · "על מה לשים את הכסף בהסתברות?" · "בחן אותי" |

**אפשר שיזמין לי מקום / יקבע תור / יבטל הזמנה?** כן, בכתיבה לעסק (כרטיס איש קשר + "תיצור איתם קשר ותזמין…") או בהרשמה באתר עם קישור. הוא מכין את ההודעה או ממלא את הטופס, מראה לך, ושולח רק אחרי ✅ שלך. באתר הוא עוצר לפני תשלום (`agent/booking.py`, ‏`agent/web_agent.py`).

**שולח הודעות מעצמו?** רק הודעת בוקר ותזכורות שביקשת. השכבה ש"שמה לב" (`SSA_PROACTIVE=1`) כבויה כברירת מחדל: עד 3 הודעות ביום, לפחות 90 דקות ביניהן, אף פעם לא בשעות השקט.

**תזכורת חשובה כשוואטסאפ על השקט?** מה שסימנת כחשוב ולא ענית עליו מקבל עוד הודעה, ואחר כך התראה ביומן גוגל שמצלצלת גם כשוואטסאפ מושתק.

**מה הוא לא עושה:** לא מתקשר בטלפון (רק כותב בוואטסאפ), לא משלם, לא מקליד סיסמאות, אין אפליקציה נפרדת (הוא חי בוואטסאפ), ואין חיבור לאינסטגרם/טלגרם כערוץ שיחה (טלגרם רק להתראות).

## 🧯 טבלת תקלות

| מה רואים | מה עושים |
|---|---|
| `no .env — run: python scripts/setup_check.py --init` | להריץ `--init`, ואז למלא את `.env` |
| `GEMINI_API_KEY — https://aistudio.google.com/apikey` (❌) | אין מפתח ב-`.env`. ליצור ב-AI Studio ולהדביק |
| `GEMINI_API_KEY does not look like a Google key` | מפתח Gemini מתחיל ב-`AIza`. אולי הודבק מפתח אחר או עם רווח/מרכאות |
| `WA_BOT_NUMBER == WA_OWNER` | אותו מספר: הבוט לא ישמע אותך. צריך מספר שני לבוט |
| `TELEGRAM_USER_ID is empty while a bot token is set` | בלי `TELEGRAM_USER_ID` הבוט בטלגרם עונה לכל אחד. למלא, או למחוק את `TELEGRAM_BOT_TOKEN` |
| `need 3.11+ (3.12 recommended)` | להתקין Python 3.12 וליצור את `venv` מחדש |
| `<package> — pip install -r requirements.txt` / `ModuleNotFoundError` | הסביבה לא פעילה או חסרה התקנה: `venv/bin/pip install -r requirements.txt` |
| `ffmpeg not found` | רק לשכבת הלימודים (אודיו וקריאת לוח). להתקין ffmpeg או `FFMPEG_PATH` |
| `config/courses.yaml missing` | `python scripts/setup_check.py --init --student`, ואז לערוך |
| `SSA_ACTIVE_COURSES lists keys not in courses.yaml` | המפתחות ב-`SSA_ACTIVE_COURSES` חייבים להיות בדיוק כמו ב-`config/courses.yaml` |
| `still example ids (Panopto folder / Moodle id)` | `python scripts/find_panopto_folders.py` ו-`course_id` מכתובת המודל |
| `tracked by git (remove + rotate!)` | קובץ סודי בגיט: `git rm --cached`, ולהחליף את הסוד |
| `not yet: …` / `WhatsApp bridge session` חסר | עוד לא בוצע צימוד (§וואטסאפ למעלה) |
| הבוט שותק, אין שגיאה | `cat data/budget_lock.json`: עבר ‎$50 בחודש? ראו "יש תקרה" |
| `invalid_grant` (Gmail/יומן) | הטוקן בוטל: `python scripts/gmail_reauth.py` או שוב `gcal_auth.py` |
| הבדיקות נכשלות אחרי שינוי קורסים | `config/courses.example.yaml` צריך את הקורסים המקוריים |

## 📖 מילון משתנים (.env)

| משתנה | מה |
|---|---|
| `GEMINI_API_KEY` | **חובה.** מפתח Gemini, ה-LLM היחיד. `GOOGLE_API_KEY` = גיבוי, גם הוא Gemini |
| `WA_TRANSPORT` | `bridge` = מכשיר מקושר (ברירת המחדל) |
| `WA_OWNER` | **חובה.** המספר שלך, ‎`9725XXXXXXXX`. היחיד שהבוט מציית לו |
| `WA_BOT_NUMBER` | **חובה.** המספר של הבוט (סים/eSIM נוסף) |
| `WA_OWNER_CHAT` | רק אם הגשר מדווח מזהה צ'אט אחר (`…@lid`) |
| `WA_OWNER_NAME` / `WA_PARTNER_NAME` | איך הבוט קורא לך / לבן או בת הזוג |
| `WA_ALLOWED` | מספרים נוספים שמותר להם לדבר עם הבוט (בן/בת זוג), מופרדים בפסיק |
| `SSA_GROUPS` | `1` = הבוט בקבוצות, רק כשאתה מתייג אותו |
| `SSA_PROACTIVE` | `1` = השכבה ש"שמה לב" (עד 3 ביום) |
| `SSA_QUIET_HOURS` | שעות שקט, ברירת מחדל `23-7` |
| `SSA_BRAIN` | `1` = המוח (מומלץ). בלי זה עונה הנתב הישן |
| `SSA_BRAIN_MODEL` / `SSA_BRAIN_DAY_CAP` / `SSA_BRAIN_MAX_COST` | מודל השיחות / תקרה יומית (‎$1) / תקרה להודעה (‎$0.10) |
| `SSA_MONTH_CAP_USD` / `SSA_SITE_API_CAP` | התקרה החודשית (‎$50, או לפי חודש ב-`config/budget_caps.json`) / בניית האתר בלילה (‎$0.10) |
| `SSA_SUBSCRIPTION` | `1` = מנוי Gemini אישי לחלק מהעבודה. **כבוי כברירת מחדל**, לקרוא קודם את `COSTS.md` |
| `NTFY_TOPIC` | התראה מחוץ לוואטסאפ כשהקישור לגשר נופל (רשות, בנוסף למייל) |
| `SSA_NOTIFY_CHANNEL` | לאן התראות: `whatsapp` או `telegram` |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_USER_ID` | ערוץ התראות שני. בלי ה-ID הבוט עונה לכולם |
| `DASHBOARD_PASSWORD` | חובה אם מריצים את הדשבורד |
| `SSA_STUDY` | `0` = רק העוזר, `1` = שכבת הלימודים |
| `TAU_USERNAME` / `TAU_ID` / `TAU_PASSWORD` | ההתחברות לאוניברסיטה (רק ב-`SSA_STUDY=1`) |
| `SSA_ACTIVE_COURSES` | המפתחות של קורסי הסמסטר מתוך `config/courses.yaml` |
| `OPENAI_API_KEY` | רק תמלול: הודעות קוליות, והרצאות בלי כתוביות כשהתמלול המקומי לא זמין |
| `SSA_LOCAL_ASR` / `SSA_ASR_PYTHON` / `SSA_ASR_MODEL` | תמלול הרצאות מקומי בשרת (‎$0) / ה-venv שלו / תיקיית המודל |
| `SMART_STUDY_DISABLE` / `SMART_STUDY_DISABLE_WHISPER` | לכבות את צינור הלימודים / אף פעם לא לשלם על תמלול |
| `GMAIL_CREDENTIALS_JSON` / `RECIPIENT_EMAIL` | סיכומים במייל |
| `SSA_AUTHORIZED_KEYS` | טביעות האצבע של מפתחות ה-SSH המותרים בשרת (לבדיקת האבטחה) |
| `BACKUP_GPG_RECIPIENT` | גיבוי סודות מוצפן ל-Drive |
| `SSA_STARTER_REPO` | ‏`owner/repo` ציבורי שהבוט עוזר לחברים להקים (רשות, כבוי כשריק) |

</div>

---

## English

Always start with `python scripts/setup_check.py`: it knows your path (`SSA_STUDY`), checks only what that path needs, and never prints a secret.

**Where do I start?** `python -m venv venv`, `venv/bin/pip install -r requirements.txt`, `python scripts/setup_check.py --init`, then `SETUP.md` in order, re-running `setup_check` after each step. Or open the folder in Claude Code and say "help me set this up" (it follows `CLAUDE.md` and never touches your keys).

**Server or my PC?** A server is recommended: Oracle Cloud Always Free (Ubuntu 22.04 ARM), $0, online 24/7. A PC (Windows/Mac/Linux) is fine to try it out, but the bot only answers while the PC is on. On Windows use `venv\Scripts\...`; the `deploy/systemd/` services are Linux only.

**Do I need a second SIM?** Yes. The bot is a linked device of one WhatsApp account (`WA_BOT_NUMBER`) and you talk to it from yours (`WA_OWNER`). The bridge ignores messages sent from its own account, so on the same number it would never hear you. A cheap second SIM or eSIM also keeps a possible ban away from your main number.

**Pairing:** `WA_TRANSPORT=bridge`, `WA_BOT_NUMBER`, `WA_OWNER` in `.env`; `venv_wa/bin/pip install neonize`; run `whatsapp/bridge.py`; the code appears in `data/wa_bridge/pair_code.txt`; on the bot's phone: Linked devices → Link a device → "Link with phone number".

**Keys and costs:** the Gemini key comes from https://aistudio.google.com/apikey (billing on plus a budget alert) and lives only in `.env`. Assistant only is about $3–10 a month, with the study layer about $15–30 (see [COSTS.md](COSTS.md)). A mind message is $0.005–0.015; fast-path answers, reminders, buttons and undo are $0; the Oracle server is $0; a lecture summary is about $0.08 with captions or local transcription, or $0.40–0.50 with the paid transcriber. The personal-subscription route (`SSA_SUBSCRIPTION=1`) is off by default. A hard $50/month lock (or one per month in `config/budget_caps.json`) stops every paid call and switches the bot to emergency mode, which answers from your data. There is also a daily cap on conversations (`SSA_BRAIN_DAY_CAP`). OpenAI is only for transcription (voice notes, and lectures without captions).

**Google Calendar:** enable the Calendar API, set the OAuth consent screen to External and then **In production** (in Testing the token dies after 7 days), create a **Desktop app** client, then run `python scripts/gcal_auth.py <client.json>` → `config/gcal_token.json`. For a server, sign in on your PC and `scp` the token.

**Safety:** only `WA_OWNER` is obeyed. Nothing goes to another person, your calendar or a website without your ✅ button. Text from mail, the web or other people is data, never a command. It never types passwords or card details and never clicks pay. Your keys stay in your own `.env` on your own server.

**Add your own layer:** a flag in `config/features.py`, a module in `agent/`, its tools in `TOOLS` of `agent/mind.py` (filtered by `active_tools()`), and its ticks in `tick_list()` of `whatsapp/worker.py`. Copy the study layer.

**What's new and where:** every feature with its switch and cost is in [FEATURES.md](FEATURES.md); how a message travels (fast path → mind → tools → fact check → outbox) and the safety gates are in [ARCHITECTURE.md](ARCHITECTURE.md).

**Update:** `git pull`, `pip install -r requirements.txt`, run the tests, then `sudo systemctl restart smart-study-wa-worker smart-study-wa-bridge`.

**What it doesn't do:** phone calls (it writes in WhatsApp), payments, typing passwords, a separate app, Instagram.

| You see | Do |
|---|---|
| `no .env` | `python scripts/setup_check.py --init` |
| `GEMINI_API_KEY does not look like a Google key` | a Gemini key starts with `AIza`; check for spaces or quotes |
| `WA_BOT_NUMBER == WA_OWNER` | same number: the bot can't hear you; use a second number |
| `TELEGRAM_USER_ID is empty while a bot token is set` | set it, or the Telegram bot answers anyone |
| `need 3.11+` | install Python 3.12 and recreate `venv` |
| `ModuleNotFoundError` | `venv/bin/pip install -r requirements.txt` in the active venv |
| `ffmpeg not found` | study layer only: install ffmpeg or set `FFMPEG_PATH` |
| `SSA_ACTIVE_COURSES lists keys not in courses.yaml` | the keys must match `config/courses.yaml` exactly |
| silent bot, no error | `cat data/budget_lock.json`: the $50 lock |
| `invalid_grant` | re-run `scripts/gmail_reauth.py` / `scripts/gcal_auth.py` |
