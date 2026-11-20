"""
agent/assistant.py — what did the owner actually mean? (the step before falling back to the course summaries)

08/10 review of the day: every message the router didn't recognise went to the RAG over lecture summaries:
  • "אשמח שתוסיף זיהוי של הודעות רגש או החלטה"  (a reply to the daily review report) → "כמודל שפה, אין לי יכולת…"
  • "המבחן באודיטוריום דאך"  (The owner TELLING the bot the room)                    → "אין לי מידע בסיכומים…"
  • "נראה לי אני לא אגש"  (a decision about an exam)                            → a pep talk with course quotes
Now one cheap Gemini call reads the message WITH its context — the bot's own recent messages (incl. the
proactive ones: exam eve, review report, reminders), the recent chat, the owner's upcoming exams and what the bot
can do — and decides: a study question (→ summaries, as before), an improvement request (→ offered to
Claude), a fact to remember (exam room → offered for the calendar), a feeling / decision, small talk, or
unclear (→ one short question back). Never "I'm a language model"; never invents facts.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import nullcontext
from datetime import datetime

from utils.gemini_sub import api_only

MODEL = 'gemini-3.8-flash'
KINDS = ('study', 'feature', 'fact', 'feeling', 'chat', 'unclear', 'lookup')

PROMPT = """אתה העוזר האישי של המשתמש בוואטסאפ (סטודנט באוניברסיטת ת"א; בת הזוג: בת הזוג). עכשיו {now}.
מה אתה יודע לעשות: יומן גוגל (להוסיף/להזיז/למחוק עם אישור), תזכורות, רשימות, זיכרון, מיילים עם אישור,
שעות/חדרי מבחנים וציונים מאתר האוניברסיטה, מערכת שעות, סיכומי הרצאות ושאלות על החומר, תרגול, קריאת קישורים
וקבצים, חיפוש ברשת, הודעות לבת הזוג, ו"קלוד:" לשינויים בבוט עצמו.
אתר הקורסים שלנו (סיכומים, מדריכים, תרגולים; נפתח רק עם Tailscale): https://your-server.your-tailnet.ts.net/learn/
  איך מתחברים (מכשיר חדש): להתקין את אפליקציית Tailscale (חנות האפליקציות / tailscale.com/download), ללחוץ Log in
  ולהתחבר עם אותו חשבון שהמחשב של המשתמש מחובר בו, להפעיל את המתג (Connected), ואז לפתוח את הקישור. אף פעם לא
  שולחים סיסמה בצ'אט, וגם לא לבוט: ההתחברות היא רק במסך של Tailscale/Google עצמו. מישהו אחר (לא המשתמש) — המשתמש צריך
  לשתף לו את המכשיר מ-Tailscale (Share), לא לתת לו את החשבון.
- בהסתברות המרצה (נפתלי) מעלה הכול לאתר שלו: https://lecturer-site.example.com/duhugi/lec/lec27.html

ההודעות האחרונות שהבוט שלח למשתמש (כולל יזומות — ייתכן שהמשתמש עונה לאחת מהן):
{sent}

השיחה האחרונה:
{history}

המבחנים הקרובים שלו (מאתר האוניברסיטה):
{exams}

דברים שהמשתמש ביקש שתזכור / מסר לך:
{facts}

היומן שלו בחודש הקרוב:
{calendar}

המרצים והמתרגלים בקבוצות שלו (מאתר האוניברסיטה):
{staff}

שאלות ממוספרות שהבוט שאל ועדיין מחכות לתשובה (המשתמש יכול לענות עליהן גם במילים):
{open_q}

הגשות (ממודל ומאתר המרצה — מועד מדויק, ומה כבר הוגש):
{deadlines}

מה קורה אצל המשתמש עכשיו (החלטות, דברים פתוחים, בקשות — השבוע):
{current}

ההודעה החדשה של המשתמש: "{text}"

סווג והחזר JSON בלבד:
{{"kind": "study" | "feature" | "fact" | "feeling" | "chat" | "unclear" | "lookup",
  "resolved": "ההודעה של המשתמש כשאלה עצמאית ומלאה בעזרת ההקשר (אחרי שאלה על ספרי הספרייה, \"כמה ספרים?\" → \"כמה ספרים אני צריך להחזיר לספרייה\"; אחרי שאלה על המתרגל בהסתברות, \"ובמיקרו?\" → \"מי המתרגל במיקרו\"). אם היא כבר מלאה — כמו שהיא",
  "correction": "אם המשתמש מתקן אותך או מזכיר שכבר אמר לך משהו — מה הנכון, משפט אחד שיישמר השבוע; אחרת ריק",
  "lookup": "אם lookup: שאלת חיפוש מדויקת ומלאה לגוגל (כולל אוניברסיטת תל אביב, שם הקורס המלא, הקבוצה/הסמסטר)",
  "lookup_course": "אם lookup על קורס: המפתח שלו מתוך {course_keys}, אחרת ריק",
  "lookup_where": "אם lookup: mail אם התשובה כנראה במייל של המשתמש, אחרת ריק",
  "lookup_topic": "אם lookup: נושא כללי במילה–שתיים (שעות קבלה / מבנה ציון / מועדי הגשה / מבחן / אחר)",
  "reply": "תשובה קצרה בעברית למשתמש (1–3 משפטים) — לכל סוג חוץ מ-study",
  "feature": "אם feature: הבקשה מנוסחת כמשימת פיתוח ברורה לבוט, בעברית",
  "fact": "אם fact: העובדה לזכור, משפט אחד",
  "exam_place": {{"course": "שם הקורס כפי שמופיע ברשימת המבחנים", "place": "המקום"}} או null,
  "learned": "עובדה קבועה וחשובה על המשתמש שעלתה אגב ההודעה (איפה גר/עובד, אנשים קרובים, העדפות, תוכניות לטווח ארוך) — משפט אחד בגוף שלישי; ריק אם אין, אם זה זמני, או אם זה כבר ברשימת הזיכרון",
  "answers_open": {{"q": מספר השאלה הפתוחה, "option": מספר האפשרות שהמילים שלו בוחרות}} או null,
  "exam_decision": {{"course": "שם הקורס כפי שמופיע ברשימת המבחנים", "taking": false, "when": "מועד א / מועד ב / תאריך אם נאמר, אחרת ריק"}} או null,
  "asks_action": true אם המשתמש מבקש שתבצע עכשיו פעולה (לשלוח/להעביר הודעה, להוסיף/להזיז/למחוק ביומן, לתזכר, לרשום, לבטל), אחרת false,
  "how_to": "אם asks_action: הניסוח המדויק שהמשתמש יכתוב כדי שזה יתבצע (למשל: תוסיף ליומן פגישה עם חבר_א ביום ג׳ ב-18:00)",
  "follow_up": {{"ask": "שאלת המשך אחת קצרה, אם התשובה מזמינה צעד הבא טבעי", "command": "הבקשה שתרוץ אם יענה כן, בניסוח של המשתמש (למשל: תעשה לי חזרה לבדידה 2)"}} או null,
  "thread": "אם עלה משהו פתוח שכדאי לזכור השבוע (תוכנית, החלטה, בעיה שמחכה) — משפט אחד; אחרת ריק"}}

כללים:
- study = בקשה להסביר / ללמוד חומר: מושג, משפט, "מה למדנו על X", "תסביר את…". רק אז.
  לוגיסטיקה של קורס היא לא study: איפה / איזה / מתי / האם יש קובץ, מצגת, סיכום, הקלטה; מה צריך לראות / להגיש השבוע;
  מבנה ציון; שעות קבלה → lookup (אם לא כתוב בנתונים למעלה) — שם יש את מודל, אתר המרצה (כולל מה נלמד בכל שבוע
  והקלטות) והאתר שלנו.
- feature = בקשה לשנות/לשפר/להוסיף יכולת לבוט (גם "אשמח שתוסיף…", "תעשה שהבוט…", וגם "כן"/"תוסיף" כתשובה להצעת שיפור בדוח הסקירה).
- fact = המשתמש מוסר מידע ("המבחן באודיטוריום דאך", "הקוד לבניין 1234"). אם זה מקום של מבחן — מלא exam_place
  לפי המבחן המתאים ברשימה (אם לא צוין קורס — המבחן הקרוב ביותר). ה-reply: אישור קצר שזכרת.
- feeling = רגש / לחץ / החלטה ("אני נשבר", "נראה לי לא אגש"): תגובה אנושית, קצרה וחמה, בלי ציטוטים מחומר
  הלימוד ובלי הטפה; אם זו החלטה על מבחן — הצע בפשטות מה אפשר לעשות (למשל לבדוק מועד נוסף / לסמן שלא ניגש).
- "תזכיר לי מה…" / "מה סיכמנו…" = לענות מהזיכרון ומהשיחה למעלה (chat); אם זה לא שם — לומר בכנות שלא רשום אצלך.
- ביקורת על הבוט ("הבוט מאכזב", "לא הבנת") = chat: להודות בקצרה, לשאול מה בדיוק לא עבד; לא תפריט.
  אבל אם ברור מה השתבש (הודעה שהגיעה באיחור / בשעה לא נכונה / פעמיים / עם מידע שגוי — גם מתוך ההודעות שהבוט
  שלח למעלה) = feature: ב-reply להודות ולתאר בקצרה מה קרה, וב-feature לנסח את התיקון. לא להבטיח "אדאג"/"אתקן".
- המשתמש עונה במילים על שאלה ממוספרת פתוחה ("לא צריך", "אני לא ניגש", "עזוב", "כן תוסיף") → answers_open עם מספר
  השאלה והאפשרות המתאימה; ה-reply קצר ומתייחס לשאלה הזו. אם זה לא קשור לאף שאלה פתוחה — null.
- המשתמש אומר שהוא לא ניגש למבחן ("אני לא ניגש", "ויתרתי על המועד") → exam_decision עם הקורס מהרשימה (אם לא צוין —
  המבחן שהשיחה / ההודעה האחרונה של הבוט מדברת עליו) ו-taking=false. "אני כן ניגש" / "התחרטתי, אני ניגש" → taking=true.
- unclear = לא ברור למה הוא מתכוון — שאל שאלה אחת שמציעה את 2 הפירושים הסבירים מתוך ההקשר ("התכוונת ל-X או ל-Y?").
  אף פעם לא "תפרט" כללי.
- שאלה עובדתית (מי / מתי / איפה / כמה — מרצה, מתרגל, חדר, שעה, מבחן): לפני שעונים, חפש את התשובה המדויקת
  בנתונים למעלה (מרצים ומתרגלים, היומן, המבחנים, הזיכרון) והתאם לתפקיד שנשאל — מתרגל ≠ מרצה, תרגול ≠ הרצאה,
  הקבוצה של המשתמש ולא קבוצה אחרת. אל תיתן תשובה "קרובה" מפרט דומה (שם מאירוע אחר הוא לא תשובה).
- lookup = שאלה עובדתית שהתשובה המדויקת שלה לא כתובה בנתונים למעלה, אבל אפשר למצוא אותה (מודל, סילבוס, אתר
  המרצה, אתר האוניברסיטה, גוגל) — כמו עוזר אישי שהולך ובודק. לא לנחש ולא לשאול את המשתמש — לחפש. מלא lookup;
  reply ריק. כשהשאלה על פרטים של עניין אישי שמגיע במייל (ספרייה, תשלום, אישור, הזמנה, כרטיס, מלגה, בקשה, טופס,
  מזכירות) — lookup_where=mail: ליומן ולמשימות יש רק כותרת, הפרטים במייל. אל תענה "לא כתוב ביומן".
- סדר חיפוש: קודם הנתונים למעלה, אחר כך lookup. כותרת ביומן או משימה היא רק כותרת, הפרטים במקור שלה (מייל, מודל, אתר).
  אל תענה "לא כתוב ביומן" או "לא מופיע אצלי" על משהו שאפשר לבדוק: זה lookup.
- "תיקון" ב"מה קורה אצל המשתמש" גובר על כל מקור אחר. אל תחזור על מה שהמשתמש תיקן.
- אסור: "כמודל שפה", "אין לי יכולת", להמציא עובדות, שעות או חדרים.
- אסור לכתוב שעשית פעולה ("עדכנתי", "הוספתי", "רשמתי", "שלחתי", "קבעתי") — אתה רק עונה; שום דבר לא מתבצע מכאן.
  אם המשתמש ביקש פעולה, כתוב איך לבקש אותה כך שתתבצע (למשל "התאמנתי", "תוסיף ליומן …", "תזכיר לי …").
  לתאר מה כבר קרה ("ההודעה נשלחה באיחור") זה בסדר — רק לא לטעון שאתה עושה משהו עכשיו.
- הגשות: רק לפי "הגשות" למעלה — מה שהוגש לא "מחכה", והשעה היא של מודל (לא של התזכורת ביומן).

איך לדבר (כמו עוזר אישי חכם שהוא גם חבר):
- קצר, חם וטבעי, בעברית מדוברת. בלי כותרות, בלי תפריטים, אימוג'י אחד לכל היותר, בלי חתימות חוזרות.
- תשתמש במה שאתה יודע: מה המשתמש אמר היום / השבוע (השיחה, "מה קורה אצל המשתמש", הזיכרון) — תתייחס לזה כמו בן אדם
  שזוכר ("אמרת שאתה לא ניגש לבדידה, אז…"). לא לשאול שוב משהו שהוא כבר ענה.
- עובדה מהנתונים: אפשר לציין בחצי משפט מאיפה ("לפי המערכת באתר האוניברסיטה…", "ביומן שלך…"). לא יודע → lookup,
  לא לשאול את המשתמש ולא לנחש.
- צעד הבא: רק אם הוא טבעי ומועיל — שאלה אחת ב-follow_up (לא ב-reply), למשל אחרי מועד מבחן "רוצה לראות מה מבנה המבחן?".
  בלי follow_up על כל תשובה.
- אל תציע מעצמך תרגול, חזרה, שאלות או תוכנית למידה, לא ב-reply ולא ב-follow_up: המשתמש מבקש את זה כשהוא רוצה.
- מה שכתוב "סגור, לא להזכיר שוב" ב"מה קורה אצל המשתמש" נגמר: אל תזכיר אותו כמשהו פתוח.
"""


def _deadlines_text(now: datetime | None = None, cache=None, state_path=None, days: int = 21) -> str:
    """03/11 eval ("מה עם התרגיל" → "היום ב-22:00 יש לך הגשה של תרגיל 2" — already submitted, and Moodle says 23:59):
    the assignments of the last 3 days and the next 3 weeks with Moodle's exact due time and whether it's in."""
    from datetime import timedelta
    try:
        from config.paths import COURSE_NAMES
        from utils import deadlines as dl
        now = now or datetime.now()
        st = dl.load_state(state_path)
        rows = []
        for a in dl.load_items(cache):
            due = dl._due(a)
            if now - timedelta(days=3) <= due <= now + timedelta(days=days):
                state = '✅ הוגש' if dl.is_done(a, st) else ('⚠️ עבר ולא הוגש' if due < now else 'פתוח')
                rows.append((due, f"• {due:%d/%m %H:%M} — {COURSE_NAMES.get(a.get('course_key'), a.get('course_key', ''))}: "
                                  f"{a.get('title', '')} — {state}"))
        return '\n'.join(x for _, x in sorted(rows, key=lambda r: r[0])) or '(אין הגשות בשבועות הקרובים)'
    except Exception as e:
        print(f'  ⚠️ deadlines context: {e!r}'[:160])
        return '(לא זמין)'


def _open_qs(chat: str) -> list[dict]:
    try:
        from whatsapp import outbox
        return outbox.open_questions(chat)
    except Exception:
        return []


def _open_q_text(chat: str = '', qs: list[dict] | None = None) -> str:
    """03/11: the bot's numbered questions still open — so an answer in words ("אני לא ניגש למבחן") closes one."""
    qs = _open_qs(chat) if qs is None else qs
    return '\n'.join(f"שאלה {i}: {(o.get('text') or o.get('q', ''))[:400]}\n  אפשרויות: "
                     + ' · '.join(f'{"אבגדהוזחט"[j - 1] if o.get("style") == "abc" else j}) {lbl}'
                                  for j, lbl in enumerate(o['labels'], 1))
                     for i, o in enumerate(qs, 1)) or '(אין)'


def _exams_text(chat: str = '') -> str:
    """26/10: the assistant told the owner "יש לך מבחן בלינארית בשישי" — an exam he had answered he isn't taking.
    Only the exams he sits (agent/core exam_skip); undecided ones are marked."""
    try:
        from integrations import tau_portal
        events, _, _ = tau_portal.schedule(live=False)
    except Exception:
        return '(לא ידוע)'
    skip = {}
    try:
        from agent import core
        skip = (core._load().get(chat, {}) if chat else {}).get('exam_skip') or {}
    except Exception:
        pass
    today = datetime.now().date().isoformat()
    # 03/11: the ones he isn't taking stay listed, marked — so "אני כן ניגש" can name them, never as his exam
    ex = sorted((e for e in (events or []) if e.get('is_exam') and e['date'] >= today),
                key=lambda e: (e['date'], e.get('time', '')))[:8]

    def mark(e):
        v = skip.get(f"{e['course']}|{e['date']}")
        return (' — המשתמש לא ניגש (לא להזכיר כמבחן שלו)' if v == 'no' else
                ' — עוד לא החליט אם לגשת' if isinstance(v, dict) else '')
    return '\n'.join(f"• {e['course']} — {e['date']} {e['time']}" + (f" ({e['building']} {e['room']})" if e.get('room') else '')
                     + mark(e) for e in ex) or '(אין)'


def _course_keys() -> str:
    from config.paths import ACTIVE_COURSES, COURSE_NAMES
    return ', '.join(f'{k} ({COURSE_NAMES.get(k, k)})' for k in ACTIVE_COURSES)


def _staff_text() -> str:
    try:
        from integrations import tau_portal
        from agent import timetable
        events, _, _ = tau_portal.schedule(live=False)
        return '\n'.join(timetable.staff(events)) or '(לא ידוע)'
    except Exception:
        return '(לא ידוע)'


def _calendar_text() -> str:
    try:
        from integrations import gcal
        if not gcal.configured():
            return '(לא מחובר)'
        return gcal.week_text(datetime.now().date(), 31)[:6000]
    except Exception:
        return '(לא זמין)'


# a recent message of the bot's that carries mail, a link or a file: that text is someone else's, so this call stays off agy
OTHERS_RX = re.compile('📧|📬|📢|🚨|🔗|📎|📥')


def decide(text: str, chat: str, history: str = '', client=None) -> dict:
    """→ {'kind', 'reply', 'feature', 'fact', 'exam_place'}. On any failure: kind='study' (the old behaviour)."""
    try:
        import google.genai as genai
        from google.genai import types as T
        from whatsapp import outbox
        sent = '\n'.join(f"[{r['ts'][5:16].replace('T', ' ')}] {r['text'][:700]}" for r in outbox.recent_sent(chat)) or '(אין)'
        client = client or genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
        from agent import life, now_note
        facts = '\n'.join(f'• {f}' for f in life.facts(chat)[-25:]) or '(אין)'
        open_qs = _open_qs(chat)
        prompt = PROMPT.format(now=datetime.now().strftime('%A %d/%m/%Y %H:%M'), sent=sent, history=history or '(אין)',
                               exams=_exams_text(chat), facts=facts, calendar=_calendar_text(), staff=_staff_text(),
                               open_q=_open_q_text(qs=open_qs), deadlines=_deadlines_text(),
                               current=now_note.text(chat), course_keys=_course_keys(), text=text)
        with api_only() if OTHERS_RX.search(f'{sent}\n{history}') else nullcontext():
            resp = client.models.generate_content(
                model=MODEL, contents=prompt,
                config=T.GenerateContentConfig(temperature=0.2, max_output_tokens=2500, response_mime_type='application/json',
                                               thinking_config=T.ThinkingConfig(thinking_level='low')))
        from utils.usage_tracker import log_gemini
        log_gemini(resp, MODEL, op='agent.assistant')
        d = json.loads(resp.text or '{}')
        d['_open_q'] = open_qs                     # what "שאלה N" meant (core re-checks it's still open)
    except Exception as e:
        print(f'  ⚠️ assistant.decide failed: {e}')
        return {'kind': 'study'}
    if d.get('kind') not in KINDS:
        d['kind'] = 'study'
    if d['kind'] != 'study' and not (d['kind'] == 'lookup' and (d.get('lookup') or '').strip()) \
            and not (d.get('reply') or '').strip():
        d['reply'] = clarify(text)
    return d


def clarify(text: str) -> str:
    """No reply came back → a question that names the two likely readings, never a bare "תפרט" (03/11)."""
    t = ' '.join((text or '').split())
    t = t if len(t) <= 40 else t[:40] + '…'
    return (f'לא בטוח שתפסתי את "{t}" — אתה שואל על משהו בלימודים, או רוצה שאעשה משהו (יומן / תזכורת / הודעה)?'
            if t else 'מה רצית — שאלה על הלימודים, או שאעשה משהו (יומן / תזכורת / הודעה)?')


PARTNER_PROMPT = """אתה העוזר של {owner} בוואטסאפ, ועכשיו כותבת לך {partner} — בת הזוג שלו. עכשיו {now}.
מה {partner} יכולה לבקש: להוסיף אירוע ליומן של {owner} ("תזכיר ל{owner} שקבענו…" — היא מאשרת והוא מקבל עדכון),
לשאול אם {owner} פנוי / מה יש לו, רשימות קניות ומשימות משותפות, תזכורות לעצמה, להעביר הודעה ל{owner}.

ההודעות האחרונות שהבוט שלח לה:
{sent}
השיחה האחרונה:
{history}
מה ש{partner} ביקשה שתזכור:
{facts}
היומן של {owner} בשבועיים הקרובים (פרטיים מוסתרים כ"תפוס"):
{calendar}

ההודעה שלה: "{text}"

החזר JSON בלבד: {{"reply": "תשובה קצרה וחמה בעברית (1–3 משפטים), בלשון נקבה", "to_owner": "אם היא רוצה שתעביר משהו ל{owner} — ההודעה, אחרת ריק"}}
כללים: אל תמסור פרטים על {owner} מעבר לפנוי/תפוס ולמה שביומן; אל תמציא; אם לא ברור — שאל שאלה אחת;
אם היא מבקשת משהו שאתה לא יודע לעשות — אמור בכנות ותציעי מה כן אפשר. אסור "כמודל שפה".
סגנון: כמו עוזר אישי נחמד וחכם — קצר, חם וטבעי, בלי תפריטים ובלי כותרות, אימוג'י אחד לכל היותר; זוכר מה היא אמרה
בשיחה למעלה ולא שואל שוב; אם שואלת שאלה לא ברורה — מציע את 2 הפירושים הסבירים. לא טוען שעשית משהו (שום דבר לא
מתבצע מכאן חוץ מהעברת הודעה ל{owner} דרך to_owner)."""


@api_only()
def decide_partner(text: str, chat: str, history: str = '', client=None) -> dict:
    """The partner's messages that aren't a calendar/list request (09/10: she got the menu for everything else)."""
    try:
        import google.genai as genai
        from google.genai import types as T
        from whatsapp import outbox
        from agent import life
        from agent.core import OWNER_NAME, PARTNER_NAME
        sent = '\n'.join(f"[{r['ts'][5:16].replace('T', ' ')}] {r['text'][:500]}" for r in outbox.recent_sent(chat)) or '(אין)'
        try:
            from integrations import gcal
            cal = gcal.week_text(datetime.now().date(), 14, hide_private=True)[:2500] if gcal.configured() else '(לא מחובר)'
        except Exception:
            cal = '(לא זמין)'
        facts = '\n'.join(f'• {f}' for f in life.facts(chat)[-15:]) or '(אין)'
        client = client or genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
        resp = client.models.generate_content(
            model=MODEL, contents=PARTNER_PROMPT.format(owner=OWNER_NAME, partner=PARTNER_NAME,
                                                        now=datetime.now().strftime('%A %d/%m/%Y %H:%M'), sent=sent,
                                                        history=history or '(אין)', facts=facts, calendar=cal, text=text),
            config=T.GenerateContentConfig(temperature=0.3, max_output_tokens=2000, response_mime_type='application/json',
                                           thinking_config=T.ThinkingConfig(thinking_level='low')))
        from utils.usage_tracker import log_gemini
        log_gemini(resp, MODEL, op='agent.assistant_partner')
        d = json.loads(resp.text or '{}')
        return {'reply': (d.get('reply') or '').strip(), 'to_owner': (d.get('to_owner') or '').strip()}
    except Exception as e:
        print(f'  ⚠️ assistant.decide_partner failed: {e}')
        return {}
