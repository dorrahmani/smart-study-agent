"""
whatsapp/worker.py — turns spooled WhatsApp messages into agent replies. NOT internet-facing.

Loop: data/wa_spool/*.json (written by whatsapp/receiver.py) → In(...) → agent.core.handle
→ send each Out through whatsapp/client → move the file to done/. A failed message is moved
to failed/ (never retried forever, never lost). Every inbound message also refreshes the
24h window that utils/notify uses for free proactive messages.

    venv/bin/python3 -m whatsapp.worker          (systemd: deploy/smart-study-wa-worker.service)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_REASK: set[str] = set()                          # chats with another open numbered question after an answer
_ANSWERED: set[str] = set()                       # 19/11 chats that just answered a question: what waited goes out now


def report_job(job: dict) -> None:
    """A finished "קלוד:" job. Its ✅ waits while another numbered question is open: a bare digit answers the latest one,
    and a job's deploy is the last question that should be answered by mistake (release_deferred sends it after)."""
    from agent import claude_job
    from whatsapp import outbox
    text, buttons = claude_job.result_message(job)
    if not text:                                    # the quiet daily review with nothing to deploy
        return
    if not buttons:
        outbox.send_text(job['chat'], text)
    elif outbox.options_open(job['chat']):
        outbox.defer_options(job['chat'], text, buttons)
    else:
        outbox.send_options(job['chat'], text, buttons)


def trusted_role(m: dict) -> str:
    """The role a spool file claims is only what someone wrote into a folder (audit A7, and a webhook body can say
    "bridge" too). A role that opens anything has to match allowed.json, the way the bridge looked it up."""
    from whatsapp import bridge
    claimed = m.get('role') or 'contact'                  # 03/11 no role = the least trusted, never the owner
    if claimed not in ('owner', 'partner', 'friend'):
        return claimed if claimed in ('contact', 'group_member') else 'contact'
    chat, who = str(m.get('chat') or ''), str(m.get('from') or '')
    ids = {i for i in (who, f'{who}@lid', f'{who}@s.whatsapp.net') if who}
    if not m.get('group') and chat:                       # in a group the chat id says nothing about the person
        ids |= {chat, chat.split('@')[0], bridge.digits(chat.split('@')[0])}
    real = bridge.role_of(ids, bridge.load_roles())
    if real == claimed:
        return claimed
    print(f'  ⚠️ a spool file claimed {claimed}, allowed.json says {real}: treated as a contact')
    return 'contact'


def safe_media(path) -> Path | None:
    """Only what the bridge saved. Any other path in a spool file would have the bot read and quote a file of ours."""
    from whatsapp import bridge
    if not path:
        return None
    try:
        p, root = Path(path).resolve(), bridge.MEDIA.resolve()
    except (OSError, ValueError, TypeError):
        return None
    if p.is_relative_to(root) and p != root:
        return p
    print('  ⚠️ a spool file pointed outside wa_media: ignored')
    return None


def _no_dump() -> None:
    """This process holds every key: nothing of the same user may ptrace it, read its environment or dump it."""
    if not sys.platform.startswith('linux'):
        return
    try:
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:      # PR_SET_DUMPABLE
            print('  ⚠️ PR_SET_DUMPABLE refused')
    except Exception as e:
        print(f'  ⚠️ PR_SET_DUMPABLE: {e!r}'[:200])


