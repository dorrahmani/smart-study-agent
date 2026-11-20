# Runbook — Smart Study Agent

מה עושים כשמשהו לא עובד. סעיפים 1–2 מבוססים על פקודות שהורצו על המערכת האמיתית (09/2026); סעיף 3 (בנייה מאפס) עוד לא תורגל מקצה לקצה.

---

## 0. מפה מהירה

| מה | איפה |
|---|---|
| Python | 3.12 (deadsnakes). `venv` הוא symlink ל-`venv312`; הישן (3.10) ב-`venv310` — חזרה: `ln -sfn venv310 venv` + restart לשירותים |
| השרת | `ubuntu@<your-server>` · מפתח: `~/.ssh/smart_study.key` · Oracle Cloud Always Free (ARM, Ubuntu 22.04) |
| קוד חי | `~/smart_study_agent` (לעולם לא עורכים שם — רק `git push vm`) |
| מאגר git בשרת | `~/smart_study_agent.git` · hooks: `deploy/hooks/` |
| סודות | `.env` (600) · `config/gmail_token.pickle` · `config/secrets/` · `~/.config/rclone/rclone.conf` · `~/.notebooklm/profiles/default/storage_state.json` |
| שירותים | `dashboard.service` · `smart-study-wa-worker.service` · `smart-study-wa-bridge.service` (הגדרות: `deploy/systemd/`). `smart-study-bot.service` (טלגרם) **כבוי מ-10/11/2026** — הבוט נמחק; הקובץ נשאר בצד, לא להפעיל |
| cron (UTC!) | `crontab -l` · מקור אמת: `deploy/crontab.txt` · יומן העטיפה: `/tmp/cron_health.log` |
| לוג הריצה הלילית | `data/daily_full_log.txt` (+ 14 ימים אחורה, דחוסים) · חותמת: `data/last_pipeline_run.json` |
| גיבוי | `gdrive:smart-study-agent-backup/<YYYY-MM-DD>/` · 14 ימים · כל לילה 04:00 UTC |
| דשבורד | **https://your-server.your-tailnet.ts.net** (Tailscale Serve, tailnet בלבד — מכל מכשיר שמחובר ל-Tailscale בחשבון user6@example.com) · או `דשבורד.bat` (מנהרת SSH) |

```bash
ssh -i ~/.ssh/smart_study.key ubuntu@<your-server>
```

## 1. איך יודעים שהכול תקין

- **כל ערב בסמסטר**: בוואטסאפ כל סיכום חדש (כשיש הרצאה) + מייל עם PDF.
- **כל בוקר**: הודעת הבוקר בוואטסאפ. אם הריצה של אתמול לא רצה / לא הסתיימה — מגיעה בוואטסאפ התראת 🚨 (`scripts/cron_heartbeat.py`, פעם ביום אחרי 07:00) — ר׳ §3.
- **התראות אוטומטיות** (וואטסאפ בלבד, `utils/notify.py`; טלגרם נמחק 10/11): ⚠️ משימה מתוזמנת נכשלה · ⚠️ דוח בריאות pipeline · 🚨 cron לא רץ 10 שעות (`scripts/cron_heartbeat.py`) · 💾 דיסק / זיכרון (`memory_watchdog.sh`).
- **התראה שוואטסאפ לא קיבל** (הגשר למטה / אין צ׳אט בעלים) לא הולכת לאיבוד: נרשמת ב-`data/undelivered_alerts.jsonl`, מוצגת בדשבורד (📭) ובדוח השבועי על הבוט. בדיקה: `tail -5 data/undelivered_alerts.jsonl`.
- **בדיקה ידנית** (דקה):
  ```bash
  cd ~/smart_study_agent && tail -5 /tmp/cron_health.log && cat data/last_pipeline_run.json && systemctl is-active dashboard smart-study-wa-worker smart-study-wa-bridge
  ```

## 2. תקלות נפוצות

### 🔑 פנופטו/מודל לא מתחברים ("ההתחברות נכשלה")
כמעט תמיד: **סיסמת TAU השתנתה**.
1. עדכן `TAU_PASSWORD` ב-`.env` **במחשב**.
2. העתק לשרת בלי להדפיס (גיבוי אוטומטי ל-`backups/env.<ts>`):
   ```bash
   grep -E "^TAU_PASSWORD=" .env | ssh -i ~/.ssh/smart_study.key ubuntu@<your-server> 'cd ~/smart_study_agent && install -m 600 .env backups/env.$(date +%Y%m%d-%H%M%S) && python3 -c "import sys;n=sys.stdin.readline().rstrip(\"\n\");L=open(\".env\").read().split(\"\n\");open(\".env\",\"w\").write(\"\n\".join(n if l.startswith(\"TAU_PASSWORD=\") else l for l in L))" && chmod 600 .env'
   ```
