"""
agent/escalate.py — "לתקשר אם אני לא עונה" (The owner 11/11: "שהבוט יהיה כמו מזכירה אישית… ולתקשר אם אני לא עונה").

Only for what the owner himself marked important — nothing is escalated on the bot's own judgement:
    • a reminder whose words say so ("…חשוב", "דחוף", "אסור לי לשכוח", "!!") or that he tapped 🚨 on
    • a "🚨 תעקוב שיצאתי" he tapped on a secretary note (a flight, an appointment → a leave-time reminder)
When such a reminder goes out and the owner doesn't react (any message from him, or a tap on it, is a reaction):
    +15 min   a second WhatsApp message: "🔔 שוב, כי סימנת שזה חשוב" · [✅ ראיתי] [⏰ עוד שעה]
    +35 min   a Google Calendar alarm: a 10-minute event in his own calendar starting a minute from now, with a popup
              at its start (rings on the phone even when WhatsApp is muted) and the calendar's own e-mail reminder.
              Free: the calendar.events scope the bot already has; no send scope, no new account. Deleted as soon as
              he answers (only events carrying our tag are ever deleted).
A deadline closer than that compresses both steps. Then it stops: no third channel without a decision of the owner's
(a phone call needs a paid account — that is the owner's call; the partner is never told anything from here).
Caps: ≤4 escalations a day; an item older than 12h is dropped. No LLM, nothing outward but the owner's own chat and calendar.
SSA_ESCALATE=0 turns it off.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'escalations.json'
NUDGE_AFTER = timedelta(minutes=15)
ALARM_AFTER = timedelta(minutes=35)
MIN_STEP = timedelta(minutes=3)
DAY_CAP = 4
STALE = timedelta(hours=12)
IMPORTANT_RX = re.compile(r'(?<![א-ת])(?:חשוב|דחוף|קריטי|אסור\s+(?:לי\s+)?(?:לשכוח|לפספס|לאחר)|בלי\s+לפספס|'
                          r'שלא\s+אפספס|שלא\s+אשכח|תוודא\s+ש)(?![א-ת])|!!')
NOT_IMPORTANT_RX = re.compile(r'(?<![א-ת])(?:לא\s+(?:כל\s+כך\s+|ממש\s+)?(?:חשוב|דחוף))(?![א-ת])')
NOTE = 'הבוט לא קיבל ממך תגובה בוואטסאפ על משהו שסימנת כחשוב. ענה לו שם (כל הודעה) וההתראה הזו תימחק מהיומן.'


def enabled() -> bool:
    return os.environ.get('SSA_ESCALATE', '1').strip() != '0'


def important(text: str) -> bool:
    """The owner's own words mark it: 'חשוב' / 'דחוף' / 'אסור לי לשכוח' / '!!' — not 'לא חשוב'."""
    t = text or ''
    return bool(IMPORTANT_RX.search(t)) and not NOT_IMPORTANT_RX.search(t)


def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d.get('items'), list):
        d['items'] = []
    if not isinstance(d.get('days'), dict):
        d['days'] = {}
    return d


def _save(d: dict, path: Path | None = None, now: datetime | None = None) -> None:
    now = now or datetime.now()
    d['items'] = [i for i in d['items'] if i.get('sent', '') >= (now - timedelta(days=3)).isoformat()]
    d['days'] = {k: v for k, v in d['days'].items() if k >= (now - timedelta(days=7)).date().isoformat()}
    p = path or STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def watch(chat: str, text: str, src: str, ref: str, now: datetime | None = None, deadline: datetime | None = None,
          path: Path | None = None) -> bool:
    """The important thing just went out to the owner: follow it up if he doesn't react. False = capped / off / already."""
    if not enabled() or not chat or not text:
        return False
    try:                                                        # "תפסיק עם המעקב" (agent/proactive.commands)
        from agent import proactive
        if proactive.is_muted('escalate'):
            return False
    except Exception:
        pass
    now = now or datetime.now()
    d = _load(path)
    day = now.date().isoformat()
    if d['days'].get(day, 0) >= DAY_CAP:
        print('  🔕 escalation: daily cap reached')
        return False
    for i in d['items']:
        if i['chat'] == chat and i['ref'] == ref and not i.get('acked') and not i.get('over'):
            return False                                        # a nag of the same reminder: the first clock runs on
    d['items'].append({'id': secrets.token_hex(3), 'chat': chat, 'text': text.strip()[:300], 'src': src, 'ref': ref,
                       'sent': now.isoformat(timespec='seconds'),
                       'deadline': deadline.isoformat(timespec='minutes') if deadline else '', 'steps': []})
    d['days'][day] = d['days'].get(day, 0) + 1
    _save(d, path, now)
    return True