def to_in_bridge(m: dict) -> 'In':
    """Linked-device bridge message (whatsapp/bridge.py) → agent In. A bare digit answering
    numbered options becomes the button it stands for."""
    from agent.core import In
    from whatsapp import outbox
    chat, kind, role = m.get('chat', ''), m.get('type', 'text'), trusted_role(m)
    media = safe_media(m.get('media'))
    if kind == 'text':
        meta: dict = {}
        # 05/11: a group confirms nothing, from anyone: no digit there answers a question
        # 19/11 a reply that quotes a question answers that question (reply_to = the quoted WhatsApp id)
        payload = None if m.get('group') else outbox.take_option(chat, m.get('text', ''), meta=meta,
                                                                 reply_to=m.get('reply_to') or None)
        if not payload and (m.get('text') or '').strip().strip('.').isdigit():
            try:                                   # 08/10: a "1" went unanswered — record why, for the next review
                allo = json.loads(outbox.OPTIONS.read_text(encoding='utf-8'))
                lst = outbox._open_list(allo, chat)
                o = lst[-1] if lst else {}
                print(f"  ⚠️ digit without options: chat={chat[-8:]} has_opts={bool(o)} "
                      f"age_min={int((time.time() - o.get('ts', 0)) / 60) if o else '-'} chats={len(allo)}")
            except (OSError, ValueError) as e:
                print(f'  ⚠️ digit without options: no options file ({e})')
        if not payload and role == 'owner' and not m.get('group'):
            try:                                   # a written reply also counts as "not ignored" (the budget)
                outbox.note_answered(chat)
                outbox.mark_replied(chat)          # 05/11: and whatever was open doesn't come back by itself
            except Exception:
                pass
        if payload:
            if meta.get('more'):                          # another question is still open → ask it again after this
                _REASK.add(chat)
            if not payload.startswith('oq:'):
                _ANSWERED.add(chat)
            # an answer to an older question → the reply names it (In.text carries it to the agent)
            return In(chat, 'button', payload=payload, channel='whatsapp', role=role,
                      text=meta.get('q', '') if meta.get('stale') else '', group=bool(m.get('group')))
        return In(chat, 'text', text=m.get('text', ''), reply_to=m.get('reply_to') or None, channel='whatsapp',
                  role=role, forwarded=bool(m.get('forwarded')), group=bool(m.get('group')),
                  context=m.get('context') or '')
    if kind == 'contact':
        return In(chat, 'contact', text=m.get('text', ''), channel='whatsapp', role=role, key=m.get('key', ''),
                  group=bool(m.get('group')))
    if role == 'contact':                                 # their texts (and media, as text-less) — keyed by phone
        return In(chat, kind if kind in ('text',) else 'other', text=m.get('text', '') if kind == 'text' else '',
                  channel='whatsapp', role=role, key=m.get('key', ''))
    if kind in ('image', 'voice', 'document'):
        return In(chat, kind, text=m.get('text', ''), media=media, reply_to=m.get('reply_to') or None,
                  forwarded=bool(m.get('forwarded')),        # a forwarded picture, voice note or file is someone else's words
                  channel='whatsapp', role=role, group=bool(m.get('group')), context=m.get('context') or '')
    if kind == 'unsupported' and not m.get('group'):    # 15/11 a video / location / poll: one line, never the menu
        return In(chat, 'unsupported', text=str(m.get('text') or '')[:20], channel='whatsapp', role=role)
    return In(chat, 'other', text='', channel='whatsapp', role=role, group=bool(m.get('group')))


def to_in(m: dict) -> 'In':
    """Meta message object → agent In (downloads voice/image media)."""
    if m.get('bridge'):
        return to_in_bridge(m)
    from agent.core import In
    from whatsapp import client as wa
    sender = str(m.get('from', ''))
    reply_to = (m.get('context') or {}).get('id')
    t = m.get('type')
    if t == 'text':
        return In(sender, 'text', text=(m.get('text') or {}).get('body', ''), reply_to=reply_to)
    if t == 'interactive':
        it = m.get('interactive') or {}
        rep = it.get('button_reply') or it.get('list_reply') or {}
        return In(sender, 'button', payload=rep.get('id', ''), text=rep.get('title', ''), reply_to=reply_to)
    if t == 'button':                                            # quick-reply of a template
        return In(sender, 'button', payload=(m.get('button') or {}).get('payload', ''), reply_to=reply_to)
    if t in ('audio', 'voice'):
        media = wa.download_media((m.get(t) or {}).get('id', ''), '.ogg')
        return In(sender, 'voice', media=media, reply_to=reply_to)
    if t == 'image':
        img = m.get('image') or {}
        media = wa.download_media(img.get('id', ''), '.jpg')
        return In(sender, 'image', media=media, text=img.get('caption', ''), reply_to=reply_to)
    return In(sender, 'text', text='', reply_to=reply_to)