3. בדוק: `venv/bin/python3 scripts/find_panopto_folders.py` → "התחברות הצליחה!".

### 📧 Gmail: `invalid_grant`
הטוקן בוטל/פג. במחשב: `python scripts/gmail_reauth.py` (פותח דפדפן) ← ואז:
```bash
scp -i ~/.ssh/smart_study.key config/gmail_token.pickle ubuntu@<your-server>:smart_study_agent/config/ && ssh -i ~/.ssh/smart_study.key ubuntu@<your-server> 'chmod 600 ~/smart_study_agent/config/gmail_token.pickle'
```

### 📓 NotebookLM: "cookie פג" / Authentication expired
במחשב: `python -m notebooklm login --browser chrome` (notebooklm-py ≥ 0.8.3; חשבון `user3@example.com` — שם 16 המחברות) ← ואז:
```bash
scp -i ~/.ssh/smart_study.key ~/.notebooklm/profiles/default/storage_state.json ubuntu@<your-server>:.notebooklm/profiles/default/storage_state.json && ssh -i ~/.ssh/smart_study.key ubuntu@<your-server> 'chmod 600 ~/.notebooklm/profiles/default/storage_state.json'
```
(NotebookLM אינו חוסם — אם הוא נופל, הסיכומים והמיילים ממשיכים.)
בדרך כלל זה קורה לבד: בשרת `scripts/nlm_keepalive.py` בודק כל 20 דק' (`logs/nlm_keepalive.log`), ובמחשב המשימה `SmartStudy_NLM_Session` (כל 4 שעות, בכניסה, ביציאה משינה) מחדשת ומעתיקה לשרת כשהוא מת — יומן: `~/.smart_study/nlm_session.log`. ידנית: `python scripts/pc/nlm_session_refresh.py --force`.

### 🔑 master token ל-NotebookLM (פתרון קבוע, החלטה של המשתמש)
השרת מחזיק *עותק* של הסשן של כרום במחשב, וגוגל מפילה את העותק אחרי שעות–ימים (15/11). עם master token השרת מנפיק לעצמו סשן משלו ומחדש אותו לבד כשהוא נופל — בלי המחשב. פעם אחת: בשרת `venv/bin/pip install 'notebooklm-py[headless]==0.8.6'`; במחשב `pip install 'notebooklm-py[headless]==0.8.6'` ואז `python -m notebooklm login --master-token --account user3@example.com` (חלון כניסה של גוגל — ייתכן שיבקש סיסמה/אישור בטלפון; את זה עושה המשתמש בעצמו), ואז להעתיק את `master_token.json` ו-`storage_state.json` מ-`~/.notebooklm/profiles/default/` לאותה תיקייה בשרת (chmod 600). ⚠️ הסיכון: ה-token הוא גישה מלאה וארוכת-טווח לכל החשבון (Gmail, Drive…) עד שמחליפים סיסמה או מסירים את "מכשיר האנדרואיד" ב-myaccount.google.com → אבטחה. מי שמשיג את הקובץ בשרת — משיג את החשבון. מתאים רק לחשבון הייעודי הזה, לא לחשבון האישי.


### 💸 אין סיכומים ואין שגיאה — נעילת תקציב
`cat data/budget_lock.json` — אם קיים, ההוצאה החודשית עברה $50. בדוק בדשבורד (עלויות) **למה** לפני שמוחקים את הקובץ. נעילה מחודש קודם מתנקה לבד.

### 🎙️ תמלול נכשל (`insufficient_quota` / 429)
נגמר הקרדיט ב-OpenAI → platform.openai.com/settings/organization/billing.

### 🚨 "אין סיכום חדש N ימים" בקורס
1. `grep -E "PIPELINE-WARN|סינון מערכת שעות|הרצאות רלוונטיות" data/daily_full_log.txt` — מה הסינון עשה?
2. `folder_id` עדיין `TO_BE_FILLED`? → `scripts/find_panopto_folders.py` ← עדכן `config/courses.yaml` ← commit ← `git push vm`.
3. "אף הקלטה לא תואמת את המערכת שלי" → השיעור זז? עדכן `my_sessions` (יום/שעה) ב-`courses.yaml`.
4. באמת לא הועלתה הקלטה? בדוק בפנופטו בדפדפן.