def _times(i: dict) -> tuple[datetime, datetime]:
    sent = datetime.fromisoformat(i['sent'])
    nudge, alarm = sent + NUDGE_AFTER, sent + ALARM_AFTER
    if i.get('deadline'):
        dl = datetime.fromisoformat(i['deadline'])
        alarm = min(alarm, max(dl - timedelta(minutes=10), sent + 2 * MIN_STEP))
        nudge = min(nudge, max(sent + MIN_STEP, sent + (alarm - sent) / 2))
    return nudge, alarm


def _alarm(text: str, at: datetime) -> str:
    from integrations import gcal
    if not gcal.configured():
        return ''
    return gcal.insert_alarm(f'🔔 {text}', at, NOTE)


def _unalarm(eid: str) -> None:
    from integrations import gcal
    gcal.delete_alarm(eid)


def tick(now: datetime | None = None, path: Path | None = None, alarm=None) -> list[tuple[str, str, list]]:
    """[(chat, text, buttons)] — the WhatsApp nudges due now; the calendar alarms are placed here too."""
    if not enabled():
        return []
    now = now or datetime.now()
    d = _load(path)
    out, changed = [], False
    for i in d['items']:
        if i.get('acked') or i.get('over'):
            continue
        sent = datetime.fromisoformat(i['sent'])
        if now - sent > STALE:
            i['over'] = True
            changed = True
            continue
        if now < sent:
            continue
        if i.get('again'):                                      # "⏰ עוד שעה": the reminder itself, again
            i.pop('again')
            changed = True
            out.append((i['chat'], f"⏰ *תזכורת (חשוב):* {i['text']}", buttons(i)))
            continue
        nudge, at = _times(i)
        if 'nudge' not in i['steps'] and now >= nudge:
            i['steps'].append('nudge')
            changed = True
            out.append((i['chat'], f"🔔 *שוב, כי סימנת שזה חשוב:* {i['text']}\n"
                                   f"לא ראיתי תגובה. עוד {max(1, int((at - now).total_seconds() // 60))} דק׳ אקפיץ לך "
                                   f"התראה מהיומן בטלפון.", buttons(i)))
            continue
        if 'alarm' not in i['steps'] and now >= at:
            i['steps'].append('alarm')
            i['over'] = True
            changed = True
            try:
                i['cal'] = (alarm or _alarm)(i['text'], now + timedelta(minutes=1)) or ''
                print(f"  📲 escalation: calendar alarm for {i['ref']}")
            except Exception as e:
                print(f'  ⚠️ escalation alarm failed: {e!r}'[:200])
    if changed:
        _save(d, path, now)
    return out


def buttons(i: dict) -> list[tuple[str, str]]:
    return [('✅ ראיתי', f"esc:ack:{i['id']}"), ('⏰ עוד שעה', f"esc:hour:{i['id']}")]


def ack(chat: str, ref: str = '', path: Path | None = None, unalarm=None, now: datetime | None = None) -> int:
    """The owner reacted (a message, a tap). Everything open for him stops; a calendar alarm already placed is removed."""
    try:
        d = _load(path)
    except Exception:
        return 0
    hit = [i for i in d['items'] if i['chat'] == chat and not i.get('acked') and (not ref or i['ref'] == ref)
           and (not i.get('over') or i.get('cal'))]
    if not hit:
        return 0
    for i in hit:
        i['acked'] = (now or datetime.now()).isoformat(timespec='seconds')
        if i.get('cal'):
            try:
                (unalarm or _unalarm)(i.pop('cal'))
            except Exception as e:
                print(f'  ⚠️ escalation: alarm not removed: {e!r}'[:200])
    _save(d, path, now)
    return len(hit)


def open_items(chat: str, path: Path | None = None) -> list[dict]:
    return [i for i in _load(path)['items'] if i['chat'] == chat and not i.get('acked') and not i.get('over')]


def button(chat: str, op: str, eid: str, now: datetime | None = None, path: Path | None = None,
           unalarm=None) -> str:
    """esc:ack → done. esc:hour → the same thing again in an hour, followed up the same way."""
    now = now or datetime.now()
    d = _load(path)
    i = next((x for x in d['items'] if x['id'] == eid and x['chat'] == chat), None)
    if not i:
        return '👍'
    if op == 'hour':
        if i.get('cal'):
            try:
                (unalarm or _unalarm)(i.pop('cal'))
            except Exception as e:
                print(f'  ⚠️ escalation: alarm not removed: {e!r}'[:200])
        i.update(sent=(now + timedelta(hours=1)).isoformat(timespec='seconds'), steps=[], again=True)
        i.pop('acked', None)
        i.pop('over', None)
        _save(d, path, now)
        return '⏰ אזכיר שוב בעוד שעה — ואם לא תגיב, שוב אנדנד ואקפיץ מהיומן.'
    ack(chat, i['ref'], path, unalarm, now)
    return '✅ מעולה, הפסקתי לעקוב.'