def send_outs_bridge(chat: str, outs: list, busy: str = 'park') -> bool:
    """busy: what a question does when another one is open (whatsapp/outbox.send_options). A turn's reply 'park's the
    open one behind it; a second question in the same reply waits for the first ('hold')."""
    from whatsapp import outbox
    ok = True
    asked = False
    for o in outs:
        if o.kind == 'document' and o.path:
            mid = outbox.send_document(chat, o.path, o.text)
            if mid and o.register_practice:
                from generators.practice_grader import register_sent
                register_sent(o.register_practice[0], o.register_practice[1], chat, mid)
            ok = ok and bool(mid)
        elif o.kind == 'buttons':
            ok = outbox.send_options(chat, o.text, o.buttons, busy='hold' if asked else busy) and ok
            asked = True
        else:
            ok = outbox.send_text(chat, o.text) and ok
    return ok


def send_outs(to: str, outs: list, reply_to: str | None = None) -> bool:
    from whatsapp import outbox as _ob
    if _ob.enabled():
        return send_outs_bridge(to, outs)
    from whatsapp import client as wa
    ok = True
    for o in outs:
        if o.kind == 'document' and o.path:
            mid = wa.send_document(to, o.path, o.text)
            if mid and o.register_practice:
                from generators.practice_grader import register_sent
                register_sent(o.register_practice[0], o.register_practice[1], to, mid)
        elif o.kind == 'buttons':
            mid = wa.send_buttons(to, o.text, o.buttons)
        else:
            mid = wa.send_text(to, o.text)
        ok = ok and bool(mid)
    return ok


def process_file(path: Path) -> bool:
    from agent.core import handle
    from whatsapp import client as wa
    from whatsapp import outbox
    m = json.loads(path.read_text(encoding='utf-8'))
    if m.get('group'):                                 # 05/11 before anything else: no model unless agent/group_mode allows
        from agent import group_mode
        handled, texts = group_mode.process(m)
        if handled:
            from agent.core import Out
            with outbox.group_reply():
                if texts and m.get('role') == 'group_friend':      # 17/11 the answer quotes the friend's message
                    q = {'id': m.get('id', ''), 'participant': m.get('participant', ''), 'text': (m.get('text') or '')[:200]}
                    return all(outbox.send_reply(m['chat'], t, q) for t in texts)
                return send_outs(m['chat'], [Out('text', t) for t in texts]) if texts else True
    wa.record_inbound()
    if not m.get('group') and trusted_role(m) == 'owner':      # 19/11 he's here (agent/catchup: "מה פספסתי")
        try:
            from agent import catchup
            catchup.note_active(str(m.get('chat') or m.get('from') or ''))
        except Exception as e:
            print(f'  ⚠️ catchup activity: {e!r}'[:200])
    if m.get('id') and not m.get('bridge'):
        wa.mark_read(m['id'])
    if m.get('type') == 'reaction':                    # 17/11 a reaction: 👍/👎 on an answer (agent/feedback), no model
        from agent import feedback
        from agent.core import Out
        said = feedback.on_reaction(m)
        return send_outs(m['chat'], [Out('text', said)]) if said else True
    msg = to_in(m)
    from agent.core import Out, slow_notice
    from agent import interim
    if m.get('group'):
        interim.end()
    else:                                              # 17/11 at most one "רגע, בודק…" line per turn (agent/interim)
        interim.begin(msg.sender)
        if note := slow_notice(msg):
            interim.say(note, send=lambda chat, t: send_outs(chat, [Out('text', t)]))
    from utils.budget import acting_for
    from utils import latency
    try:
        with acting_for(msg.role), latency.turn(msg.role, msg.channel):   # 18/11 data/latency_summary.json
            outs = handle(msg)
    finally:
        interim.end()
    if m.get('group'):
        with outbox.group_reply():
            ok = send_outs(msg.sender, group_mode.safe_outs(outs), m.get('id'))
    else:
        try:                                           # 17/11 a long answer, a few times a day: "👍/👎 כריאקשן"
            from agent import feedback
            outs = feedback.offer(msg, outs, m.get('id', ''))
        except Exception as e:
            print(f'  ⚠️ feedback offer: {e!r}'[:200])
        ok = send_outs(msg.sender, outs, m.get('id'))
    if msg.sender in _REASK:
        _REASK.discard(msg.sender)
        from whatsapp import outbox
        if not any(o.kind == 'buttons' for o in outs):   # the answer didn't ask something new itself
            try:
                outbox.reask_open(msg.sender)
            except Exception as e:
                print(f'  ⚠️ re-ask failed: {e}')
    return ok