### 🚨 "הצינור לא רץ" / "cron לא רץ 10 שעות"
```bash
ls -l ~/smart_study_agent/scripts/*.sh      # כולם חייבים x (‎-rwxr...)
crontab -l | head; systemctl is-active cron
tail -20 /tmp/cron_health.log; tail -5 /tmp/*.log
```
הרצה ידנית של הריצה הלילית (גם מחוץ לסמסטר):
```bash
cd ~/smart_study_agent && SMART_STUDY_FORCE=1 setsid nohup scripts/run_with_alert.sh "Daily Pipeline (manual)" scripts/run_daily.sh > /tmp/manual_run.out 2>&1 < /dev/null &
```

### 🛑 הפריסה נדחתה ("SAFETY TESTS FAILED")
זה השער עושה את העבודה שלו — השרת לא נגע. קרא את הבדיקה שנכשלה, תקן מקומית:
```bash
PYTHONIOENCODING=utf-8 python -m tests.test_safety_critical
```
**לעולם לא עוקפים את השער.**

### ↩️ פריסה שברה משהו
```bash
git revert <sha> && git push vm
```
(force-push חסום בכוונה.)

### 📵 הגשר של וואטסאפ התנתק (התראה מחוץ לוואטסאפ)
`scripts/wa_bridge_watch.py` (cron כל 5 דק׳, בלי LLM, `bridge.py` לא נגוע) קורא את `data/wa_bridge/status.json`, קיום `session.sqlite3`, `systemctl is-active` ואת היומן של `smart-study-wa-bridge`. **התנתקות/סשן חסר → התראה מיד; מנותק או שירות למטה 10+ דק׳ → התראה.** ניתוק של שניות (websocket EOF ואז "connected to WhatsApp") הוא רגיל ולא מתריע. התראה אחת לכל תקלה + הודעת "חזר" אחת; מצב ב-`data/wa_bridge_watch.json`, לוג ב-`logs/wa_bridge_watch.log`.
**ערוצים (כולם מחוץ לוואטסאפ, כל מה שמוגדר):** מייל למשתמש עצמו (`RECIPIENT_EMAIL` ב-.env או `settings.email.recipient` ב-courses.yaml) דרך טוקן ה-Gmail הקיים (`config/gmail_token.pickle`, הרשאת send) · ו-ntfy.sh אם `NTFY_TOPIC` מוגדר ב-.env (נושא ארוך ואקראי, **לא ב-git**). אם `invalid_grant` — ראה "Gmail" למעלה; אף ערוץ לא עבד → נרשם ב-`data/undelivered_alerts.jsonl` ומנסים שוב כל 5 דק׳.
**הפעלת ntfy (פעם אחת, המשתמש):** 1. בטלפון: להתקין את האפליקציה ntfy (חנות האפליקציות). 2. בשרת: `echo "NTFY_TOPIC=ssa-$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" >> ~/smart_study_agent/.env` ואז `grep ^NTFY_TOPIC ~/smart_study_agent/.env` כדי לראות את השם. 3. באפליקציה: ＋ ← Subscribe to topic ← להדביק את השם (שרת ברירת מחדל ntfy.sh). 4. בדיקה: `curl -d "בדיקה" ntfy.sh/<השם>` — צריכה להגיע התראה. (כל מי שיודע את השם יכול לקרוא — לכן הוא ארוך ואקראי ולא נשמר ב-git.)
**חיבור מחדש (הגשר לא מדפיס QR — מזווג בקוד):**
1. `ssh <user>@<your-server>`
2. `cd ~/smart_study_agent/data/wa_bridge && mkdir -p old && mv session.sqlite3* old/` (מזיזים הצידה, לא מוחקים)
3. `sudo systemctl restart smart-study-wa-bridge; sleep 40; cat ~/smart_study_agent/data/wa_bridge/pair_code.txt` (קוד של 8 תווים; מגיע גם כהתראת push בטלפון של הבוט)
4. בטלפון של מספר הבוט: WhatsApp ← מכשירים מקושרים (Linked devices) ← קישור מכשיר (Link a device) ← "קישור עם מספר טלפון במקום" ← להקליד את הקוד.
5. `cat ~/smart_study_agent/data/wa_bridge/status.json` צריך להראות `connected`; תוך 5 דק׳ תגיע הודעת "חזר". אם `WA_BOT_NUMBER` חסר ב-`config/wa_bridge.env` הגשר לא יזווג.
**בדיקה בלי להפיל כלום:** `venv/bin/python3 scripts/wa_bridge_watch.py --simulate logged_out` (מדפיס את ההודעה, לא שולח).

### 🔌 השרת לא עונה ל-SSH
1. חכה 5 דק׳ (ה-watchdog במחשב מנסה לאושש).
2. Oracle Cloud Console → Compute → Instances → smart-study-agent → **Reboot**.
3. אחרי עלייה: `systemctl is-active dashboard smart-study-wa-worker smart-study-wa-bridge cron` + `python -m tests.test_safety_critical`.