def scrub_group(f: Path) -> None:
    """A processed group message keeps its id (the bridge dedupes on the name) and loses people's words: they stayed
    on disk in done/, and they aren't ours to keep."""
    try:
        m = json.loads(f.read_text(encoding='utf-8'))
        if m.get('group'):
            if m.get('media'):
                Path(m['media']).unlink(missing_ok=True)
            f.write_text(json.dumps({'id': m.get('id'), 'group': True}), encoding='utf-8')
    except (OSError, ValueError):
        pass


def run_once(spool: Path) -> int:
    done, failed = spool / 'done', spool / 'failed'
    done.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(spool.glob('*.json')):
        try:
            ok = process_file(f)
        except Exception as e:
            print(f'  ⚠️ worker: {f.name}: {e!r}')
            ok = False
        scrub_group(f)
        f.replace((done if ok else failed) / f.name)
        n += 1
    return n


def _one_tick(tick) -> list:
    """07/11 a tick stopped at the budget lock (or by anything else) doesn't take the batch's reminders down with it."""
    try:
        return tick()
    except Exception as e:
        print(f'  ⚠️ {getattr(tick, "__name__", "tick")} failed: {e!r}'[:200])
        return []


def _study() -> bool:
    """The student domain (config/features.py, SSA_STUDY). Off = assistant only: no review sessions, exams, timetable,
    university mail or course-site ticks — the secretary ticks run either way."""
    try:
        from config.features import study_enabled
        return study_enabled()
    except Exception:
        return False


def tick_list(core) -> list:
    """The periodic ticks of one batch, in their old order: the secretary's always, the student domain's only when on."""
    ticks = [core.event_reminder_tick, core.exam_sync_prompts, core.exam_day_messages, core.timetable_tick, core.mail_tick,
             core.morning_brief_tick, core.review_offer_tick, core.lesson_sync_tick, core.review_resume_tick,
             core.homework_site_tick, core.learn_tick, core.loops_tick, core.office_hours_tick, core.tasks_tick]
    if not _study():
        study = {core.exam_sync_prompts, core.exam_day_messages, core.timetable_tick, core.mail_tick,
                 core.review_offer_tick, core.lesson_sync_tick, core.review_resume_tick, core.homework_site_tick,
                 core.loops_tick, core.office_hours_tick}
        ticks = [x for x in ticks if x not in study]
    return ticks + [proactive_outs]


def proactive_outs() -> list:
    """03/11 (agent/proactive.py) — guarded on its own: a crash here must not drop the rest of the batch (the other
    ticks have already marked their messages as sent)."""
    try:
        from agent import proactive
        return proactive.tick()
    except Exception as e:
        print(f'  ⚠️ proactive tick failed: {e!r}'[:200])
        return []


def approvals_outs() -> list:
    """19/11 (agent/approvals): a pending ✅ a day old → one reminder; two days later → expired, with a note."""
    from agent import approvals
    return approvals.run()


def load_radar_outs() -> list:
    """19/11 (agent/load_radar): a week in the next 10 days with 3+ deadlines → one heads-up per week."""
    from agent import load_radar
    from agent.core import Out
    from whatsapp import outbox
    chat = outbox.owner_chat()
    return [(chat, Out('text', t)) for t in load_radar.tick()] if chat else []


def ladder_outs() -> list:
    """20/11 (agent/deadline_ladder): 7 days / 3 / 1 / the morning / 6h / 2h before a deadline, until it's submitted."""
    from agent import deadline_ladder
    return deadline_ladder.tick()


def away_outs() -> list:
    """20/11 (agent/away): a trip / busy period ahead → one question if unsure, one battle plan, what he missed."""
    from agent import away
    return away.tick()


def catchup_batch(ticks, owner: str, now=None, held_path=None, activity_path=None) -> tuple[list, bool]:
    """19/11 (agent/catchup, "מה פספסתי"): run the batch's ticks; while the owner is away a non-urgent message is held
    instead of sent; when he's back the held ones come as ONE digest at the head of the batch, their questions after
    it (through the same budget / one-at-a-time rules below). → (batch, away)."""
    from datetime import datetime
    from agent import catchup
    from agent.core import Out
    now = now or datetime.now()
    batch = []
    for tick in ticks:
        src = getattr(tick, '__name__', '')
        for chat, out in _one_tick(tick):
            try:
                if catchup.should_hold(chat, out, src, owner, now, activity_path):
                    catchup.hold(chat, out.kind, out.text, out.buttons, src, path=held_path)
                    print(f'  📥 held for the catch-up ({src}): {out.text[:50]!r}')
                    continue
            except Exception as e:
                print(f'  ⚠️ catchup hold: {e!r}'[:200])
            batch.append((chat, out))
    away = False
    if owner:
        try:
            away = catchup.away(owner, now.timestamp(), activity_path)
            digest, items = catchup.release(owner, now, held_path, activity_path)
            if digest:
                batch.insert(0, (owner, Out('text', digest)))
            for i in items:
                batch.append((owner, Out('buttons', i['text'], buttons=[tuple(b) for b in i['buttons']])
                              if i['kind'] == 'buttons' else Out('text', i['text'])))
        except Exception as e:
            print(f'  ⚠️ catchup release: {e!r}'[:200])
    return batch, away