### 🔐 שחזור סודות מהגיבוי (מוצפן)
מאז 28.09.2026 הסודות (`.env`, `config/secrets`, טוקן Gmail, `rclone.conf`, סשן NotebookLM) נשמרים ב-Drive **רק** כ-`secrets.tar.gz.gpg`, מוצפן למפתח `<YOUR_GPG_FINGERPRINT>`.
**המפתח הפרטי נמצא רק במחשב שלך** (`gpg --list-secret-keys`). בלעדיו אי אפשר לפתוח את הגיבוי — ר' "גיבוי המפתח" למטה.
```bash
# על ה-VM: הורד את הקובץ המוצפן (בטוח — הוא מוצפן)
rclone copyto gdrive:smart-study-agent-backup/<YYYY-MM-DD>/secrets.tar.gz.gpg /tmp/s.gpg
# במחשב: פענח וחלץ לתיקייה פרטית
scp -i ~/.ssh/smart_study.key ubuntu@<your-server>:/tmp/s.gpg . && gpg -d s.gpg | tar -xzf - -C <private-dir>
```
**גיבוי המפתח הפרטי (פעם אחת, חובה):** `gpg --armor --export-secret-keys <YOUR_GPG_FINGERPRINT> > backup-key.asc` ← שמור במנהל הסיסמאות / USB, ומחק את הקובץ מהדיסק.

### 🛡️ הקשחת השרת (מה מוגדר ואיפה)
- חומת אש: רק 22/tcp (`sudo ufw status`). CUPS ו-rpcbind כבויים.
- SSH: `deploy/ssh/00-hardening.conf` → `/etc/ssh/sshd_config.d/` (בלי root, בלי סיסמאות, 3 ניסיונות, רק `ubuntu`). מפתח מורשה יחיד: `ssh-key-2026-05-08` = `~/.ssh/smart_study.key`.
- fail2ban: `deploy/fail2ban/sshd-local.conf` (חסימה שעה, מתארכת בחזרות). בדיקה: `sudo fail2ban-client status sshd`.
- **שינוי ב-sshd תמיד עם רשת ביטחון**: `sudo systemd-run --unit=ssh-revert --on-active=180 <החזרה>` → reload → התחברות חדשה → `sudo systemctl stop ssh-revert.timer`.

### 🔒 בדיקת אבטחה לילית (`scripts/security_check.py`)
בודקת כל לילה שההקשחה לא נסוגה: פורטים מול רשימה, ufw, כללי היציאה של claudeagent/geminiagent, הרשאות קבצי סוד,
קבוצות/משתמשים/sudoers/מפתחות SSH/sshd, fail2ban, הקשחת הגשר והדשבורד, השער המבודד, עדכונים, כניסות SSH, tailscale,
טוקנים בלוגים וב-git (רק ספירה, אף פעם לא הערך), בדיקה חיה של הטוקנים, ו-api_only/תקרות/בדיקות השער. בלי LLM.
- **cron** (UTC, 03:30 = 05:30 בישראל; ההתראה מחכה לסוף שעות השקט, חוץ מחשד לפריצה שנשלח מיד):
  `30 3 * * * TZ=Asia/Jerusalem /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Security Check" bash -c "cd /home/ubuntu/smart_study_agent && PYTHONIOENCODING=utf-8 venv/bin/python3 scripts/security_check.py" >> /home/ubuntu/smart_study_agent/logs/security_check.log 2>&1`
- **ידני:** `venv/bin/python3 scripts/security_check.py --dry` (טבלה בלבד, בלי קובץ ובלי התראה); `--no-live` בלי קריאות לגוגל.
- **תוצאה:** `data/security_status.json` (אחרון) + `data/security_history.jsonl` (שורה ללילה). התראה בוואטסאפ רק על FAIL חדש;
  הדוח השבועי על הבוט מראה "🔒 אבטחה: N בדיקות עברו, X אזהרות".
- **שינוי מכוון** (מפתח SSH חדש, קובץ sudoers, מאזין חדש) יופיע פעם אחת כ-FAIL/WARN: לעדכן את הקבוע המתאים בראש הסקריפט
  (`EXPOSED_OK`, `AUTHORIZED_KEYS`, `SUDOERS_D`, `SHELL_USERS`, `API_ONLY_EXPECTED`...) באותו commit של השינוי.
- **תיקונים נפוצים:** לוגים ב-/tmp קריאים לכולם → `chmod 600 /tmp/*.log`; נדרש אתחול → `sudo reboot` (בשעה שקטה);
  קובץ סוד פתוח → `chmod 600 <הקובץ>`; שער לא מבודד → שתי השורות האחרונות ב-`deploy/hooks/README.md`.

## 3. בניית שרת חדש מאפס (אסון)

יעד: פחות משעה. צריך: המחשב שלך (קוד + `.env`) והגיבוי ב-Drive.

1. **Oracle**: instance חדש, Ubuntu 22.04 ARM (Ampere A1, 4 OCPU / 24GB), המפתח הציבורי `ssh-key-2026-05-16`.
2. **מערכת**:
   ```bash
   sudo apt update && sudo apt install -y software-properties-common git ffmpeg rclone ufw gnupg
   sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt install -y python3.12 python3.12-venv python3.12-dev
   sudo ufw allow 22/tcp && sudo ufw enable
   ```
3. **קוד** (מהמחשב): צור bare repo בשרת, התקן hooks מ-`deploy/hooks/`, ואז `git remote set-url vm ...` + `git push vm`.
4. **סביבה**:
   ```bash
   cd ~/smart_study_agent && python3.12 -m venv venv && venv/bin/pip install -r requirements.lock.txt && venv/bin/playwright install --with-deps chromium
   ```
5. **סודות**: `scp .env` מהמחשב (chmod 600), או פענוח `secrets.tar.gz.gpg` (ר' "שחזור סודות") · `rclone.conf` (ר' `scripts/vm_setup_rclone.sh`).
   הקשחה: `sudo install -m 644 deploy/ssh/00-hardening.conf /etc/ssh/sshd_config.d/` (עם רשת הביטחון!) · `sudo apt install fail2ban && sudo install -m 644 deploy/fail2ban/sshd-local.conf /etc/fail2ban/jail.d/`
   מפתח ציבורי לגיבוי: `gpg --armor --export <YOUR_GPG_FINGERPRINT>` במחשב → `gpg --import` בשרת.
6. **נתונים מהגיבוי** (כל נתיב חוזר למקומו):
   ```bash
   D=<YYYY-MM-DD>; rclone copy gdrive:smart-study-agent-backup/$D/ ~/smart_study_agent/ && chmod 600 ~/smart_study_agent/.env ~/smart_study_agent/config/*.pickle
   ```
7. **שירותים ו-cron**:
   ```bash
   sudo cp deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now dashboard smart-study-wa-worker smart-study-wa-bridge   # לא smart-study-bot (טלגרם, נמחק 10/11)
   sudo install -m 644 deploy/logrotate/smart-study /etc/logrotate.d/smart-study
   crontab deploy/crontab.txt
   ```
8. **אימות**: `python -m tests.test_safety_critical` · `scripts/dress_rehearsal.py --no-email` · `דשבורד.bat` (עדכן IP).

## 4. תחילת סמסטר חדש — צ׳קליסט

1. `config/courses.yaml`: `semester_start`, `semester_end`, `no_class_dates` (מקור אמת יחיד — גם `run_daily.sh` קורא מכאן).
2. קורסים חדשים ב-`courses.yaml` (+ `my_sessions` מהיומן) וב-`config/paths.py` (`COURSE_NAMES`, `ACTIVE_COURSES`).
3. `scripts/find_panopto_folders.py` → `folder_id` לכל קורס.
4. `python -m tests.test_safety_critical` → commit → `git push vm`.
5. `scripts/dress_rehearsal.py` על הרצאה ישנה (מייל רק אליך).
6. אחרי ההרצאה הראשונה: בדוק שנקלטה רק הקבוצה שלך.

## 5. ⚠️ לפני שמשתפים את הקוד (GitHub / חברים)
- `python scripts/release_scan.py` חייב להחזיר "clean" (וגם `--all` אם משתפים תיקייה ולא git).
- אף פעם לא: `.env`, `config/*.pickle`, `storage_state.json`, `config/courses.yaml` (המערכת שלך), `data/`, `logs/`.
- אם סוד נכנס פעם להיסטוריה — מחליפים (rotate) את הסוד; מחיקה מהקובץ לא מספיקה.

## 6. Tailscale (גישה לדשבורד מכל מקום)
- השרת `smart-study` (<server-tailnet-ip>) ב-tailnet של user6@example.com. הדשבורד: `tailscale serve --bg 8501` → `https://your-server.your-tailnet.ts.net` (**tailnet only** — לא Funnel, לא ציבורי). שום פורט לא נפתח ב-ufw/Oracle.
- בדיקה: `tailscale serve status` (צריך להראות "(tailnet only)"). **לעולם לא** `tailscale funnel` — זה היה חושף את הדשבורד לאינטרנט.
- מכשיר חדש: להתקין Tailscale ולהתחבר באותו חשבון. להוציא מכשיר: login.tailscale.com → Machines → Remove.
- **ACL (01/10):** השרת מתויג `tag:server`; מדיניות: המכשירים האישיים (autogroup:member) → אחד לשני + `tag:server:443` בלבד; לשרת אין שום גישה החוצה למכשירים. אומת: PC→דשבורד 200, שרת→PC חסום (ping + tcp).
- ⚠️ `pkill -f "tailscale serve"` הורג גם את סשן ה-SSH שמריץ אותו (הטקסט מופיע בשורת הפקודה) — לא להשתמש.