def main() -> int:
    _no_dump()
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=True)
    except ImportError:
        pass
    try:                                        # one gate on every API call, under the subscription wrapper
        from utils.budget import install_guard
        install_guard()
    except Exception as e:
        print(f'  ⚠️ budget guard not installed: {e!r}'[:200])
    try:                                        # bot conversations on the owner's Gemini subscription (opt-in)
        from utils.gemini_sub import install
        install()
    except Exception as e:
        print(f'gemini-sub not installed: {e}')
    try:                                        # 18/11 outermost: every model call's wall time, per turn (utils/latency)
        from utils.latency import install_timer
        install_timer()
    except Exception as e:
        print(f'  ⚠️ latency timer not installed: {e!r}'[:200])
    from whatsapp import client as wa, outbox
    if not (wa.configured() or outbox.enabled()):
        print('WhatsApp not configured (WA_TRANSPORT=bridge, or WA_TOKEN / WA_PHONE_NUMBER_ID / WA_OWNER) — exiting')
        return 0
    from whatsapp.receiver import spool_dir
    spool = spool_dir()
    print(f'whatsapp worker: watching {spool}')
    try:
        from agent import group_mode
        group_mode.sync_gate()
    except Exception as e:
        print(f'  ⚠️ group gate failed: {e!r}'[:200])
    last_poll = last_nudge = last_exam = last_advance = last_esc = last_ladder = 0.0
    while True:
        run_once(spool)
        while _ANSWERED:                            # 19/11 a question that waited for this answer goes out right after it
            try:                                    # (quiet hours hold it for the morning tick, like any proactive one)
                from utils.notify import in_quiet_hours as _q0
                chat = _ANSWERED.pop()
                if not _q0():
                    outbox.release_deferred(only=chat)
            except Exception as e:
                print(f'  ⚠️ release after an answer failed: {e!r}'[:200])
        try:                                        # 05/11 debates in groups: the clock, and a held reply
            from agent import group_mode as _gm
            with outbox.group_reply():
                for item in _gm.tick():
                    chat, text = item[0], item[1]
                    if len(item) > 2 and item[2]:          # a friend's held question: the answer quotes it
                        outbox.send_reply(chat, text, item[2])
                    else:
                        outbox.send_text(chat, text)
        except Exception as e:
            print(f'  ⚠️ group tick failed: {e!r}'[:200])
        try:                                        # scheduled review sessions (agent/tutor.py)
            from agent import tutor, core as _core
            for sess in (tutor.due_sessions() if _study() else []):
                send_outs_bridge(sess['chat'], _core.start_tutor(sess['chat'], sess['plan']))
        except Exception as e:
            print(f'  ⚠️ tutor schedule tick failed: {e}')
        try:                                        # idle review sessions: one nudge (05/11: only with SSA_STUDY_PUSH=1)
            from utils.notify import in_quiet_hours as _quiet
            if _study() and time.time() - last_nudge > 300 and not _quiet():
                last_nudge = time.time()
                from agent import core as _c2
                for chat, text in _c2.tutor_nudges():
                    outbox.send_text(chat, text)
        except Exception as e:
            print(f'  ⚠️ tutor nudge tick failed: {e}')
        try:                                        # once a day: exams on the university site missing from the calendar
            from utils.notify import in_quiet_hours as _q2
            if not _q2() and time.time() - last_exam > 60:      # cached data only — cheap, once a minute
                last_exam = time.time()
                from agent import core as _c3
                # 03/11 proactive_outs = the noticing layer (agent/proactive.py, SSA_PROACTIVE=1); study ticks only with SSA_STUDY
                ticks = tuple(tick_list(_c3)) + (approvals_outs, load_radar_outs)   # 19/11 a ✅ that never came · a week with 3+ deadlines
                ticks += (ladder_outs, away_outs)            # 20/11 deadlines pushed until submitted · trips planned ahead
                batch, away = catchup_batch(ticks, outbox.owner_chat())
                for chat, out in _c3.merge_outs(batch):     # one message per chat per tick, not a burst
                    # asked to be nagged: his tasks, and (14/11) what he owes in Moodle until done — the proactive cap
                    # (≤3 a day) still holds; the 7-day same-text rule would drop the every-2-days quiz reminder
                    from agent import noise         # 16/11 a kind he turned off ("תפסיק לשלוח לי דברים שלא רלוונטיים")
                    if (nk := noise.blocked(out.text)):
                        print(f'  🔕 {nk}: turned off by the owner — not sent')
                        continue
                    nag = out.kind == 'buttons' and str(out.buttons[0][1]).startswith(('task:', 'sec:odone:'))
                    if out.kind == 'buttons' and not nag and outbox.already_asked(chat, out.text):
                        print(f'  ⏭️ proactive question already asked this week — not sending again')
                        continue
                    if out.kind == 'buttons' and not nag:   # 19/10 budget: ignored kinds stop, ≤5 questions a day
                        g = outbox.gate(chat, out.text, out.buttons)
                        if g == 'mute':
                            print(f'  🔕 {outbox.kind_of(out.buttons)}: muted (ignored / "תפסיק לשאול") — not sent')
                            continue
                        if g == 'defer':
                            outbox.defer_options(chat, out.text, out.buttons, gated=False)
                            continue
                    if out.kind == 'buttons' and outbox.options_open(chat):
                        outbox.defer_options(chat, out.text, out.buttons)   # one numbered question at a time
                    else:
                        send_outs_bridge(chat, [out])
                if not away:                                # 19/11 he's away: the waiting questions wait too
                    outbox.release_deferred()               # the next waiting question, once the last is answered
        except Exception as e:
            print(f'  ⚠️ exam sync tick failed: {e}')
        try:                                        # due reminders (agent/life.py)
            from agent import life
            for rec, buttons in life.due_now():
                outbox.send_options(rec['chat'], f"⏰ *תזכורת{' (🚨 חשוב)' if rec.get('important') else ''}:* "
                                                 f"{rec['text']}", buttons, busy='letters')   # 19/11 it can't wait: א/ב
                if rec.get('important'):                  # 11/11 no reaction → a nudge, then a calendar alarm
                    from agent import escalate
                    escalate.watch(rec['chat'], rec['text'], 'reminder', rec['id'])
        except Exception as e:
            print(f'  ⚠️ reminders tick failed: {e}')
        try:                                        # 20/11 quiet hours: only the last 6 hours before a deadline wake him
            from utils.notify import in_quiet_hours as _q3
            if _q3() and time.time() - last_ladder > 60:
                last_ladder = time.time()
                from agent import deadline_ladder
                for chat, out in deadline_ladder.urgent_outs():
                    send_outs_bridge(chat, [out])
        except Exception as e:
            print(f'  ⚠️ ladder (quiet hours) failed: {e!r}'[:200])
        try:                                        # 11/11 what he marked important and didn't answer (agent/escalate)
            if time.time() - last_esc > 30:
                last_esc = time.time()
                from agent import escalate
                for chat, text, buttons in escalate.tick():
                    outbox.send_options(chat, text, buttons, busy='letters')
        except Exception as e:
            print(f'  ⚠️ escalation tick failed: {e!r}'[:200])
        try:                                        # 03/11 the browser agent paused / finished (The owner asked — any hour)
            from agent import web_agent
            for chat, text, buttons, shot in web_agent.poll():
                if shot:
                    outbox.send_media(chat, shot, wait=0)
                if buttons:
                    outbox.send_options(chat, text, buttons, busy='letters')     # it waits on him: can't be held
                else:
                    outbox.send_text(chat, text)
        except Exception as e:
            print(f'  ⚠️ web agent poll failed: {e!r}'[:200])
        try:                                        # 03/11 someone joined with the owner's one-time code
            from whatsapp import invites
            for chat, text in invites.joined():
                outbox.send_text(chat, text)
        except Exception as e:
            print(f'  ⚠️ invites tick failed: {e!r}'[:200])
        from utils.notify import in_quiet_hours
        if time.time() - last_advance > 20:                 # 05/11 a limit that resets at 03:00 is used at 03:00
            last_advance = time.time()
            try:
                from agent import claude_job
                claude_job.advance()
            except Exception as e:
                print(f'  ⚠️ claude job tick failed: {e!r}'[:200])
        if time.time() - last_poll > 15 and not in_quiet_hours():   # finished "קלוד:" jobs → report (not at night)
            last_poll = time.time()
            try:
                from agent import claude_job
                for chat, text in claude_job.notices():
                    outbox.send_text(chat, text)
                for job in claude_job.poll():
                    report_job(job)
            except Exception as e:
                print(f'  ⚠️ claude job poll failed: {e}')
        time.sleep(1.0)


if __name__ == '__main__':
    os.chdir(str(ROOT))
    os.environ.setdefault('SSA_ROUTER', 'agent')       # compact intent prompt for the agent (bot/brain.py)
    os.environ.setdefault('SSA_BRAIN', '1')            # the agent loop (agent/mind.py); SSA_BRAIN=0 in .env turns it off
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    sys.exit(main())