## 7. סוכן אישי — וואטסאפ + יומן (הפעלה)
הקוד כבוי עד שממלאים את המשתנים ב-`.env` (ראו SETUP.md).

**יומן גוגל:**
1. ב-Google Cloud Console של הפרויקט: להפעיל Calendar API; במסך ה-OAuth consent: מצב **In production** (ב-Testing הטוקן מת אחרי 7 ימים); לקוח OAuth מסוג Desktop → להוריד JSON.
2. במחשב: `python scripts/gcal_auth.py <client.json>` → אישור בדפדפן (הרשאה: אירועים בלבד).
3. `scp -i ~/.ssh/smart_study.key config/gcal_token.json ubuntu@<your-server>:smart_study_agent/config/` ואז בשרת `chmod 600 ~/smart_study_agent/config/gcal_token.json`.
4. בדיקה בוואטסאפ: "מה יש לי מחר ביומן".
5. (אופציונלי) דדליינים ליומן: `SSA_DEADLINES_TO_CALENDAR=1` ב-.env.

**וואטסאפ:**
1. developers.facebook.com → אפליקציה (Business) → מוצר WhatsApp. לרשום את המספר של המשתמש כנמען (קוד בוואטסאפ).
2. System User + טוקן קבוע עם הרשאות `whatsapp_business_messaging`, `whatsapp_business_management`.
3. ב-.env (600): `WA_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_OWNER=9725XXXXXXXX` (`SSA_NOTIFY_CHANNEL` כבר לא נקרא — וואטסאפ הוא הערוץ היחיד מ-10/11).
4. `config/wa_receiver.env` (600) — **רק**: `WA_APP_SECRET`, `WA_VERIFY_TOKEN` (מחרוזת אקראית), `WA_OWNER`, `WA_SPOOL_DIR=/home/ubuntu/smart_study_agent/data/wa_spool`.
5. שירותים:
   `sudo cp deploy/systemd/smart-study-wa-{receiver,worker}.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now smart-study-wa-receiver smart-study-wa-worker`
6. Funnel (רק פורט 8443 → רכיב הקבלה; הדשבורד נשאר על 443 בתוך ה-tailnet):
   במדיניות Tailscale: `"nodeAttrs": [{"target": ["tag:server"], "attr": ["funnel"]}]`, ואז בשרת
   `sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8090` · בדיקה: `tailscale funnel status` — חייב להראות **רק** 8443.
7. ב-Meta → Webhooks: Callback `https://your-server.your-tailnet.ts.net:8443/wa/webhook`, Verify token = `WA_VERIFY_TOKEN`, להירשם ל-`messages`.
8. בדיקה: לשלוח "היי" לבוט → תפריט. לוגים: `logs/wa_worker.log`, `journalctl -u smart-study-wa-receiver`.
**כיבוי חירום:** `sudo tailscale funnel --https=8443 off` (מנתק מהאינטרנט מיד) · "עצור סוכן" בצ'אט (עוצר פעולות).

### 🧠 המוח של הבוט (`agent/mind.py`)
הודעה רגילה של המשתמש עוברת לולאה של Gemini עם כלים לקריאה בלבד: יומן, משימות, הגשות, מערכת, מייל, חומרי קורס, סיכומים, זיכרון, אינטרנט. פקודות מדויקות ("עצור סוכן", "קלוד:", כפתורים) נשארות כמו שהיו. קבוצות, חברים, בת הזוג ואנשי קשר לא נכנסים ללולאה.
**כיבוי:** `SSA_BRAIN=0` ב-`.env` ואז הפעלה מחדש של ה-worker: חוזר המסלול הקודם. כשל, טיימאאוט, נעילת תקציב או תקרה יומית חוזרים אליו לבד.
**מגבלות:** 7 סיבובים ו-8 כלים להודעה, `SSA_BRAIN_MAX_COST` (0.10$), `SSA_BRAIN_DAY_CAP` (1$ ליום), `SSA_BRAIN_TIMEOUT` (70 שניות).
**לוג:** `data/mind_log.jsonl`, מטא-דאטה בלבד (כלים, עלות, זמן, מה נבלם).

### 🧪 סט שיחות לרגרסיה (`tests/conversation_evals.py`)
מקרים אמיתיים (מאונונמים) מהלוג החי, מקובצים לפי סוג התקלה: טענת ביצוע אחרי שהשער סירב, טקסט משימה לקלוד שנבנה מההיסטוריה, "כן"/ספרה בודדים, "כן בוצע" לשאלה של הבוט, המשך של בת הזוג, התראה מוכרת (`agent/known_alerts.py`), מייל, טקסט מועבר, תקרת עלות. Gemini לא רץ — מה שהמודל "החליט" כתוב במקרה.
**ריצה:** `PYTHONIOENCODING=utf-8 python -m tests.conversation_evals` (לוח תוצאות לפי סוג). השער בפריסה מריץ את כל `test_conv_*` דרך `tests/test_safety_critical.py`.
**מקרה חדש:** להעתיק שורה מ-`data/agent_conversations.jsonl`, להוריד מספרים/מזהים/כתובות, לכתוב ב-`live` מה הבוט עשה. מקרה שהקוד עוד לא עומד בו נכנס עם `gap='…'`: מדווח, לא מפיל את השער.

### 📊 דוח שבועי על הבוט (`scripts/weekly_bot_report.py`)
הודעה קצרה למשתמש פעם בשבוע: הוצאות API (מ-`data/usage.jsonl`, וחלק הבוט), מספר פניות, כמה פעולות השער עצר, תקלות, משימות קלוד (רצו/נפרסו/נדחו) ומה נפרס (`git log` על כל `deploy A -> B` ב-`logs/deploy.log*`). בלי תוכן לימודי, בלי LLM. פעם אחת לשבוע ISO (`data/weekly_bot_report_sent.json`).
**בדיקה:** `venv/bin/python3 scripts/weekly_bot_report.py --dry` (מדפיס, לא שולח, לא מסמן). `--force` שולח שוב באותו שבוע.
17/11: אותה ריצה שולחת קודם הודעה נפרדת — **"🗓️ השבוע שלך"** (`agent/weekly_personal.py`: מה נסגר, מה נדחה, 7 הימים הבאים, רצפים; מהקבצים בלבד, פעם בשבוע ISO, `data/weekly_personal_sent.json`, לא בשעות שקט). `--personal-dry` מדפיס אותה בלבד. הדוח עצמו מראה גם 👍/👎 (`data/feedback_ratings.jsonl`) ואת **הקצב של המשתמש** (`agent/rhythm.py` → `data/rhythm.json`; הטבלה המלאה: `venv/bin/python3 -m agent.rhythm`).
**הפעלה (crontab -e על ה-VM, ראשון 09:00 שעון ישראל בחורף / 10:00 בקיץ):**
```
0 7 * * 0 TZ=Asia/Jerusalem /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Weekly Bot Report" bash -c "cd /home/ubuntu/smart_study_agent && PYTHONIOENCODING=utf-8 venv/bin/python3 scripts/weekly_bot_report.py" >> /home/ubuntu/smart_study_agent/logs/weekly_bot_report.log 2>&1
```
ה-git של הפריסות: `~/smart_study_agent.git` (או `SSA_GIT_DIR` ב-.env).

### 👩‍💼 המוח השני ב-Obsidian (`scripts/vault_assistant.py`, `vault_sync/`)
כל מה שהעוזר יודע ועושה → `Smart Study Agent/👩‍💼 העוזר/` (אנשים, מסמכים, פתוח עכשיו, השבוע, נסגרו, חובות לפי קורס, יומן יומי, דברים שלמדתי, השתקות, הבוט). בלי מודל, בלי רשת מלבד rclone: $0. הערה נכתבת רק כשהטקסט שלה השתנה.
- **✍️ שלי** (ההערות שהמשתמש כותב ב-PC): `vault_pull.ps1` מעלה רק את התיקייה הזו ל-`gdrive:smart-study-agent/vault_inbox` (+ `.ssa_manifest.txt`), והמשיכה מדלגת עליה. ב-VM: `vault_sync/mine.py` מושך ל-`data/vault_inbox_staging` וממזג: אף פעם לא מוחק ולא דורס (גרסה ישנה → `.replaced/`, התנגשות → שני העותקים).
- שורה `- [ ] תזכיר לי …` / `לעוזר: …` בהערה שלו → שאלה בוואטסאפ עם ✅ (פעם אחת לשורה, בתוך תקציב השאלות). רק ✅ שלו הופך אותה להודעה שלו (`vi:` ב-core).
- `private: true` (היומן, הבוט, ✍️ שלי; `config/vault_privacy.json` לשינוי לפי תחום) → לא עולה ל-NotebookLM.
**בדיקה:** `venv/bin/python3 scripts/vault_assistant.py --dry --out /tmp/vault_dry` (כותב רק שם, לא שולח).
**הפעלה (crontab -e על ה-VM):**
```
40 8,23 * * * TZ=Asia/Jerusalem /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Vault Assistant" bash -c "cd /home/ubuntu/smart_study_agent && PYTHONIOENCODING=utf-8 venv/bin/python3 scripts/vault_assistant.py" >> /home/ubuntu/smart_study_agent/logs/vault_assistant.log 2>&1
```

### 🧠 שיפור עצמי (`agent/improve.py`, נקרא מ-`scripts/agent_daily_review.py`)
רץ על **מנוי Claude** בלבד (משימות `claude_job` כ-`claudeagent`, כולל המתנה למכסה). אין Gemini, אין מפתח API.
1. **בדיקה מוקדמת בלי מודל** (כל יום, ב-cron של "Agent Review"): תלונות של המשתמש ("לא הבנת", "שוב", "כבר אמרתי"), שאלה שחזרה, "אין לי אפשרות", תורות איטיות/יקרות, עצירות של השער, פריסות שנדחו, תקלות, התראות צנרת, עלות לפי פיצ'ר, משימות קלוד שנכשלו, שיחות Claude Code של המשתמש (`data/claude_sessions`), הרגלים לפי יום בשבוע.
2. **קצב** (`data/improve_state.json`): יום → אחרי 5 ריצות ריקות כל 3 ימים → שוב 5 → שבוע. תלונה / 👎 / עצירת שער / פריסה שנדחתה / 3+ תקלות / כשל אבטחה → חזרה ליום. יום בלי שום אות = ריצה ריקה בלי מודל.
3. **סוקר** (משימת קלוד שקטה, `kind=scout`): רק מנתח, כותב `.improve_findings.json` ב-ws. A = תיקון בקבצים שמותר לפרוס מהטלפון + מקרה ב-`tests/improve_cases.json` → עד 2 משימות תיקון (אחרונות בתור, **רק בלילה 23:00–07:30**, כדי שלא יעכבו "קלוד:" של המשתמש). B → `data/improvement_backlog.json`. C → שאלה אחת לדוח השבועי.
4. **אישור**: הודעה אחת לכל ריצה, רק כשיש מה לאשר: "N שיפורים מוכנים" [🚀 לפרוס הכול] [🔍 פרטים] [⏳]. הפריסה עוברת את אותו שער. `settings.auto_docs` (כבוי): תיקון שנוגע רק ב-docs / מקרי בדיקה נפרס לבד.
5. **אסור ללולאה לשנות** (`improve.DENY_RX`, נבדק גם ב-deploy): claude_job/improve/הסקירה, core, mind, whatsapp/, deploy/, תקציב, notify, אבטחה, config, requirements, קוד בדיקות. `tests/improve_cases.json` נפרס מהטלפון רק כהוספה (מחיקה/שינוי של מקרה → נדחה).
**וואטסאפ:** "קלוד, תבדוק את עצמך עכשיו" · "כל כמה זמן אתה משתפר?" · "קלוד, תפרוס לבד תיקוני תיעוד" / "קלוד, בלי פריסה לבד".
**בדיקה יבשה:** `PYTHONIOENCODING=utf-8 venv/bin/python3 scripts/agent_daily_review.py --dry-run` (מדפיס את האותות וההחלטה, לא שומר ולא מריץ).

### 🗂️ הבוט מכיר את הפרויקטים (`agent/project_qa.py`, 17/11)
המשתמש (רק הבעלים — כלי `project` ב-`agent/mind.py`, `owner_only`) שואל "איך עובד X", "איזה קרונים רצים", "מה השתנה השבוע", "מה העלות".
- **הריפו הזה:** אינדקס מקומי ל-HEAD (`data/project_index.json`, נבנה מחדש כשה-HEAD זז): docs, docstrings, config לא-סודי, deploy/, משתני סביבה, `git log`. בלי .env, secrets, tokens, data/, logs/; כל קטע עובר סינון סודות.
- **מצב חי:** `crontab -l`, `/tmp/cron_health.log`, `agent/weekly.cost_lines`, נעילת תקציב, `utils/pipeline_health`, `systemctl is-active`.
- **שאר הפרויקטים (אופציונלי):** `scripts/pc/projects_digest.py --push` במחשב (ידנית או ממשימה מתוזמנת; `SSA_SERVER` ב-.env) → `data/projects_digest.json` בשרת. פרויקט שמסומן ב-`SSA_SENSITIVE_RX`: רק תיעוד ומבנה. כינויים: `config/project_aliases.json`.
- **עלות:** קריאת Gemini flash אחת לשאלה, מטמון לפי commit (`data/project_qa_cache.json`). בדיקה: `python scripts/project_qa_eval.py` (≥90%), שאלה בודדת: `python -m agent.project_qa "איזה קרונים רצים?"`.
