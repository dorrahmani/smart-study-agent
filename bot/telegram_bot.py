"""
bot/telegram_bot.py — UNUSED since 10/11/2026 (The owner deleted the bot; smart-study-bot.service is disabled).
Kept aside, not deleted; nothing imports it at runtime (the .env loader moved to utils/env.py).
Was: Telegram transport for Smart Study Agent.

Usage:
    1. Open @BotFather on Telegram → /newbot → save the token.
    2. Set environment variables (or add to .env):
        TELEGRAM_BOT_TOKEN=123456:ABC...
        TELEGRAM_USER_ID=987654321   (your numeric user ID — bot rejects everyone else)
       Find your user ID by sending /start once and watching the log.
    3. pip install python-telegram-bot==21.*
    4. python -m bot.telegram_bot

The bot uses long-polling (no webhook needed). It only responds to chat
IDs in TELEGRAM_USER_ID (comma-separated for multiple). All other chats
are silently ignored.

Commands map 1:1 onto bot.handlers — adding/removing transports doesn't
change behavior, only the wire format.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import logging
from typing import Awaitable, Callable

# Encode console output for Hebrew on Windows.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def _load_env_dotfile() -> None:
    """Best-effort .env loader (no external dependency)."""
    from pathlib import Path
    env = Path(__file__).resolve().parent.parent / '.env'
    if not env.exists():
        return
    for line in env.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        # Override empty values from shell env (e.g. ANTHROPIC_API_KEY="" set globally)
        if not os.environ.get(k):
            os.environ[k] = v


_load_env_dotfile()
# כפה Gemini (לא לשתמש ב-GOOGLE_API_KEY של פרויקט שאין בו Gemini API)
if os.environ.get('GEMINI_API_KEY'):
    os.environ.pop('GOOGLE_API_KEY', None)

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    level=logging.INFO,
)
# httpx logs every request URL at INFO — and Telegram URLs carry the bot token
# (https://api.telegram.org/bot<TOKEN>/getUpdates). Until 09/2026 that wrote the
# token into logs/bot.log ~8,500 times a day.
for _noisy in ('httpx', 'httpcore'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger('telegram_bot')

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
ALLOWED_IDS_RAW = os.environ.get('TELEGRAM_USER_ID', '').strip()
ALLOWED_IDS: set[int] = {
    int(x) for x in ALLOWED_IDS_RAW.split(',') if x.strip().lstrip('-').isdigit()
}


def _allowed(chat_id: int) -> bool:
    """Strict allowlist, fail-CLOSED: an empty/broken TELEGRAM_USER_ID locks
    the bot instead of opening it to the world (the bot can run the pipeline
    and answer from all course material). Rejected ids are logged, so a new
    user's chat_id can be read from the service log and added to .env."""
    if chat_id in ALLOWED_IDS:
        return True
    log.warning('rejected chat_id=%s (not in TELEGRAM_USER_ID)', chat_id)
    return False


# ─── Application setup ─────────────────────────────────────────────────────

def _import_ptb():
    try:
        from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.ext import (
            ApplicationBuilder, ContextTypes,
            CommandHandler, MessageHandler, filters, CallbackQueryHandler,
        )
        return (Update, ApplicationBuilder, ContextTypes, CommandHandler,
                MessageHandler, filters, InlineKeyboardButton,
                InlineKeyboardMarkup, CallbackQueryHandler)
    except Exception as e:
        print('⚠️  python-telegram-bot is not installed.')
        print('    pip install "python-telegram-bot==21.*"')
        raise SystemExit(2) from e


_LAST_FILES: dict = {}  # chat_id -> [file paths] of the last result, for "שלח לי אותה"
_LAST_CHOICES: dict = {}  # chat_id -> [file paths] awaiting a pick:<i> button

def _is_pronoun_ref(t: str) -> bool:
    """True if the message is a bare back-reference to the previous result
    (e.g. 'שלח לי אותה', 'תשלח את זה', 'אותו דבר') with no concrete file named."""
    import re as _r
    t = (t or '').strip()
    t = _r.sub(r'^\s*(תשלח|שלח|תן|תוריד|הורד|תביא)\s+(לי\s+)?(את\s+)?', '', t)
    t = _r.sub(r"\s+(בבקשה|למייל|במייל|לצאט|לצ'אט|לכאן|פה|גם)$", '', t).strip()
    t = _r.sub(r"^(גם\s+)", '', t).strip()
    return bool(_r.fullmatch(r"אות[הוםן]|אות[הו] דבר|את\s*ז[הו]|ז[הו]|אלה|אלו|הללו", t))

def _ordinal_mismatch_note(query: str, path) -> str:
    """If the user asked 'הרצאה N' but the summary's own slides call it a
    different 'שיעור M', return a gentle clarifying note (else '')."""
    import re as _r
    from pathlib import Path as _P
    m = _r.search(r'(?:הרצאה|שיעור|lecture)\s*(\d{1,2})', query or '')
    if not m:
        return ''
    asked = int(m.group(1))
    try:
        txt = _P(str(path)).with_suffix('.md').read_text(encoding='utf-8', errors='replace')[:4000]
    except Exception:
        return ''
    im = _r.search(r'שיעור\s*(\d{1,2})', txt)
    if im and int(im.group(1)) != asked:
        return ('\n\nℹ️ לפי סדר התאריכים זו הרצאה %d, אבל בשקפים היא מסומנת '
                '"שיעור %s" (המספור של המרצה לא תמיד תואם את סדר התאריכים).'
                % (asked, im.group(1)))
    return ''


def _run_bot() -> None:
    """One attempt to run the bot. Raises on failure (caller retries)."""
    try:                                        # one gate on every API call; only ALLOWED_IDS reach this bot
        from utils.budget import install_guard
        install_guard(default_actor='owner')
    except Exception as e:
        print(f'⚠️ budget guard not installed: {e!r}'[:200])
    try:                                        # bot conversations on the owner's Gemini subscription (opt-in)
        from utils.gemini_sub import install
        install()
    except Exception as e:
        print(f'gemini-sub not installed: {e}')
    if not TOKEN:
        print('❌ TELEGRAM_BOT_TOKEN is not set. Add it to .env or env vars.')
        raise SystemExit(1)

    (Update, ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
     filters, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQueryHandler) = _import_ptb()
    from bot import handlers

    # ── Quick-action keyboard (shown on /menu and after greetings) ─────────
    def _main_menu_kb():
        kb = [
            [InlineKeyboardButton('📅 היום', callback_data='act:today'),
             InlineKeyboardButton('📚 סיכומים', callback_data='act:list_summaries')],
            [InlineKeyboardButton('📝 דדליינים', callback_data='act:assignments'),
             InlineKeyboardButton('📊 סטטוס', callback_data='act:status')],
            [InlineKeyboardButton('🧠 בחן אותי', callback_data='act:quiz_chat'),
             InlineKeyboardButton('❓ עזרה', callback_data='act:help')],
        ]
        return InlineKeyboardMarkup(kb)
    from telegram.error import BadRequest
    import asyncio as _asyncio_mod

    # ---- Cancellation state (per-chat) for long-running tasks (e.g. summarize)
    # _active_tasks: which chats have a long task currently running.
    # _cancel_events: asyncio.Event per chat — set to signal "abort".
    _active_tasks: set[int] = set()
    _cancel_events: dict[int, _asyncio_mod.Event] = {}

    # ---- Conversation memory (last N turns per chat, persisted to disk) ──
    # Each entry: {'role': 'user'|'bot', 'text': str, 'ts': iso8601}
    _HIST_PATH = Path(__file__).resolve().parent.parent / 'data' / 'bot_chat_history.json'
    _HIST_MAX = 20  # keep last 20 turns per chat
    _chat_history: dict[int, list[dict]] = {}
    try:
        import json as _json_hist
        if _HIST_PATH.exists():
            _chat_history = {int(k): v for k, v in
                             _json_hist.loads(_HIST_PATH.read_text(encoding='utf-8')).items()}
    except Exception:
        _chat_history = {}

    def _hist_add(chat_id: int, role: str, text: str) -> None:
        if not text:
            return
        from datetime import datetime as _dt
        import json as _json_hist
        buf = _chat_history.setdefault(chat_id, [])
        buf.append({'role': role, 'text': text[:1500], 'ts': _dt.now().isoformat()})
        if len(buf) > _HIST_MAX:
            del buf[:len(buf) - _HIST_MAX]
        # persist (best-effort)
        try:
            _HIST_PATH.parent.mkdir(exist_ok=True)
            _HIST_PATH.write_text(
                _json_hist.dumps({str(k): v for k, v in _chat_history.items()},
                                 ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
        except Exception:
            pass

    def _hist_recent(chat_id: int, n: int = 6) -> list[dict]:
        """Return last n turns of conversation (most recent last)."""
        buf = _chat_history.get(chat_id, [])
        return buf[-n:] if buf else []

    def _hist_as_context(chat_id: int, n: int = 6) -> str:
        """Format recent history as a string for LLM context."""
        turns = _hist_recent(chat_id, n)
        if not turns:
            return ''
        lines = []
        for t in turns:
            who = 'משתמש' if t['role'] == 'user' else 'בוט'
            lines.append(f'{who}: {t["text"]}')
        return '\n'.join(lines)

    def _get_cancel_event(chat_id: int) -> _asyncio_mod.Event:
        ev = _cancel_events.get(chat_id)
        if ev is None:
            ev = _asyncio_mod.Event()
            _cancel_events[chat_id] = ev
        return ev

    def _trigger_cancel(chat_id: int) -> None:
        _get_cancel_event(chat_id).set()

    def _task_active(chat_id: int) -> bool:
        return chat_id in _active_tasks

    CANCEL_WORDS = {'בטל', 'בטלי', 'עצור', 'די', 'תפסיק', 'תפסיקי',
                    'stop', 'cancel', 'abort', 'לא', 'no'}

    def friendly_error(exc: Exception, context: str = '') -> str:
        """Translate common exceptions into clear Hebrew with suggested fix.

        Args:
            exc: The exception caught.
            context: One-line description of what was being attempted, e.g.
                     "שליחת מייל" or "יצירת סיכום". Shown in the message.

        Returns a single string ready to send via safe_reply.
        """
        msg = str(exc)
        msg_l = msg.lower()
        cls = type(exc).__name__

        ctx_prefix = f'❌ נכשל {context}.' if context else '❌ שגיאה.'

        # Gemini / Google API
        if '503' in msg or 'UNAVAILABLE' in msg or 'overloaded' in msg_l:
            return f'{ctx_prefix}\n🔁 Gemini עמוס כרגע (503). תנסה שוב בעוד דקה.'
        if '429' in msg or 'RESOURCE_EXHAUSTED' in msg or 'quota' in msg_l:
            return f'{ctx_prefix}\n🚫 חרגנו ממכסת ה-API היומית. ננסה שוב מחר או נחליף מודל.'
        if '400' in msg and ('Budget' in msg or 'thinking' in msg_l):
            return f'{ctx_prefix}\n⚙️ המודל דורש הגדרת thinking_budget — תיקון בקוד נדרש.'
        if '400' in msg and 'INVALID_ARGUMENT' in msg:
            return f'{ctx_prefix}\n⚠️ הפרומט גדול מדי או לא תקין. נסה לקצר.'
        if 'API key' in msg_l or 'no api key' in msg_l:
            return f'{ctx_prefix}\n🔑 חסר GEMINI_API_KEY ב-.env. צריך להגדיר.'

        # Gmail OAuth
        if 'invalid_grant' in msg_l or 'token has been expired or revoked' in msg_l:
            return (f'{ctx_prefix}\n🔐 ה-Gmail token פג. צריך לרענן:\n'
                    f'הרץ מקומית `python scripts/reauth_gmail.py` ואז העלה את\n'
                    f'`config/gmail_token.pickle` ל-VM.')
        if 'gmail' in msg_l and 'permission' in msg_l:
            return f'{ctx_prefix}\n🔐 הרשאת Gmail לא מספיקה (צריך scope של read/send).'

        # NotebookLM
        if 'session expired' in msg_l or 'notebooklm' in msg_l and 'auth' in msg_l:
            return (f'{ctx_prefix}\n🎓 הסשן של NotebookLM פג.\n'
                    f'התחבר מחדש דרך VNC ל-VM (ראה `config/secrets/VNC_INSTRUCTIONS.md`).')

        # Files / disk
        if isinstance(exc, FileNotFoundError):
            return f'{ctx_prefix}\n📁 קובץ לא נמצא: `{exc.filename or msg}`.'
        if isinstance(exc, PermissionError):
            return f'{ctx_prefix}\n🔒 אין הרשאה לקובץ: `{exc.filename or msg}`.'
        if 'no space left' in msg_l:
            return f'{ctx_prefix}\n💽 אין מקום פנוי בדיסק. צריך לפנות.'

        # Network
        if isinstance(exc, (ConnectionError, ConnectionResetError, ConnectionRefusedError)):
            return f'{ctx_prefix}\n🌐 בעיית חיבור: {msg[:80]}.'
        if 'timeout' in msg_l or isinstance(exc, TimeoutError):
            return f'{ctx_prefix}\n⏱️ הפעולה לקחה יותר מדי זמן. אולי הקובץ גדול או השרת איטי — נסה שוב.'
        if 'name resolution' in msg_l or 'getaddrinfo' in msg_l:
            return f'{ctx_prefix}\n🌐 לא הצלחתי למצוא את הכתובת. בעיית DNS/אינטרנט?'

        # Python imports
        if isinstance(exc, ImportError) or 'no module named' in msg_l:
            mod = ''
            try: mod = exc.name or ''
            except: pass
            mod_hint = f' חסר: `{mod}`' if mod else ''
            return f'{ctx_prefix}\n📦 חבילת Python חסרה.{mod_hint}\nהרץ `pip install <חבילה>` ב-VM.'

        # JSON
        if isinstance(exc, ValueError) and 'json' in msg_l:
            return f'{ctx_prefix}\n📄 הקלט/הפלט לא בפורמט JSON תקין.'

        # Syntax (e.g. when running student code)
        if isinstance(exc, SyntaxError):
            ln = getattr(exc, 'lineno', '?')
            return f'{ctx_prefix}\n💢 שגיאת תחביר בקוד, שורה {ln}: {exc.msg if hasattr(exc, "msg") else msg}.'

        # PDF
        if 'eof marker not found' in msg_l or 'pdf' in msg_l and 'corrupt' in msg_l:
            return f'{ctx_prefix}\n📄 קובץ PDF פגום או לא שלם.'

        # Generic fallback
        return f'{ctx_prefix}\n🐛 {cls}: {msg[:200]}'

    async def safe_reply(update: Update, text: str) -> None:
        """Send as plain text with network retries. Unicode formatting is sufficient."""
        if not text:
            text = '(אין תוכן)'
        import re as _re, asyncio as _asyncio
        plain = text.replace('**', '').replace('__', '')
        plain = _re.sub(r'\*([^*\n]+)\*', r'\1', plain)
        plain = _re.sub(r'_([^_\n]+)_', r'\1', plain)
        plain = plain[:4000]
        for attempt in range(5):
            try:
                await update.message.reply_text(plain)
                return
            except Exception as e:
                log.warning('reply attempt %d failed: %r', attempt + 1, e)
                await _asyncio.sleep(2 ** attempt)
        log.error('reply gave up after 5 attempts')

    app = ApplicationBuilder().token(TOKEN).build()

    # ---- /start: register user; print chat_id so the user can lock the bot
    async def cmd_start(update: Update, ctx) -> None:
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        log.info('cmd /start from chat_id=%s', chat_id)
        # First-run: allowlist empty → reply ONLY with chat_id (harmless) so the
        # owner can add it to .env; every other command stays locked (_allowed).
        # Locked-down: only allowlisted chats get HELP.
        if not ALLOWED_IDS:
            await update.message.reply_text(
                f'🎓 *Smart Study Bot — First run!*\n\n'
                f'ה-`chat_id` שלך הוא:\n`{chat_id}`\n\n'
                f'הוסף ל-`.env`:\n`TELEGRAM_USER_ID={chat_id}`\n\n'
                f'אז תפעיל מחדש ותוכל להשתמש בכל הפקודות.',
                parse_mode='Markdown',
            )
            return
        if not _allowed(chat_id):
            return  # silent rejection
        # Wrap `chat_id` in backticks so Markdown doesn't see the underscore
        # as the start of italic. Also fall back to safe_reply (plain text) if
        # Markdown parsing fails for any reason — never leave /start broken.
        welcome = (
            f'🎓 ברוך הבא ל-Smart Study Agent.\n'
            f'`chat_id`: `{chat_id}`\n\n'
            + handlers.HELP
        )
        try:
            await update.message.reply_text(welcome, parse_mode='Markdown')
        except BadRequest as e:
            log.warning('cmd_start markdown failed (%s) — falling back to plain text', e)
            await safe_reply(update, welcome)

    # ---- generic factory: command name → handler
    def make_cmd(handler_fn: Callable[..., str]):
        async def _wrapped(update: Update, ctx) -> None:
            if not update.effective_chat or not _allowed(update.effective_chat.id):
                return
            args = ' '.join(ctx.args or [])
            try:
                text = handler_fn(args) if handler_fn.__code__.co_argcount == 1 \
                       else handler_fn(args, None)
            except Exception as e:
                log.exception('handler error')
                text = friendly_error(e, f'הרצת /{handler_fn.__name__.replace("handle_", "")}')
            await safe_reply(update, text)
        return _wrapped

    async def cmd_ask(update: Update, ctx) -> None:
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        q = ' '.join(ctx.args or [])
        text = handlers.handle_ask(q)
        await safe_reply(update, text)

    async def cmd_remind(update: Update, ctx) -> None:
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        from bot import reminders as rem
        args = ' '.join(ctx.args or [])
        text = rem.handle_remind(args, update.effective_chat.id)
        await safe_reply(update, text)

    async def cmd_image(update: Update, ctx) -> None:
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        prompt = ' '.join(ctx.args or [])
        if not prompt.strip():
            await safe_reply(update, '🎨 שימוש: `/image <תיאור התמונה>`\nדוגמה: `/image גרף של פונקציה $f(x)=x^2$ עם מינימום ב-0`')
            return
        await update.message.reply_text('🎨 מייצר תמונה...')
        try:
            from google import genai
            from google.genai import types as genai_types
            key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
            client = genai.Client(api_key=key)
            resp = client.models.generate_content(
                model='gemini-2.5-flash-image-preview',
                contents=f'Generate an educational diagram/illustration. {prompt}',
            )
            # Find image in response parts
            img_bytes = None
            for cand in resp.candidates or []:
                for part in cand.content.parts or []:
                    if getattr(part, 'inline_data', None) and part.inline_data.data:
                        img_bytes = part.inline_data.data
                        break
                if img_bytes: break
            if not img_bytes:
                await safe_reply(update, '⚠️ לא הצלחתי לייצר תמונה. (המודל לא חזר עם תמונה)')
                return
            tmp = Path(__file__).resolve().parent.parent / 'data' / 'tmp_image.png'
            tmp.write_bytes(img_bytes)
            with open(tmp, 'rb') as f:
                await update.message.reply_photo(photo=f, caption=prompt[:200])
        except Exception as e:
            await safe_reply(update, f'⚠️ שגיאה ביצירת תמונה: {e}')

    COURSE_MAP = {
        'מבוא': 'intro_to_cs', 'cs': 'intro_to_cs', 'intro': 'intro_to_cs', 'מורחב': 'intro_to_cs',
        'חדואא': 'calculus_2', 'חדוא': 'calculus_2', 'חדו"א': 'calculus_2', 'calc': 'calculus_2',
        'בדידה': 'discrete_math_2', 'discrete': 'discrete_math_2',
        'לינארית': 'linear_algebra_2', 'linear': 'linear_algebra_2',
        'מיקרו': 'microeconomics', 'micro': 'microeconomics', 'כלכלה': 'microeconomics',
    }
    def _course_from_arg(arg: str) -> str | None:
        a = (arg or '').lower().strip()
        for kw, ck in COURSE_MAP.items():
            if kw in a: return ck
        return None

    async def _nlm_generate(update, ctx, kind: str, fn_name: str, emoji: str, msg_label: str):
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        course_key = _course_from_arg(' '.join(ctx.args or []))
        if not course_key:
            await safe_reply(update, f'{emoji} שימוש: `/{kind} <קורס>`\nקורסים: מבוא, חדו"א, בדידה, לינארית, מיקרו')
            return
        await update.message.reply_text(f'{emoji} מייצר {msg_label} ל-{course_key}... (1-3 דק׳)')
        try:
            from generators.notebooklm_agent import NotebookLMAgent
            agent = NotebookLMAgent()
            result = getattr(agent, fn_name)(course_key, scope='all')
            # result can be a path string OR a dict with 'file_path' or 'text'
            if isinstance(result, dict):
                if result.get('file_path') and Path(result['file_path']).exists():
                    fp = Path(result['file_path'])
                    with open(fp, 'rb') as f:
                        if fp.suffix.lower() in ('.mp4', '.mov', '.webm'):
                            await update.message.reply_video(video=f, caption=f'{emoji} {course_key}')
                        elif fp.suffix.lower() in ('.mp3', '.m4a', '.ogg', '.wav'):
                            await update.message.reply_audio(audio=f, caption=f'{emoji} {course_key}')
                        else:
                            await update.message.reply_document(document=f, filename=fp.name)
                    return
                if result.get('text'):
                    await safe_reply(update, result['text'][:4000])
                    return
            elif isinstance(result, str) and Path(result).exists():
                fp = Path(result)
                with open(fp, 'rb') as f:
                    await update.message.reply_document(document=f, filename=fp.name)
                return
            await safe_reply(update, f'⚠️ NotebookLM לא החזיר תוצר תקין.')
        except Exception as e:
            await safe_reply(update, friendly_error(e, f'יצירת {label}'))

    async def cmd_video(update, ctx):       await _nlm_generate(update, ctx, 'video',      'generate_video',       '🎬', 'סרטון')
    async def cmd_podcast(update, ctx):     await _nlm_generate(update, ctx, 'podcast',    'generate_podcast',     '🎧', 'פודקאסט')
    async def cmd_quiz(update, ctx):        await _nlm_generate(update, ctx, 'quiz',       'generate_quiz',        '🧠', 'חידון')
    async def cmd_flashcards(update, ctx):  await _nlm_generate(update, ctx, 'flashcards', 'generate_flashcards',  '🎴', 'פלאשקארדים')
    async def cmd_studyguide(update, ctx):  await _nlm_generate(update, ctx, 'studyguide', 'generate_study_guide', '📖', 'מדריך לימוד')

    async def cmd_preview(update: Update, ctx) -> None:
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        await safe_reply(update, handlers.handle_sync('', preview=True))

    # ─── File send capability ───
    SUMMARIES_ROOT = Path(__file__).resolve().parent.parent / 'data' / 'summaries'
    ASSIGNMENTS_ROOT = Path(__file__).resolve().parent.parent / 'data' / 'assignments'

    # course aliases (module-level for re-use)
    _COURSE_ALIASES = {
        # calculus
        'חדוא': 'calculus_2', 'חדואא': 'calculus_2', 'חדוא2': 'calculus_2',
        'חשבון': 'calculus_2', 'חשבון2': 'calculus_2', 'אינטגרל': 'calculus_2',
        'אינטגרלים': 'calculus_2', 'קלקולוס': 'calculus_2', 'calculus': 'calculus_2',
        # discrete math
        'בדידה': 'discrete_math_2', 'בדיד': 'discrete_math_2',
        'מתמטיקה': 'discrete_math_2', 'קומבינטוריקה': 'discrete_math_2',
        'discrete': 'discrete_math_2',
        # linear algebra
        'לינארית': 'linear_algebra_2', 'לינארית2': 'linear_algebra_2',
        'לינארי': 'linear_algebra_2', 'אלגברה': 'linear_algebra_2',
        'מטריצות': 'linear_algebra_2', 'וקטורים': 'linear_algebra_2',
        'linalg': 'linear_algebra_2', 'linear': 'linear_algebra_2',
        # intro CS
        'מבוא': 'intro_to_cs', 'מורחב': 'intro_to_cs',
        'מדמח': 'intro_to_cs', 'מדמ"ח': 'intro_to_cs', 'מד"מח': 'intro_to_cs',
        'מדעי': 'intro_to_cs', 'המחשב': 'intro_to_cs', 'פייתון': 'intro_to_cs',
        'cs': 'intro_to_cs', 'introcs': 'intro_to_cs', 'python': 'intro_to_cs',
        # microeconomics (semester B 5786 — history; "מיקרו" alone now means micro 2)
        'מיקרו1': 'microeconomics', 'מיקרוא': 'microeconomics',
        # --- semester A 5787 ---
        'הסתברות': 'probability', 'סתברות': 'probability', 'הסתברה': 'probability',
        'סטטיסטיקה': 'probability', 'סטט': 'probability', 'probability': 'probability',
        'תוכנה': 'software_1', 'תוכנה1': 'software_1', "ג'אווה": 'software_1',
        'גאווה': 'software_1', 'java': 'software_1', 'software': 'software_1',
        'מיקרו': 'microeconomics_2', 'מיקרו2': 'microeconomics_2', 'מיקרוב': 'microeconomics_2',
        'micro': 'microeconomics_2', 'כלכלה': 'microeconomics_2', 'economics': 'microeconomics_2',
        'מאקרו': 'macroeconomics', 'מקרו': 'macroeconomics', 'macro': 'macroeconomics',
    }
    # course folder names (for file path matching)
    _COURSE_FOLDERS = {
        'intro_to_cs': ['intro_to_cs'],
        'calculus_2': ['calculus_2'],
        'discrete_math_2': ['discrete_math_2'],
        'linear_algebra_2': ['linear_algebra_2'],
        'microeconomics': ['microeconomics'],
        'probability': ['probability'],
        'software_1': ['software_1'],
        'microeconomics_2': ['microeconomics_2'],
        'macroeconomics': ['macroeconomics'],
    }
    # Teacher/lecturer/tutor names → course mapping (helps when user names a
    # person instead of the course, e.g. "תשלח לי הרצאה של מרצה_כ").
    _TEACHER_TO_COURSE = {
        'מרצה_כ':  'calculus_2',      # lecturer
        'קהת':   'calculus_2',
        'מרצה_י': 'calculus_2',      # tutor
        'מרצה_ס':  'calculus_2',
        'ראובן':   'calculus_2',      # back-up lecturer (rare)
        'מרצה_צ':   'linear_algebra_2',  # lecturer
        'מרצה_ת':   'linear_algebra_2',  # tutor
        'בנימין':   'linear_algebra_2',
        'יוסף':    'linear_algebra_2',  # writes notes
        'לאה':   'discrete_math_2',   # lecturer (מרצה_ע)
        'מרצה_ע':  'discrete_math_2',
        'מרצה_פ':   'discrete_math_2',   # tutor (בן מרצה_מ / דן-צור)
        'דן-צור': 'discrete_math_2',
        'מרצה_מ':  'discrete_math_2',
        'אפרים':   'intro_to_cs',       # tutor
        'מנשה':     'intro_to_cs',       # tutor
        'עמרם':    'intro_to_cs',       # tutor
        'אהרן':    'intro_to_cs',       # lecturer
        'מרצה_ג': 'intro_to_cs',
    }

    def _strip_he_prefix(w: str, stop_on_alias: bool = True) -> str:
        """Strip Hebrew article/preposition prefixes one char at a time.
        If stop_on_alias=True, stops as soon as the stripped form matches a known
        course alias — prevents over-stripping ('במבוא' → 'מבוא', not 'וא')."""
        if stop_on_alias and w in _COURSE_ALIASES:
            return w
        while len(w) > 2 and w[0] in 'הבלמשכו':
            w = w[1:]
            if stop_on_alias and w in _COURSE_ALIASES:
                return w
        return w

    def _normalize_he(w: str) -> str:
        """Normalize for fuzzy match: strip ALL prefixes, collapse plural."""
        w = _strip_he_prefix(w.lower())
        if w.endswith('ות') and len(w) > 3:
            w = w[:-2] + 'ה'
        elif w.endswith('ים') and len(w) > 3:
            w = w[:-2]
        elif w.endswith('יים') and len(w) > 4:
            w = w[:-3]
        return w

    def _find_files_for_query(q: str) -> list[Path]:
        """Smart file search with Hebrew prefix/plural handling, ALL/קל support, and intent.
        Returns up to 25 files (for batch sends like 'all lectures')."""
        import re as _re
        ql = q.strip()
        ql = _re.sub(r'^\s*(תשלח|שלח|תן|תוריד|הורד)\s+(לי\s+)?(את\s+)?', '', ql)
        ql = _re.sub(r'\s+(קובץ|PDF|pdf|בבקשה)$', '', ql).strip()
        if not ql: return []

        wants_latest = bool(_re.search(r'אחרון|אחרונה|האחרון|האחרונה|latest|last', ql))
        wants_all    = bool(_re.search(r'\bכל\b|\bכל ה|all\b', ql))

        # Detect kind filter (lecture vs tutorial)
        wants_lectures  = bool(_re.search(r'הרצא', ql))
        wants_tutorials = bool(_re.search(r'תרגול|תרגיל', ql))
        # if user wrote both (e.g. "כל ההרצאות והתרגולים") → both kinds
        if not (wants_lectures or wants_tutorials):
            wants_lectures = wants_tutorials = True

        # Ordinal: "הרצאה 3" / "שיעור 3" / "תרגול 5" / "lecture 3" -> Nth BY DATE
        _m_ord = _re.search(r'(?:הרצאה|הרצאת|שיעור|תרגול|תרגיל|lecture|lesson)\s*(\d{1,2})\b', ql)
        ordinal_n = int(_m_ord.group(1)) if _m_ord else None
        if ordinal_n is None:
            _HE_ORD = {'ראשון':1,'ראשונה':1,'שני':2,'שניה':2,'שנייה':2,
                       'שלישי':3,'שלישית':3,'רביעי':4,'רביעית':4,'חמישי':5,'חמישית':5,
                       'שישי':6,'שישית':6,'שביעי':7,'שביעית':7,'שמיני':8,'שמינית':8,
                       'תשיעי':9,'תשיעית':9,'עשירי':10,'עשירית':10}
            for _tok in _re.split(r'\s+', ql):
                _raw = _tok.strip()
                _cands = {_raw, (_raw[1:] if _raw[:1] == 'ה' else _raw), _strip_he_prefix(_raw)}
                _hit = next((_HE_ORD[c] for c in _cands if c in _HE_ORD), None)
                if _hit:
                    ordinal_n = _hit; break

        STOP = {'של', 'עם', 'את', 'אני', 'הוא', 'היא',
                'מ', 'ב', 'ל', 'ו', 'ה', 'כל', 'all',
                'אחרון', 'אחרונה', 'האחרון', 'האחרונה', 'latest', 'last',
                # don't include הרצאה/תרגול as keywords; handled via folder/wants_*
                'הרצאה', 'הרצאת', 'הרצאות', 'תרגול', 'תרגולים', 'תרגיל', 'תרגילים',
                # post-strip variants (after recursive prefix removal):
                'רצאה', 'רצאות', 'רצאת', 'רגול', 'רגולים', 'רגיל', 'רגילים',
                'רצא', 'רגו',
                # filler query words that don't appear in filenames:
                'מה', 'שלמדנו', 'שנלמד', 'שעשינו', 'ילמד', 'נלמד', 'למדנו',
                'שלמד', 'שיש', 'יש', 'קיים', 'קיימת', 'חומר', 'חומרים',
                'ממה', 'שיעור', 'בשיעור', 'בקורס', 'בכל', 'כל',
                # file-meta words that describe the file but don't appear in filename:
                'סיכום', 'סיכומים', 'הסיכום', 'הסיכומים',
                'מסמך', 'מסמכים', 'תיק', 'תיקים', 'דף', 'דפים',
                'תוכן', 'תכנים', 'גרסה', 'גרסת', 'גרסאות',
                'summary', 'summaries', 'document', 'doc',
                # post-strip "סיכום" variants:
                'יכום', 'יכומים',}

        course_filter: str | None = None
        raw_tokens = [t for t in _re.split(r'\s+', ql.lower()) if t]
        clean_tokens = []
        for t in raw_tokens:
            ts = _strip_he_prefix(t)
            if ts in _COURSE_ALIASES:
                course_filter = _COURSE_ALIASES[ts]; continue
            if t in _COURSE_ALIASES:
                course_filter = _COURSE_ALIASES[t]; continue
            # Teacher name → infer course (but keep name as filter token so we
            # match the specific teacher's files within that course).
            if ts in _TEACHER_TO_COURSE:
                if not course_filter:
                    course_filter = _TEACHER_TO_COURSE[ts]
                clean_tokens.append(ts)
                continue
            if t in _TEACHER_TO_COURSE:
                if not course_filter:
                    course_filter = _TEACHER_TO_COURSE[t]
                clean_tokens.append(t)
                continue
            tn = _normalize_he(t)
            if t in STOP or ts in STOP or tn in STOP: continue
            if t.isdigit(): continue  # lecture/exercise numbers handled as ordinals
            # remove pure prefix-only/short tokens
            if len(ts) <= 1: continue
            clean_tokens.append(tn)

        # Date hint
        date_token = None
        for t in raw_tokens:
            m = _re.match(r'(\d{1,2})[\./\-](\d{1,2})(?:[\./\-](\d{2,4}))?$', t)
            if m:
                d, mo, y = m.groups()
                y = y or '2026'
                if len(y) == 2: y = '20' + y
                date_token = f'{int(y):04d}-{int(mo):02d}-{int(d):02d}'
                break
            m = _re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})$', t)
            if m:
                y, mo, d = m.groups()
                date_token = f'{int(y):04d}-{int(mo):02d}-{int(d):02d}'
                break

        # User intent: did they explicitly ask for assignment files?
        wants_assignments = bool(_re.search(r'מטלה|מטלות|hw|homework|תרגיל\s+\d|תרגיל\s+בית', ql))

        # Search
        candidates = []
        seen_stems: set[str] = set()
        # Choose roots: assignments only if user asked; otherwise summaries only
        if wants_assignments and not (wants_lectures or wants_tutorials):
            roots = [ASSIGNMENTS_ROOT]
        elif wants_assignments:
            roots = [SUMMARIES_ROOT, ASSIGNMENTS_ROOT]
        else:
            roots = [SUMMARIES_ROOT]
        # Look for both PDF and MD (PDF preferred — sent first)
        for root in roots:
            if not root.exists(): continue
            for pdf in list(root.rglob('*.pdf')) + list(root.rglob('*.md')):
                path_l = str(pdf).replace('\\', '/').lower()
                stem_l = pdf.stem.lower()

                # course filter
                if course_filter:
                    folders = _COURSE_FOLDERS.get(course_filter, [course_filter])
                    if not any(f in path_l for f in folders):
                        continue

                # kind filter (lectures/tutorials)
                parent_l = pdf.parent.name.lower()
                is_lecture = 'lecture' in parent_l or parent_l == 'lectures'
                is_tutorial = 'tutorial' in parent_l or parent_l == 'tutorials' or 'תרגול' in stem_l
                is_assignment = 'assignments' in path_l.split('/')
                if wants_lectures and wants_tutorials and not wants_assignments:
                    # exclude assignments
                    if is_assignment: continue
                elif wants_lectures and wants_tutorials:
                    pass  # accept all
                else:
                    if wants_lectures and not is_lecture: continue
                    if wants_tutorials and not is_tutorial: continue

                if date_token and date_token not in stem_l:
                    continue

                # Each clean token must appear (with prefix/plural tolerance).
                # Skip token matching when wants_all — course+kind filter is enough.
                stem_norm = _normalize_he(stem_l)
                if clean_tokens and not wants_all:
                    if not all(t in stem_l or t in stem_norm for t in clean_tokens):
                        continue
                # Dedup: prefer PDF over MD when both exist for same stem
                key = (str(pdf.parent), pdf.stem)
                if key in seen_stems:
                    # same stem — keep only first (PDF if comes first via list order)
                    continue
                seen_stems.add(key)
                candidates.append(pdf)

        # Sort by date (filename) desc
        def date_in_stem(p: Path) -> str:
            m = _re.search(r'(\d{4}-\d{2}-\d{2})', p.stem)
            return m.group(1) if m else ''
        candidates.sort(key=lambda p: (date_in_stem(p), p.stat().st_mtime), reverse=True)

        # Ordinal pick: Nth lecture/tutorial by ASCENDING date (one file per date)
        if ordinal_n is not None and candidates:
            by_date = {}
            for _p in candidates:
                _d = date_in_stem(_p)
                if _d and _d not in by_date:
                    by_date[_d] = _p
            _asc = sorted(by_date)
            _idx = ordinal_n - 1
            if 0 <= _idx < len(_asc):
                return [by_date[_asc[_idx]]]
            return []

        if wants_latest and candidates:
            return [candidates[0]]
        if wants_all:
            return candidates[:25]
        return candidates[:5]

    _SEND_KEYWORDS = ('שלח לי', 'תשלח לי', 'תן לי', 'תוריד לי', 'הורד לי',
                       'תשלח את', 'שלח את')

    def _looks_like_send_request(text: str) -> bool:
        return any(kw in text for kw in _SEND_KEYWORDS)

    # ---- /cancel: stop any running long task in this chat
    async def cmd_cancel(update: Update, ctx) -> None:
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        chat_id = update.effective_chat.id
        if _task_active(chat_id):
            _trigger_cancel(chat_id)
            await update.message.reply_text('🛑 ביטול נרשם — אעצור אחרי השלב הנוכחי.')
        else:
            await update.message.reply_text('אין כרגע משימה פעילה.')

    async def cmd_menu(update: Update, ctx) -> None:
        """Show quick-action inline keyboard."""
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        await update.message.reply_text(
            '🎓 *תפריט מהיר* — בחר פעולה:',
            reply_markup=_main_menu_kb(),
            parse_mode='Markdown',
        )

    async def on_callback(update: Update, ctx) -> None:
        """Inline keyboard button clicks → dispatch as if user typed the action."""
        q = update.callback_query
        if not q or not q.message:
            return
        if not _allowed(q.message.chat.id):
            return
        chat_id = q.message.chat.id
        data = q.data or ''
        await q.answer()  # remove the loading spinner on the button

        # File chooser: "pick:<idx>" from a multi-match file_send
        if data.startswith('pick:'):
            try:
                _idx = int(data.split(':', 1)[1])
            except Exception:
                return
            _paths = _LAST_CHOICES.get(chat_id) or []
            if not (0 <= _idx < len(_paths)):
                await q.message.reply_text('⚠️ הבחירה כבר לא זמינה — בקש שוב.')
                return
            _fp = Path(_paths[_idx])
            if not _fp.exists():
                await q.message.reply_text('⚠️ הקובץ כבר לא קיים.')
                return
            _LAST_FILES[chat_id] = [str(_fp)]
            await q.message.reply_text(f'📎 שולח: {_fp.stem}')
            try:
                with open(_fp, 'rb') as _fpz:
                    await q.message.reply_document(document=_fpz, filename=_fp.name)
            except Exception as _e:
                await q.message.reply_text(f'⚠️ שליחה נכשלה: {_e}')
            return

        # Summary rating from the nightly push: "rt:<short_id>:u|d"
        if data.startswith('rt:'):
            from utils.summary_feedback import key_for, record
            parts = data.split(':')
            key = key_for(parts[1]) if len(parts) > 2 else None
            if not key:
                await q.message.reply_text('⚠️ לא מצאתי את הסיכום הזה (אולי ישן מדי).')
                return
            name = key.split('/')[-1].removesuffix('.md')
            if parts[2] == 'u':
                record(key, rating='up')
                await q.message.reply_text(f'👍 נרשם — "{name}". הסיכום הבא בקורס ישמור על אותו סגנון.')
            else:
                record(key, rating='down')
                ctx.chat_data['await_summary_feedback'] = key
                await q.message.reply_text(
                    f'👎 נרשם — "{name}".\nמה לא היה טוב או מה חסר? כתוב במשפט אחד '
                    f'(או "דלג"). הסיכום הבא בקורס יתחשב בזה.')
            return

        # Flashcards: "fc:show:<card_id>" or "fc:grade:<card_id>:<grade>"
        if data.startswith('fc:'):
            parts = data.split(':')
            op = parts[1] if len(parts) > 1 else ''
            cid = parts[2] if len(parts) > 2 else ''
            from generators import sm2 as _sm2
            deck = _sm2._load()
            card = next((c for c in deck if c.get('id') == cid), None)
            if not card:
                await q.message.reply_text('⚠️ הכרטיסייה לא נמצאה.')
                return
            if op == 'show':
                # Reveal answer, keep grading buttons
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton('1️⃣ לא ידעתי', callback_data=f'fc:grade:{cid}:1'),
                        InlineKeyboardButton('3️⃣ בקושי',   callback_data=f'fc:grade:{cid}:3'),
                        InlineKeyboardButton('4️⃣ ידעתי',   callback_data=f'fc:grade:{cid}:4'),
                        InlineKeyboardButton('5️⃣ קל!',     callback_data=f'fc:grade:{cid}:5'),
                    ]
                ])
                await q.message.reply_text(
                    f'💡 *תשובה:*\n{card["a"]}',
                    parse_mode='Markdown', reply_markup=kb,
                )
                return
            if op == 'grade':
                try: grade = int(parts[3])
                except: grade = 3
                updated = _sm2.update_card(cid, grade)
                if not updated:
                    await q.message.reply_text('⚠️ עדכון נכשל.')
                    return
                next_label = updated['next_review']
                from datetime import date as _date
                days_until = (_date.fromisoformat(next_label) - _date.today()).days
                emoji = '🎉' if grade >= 4 else ('💪' if grade == 3 else '🔄')
                txt = (f'{emoji} נקלט (ציון {grade}/5)\n'
                       f'הכרטיסייה תחזור: עוד {days_until} ימים '
                       f'({next_label})')
                await q.message.reply_text(txt)
                return
            return

        # Personal-agent buttons (calendar ✅/❌)
        if data.startswith('cal:'):
            import asyncio as _aio_cb
            from agent import core as _agent
            _outs = await _aio_cb.to_thread(_agent.handle, _agent.In(f'tg:{q.message.chat_id}', 'button',
                                                                     payload=data, channel='telegram'))
            await _send_agent_outs(q.message, _outs)
            return

        # Homework reminders (utils/deadlines): "hw:done:<key>" / "hw:prac:<key>"
        if data.startswith('hw:'):
            _, op, hw_key = (data.split(':', 2) + ['', ''])[:3]
            from utils import deadlines as _dl
            if op == 'done':
                _dl.mark_done(hw_key)
                await q.message.reply_text('✅ סומן כהוגש — לא אזכיר יותר על המטלה הזו.')
                return
            if op == 'prac':
                _hw_item = _dl.find(hw_key)
                if not _hw_item:
                    await q.message.reply_text('⚠️ לא מצאתי את המטלה (אולי הוסרה מהמודל).')
                    return
                from utils.open_homework import next_assignment as _next_hw, describe as _desc_hw
                _ck_hw = _hw_item.get('course_key', '')
                _hw = next((h for h in [_next_hw(_ck_hw, horizon_days=30)] if h and _dl.key_of(h) == hw_key), None)
                await q.message.reply_text(
                    '🏋️ בונה סט תרגול לקראת המטלה (אותן מיומנויות, תרגילים אחרים)... כדקה וחצי'
                    if _hw and _hw.get('pdf') else
                    '🏋️ אין לי את קובץ המטלה, אז בונה סט על החומר האחרון בקורס... כדקה')
                import asyncio as _aio_hw
                from generators.practice_set import practice_pdf as _ppdf_hw
                from generators.practice_grader import register_sent as _reg_hw, FOLLOW_UP as _FU_hw
                try:
                    _pf = await _aio_hw.to_thread(_ppdf_hw, _ck_hw, None, None, 6,
                                                  _hw if _hw and _hw.get('pdf') else None)
                except Exception as _e_hw:
                    log.warning('hw practice failed: %r', _e_hw)
                    await q.message.reply_text(f'⚠️ לא הצלחתי לבנות סט: {_e_hw}')
                    return
                with open(_pf, 'rb') as _fp:
                    _sent_hw = await q.message.reply_document(document=_fp, filename=_pf.name,
                        caption='🏋️ סט תרגול — הפתרונות בחלק ב׳\n' + _FU_hw)
                _reg_hw(_pf.with_suffix('.md'), _ck_hw, q.message.chat_id, getattr(_sent_hw, 'message_id', None))
                return
            return

        if not data.startswith('act:'):
            return
        action_key = data[4:]
        # Map button → text we'd otherwise type
        action_map = {
            'today':           'מה חדש?',
            'list_summaries':  'תראה לי את הסיכומים',
            'assignments':     'מטלות פתוחות',
            'status':          'מה הסטטוס?',
            'quiz_chat':       'בחן אותי',
            'help':            '/help',
        }
        text = action_map.get(action_key)
        if not text:
            return
        # Fake a message-like dispatch by calling on_message via a small shim
        class _FakeMsg:
            def __init__(self, m, txt):
                self._m = m; self.text = txt; self.chat = m.chat
            def __getattr__(self, name): return getattr(self._m, name)
        class _FakeUpdate:
            def __init__(self, u, txt):
                self._u = u
                self.message = _FakeMsg(u.callback_query.message, txt)
                self.effective_chat = u.effective_chat
            def __getattr__(self, name): return getattr(self._u, name)
        await on_message(_FakeUpdate(update, text), ctx)

    # Plain message → smart router (Gemini classifies intent)
    async def on_message(update: Update, ctx) -> None:
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        if not update.message or not update.message.text:
            return
        msg_text = update.message.text
        chat_id = update.effective_chat.id

        # Follow-up to a 👎 on a summary: this message IS the feedback
        _fb_key = ctx.chat_data.pop('await_summary_feedback', None) if ctx.chat_data else None
        if _fb_key:
            if msg_text.strip() not in ('דלג', 'skip', '-'):
                from utils.summary_feedback import record
                record(_fb_key, note=msg_text)
                await update.message.reply_text('📝 תודה — ההערה נשמרה, והסיכום הבא בקורס יתחשב בה.')
            else:
                await update.message.reply_text('בסדר, דילגתי.')
            return

        # Quick cancel: if a long task is running and the message is one of the
        # cancel words, abort immediately without consulting the classifier.
        if _task_active(chat_id):
            stripped = msg_text.strip().lower().rstrip('!?.,')
            if stripped in CANCEL_WORDS:
                _trigger_cancel(chat_id)
                await update.message.reply_text('🛑 ביטול נרשם — אעצור אחרי השלב הנוכחי.')
                return

        # Record user turn in conversation history (used by classifier + RAG)
        _hist_add(chat_id, 'user', msg_text)
        history_ctx = _hist_as_context(chat_id, n=6)

        # Feynman teach-back: if an explanation session is active, this
        # message IS the explanation — grade it (bypass intent routing).
        _tb_state = None
        for _turn in reversed(_chat_history.get(chat_id, [])):
            if _turn.get('role') == 'teachback' and 'STATE:' in _turn.get('text', ''):
                try:
                    _tb_state = json.loads(_turn['text'].split('STATE:', 1)[1])
                except Exception:
                    pass
                break
        if _tb_state:
            _chat_history[chat_id] = [
                _t for _t in _chat_history.get(chat_id, []) if _t.get('role') != 'teachback']
            if msg_text.strip().rstrip('!.').lower() in ('סיים', 'די', 'עצור', 'בטל', 'stop'):
                await safe_reply(update, '👍 יצאנו ממצב הסבר.')
                return
            await update.message.reply_text('🧑\u200d🏫 בודק את ההסבר שלך מול חומר הקורס...')
            import asyncio as _aio_tb
            _fb = await _aio_tb.to_thread(
                handlers.grade_teachback,
                _tb_state.get('concept', ''), _tb_state.get('course'), msg_text)
            _hist_add(chat_id, 'bot', _fb)
            await safe_reply(update, _fb)
            return

        # The personal agent is waiting for a calendar detail ("באיזו שעה?") → it gets this message
        from agent import core as _agent
        if _agent.has_draft(f'tg:{chat_id}'):
            import asyncio as _aio_ag
            _outs = await _aio_ag.to_thread(_agent.handle, _agent.In(f'tg:{chat_id}', 'text', text=msg_text,
                                                                     channel='telegram'))
            await _send_agent_outs(update.message, _outs)
            return

        # Smart routing via Gemini
        from bot import brain

        # Step 1: resolve to standalone question (merge in history if needed)
        # E.g. "תפרט" after "מה זה משפט קיילי?" → "פרט יותר על משפט קיילי"
        resolved = brain.resolve_standalone(msg_text, history=history_ctx)
        if resolved != msg_text:
            log.info('resolved: %r → %r', msg_text, resolved)

        # Step 2: classify the resolved question (more accurate than raw msg_text)
        intent = brain.classify(resolved, history=history_ctx)
        action = intent.get('action', 'ask')
        params = intent.get('params', {}) or {}
        log.info('intent: %s | %s', action, params)
        # Use the resolved text downstream so actions get the full context
        msg_text_resolved = resolved

        # ─── Dispatch ───
        if action == 'file_send':
            query = params.get('query') or msg_text
            files = _find_files_for_query(query)
            if not files and (_is_pronoun_ref(msg_text) or _is_pronoun_ref(query)):
                files = [Path(p) for p in _LAST_FILES.get(chat_id, []) if Path(p).exists()]
            if files:
                _LAST_FILES[chat_id] = [str(x) for x in files]
            if not files:
                await safe_reply(update, '🔍 לא מצאתי קובץ שמתאים. נסה: "שלח לי הרצאה 5 מרצה_כ" או "שלח לי תרגיל 3 בדידה".')
                return
            import re as _re_pick
            _wants_all = bool(_re_pick.search(r'\bכל\b|הכל|כולם', (query or '') + ' ' + (msg_text or '')))
            if 2 <= len(files) <= 5 and not _wants_all:
                _LAST_CHOICES[chat_id] = [str(x) for x in files]
                _nums = ['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣']
                _kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f'{_nums[i]} {Path(p).stem[:38]}', callback_data=f'pick:{i}')]
                     for i, p in enumerate(_LAST_CHOICES[chat_id])])
                await update.message.reply_text(
                    f'מצאתי {len(files)} קבצים מתאימים — איזה לשלוח?', reply_markup=_kb)
                return
            _mnote = _ordinal_mismatch_note(query, files[0]) if len(files) == 1 else ''
            await update.message.reply_text(f'📎 מצאתי {len(files)} קובץ(ים), שולח...{_mnote}')
            for f in files:
                try:
                    with open(f, 'rb') as fp:
                        await update.message.reply_document(document=fp, filename=f.name)
                except Exception as e:
                    log.warning('failed to send %s: %r', f.name, e)
            return

        if action == 'email_send':
            import re as _re_em
            from config.paths import COURSE_NAMES as _CN_EMAIL
            query = params.get('query') or msg_text
            # Recipient: prefer param, else regex-extract from message, else default
            recipient = (params.get('recipient') or '').strip()
            if not recipient:
                m = _re_em.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', msg_text or '')
                if m: recipient = m.group(0)
            if not recipient:
                from config.paths import owner_email
                recipient = owner_email()
            # File type filter
            file_type = (params.get('file_type') or '').lower().strip()
            if not file_type:
                if _re_em.search(r'רק\s*(סיכומי\s*)?PDF|PDFs?\s*בלבד', msg_text or '', _re_em.IGNORECASE):
                    file_type = 'pdf'
                elif _re_em.search(r'רק\s*MD|MD\s*בלבד', msg_text or '', _re_em.IGNORECASE):
                    file_type = 'md'

            files = _find_files_for_query(query)
            if not files and (_is_pronoun_ref(msg_text) or _is_pronoun_ref(query)):
                files = [Path(p) for p in _LAST_FILES.get(chat_id, []) if Path(p).exists()]
            if files:
                _LAST_FILES[chat_id] = [str(x) for x in files]
            # Apply file_type filter
            if file_type == 'pdf':
                files = [f.with_suffix('.pdf') if f.suffix.lower() != '.pdf' else f
                         for f in files]
                files = [f for f in files if f.exists()]
            elif file_type == 'md':
                files = [f.with_suffix('.md') if f.suffix.lower() != '.md' else f
                         for f in files]
                files = [f for f in files if f.exists()]
            if not files:
                await safe_reply(update, '🔍 לא מצאתי קבצים מתאימים לשליחה במייל.')
                return
            # Validate recipient looks like an email
            if not _re_em.match(r'^[\w.+-]+@[\w-]+\.[\w.-]+$', recipient):
                await safe_reply(update, f'⚠️ כתובת המייל לא תקינה: `{recipient}`')
                return

            type_label = f' (רק {file_type.upper()})' if file_type else ''
            await update.message.reply_text(
                f'📧 שולח {len(files)} קבצים ל-{recipient}{type_label}...\n'
                f'_שלח "עצור" כדי לקטוע._',
                parse_mode='Markdown')

            # Register active task + reset cancel event
            cancel_ev = _get_cancel_event(chat_id)
            cancel_ev.clear()
            _active_tasks.add(chat_id)

            try:
                from generators.gmail_sender import GmailSender
                gmail = GmailSender()
            except Exception as e:
                _active_tasks.discard(chat_id)
                await safe_reply(update, friendly_error(e, 'התחברות ל-Gmail'))
                return
            ok_count = 0
            fail_count = 0
            cancelled = False
            for i, f in enumerate(files, 1):
                if cancel_ev.is_set():
                    cancelled = True; break
                pdf_path = f if f.suffix.lower() == '.pdf' else f.with_suffix('.pdf')
                md_path  = f if f.suffix.lower() == '.md'  else f.with_suffix('.md')
                stem = f.stem
                course = '?'
                for ck in _CN_EMAIL:
                    if ck in str(f).replace('\\', '/'):
                        course = _CN_EMAIL[ck]; break
                try:
                    # GmailSender uses Playwright Sync API — must run in worker thread
                    # to avoid "Sync API inside asyncio loop" error
                    import asyncio as _aio
                    ok = await _aio.to_thread(
                        gmail.send_summary,
                        course_name=course,
                        lecture_title=stem,
                        summary_md_path=str(md_path) if md_path.exists() else str(f),
                        summary_pdf_path=str(pdf_path) if pdf_path.exists() else None,
                        recipient=recipient,
                    )
                    if ok:
                        ok_count += 1
                    else:
                        fail_count += 1
                    # progress update every 5
                    if i % 5 == 0 or i == len(files):
                        await update.message.reply_text(f'  {i}/{len(files)} — ✅ {ok_count} | ❌ {fail_count}')
                except Exception as e:
                    fail_count += 1
                    log.warning('email send failed for %s: %r', stem, e)
            # Clear active task registration
            _active_tasks.discard(chat_id)
            header = '⏹ *נעצר!*' if cancelled else '📬 *סיימתי!*'
            await safe_reply(update,
                f'{header}\n'
                f'נשלחו: {ok_count}/{len(files)}\n'
                f'נכשלו: {fail_count}\n'
                f'יעד: {recipient}')
            return

        if action == 'homework_grade':
            # Pure-text route: user wrote "תבדוק את שיעורי הבית" without uploading anything
            # → guide them to upload a photo.
            await safe_reply(update,
                '📸 *לבדיקת שיעורי בית — שלח צילום של התרגיל הפתור* (תמונה אחת או יותר).\n\n'
                'אם יש קונטקסט (מטלה X, פרק Y, איזה קורס) — צרף ב-caption של התמונה.\n'
                'כדי לקבל גם פתרון מלא: כתוב ב-caption "תן פתרון מלא".')
            return

        if action == 'study_plan':
            course = (params.get('course') or '').strip()
            try: days = int(params.get('days') or 0)
            except: days = 0
            exam_date = (params.get('exam_date') or '').strip()
            if not course or days <= 0:
                await safe_reply(update,
                    '⚠️ לא הבנתי. דוגמה: "תכנן לי לימוד למבחן בלינארית בעוד 12 ימים"')
                return
            await update.message.reply_text(
                f'📅 בונה תוכנית ל-{course} ({days} ימים)...')
            try:
                from generators.study_planner import build_plan
                import asyncio as _aio
                plan = await _aio.to_thread(build_plan, course, days, exam_date)
                await safe_reply(update, plan)
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'תכנון לימוד'))
            return

        if action == 'ask_pdf':
            question = (params.get('question') or msg_text or '').strip()
            course = (params.get('course') or '').strip() or None
            # Strip the "PDF" / "תן לי PDF" trigger words from the question
            import re as _re_pdf
            question = _re_pdf.sub(
                r'\b(תן\s*לי\s*)?PDF\b|תייצא\s*ל?PDF|PDF\s*של|תשובה\s*כ?קובץ|כ?קובץ\s*PDF',
                '', question, flags=_re_pdf.IGNORECASE,
            ).strip(' ,.-:')
            if not question:
                await safe_reply(update,
                    '⚠️ מה השאלה? לדוגמה: "תסביר על משפט קיילי-המילטון ותן לי PDF"')
                return
            await update.message.reply_text(
                f'📄 עונה ושומר ל-PDF... 15-30 שניות.')
            try:
                from rag.chat import chat as _rag_chat
                import asyncio as _aio
                result = await _aio.to_thread(_rag_chat, question, 5, 'gemini-2.5-flash', course)
                answer = (result.get('answer') or '').strip()
                if not answer:
                    await safe_reply(update, '🔍 לא מצאתי תשובה.')
                    return
                # Build a markdown document with question + answer + sources
                lines = [
                    f'# {question[:120]}',
                    '',
                    '## תשובה',
                    '',
                    answer,
                    '',
                ]
                refs = result.get('sources') or []
                if refs:
                    lines.append('## מקורות')
                    lines.append('')
                    seen = set()
                    for r in refs[:8]:
                        ti = (r.get('title') or '').strip()
                        if not ti or ti in seen: continue
                        seen.add(ti)
                        ck = r.get('course_key', '')
                        dt = r.get('date', '')
                        lines.append(f"- **{ti}** — {ck}{f' ({dt})' if dt else ''}")
                md_text = '\n'.join(lines)
                from generators.pdf_generator import markdown_to_pdf
                from pathlib import Path as _P
                import tempfile, hashlib as _h
                stem = _h.sha1(question.encode('utf-8')).hexdigest()[:8]
                tmp_pdf = _P(tempfile.gettempdir()) / f'answer_{stem}.pdf'
                await _aio.to_thread(markdown_to_pdf, md_text, str(tmp_pdf),
                                     question[:80], course or '')
                if not tmp_pdf.exists():
                    await safe_reply(update, '⚠️ לא הצלחתי לבנות PDF.')
                    return
                size_kb = round(tmp_pdf.stat().st_size / 1024, 1)
                with open(tmp_pdf, 'rb') as fp:
                    await update.message.reply_document(
                        document=fp,
                        filename=f'answer_{stem}.pdf',
                        caption=f'📄 *תשובה כ-PDF* ({size_kb} KB)\n\nשאלה: _{question[:120]}_',
                        parse_mode='Markdown',
                    )
                try: tmp_pdf.unlink()
                except: pass
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'יצירת PDF לתשובה'))
            return

        if action == 'course_summary':
            course = (params.get('course') or '').strip()
            if not course:
                await safe_reply(update,
                    '⚠️ לא ציינת קורס. דוגמה: "סיכום-על של לינארית"')
                return
            await update.message.reply_text(
                f'📚 בונה סיכום-על מקיף ל-{course}... 1-3 דקות.')
            try:
                from generators.course_summary_builder import build_course_summary
                import asyncio as _aio
                md_path = await _aio.to_thread(build_course_summary, course, False)
                if not md_path:
                    await safe_reply(update,
                        '🔍 לא הצלחתי לבנות סיכום-על — אולי אין מספיק חומר לקורס.')
                    return
                from pathlib import Path as _P
                pdf_path = _P(str(md_path).replace('.md', '.pdf'))
                send_path = pdf_path if pdf_path.exists() else md_path
                size_kb = round(send_path.stat().st_size / 1024, 1)
                with open(send_path, 'rb') as fp:
                    await update.message.reply_document(
                        document=fp, filename=send_path.name,
                        caption=f'📚 *סיכום-על — {course}*\n({size_kb} KB)\n\n'
                                f'_שונה מ-cheatsheet: יותר מקיף עם הסברים._',
                        parse_mode='Markdown',
                    )
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'בניית סיכום-על'))
            return

        if action == 'cheatsheet':
            course = (params.get('course') or '').strip()
            if not course:
                await safe_reply(update, '⚠️ לא ציינת קורס. דוגמה: "דף עזר של לינארית"')
                return
            await update.message.reply_text(f'📋 בונה דף עזר ל-{course}...')
            try:
                from generators.cheatsheet_builder import build_cheatsheet
                import asyncio as _aio
                md_path = await _aio.to_thread(build_cheatsheet, course, False)
                if not md_path:
                    await safe_reply(update, '🔍 לא הצלחתי למצוא חומר לקורס הזה.')
                    return
                from pathlib import Path as _P
                pdf_path = _P(str(md_path).replace('.md', '.pdf'))
                send_path = pdf_path if pdf_path.exists() else md_path
                size_kb = round(send_path.stat().st_size / 1024, 1)
                with open(send_path, 'rb') as fp:
                    await update.message.reply_document(
                        document=fp, filename=send_path.name,
                        caption=f'📋 *דף עזר — {course}*\n({size_kb} KB, מתעדכן אוטומטית)',
                        parse_mode='Markdown',
                    )
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'בניית דף עזר'))
            return

        if action == 'nlm_reset':
            from bot import nlm_conversations as _nlmc
            n = _nlmc.reset(chat_id)
            await safe_reply(update,
                f'🔄 שיחה אופסה ({n} contexts נקיים). '
                f'השאלה הבאה תתחיל מאפס.' if n else
                '✅ אין שיחת NotebookLM פעילה לאיפוס.')
            return

        if action == 'notebooklm_ask':
            course = (params.get('course') or '').strip()
            scope = (params.get('scope') or 'lectures').strip()
            question = (params.get('question') or msg_text or '').strip()
            if not course:
                await safe_reply(update, '⚠️ לא ציינת קורס. דוגמה: "תשאל את NotebookLM של לינארית: מה ההבדל בין דמיון לאקוויולנטיות?"')
                return
            # Conversation continuity — reuse previous conv_id if recent
            from bot import nlm_conversations as _nlmc
            from bot import nlm_citations as _nlmcit
            prev_conv = _nlmc.get(chat_id, course, scope)
            continuation = ' (המשך שיחה)' if prev_conv else ''
            await update.message.reply_text(
                f'🎓 שולח ל-NotebookLM ({course}/{scope}){continuation}... 10-30 שניות.')
            try:
                from generators.notebooklm_agent import NotebookLMAgent
                import asyncio as _aio
                agent = NotebookLMAgent()
                result = await _aio.to_thread(agent.ask, course, scope, question, prev_conv)
                answer = (result.get('answer') or '').strip() or '_(אין תשובה)_'
                conv_id = result.get('conversation_id')
                if conv_id:
                    _nlmc.update(chat_id, course, scope, conv_id)
                refs = result.get('references') or []
                if len(answer) > 3500:
                    answer = answer[:3500] + '\n\n_(...נחתך)_'
                lines = [f'🎓 *NotebookLM אומר:*\n', answer]
                refs_md = _nlmcit.format_references(refs, max_items=5)
                if refs_md:
                    lines.append('\n\n📚 *מקורות:*')
                    lines.append(refs_md)
                lines.append('\n_💡 להמשך שיחה: שאל שוב תוך 30 דק׳. לאיפוס: "נקה שיחה"._')
                await safe_reply(update, '\n'.join(lines))
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'NotebookLM Q&A'))
            return

        if action == 'nlm_compare':
            # Multi-notebook: query 2 NotebookLMs in parallel, synthesise with Gemini
            ca = (params.get('course_a') or '').strip()
            cb = (params.get('course_b') or '').strip()
            question = (params.get('question') or msg_text or '').strip()
            if not (ca and cb and question):
                await safe_reply(update,
                    '⚠️ צריך 2 קורסים ושאלה. דוגמה: "מה ההבדל בין דמיון מטריצות '
                    'בלינארית לבין שקילות במבוא?"')
                return
            await update.message.reply_text(
                f'🎓 שואל את NotebookLM של *{ca}* ו-*{cb}* במקביל... 30-60 שניות.',
                parse_mode='Markdown')
            try:
                from generators.notebooklm_agent import NotebookLMAgent
                import asyncio as _aio
                agent = NotebookLMAgent()
                # Run both queries in parallel
                ans_a, ans_b = await _aio.gather(
                    _aio.to_thread(agent.ask, ca, 'lectures', question, None),
                    _aio.to_thread(agent.ask, cb, 'lectures', question, None),
                )
                # Synthesise with Gemini
                from google import genai
                from google.genai import types as genai_types
                key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
                client = genai.Client(api_key=key)
                synth_prompt = (
                    f"שאלה: {question}\n\n"
                    f"להלן שתי תשובות מ-NotebookLM של שני קורסים שונים. "
                    f"סנתז אותן לתשובה אחת מאוחדת ומובנית:\n"
                    f"1. הצג בקצרה את הגישה בכל קורס.\n"
                    f"2. הדגש את הדמיון/השוני.\n"
                    f"3. תן דוגמה משווה אם רלוונטי.\n\n"
                    f"=== תשובה מ-{ca} ===\n{ans_a.get('answer','')[:2000]}\n\n"
                    f"=== תשובה מ-{cb} ===\n{ans_b.get('answer','')[:2000]}"
                )
                resp = client.models.generate_content(
                    model='gemini-2.5-flash', contents=synth_prompt,
                    config=genai_types.GenerateContentConfig(
                        max_output_tokens=2000, temperature=0.3,
                        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                    ),
                )
                try:
                    from utils.usage_tracker import log_gemini
                    log_gemini(resp, 'gemini-2.5-flash', op='nlm.compare_synth')
                except Exception: pass
                synth = (resp.text or '').strip() or '_(אין סינתזה)_'
                await safe_reply(update,
                    f'🔀 *השוואה: {ca} vs {cb}*\n\n{synth}')
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'השוואת NotebookLM'))
            return

        # NotebookLM artifact generators (Study Guide / FAQ / Timeline / Mindmap)
        if action in ('nlm_study_guide', 'nlm_faq', 'nlm_timeline', 'nlm_mindmap'):
            course = (params.get('course') or '').strip()
            scope = (params.get('scope') or 'lectures').strip()
            if not course:
                await safe_reply(update,
                    '⚠️ לא ציינת קורס. דוגמה: "תייצר מדריך לימוד של לינארית"')
                return
            label_map = {
                'nlm_study_guide': ('📘', 'מדריך לימוד'),
                'nlm_faq':         ('❓', 'FAQ'),
                'nlm_timeline':    ('📅', 'ציר זמן'),
                'nlm_mindmap':     ('🧠', 'מפת מושגים'),
            }
            emoji, name = label_map[action]
            await update.message.reply_text(
                f'{emoji} מייצר {name} ל-{course} מ-NotebookLM... '
                f'זה יכול לקחת 2-5 דקות.')
            try:
                from generators.notebooklm_agent import NotebookLMAgent
                import asyncio as _aio
                agent = NotebookLMAgent()
                fn_map = {
                    'nlm_study_guide': agent.generate_study_guide,
                    'nlm_faq':         agent.generate_faq,
                    'nlm_timeline':    agent.generate_timeline,
                    'nlm_mindmap':     agent.generate_mindmap,
                }
                path = await _aio.to_thread(fn_map[action], course, scope)
                from pathlib import Path as _P
                p = _P(path)
                if not p.exists():
                    await safe_reply(update, '⚠️ NotebookLM לא הפיק קובץ.')
                    return
                size_kb = round(p.stat().st_size / 1024, 1)
                with open(p, 'rb') as fp:
                    await update.message.reply_document(
                        document=fp, filename=p.name,
                        caption=f'{emoji} *{name} — {course}*\n({size_kb} KB)',
                        parse_mode='Markdown',
                    )
            except Exception as e:
                await safe_reply(update, friendly_error(e, f'{name} מ-NotebookLM'))
            return

        if action == 'anki_export':
            course = params.get('course') or None
            await update.message.reply_text('🃏 בונה Anki deck...')
            try:
                from generators.anki_export import export_apkg
                import asyncio as _aio
                p = await _aio.to_thread(export_apkg, course_filter=course)
                size_kb = round(p.stat().st_size / 1024, 1)
                # Count cards
                from generators import sm2 as _sm2
                deck = _sm2._load()
                if course:
                    deck = [c for c in deck if c.get('course_key') == course]
                with open(p, 'rb') as fp:
                    await update.message.reply_document(
                        document=fp, filename=p.name,
                        caption=(f'🃏 *{len(deck)} כרטיסיות* ({size_kb} KB)\n\n'
                                 f'📱 *באייפון/אנדרואיד:* פתח Anki Mobile → "+" → import → בחר את הקובץ.\n'
                                 f'🖥 *בדסקטופ:* Anki → File → Import → בחר.'),
                        parse_mode='Markdown',
                    )
            except FileNotFoundError:
                await safe_reply(update,
                    '⚠️ אין עדיין כרטיסיות. הן נוצרות אוטומטית בכל יום ב-16:00, '
                    'או שלח "תכין כרטיסיות".')
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'ייצוא Anki'))
            return

        if action == 'budget_unlock':
            lock = Path(__file__).resolve().parent.parent / 'data' / 'budget_lock.json'
            if lock.exists():
                try:
                    lock.unlink()
                    await safe_reply(update,
                        '✅ *נעילת התקציב הוסרה*\n\n'
                        'Summarization יחזור לפעולה. שים לב — תקבל התראה שוב כשתעבור $20.')
                except Exception as e:
                    await safe_reply(update, friendly_error(e, 'הסרת נעילה'))
            else:
                await safe_reply(update, '✅ אין נעילת תקציב פעילה — הכל רץ כרגיל.')
            return

        if action == 'costs':
            since = (params.get('since') or '30d').strip()
            try:
                from utils.usage_tracker import summary_text
                txt = summary_text(since)
                await safe_reply(update, txt)
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'הצגת עלויות'))
            return

        if action == 'ical_send':
            # Generate fresh .ics from current assignments cache and send as document
            await update.message.reply_text('📅 מייצר קובץ לוח שנה...')
            try:
                from generators.ical_export import build_ics
                ics_path = build_ics()
                # Count events for friendly summary
                import re as _re_ics
                txt = ics_path.read_text(encoding='utf-8', errors='replace')
                n_events = len(_re_ics.findall(r'BEGIN:VEVENT', txt))
                with open(ics_path, 'rb') as fp:
                    await update.message.reply_document(
                        document=fp,
                        filename='deadlines.ics',
                        caption=(f'📅 *{n_events} דדליינים*\n\n'
                                 f'📱 *באייפון:* פתח את הקובץ → "הוסף ללוח שנה" → "סוכן לימוד".\n'
                                 f'🔄 בכל פעם שיש דדליין חדש, ההצגה תתעדכן (שלח לי "תן לי קלנדר" שוב).'),
                        parse_mode='Markdown',
                    )
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'יצירת קובץ לוח שנה'))
            return

        if action == 'zip_send':
            query = params.get('query') or msg_text
            files = _find_files_for_query(query)
            if not files:
                await safe_reply(update, '🔍 לא מצאתי קבצים לארוז.')
                return
            await update.message.reply_text(f'📦 אורז {len(files)} קבצים ב-ZIP...')
            import zipfile, tempfile
            from datetime import datetime as _dt
            ts = _dt.now().strftime('%Y%m%d_%H%M%S')
            tmp_zip = Path(tempfile.gettempdir()) / f'bundle_{ts}.zip'
            try:
                with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in files:
                        zf.write(f, arcname=f.name)
                        # also include sibling PDF if MD was found, and vice versa
                        sib = f.with_suffix('.pdf' if f.suffix == '.md' else '.md')
                        if sib.exists() and sib != f:
                            zf.write(sib, arcname=sib.name)
                size_kb = tmp_zip.stat().st_size // 1024
                with open(tmp_zip, 'rb') as fp:
                    await update.message.reply_document(
                        document=fp, filename=tmp_zip.name,
                        caption=f'📦 {len(files)} קבצים ({size_kb}KB)',
                    )
            except Exception as e:
                await safe_reply(update, f'⚠️ שגיאת ארגון ZIP: {e}')
            finally:
                try: tmp_zip.unlink()
                except: pass
            return

        if action == 'image':
            prompt = params.get('prompt') or msg_text
            await update.message.reply_text('🎨 מייצר תמונה...')
            try:
                from google import genai
                from google.genai import types as genai_types
                key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
                client = genai.Client(api_key=key)
                resp = client.models.generate_content(
                    model='gemini-2.5-flash-image-preview',
                    contents=f'Generate an educational diagram/illustration. {prompt}',
                )
                img_bytes = None
                for cand in resp.candidates or []:
                    for part in cand.content.parts or []:
                        if getattr(part, 'inline_data', None) and part.inline_data.data:
                            img_bytes = part.inline_data.data; break
                    if img_bytes: break
                if not img_bytes:
                    await safe_reply(update, '⚠️ המודל לא חזר עם תמונה.')
                    return
                tmp = Path(__file__).resolve().parent.parent / 'data' / 'tmp_image.png'
                tmp.write_bytes(img_bytes)
                with open(tmp, 'rb') as f:
                    await update.message.reply_photo(photo=f, caption=prompt[:200])
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'יצירת תמונה'))
            return

        if action in ('video', 'podcast', 'quiz', 'flashcards', 'studyguide'):
            course = params.get('course')
            if not course:
                await safe_reply(update, f'איזה קורס? (מבוא / חדו"א / בדידה / לינארית / מיקרו)')
                return
            fn_name = {'video': 'generate_video', 'podcast': 'generate_podcast',
                       'quiz': 'generate_quiz', 'flashcards': 'generate_flashcards',
                       'studyguide': 'generate_study_guide'}[action]
            emoji = {'video': '🎬', 'podcast': '🎧', 'quiz': '🧠',
                     'flashcards': '🎴', 'studyguide': '📖'}[action]
            label = {'video': 'סרטון', 'podcast': 'פודקאסט', 'quiz': 'חידון',
                     'flashcards': 'פלאשקארדים', 'studyguide': 'מדריך לימוד'}[action]
            await update.message.reply_text(f'{emoji} מייצר {label} ל-{course}... (1-3 דק׳)')
            try:
                from generators.notebooklm_agent import NotebookLMAgent
                agent = NotebookLMAgent()
                result = getattr(agent, fn_name)(course, scope='all')
                if isinstance(result, dict):
                    fp = result.get('file_path')
                    if fp and Path(fp).exists():
                        with open(fp, 'rb') as f:
                            ext = Path(fp).suffix.lower()
                            if ext in ('.mp4', '.mov', '.webm'):
                                await update.message.reply_video(video=f, caption=f'{emoji} {course}')
                            elif ext in ('.mp3', '.m4a', '.ogg', '.wav'):
                                await update.message.reply_audio(audio=f, caption=f'{emoji} {course}')
                            else:
                                await update.message.reply_document(document=f, filename=Path(fp).name)
                        return
                    if result.get('text'):
                        await safe_reply(update, result['text'][:4000]); return
                elif isinstance(result, str) and Path(result).exists():
                    with open(result, 'rb') as f:
                        await update.message.reply_document(document=f, filename=Path(result).name)
                    return
                await safe_reply(update, f'⚠️ NotebookLM לא חזר עם תוצר תקין.')
            except Exception as e:
                await safe_reply(update, friendly_error(e, f'יצירת {label} ב-NotebookLM'))
            return

        if action == 'summarize':
            # Refuse to start a second task while one is running
            if _task_active(chat_id):
                await safe_reply(update, '⏳ כבר רץ סיכום. כתוב "בטל" כדי לעצור.')
                return

            from datetime import date as _date, timedelta as _timedelta
            from pathlib import Path as _Path
            from config.paths import PANOPTO_DIR, COURSE_NAMES
            from utils.srt_quality import srt_is_good_hebrew

            course   = params.get('course')   # optional
            scope    = (params.get('scope') or 'today').lower()
            req_date = (params.get('date') or '').strip()

            # ---- 1. Resolve dates we care about
            today = _date.today()
            if req_date:
                target_dates = {req_date}
            elif scope == 'today':
                target_dates = {today.strftime('%Y-%m-%d')}
            elif scope == 'recent':
                target_dates = {(today - _timedelta(days=i)).strftime('%Y-%m-%d') for i in range(7)}
            else:
                target_dates = None  # 'latest' handled below

            courses = [course] if course else list(COURSE_NAMES.keys())

            # ---- 2. Build candidate list (course_key, date, title, srt_path)
            candidates: list[tuple[str, str, str, _Path]] = []
            for ck in courses:
                if ck not in COURSE_NAMES:
                    continue
                srts = sorted(PANOPTO_DIR.glob(f'{ck}_*.srt'))
                srts = [s for s in srts if not s.name.endswith('.bad') and not s.name.endswith('.bak')]

                # parse (date,title) from each SRT name `{ck}_YYYY-MM-DD_title.srt`
                parsed = []
                for s in srts:
                    rest = s.stem[len(ck) + 1:]
                    if len(rest) < 11 or rest[4] != '-' or rest[7] != '-':
                        continue
                    d = rest[:10]
                    t = rest[11:]
                    parsed.append((d, t, s))

                if scope == 'latest' and not req_date:
                    # newest per course (lectures only, skip tutorials)
                    lec = [p for p in parsed if 'תרגול' not in p[1] and 'Tutorial' not in p[1].lower()]
                    pool = lec or parsed
                    if pool:
                        d, t, s = sorted(pool, key=lambda x: x[0])[-1]
                        candidates.append((ck, d, t, s))
                else:
                    for d, t, s in parsed:
                        if d in target_dates:
                            candidates.append((ck, d, t, s))

            # ---- 3. Drop ones that already have summaries
            from generators.summarizer import AISummarizer
            todo = []
            for ck, d, t, s in candidates:
                item_type = 'tutorial' if ('תרגול' in t or 'tutorial' in t.lower()) else 'lecture'
                md_path = _Path(AISummarizer.build_summary_path(ck, item_type, t, d))
                if md_path.exists() and md_path.stat().st_size > 5000:
                    continue
                todo.append((ck, d, t, s, item_type, md_path))

            if not todo:
                await safe_reply(update, '✅ אין הרצאות חדשות לסכם — הכל כבר מסוכם.')
                return

            # Cap to avoid runaway
            MAX_RUN = 5
            if len(todo) > MAX_RUN:
                await update.message.reply_text(
                    f'⚠️ מצאתי {len(todo)} הרצאות, אסכם רק את {MAX_RUN} הראשונות.')
                todo = todo[:MAX_RUN]

            # Show the user EXACTLY what we're about to do + 10s cancel window
            preview_lines = [f'📝 מתכוון לסכם {len(todo)} הרצאות:']
            for ck, d, t, _s, _it, _mp in todo:
                preview_lines.append(f'  • {COURSE_NAMES.get(ck, ck)} | {d} | {t}')
            preview_lines.append('')
            preview_lines.append('⏳ מתחיל בעוד 10 שניות. כתוב "בטל" כדי לעצור.')
            await safe_reply(update, '\n'.join(preview_lines))

            # Mark active + reset cancel event, then wait grace window
            _active_tasks.add(chat_id)
            cancel_ev = _get_cancel_event(chat_id)
            cancel_ev.clear()
            try:
                try:
                    await _asyncio_mod.wait_for(cancel_ev.wait(), timeout=10.0)
                    # cancelled during grace
                    await safe_reply(update, '🛑 בוטל לפני תחילה.')
                    return
                except _asyncio_mod.TimeoutError:
                    pass  # grace ended without cancel — proceed

                # ---- 4. Run AISummarizer + send PDFs
                try:
                    import yaml as _yaml
                    _cfg = _yaml.safe_load(open('config/courses.yaml', encoding='utf-8'))
                except Exception:
                    _cfg = {'courses': {}}

                summarizer = AISummarizer()

                def _moodle_pdfs(ck: str) -> list | None:
                    pdfs = []
                    sp = (_cfg.get('courses', {}).get(ck, {}) or {}).get('sources', {}).get('static_pdf', '')
                    if sp and _Path(sp).exists():
                        pdfs.append(sp)
                    mdir = _Path(f'data/moodle/{ck}')
                    if mdir.exists():
                        pdfs += sorted(str(f) for f in mdir.glob('*.pdf'))
                    return pdfs or None

                ok_count = 0
                for ck, d, t, s, item_type, md_path in todo:
                    if cancel_ev.is_set():
                        await safe_reply(update, f'🛑 בוטל. הספקתי {ok_count}/{len(todo)}.')
                        return

                    course_name = COURSE_NAMES.get(ck, ck)
                    if not srt_is_good_hebrew(str(s)):
                        await safe_reply(update, f'⚠️ דילגתי על {course_name} {d} — SRT לא תקין (לא עברי).')
                        continue

                    await update.message.reply_text(f'🤖 {course_name} | {d} | {t}...')
                    pdf_result = None
                    try:
                        summary, sources = summarizer.generate_summary(
                            course_name=course_name,
                            lecture_title=t,
                            caption_path=str(s),
                            pdf_path=_moodle_pdfs(ck),
                            pdf_source_type='moodle',
                            video_path=None,
                        )
                        if not summary or len(summary) < 2000 or summary.startswith('# שגיאה'):
                            await safe_reply(update, f'❌ {course_name} {d} — סיכום ריק/קצר.')
                            continue
                        pdf_result = summarizer.save_summary(
                            summary=summary,
                            output_path=str(md_path),
                            course_name=course_name,
                            lecture_title=t,
                            sources=sources,
                        )
                    except Exception as e:
                        log.exception('summarize failed')
                        await safe_reply(update, friendly_error(e, f'סיכום {course_name} {d}'))
                        continue

                    # PDF fallback: if Playwright sync failed in async context, run via subprocess
                    if (not pdf_result) or (not _Path(pdf_result).exists()):
                        expected_pdf = str(md_path.with_suffix('.pdf'))
                        import sys as _sys, subprocess as _sub
                        try:
                            _sub.run(
                                [_sys.executable, 'scripts/render_pdf.py',
                                 str(md_path), expected_pdf, t, course_name],
                                cwd=str(_Path.cwd()), timeout=180, check=True,
                                capture_output=True,
                            )
                            if _Path(expected_pdf).exists():
                                pdf_result = expected_pdf
                                log.info('PDF created via subprocess fallback: %s', expected_pdf)
                        except Exception as e:
                            log.warning('subprocess PDF fallback failed: %r', e)

                    if cancel_ev.is_set():
                        await safe_reply(update, f'🛑 בוטל אחרי {course_name} {d}. הספקתי {ok_count}/{len(todo)}.')
                        return

                    # Send PDF
                    if pdf_result and _Path(pdf_result).exists():
                        try:
                            with open(pdf_result, 'rb') as fp:
                                await update.message.reply_document(
                                    document=fp, filename=_Path(pdf_result).name,
                                    caption=f'📄 {course_name} | {d} | {t}')
                            ok_count += 1
                        except Exception as e:
                            await safe_reply(update, f'⚠️ {course_name} {d} — סיכום נוצר אבל שליחה נכשלה: {e}')
                    else:
                        await safe_reply(update, f'⚠️ {course_name} {d} — סיכום נוצר אבל בלי PDF.')

                await safe_reply(update, f'✅ סיימתי. {ok_count}/{len(todo)} סיכומים נשלחו.')
            finally:
                _active_tasks.discard(chat_id)
            return

        if action == 'teach_back':
            _concept = (params.get('concept') or '').strip()
            _course = (params.get('course') or '').strip() or None
            if not _concept:
                await safe_reply(update,
                    '🧑\u200d🏫 מה תרצה להסביר לי? כתוב למשל: "אני אסביר לך את משפט קיילי-המילטון"')
                return
            _hist_add(chat_id, 'teachback',
                      'STATE:' + json.dumps({'concept': _concept, 'course': _course}, ensure_ascii=False))
            await safe_reply(update,
                f'🧑\u200d🏫 מצב פיינמן! הסבר לי את "{_concept}" במילים שלך, '
                f'כאילו אני חבר שפספס את ההרצאה. אפשר גם בהודעה קולית 🎙️\n'
                f'(כתוב "סיים" כדי לצאת)')
            return

        if action == 'quiz_chat':
            # Interactive multiple-choice quiz inside the chat.
            # State per chat is kept in _chat_history under role='quiz_state'.
            course = params.get('course')
            # If user has an active quiz answer (a/b/c/d), score it.
            stripped = msg_text.strip().lower().rstrip('!?.,').replace('"','')
            ans_letter = None
            for letter in ('א','ב','ג','ד','a','b','c','d','1','2','3','4'):
                if stripped == letter or stripped.startswith(letter + ' '):
                    ans_letter = letter; break

            # Look for active quiz state in recent history
            active_state = None
            for turn in reversed(_chat_history.get(chat_id, [])):
                if turn.get('role') == 'quiz' and 'STATE:' in turn.get('text', ''):
                    try:
                        active_state = json.loads(turn['text'].split('STATE:', 1)[1])
                        break
                    except: pass

            # Scoring path
            if ans_letter and active_state:
                correct = active_state.get('correct', '').lower()
                letter_map = {'א':'a','ב':'b','ג':'c','ד':'d','1':'a','2':'b','3':'c','4':'d'}
                normalized = letter_map.get(ans_letter, ans_letter)
                explanation = active_state.get('explanation', '')
                _q_course = active_state.get('course', '')
                _q_topic = active_state.get('topic') or active_state.get('question', '')[:120]
                _opts = active_state.get('options') or {}
                from utils.mastery import record as _mastery
                _mastery(_q_course, _q_topic, 1.0 if normalized == correct else 0.0, 'quiz')
                if normalized == correct:
                    reply = f'✅ נכון! {explanation}\n\nכתוב "עוד" לשאלה נוספת או "די" לעצור.'
                else:
                    reply = (f'❌ לא נכון. התשובה הנכונה: {correct.upper()}\n'
                            f'{explanation}\n\nכתוב "עוד" לעוד שאלה או "די" לעצור.\n'
                            f'📕 נוסף למחברת הטעויות — יחזור אליך ככרטיסייה.')
                    from utils.mistakes import record as _mistake
                    _mistake(_q_course, 'quiz', topic=_q_topic,
                             question=active_state.get('question', ''),
                             wrong=_opts.get(normalized, normalized.upper()),
                             correct=f"{_opts.get(correct, correct.upper())} — {explanation}")
                _chat_history[chat_id] = [t for t in _chat_history.get(chat_id, [])
                                          if t.get('role') != 'quiz']
                _hist_add(chat_id, 'bot', reply)
                await safe_reply(update, reply)
                return

            # Generate new quiz question
            if not course:
                # Try to infer from history
                for turn in reversed(_chat_history.get(chat_id, [])[-10:]):
                    txt = turn.get('text', '')
                    for ck_name, ck_key in (('הסתברות','probability'),('סטטיסטיקה','probability'),
                                            ('תוכנה','software_1'),('מאקרו','macroeconomics'),
                                            ('מיקרו','microeconomics_2'),('בדידה','discrete_math_2'),
                                            ('לינארית','linear_algebra_2'),('חדוא','calculus_2'),
                                            ('מבוא','intro_to_cs')):
                        if ck_name in txt:
                            course = ck_key; break
                    if course: break
            if not course:
                await safe_reply(update,
                    'איזה קורס? (הסתברות / תוכנה 1 / מיקרו / מאקרו)')
                return

            await update.message.reply_text(f'🧠 מכין שאלה בקורס...')
            try:
                from rag.vault_indexer import search as _rag_search
                # Get random relevant content from the course
                from config.paths import COURSE_NAMES as _CN
                hits = _rag_search(_CN.get(course, ''), k=20, course_filter=course)
                if not hits:
                    await safe_reply(update, '⚠️ לא מצאתי תוכן בקורס לחידון.')
                    return
                import random
                ctx_hit = random.choice(hits[:10])
                ctx_text = ctx_hit.get('text', '')[:2500]
                ctx_stem = ctx_hit.get('stem', '?')

                import os as _os
                from google import genai as _genai
                from google.genai import types as _gtypes
                key = _os.environ.get('GEMINI_API_KEY') or _os.environ.get('GOOGLE_API_KEY')
                client = _genai.Client(api_key=key)
                qprompt = (
                    f'בנה שאלה אמריקאית קצרה בעברית על החומר הבא.\n\n'
                    f'מקור (מתוך: {ctx_stem}):\n{ctx_text}\n\n'
                    f'החזר JSON בלבד עם השדות:\n'
                    f'{{"question": "...", "options": {{"a": "...", "b": "...", '
                    f'"c": "...", "d": "..."}}, "correct": "a/b/c/d", '
                    f'"explanation": "הסבר קצר למה זה נכון", "topic": "נושא השאלה בקורס, 2-5 מילים"}}'
                )
                resp = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=qprompt,
                    config=_gtypes.GenerateContentConfig(
                        max_output_tokens=600, temperature=0.5,
                        response_mime_type='application/json',
                        thinking_config=_gtypes.ThinkingConfig(thinking_budget=0),
                    ),
                )
                q = json.loads((resp.text or '').strip())
                opts = q.get('options', {})
                msg = (f'🧠 *שאלה ({_CN.get(course, course)})*\n\n'
                       f'{q.get("question", "")}\n\n'
                       f'א) {opts.get("a", "?")}\n'
                       f'ב) {opts.get("b", "?")}\n'
                       f'ג) {opts.get("c", "?")}\n'
                       f'ד) {opts.get("d", "?")}\n\n'
                       f'_ענה: א/ב/ג/ד (או a/b/c/d)_')
                # Save state
                _hist_add(chat_id, 'quiz',
                          'STATE:' + json.dumps({
                              'correct': q.get('correct', 'a'),
                              'explanation': q.get('explanation', ''),
                              'course': course,
                              'question': q.get('question', ''),
                              'options': opts,
                              'topic': q.get('topic', ''),
                          }))
                await update.message.reply_text(msg, parse_mode='Markdown')
            except Exception as e:
                log.exception('quiz_chat failed')
                await safe_reply(update, f'⚠️ לא הצלחתי לבנות שאלה: {e}')
            return

        if action == 'no_class':
            # Parse natural Hebrew date refs ("ראשון שני", "מחר", "25/05") into ISO dates
            # via Gemini, save to data/no_class_days.json.
            raw = params.get('raw') or msg_text
            action_type = params.get('action_type', 'add')

            from datetime import date as _date, datetime as _dt, timedelta as _td
            from pathlib import Path as _P
            import json as _j

            today = _date.today()
            try:
                from google import genai as _g
                from google.genai import types as _gt
                key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
                client = _g.Client(api_key=key)
                parse_prompt = (
                    f'התאריך היום: {today.isoformat()} (יום ' +
                    ['שני','שלישי','רביעי','חמישי','שישי','שבת','ראשון'][today.weekday()] + ').\n'
                    f'פרש את ההודעה הבאה בעברית ל-רשימת תאריכי ISO (YYYY-MM-DD) של ימים בלי לימודים.\n'
                    f'אם המשתמש כותב יום בשבוע (ראשון/שני/...) — קח את היום הקרוב הבא של אותו יום.\n'
                    f'אם רב-יומי ("ראשון שני") — כל הימים בטווח.\n'
                    f'הודעה: «{raw}»\n\n'
                    f'החזר JSON: {{"dates": ["YYYY-MM-DD", ...], "reason": "תיאור קצר"}}'
                )
                resp = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=parse_prompt,
                    config=_gt.GenerateContentConfig(
                        max_output_tokens=300, temperature=0.0,
                        response_mime_type='application/json',
                        thinking_config=_gt.ThinkingConfig(thinking_budget=0),
                    ),
                )
                data_parsed = json.loads((resp.text or '{}').strip())
                dates_list = data_parsed.get('dates', [])
                reason = data_parsed.get('reason', '')
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'פירוש התאריכים'))
                return

            if not dates_list:
                await safe_reply(update,
                    '🤔 לא הצלחתי להבין את התאריכים. נסה: "אין לימודים ראשון 25/5" / '
                    '"אין לימודים מחר" / "אין לימודים ראשון שני השבוע".')
                return

            # Load existing
            nc_path = _P('data') / 'no_class_days.json'
            nc_path.parent.mkdir(exist_ok=True)
            current = {}
            if nc_path.exists():
                try: current = _j.loads(nc_path.read_text(encoding='utf-8'))
                except: current = {}
            dates_set = set(current.get('dates', []))

            if action_type == 'remove':
                removed = dates_set & set(dates_list)
                dates_set -= set(dates_list)
                msg = f'🗑️ הוסרו {len(removed)} ימים מרשימת "ללא לימודים":\n' + \
                      '\n'.join(f'  • {d}' for d in sorted(removed)) if removed else \
                      '⚠️ אף אחד מהתאריכים לא היה ברשימה.'
            else:
                added = set(dates_list) - dates_set
                dates_set |= set(dates_list)
                msg = (f'✅ נשמרו {len(added)} ימים כ"ללא לימודים" ({reason}):\n' +
                       '\n'.join(f'  • {d} ({["שני","שלישי","רביעי","חמישי","שישי","שבת","ראשון"][_dt.fromisoformat(d).weekday()]})'
                                for d in sorted(added))
                       if added else '✅ כל התאריכים כבר היו ברשימה.')
                msg += '\n\nהקרון ידלג על הימים האלה (אין סנכרון, אין סיכומים).'

            current['dates'] = sorted(dates_set)
            current['updated_at'] = _dt.now().isoformat()
            current['updated_by'] = 'telegram'
            nc_path.write_text(_j.dumps(current, ensure_ascii=False, indent=2),
                               encoding='utf-8')
            await safe_reply(update, msg)
            return

        if action == 'list_no_class':
            from pathlib import Path as _P
            import json as _j
            nc_path = _P('data') / 'no_class_days.json'
            if not nc_path.exists():
                await safe_reply(update, '📅 אין ימי "ללא לימודים" שמורים.')
                return
            try:
                data = _j.loads(nc_path.read_text(encoding='utf-8'))
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'קריאת רשימת חופשים'))
                return
            dates = data.get('dates', [])
            if not dates:
                await safe_reply(update, '📅 אין ימי "ללא לימודים" שמורים.')
                return
            from datetime import date as _date, datetime as _dt
            today = _date.today()
            future = sorted([d for d in dates if d >= today.isoformat()])
            past   = sorted([d for d in dates if d < today.isoformat()], reverse=True)
            lines = [f'📅 *ימים בלי לימודים ({len(dates)} סה"כ):*\n']
            if future:
                lines.append('🔜 עתידיים:')
                for d in future[:15]:
                    wd = ['שני','שלישי','רביעי','חמישי','שישי','שבת','ראשון'][_dt.fromisoformat(d).weekday()]
                    lines.append(f'  • {d} ({wd})')
                lines.append('')
            if past:
                lines.append(f'(עברו: {len(past)} ימים)')
            await safe_reply(update, '\n'.join(lines))
            return

        if action == 'reminder':
            from bot import reminders as rem
            raw = params.get('raw') or msg_text
            # strip leading "תזכיר לי" type triggers
            import re as _re
            raw = _re.sub(r'^(תזכיר לי|תיצור תזכורת|זכור|תזכורת)\s+', '', raw).strip()
            text = rem.handle_remind(raw, chat_id)
            await safe_reply(update, text)
            return

        if action == 'assignments':
            text = handlers.handle_grades() + '\n\n━━━━━━━━━━━\n' + handlers.handle_deadline()
            await safe_reply(update, text)
            return

        if action == 'today':
            await safe_reply(update, handlers.handle_today())
            return

        if action == 'status':
            from pathlib import Path as _Path
            from datetime import datetime as _dt
            lines = ['📊 סטטוס מערכת']
            # Last pipeline run
            log_path = _Path('data/daily_full_log.txt')
            if log_path.exists():
                mtime = _dt.fromtimestamp(log_path.stat().st_mtime)
                lines.append(f'• ריצה אחרונה: {mtime.strftime("%d/%m/%Y %H:%M")}')
            else:
                lines.append('• ריצה אחרונה: אין לוג זמין')
            # Active tasks
            if _task_active(chat_id):
                lines.append('• כרגע רץ: סיכום (אפשר לבטל עם "בטל")')
            else:
                lines.append('• כרגע פעיל: אין משימה')
            # Cron next runs
            lines.append('• קרון יומי: 09:00 ו-18:00 (שעון ישראל)')
            # Latest summaries
            from config.paths import SUMMARIES_DIR, COURSE_NAMES
            recent = []
            for ck in COURSE_NAMES:
                d = SUMMARIES_DIR / ck / 'lectures'
                if not d.exists():
                    continue
                files = sorted(d.glob('*.md'), key=lambda p: p.stat().st_mtime)[-1:]
                for f in files:
                    recent.append((f.stat().st_mtime, ck, f.stem))
            recent.sort(reverse=True)
            if recent:
                lines.append('')
                lines.append('סיכומים אחרונים:')
                for _, ck, stem in recent[:5]:
                    lines.append(f'  • {COURSE_NAMES.get(ck, ck)}: {stem}')
            await safe_reply(update, '\n'.join(lines))
            return

        if action == 'list_summaries':
            from pathlib import Path as _Path
            from config.paths import SUMMARIES_DIR, COURSE_NAMES
            ck_filter = params.get('course')
            courses_to_show = [ck_filter] if ck_filter and ck_filter in COURSE_NAMES else list(COURSE_NAMES.keys())
            lines = ['📚 סיכומים זמינים:']
            total = 0
            for ck in courses_to_show:
                d = SUMMARIES_DIR / ck / 'lectures'
                if not d.exists():
                    continue
                files = sorted(d.glob('*.md'), key=lambda p: p.stem, reverse=True)
                if not files:
                    continue
                lines.append('')
                lines.append(f'━━ {COURSE_NAMES.get(ck, ck)} ━━')
                for f in files[:10]:  # 10 newest per course
                    lines.append(f'  • {f.stem}')
                    total += 1
                if len(files) > 10:
                    lines.append(f'  …ועוד {len(files) - 10}')
            if total == 0:
                await safe_reply(update, '🔍 לא נמצאו סיכומים.')
                return
            lines.append('')
            lines.append(f'סה"כ: {total} סיכומים. בקש "שלח לי את X" כדי לקבל קובץ.')
            await safe_reply(update, '\n'.join(lines))
            return

        if action == 'sync':
            await update.message.reply_text('🔄 מסנכרן...')
            try:
                text = handlers.handle_sync('')
                await safe_reply(update, text)
            except Exception as e:
                await safe_reply(update, friendly_error(e, 'סנכרון'))
            return

        if action == 'exam_report':
            from config.paths import COURSE_NAMES as _CN_ER
            _c = (params.get('course') or '').strip() or None
            if _c and _c not in _COURSE_FOLDERS:
                _c = _COURSE_ALIASES.get(_strip_he_prefix(_c), _c)
            _targets = [_c] if (_c and _c in _COURSE_FOLDERS) else list(_CN_ER.keys())
            from generators.exam_report import build_exam_report as _ber
            import asyncio as _aio_er
            if len(_targets) > 1:
                await update.message.reply_text(f'📝 בונה דוח מבחן ל-{len(_targets)} קורסים (כ-{len(_targets)*15} שניות)...')
            else:
                await update.message.reply_text('📝 בונה דוח מבחן... ~15 שניות')
            for _ck in _targets:
                _rep = await _aio_er.to_thread(_ber, _ck)
                await safe_reply(update, _rep)
            return

        if action == 'topic_guide':
            _topic = (params.get('topic') or '').strip()
            _c = (params.get('course') or '').strip() or None
            if _c and _c not in _COURSE_FOLDERS:
                _c = _COURSE_ALIASES.get(_strip_he_prefix(_c), _c)
            if not _topic:
                await safe_reply(update, 'איזה נושא ללמד מאפס? למשל: "תכין לי מדריך מאפס לטורי פוריה".')
                return
            await update.message.reply_text(f'📘 בונה מדריך מאפס ל"{_topic[:40]}"... ~20 שניות')
            from generators.topic_guide import build_topic_guide as _btg
            import asyncio as _aio_tg
            _g = await _aio_tg.to_thread(_btg, _topic, _c if _c in _COURSE_FOLDERS else None)
            _hist_add(chat_id, 'bot', _g[:1500])
            import re as _re_tg
            _want_mail = bool(_re_tg.search(r'מייל|אימייל|מיל|email', msg_text or ''))
            try:
                from generators.pdf_generator import markdown_to_pdf as _m2p
                from pathlib import Path as _P_tg
                _gd = _P_tg('data/topic_guides'); _gd.mkdir(parents=True, exist_ok=True)
                _safe = _re_tg.sub(r'[^\w \u0590-\u05FF-]', '', _topic)[:40].strip() or 'guide'
                _pf = _gd / (f'מדריך - {_safe}.pdf')
                await _aio_tg.to_thread(_m2p, _g, str(_pf), f'מדריך מאפס: {_topic}', '')
                with open(_pf, 'rb') as _fp:
                    await update.message.reply_document(document=_fp, filename=_pf.name,
                        caption=f'📘 מדריך מאפס (ממוקד-מבחן): {_topic}')
                if _want_mail:
                    from generators.gmail_sender import GmailSender as _GS_tg
                    from config.paths import owner_email as _owner_email
                    _rec = _owner_email()
                    _body = f"<div dir='rtl' style='font-family:Arial'><h2>📘 מדריך מאפס: {_topic}</h2><p>מצורף כ-PDF, ממוקד למבחן.</p></div>"
                    _ok = await _aio_tg.to_thread(lambda: _GS_tg().send_email(
                        to=_rec, subject=f'📘 מדריך מאפס: {_topic}', body_html=_body, attachment_path=str(_pf)))
                    await update.message.reply_text('📧 גם נשלח למייל שלך.' if _ok else '⚠️ שליחת המייל נכשלה.')
            except Exception as _e_tg:
                log.warning('topic_guide pdf failed: %r', _e_tg)
                await safe_reply(update, _g)
            return

        if action in ('calendar_add', 'calendar_list'):           # personal agent (agent/core.py)
            import asyncio as _aio_cal
            from agent import core as _agent
            _outs = await _aio_cal.to_thread(_agent.handle_action, f'tg:{chat_id}', action, params,
                                             msg_text, 'telegram')
            await _send_agent_outs(update.message, _outs)
            return

        if action == 'practice_set':
            _c = (params.get('course') or '').strip() or None
            if _c and _c not in _COURSE_FOLDERS:
                _c = _COURSE_ALIASES.get(_strip_he_prefix(_c), _c)
            if not _c or _c not in _COURSE_FOLDERS:
                await safe_reply(update, 'באיזה קורס לתרגל? למשל: "תן לי תרגילים בהסתברות" או "סט תרגול על התפלגות בינומית בהסתברות".')
                return
            _topic = (params.get('topic') or '').strip() or None
            try:
                _n = int(params.get('n') or 6)
            except (TypeError, ValueError):
                _n = 6
            # no specific topic → aim at the open assignment, if there is one with a PDF
            from utils.open_homework import next_assignment as _next_hw, describe as _desc_hw
            _hw = None if _topic else _next_hw(_c)
            _hw = _hw if _hw and _hw.get('pdf') else None
            if params.get('homework') and not _hw:
                await update.message.reply_text('ℹ️ אין מטלה פתוחה (עם קובץ) בקורס הזה ב-10 הימים הקרובים — בונה סט על החומר האחרון.')
            await update.message.reply_text(
                '🏋️ בונה סט תרגול עם פתרונות מלאים'
                + (f' על "{_topic[:40]}"' if _topic else '')
                + (f'\n🎯 מכוון למטלה: {_desc_hw(_hw)} (אותן מיומנויות, תרגילים אחרים)' if _hw else '')
                + '... כדקה')
            import asyncio as _aio_ps
            from generators.practice_set import practice_pdf as _ppdf
            try:
                _pf = await _aio_ps.to_thread(_ppdf, _c, _topic, None, _n, _hw)
            except ValueError as _e_ps:          # no material yet — a normal answer, not an error
                await safe_reply(update, f'🤷 {_e_ps}')
                return
            except Exception as _e_ps:
                log.warning('practice_set failed: %r', _e_ps)
                await safe_reply(update, '⚠️ בניית סט התרגול נכשלה. נסה שוב בעוד רגע.')
                return
            from generators.practice_grader import register_sent as _reg_ps, FOLLOW_UP as _FU_PS
            with open(_pf, 'rb') as _fp:
                _sent_ps = await update.message.reply_document(document=_fp, filename=_pf.name,
                    caption='🏋️ סט תרגול — קודם לפתור לבד, הפתרונות בחלק ב׳\n' + _FU_PS)
            _reg_ps(_pf.with_suffix('.md'), _c, chat_id, getattr(_sent_ps, 'message_id', None))
            _hist_add(chat_id, 'bot', f'[שלחתי סט תרגול PDF: {_pf.name}]')
            return

        if action == 'mistakes':
            _c = (params.get('course') or '').strip() or None
            if _c and _c not in _COURSE_FOLDERS:
                _c = _COURSE_ALIASES.get(_strip_he_prefix(_c), _c)
            if not _c or _c not in _COURSE_FOLDERS:
                await safe_reply(update, 'של איזה קורס? למשל: "מחברת טעויות בהסתברות".')
                return
            from utils.mistakes import notebook_md as _nb_md
            _md_nb = _nb_md(_c)
            if not _md_nb:
                await safe_reply(update, '📕 מחברת הטעויות בקורס הזה עוד ריקה — היא מתמלאת מטעויות בסטי תרגול, '
                                         'בשיעורי בית ובחידון.')
                return
            import asyncio as _aio_nb
            from generators.pdf_generator import markdown_to_pdf as _m2p_nb
            from config.paths import COURSE_NAMES as _CN_NB
            _nb_dir = Path('data/mistakes_pdf'); _nb_dir.mkdir(parents=True, exist_ok=True)
            _nb_pdf = _nb_dir / f'מחברת טעויות - {_c}.pdf'
            await _aio_nb.to_thread(_m2p_nb, _md_nb, str(_nb_pdf), 'מחברת טעויות', _CN_NB.get(_c, _c))
            with open(_nb_pdf, 'rb') as _fp:
                await update.message.reply_document(document=_fp, filename=_nb_pdf.name,
                    caption='📕 מחברת הטעויות — רק מה שעוד לא תוקן, לפי נושא')
            return

        if action == 'mastery_map':
            _c = (params.get('course') or '').strip() or None
            if _c and _c not in _COURSE_FOLDERS:
                _c = _COURSE_ALIASES.get(_strip_he_prefix(_c), _c)
            from config.paths import ACTIVE_COURSES as _AC_MM
            _targets_mm = [_c] if (_c and _c in _COURSE_FOLDERS) else list(_AC_MM)
            import asyncio as _aio_mm
            from utils.mastery import map_text as _map_text
            await update.message.reply_text('🗺️ בונה מפת שליטה...')
            for _ck in _targets_mm:
                _txt = await _aio_mm.to_thread(_map_text, _ck)
                await safe_reply(update, _txt)
            return

        if action == 'coverage_map':
            _course = (params.get('course') or '').strip() or None
            if _course and _course not in _COURSE_FOLDERS:
                _course = _COURSE_ALIASES.get(_strip_he_prefix(_course), _course)
            if not _course or _course not in _COURSE_FOLDERS:
                await safe_reply(update, 'לאיזה קורס? למשל: "מפת כיסוי של מיקרו" או "אילו נושאים ירדו בלינארית".')
                return
            await update.message.reply_text('🗺️ בונה מפת כיסוי (תכנית הקורס מול מה שנלמד)... ~20 שניות')
            import asyncio as _aio_cm
            from generators.coverage_map import build_coverage_map as _bcm
            _cm = await _aio_cm.to_thread(_bcm, _course)
            _hist_add(chat_id, 'bot', _cm)
            await safe_reply(update, _cm)
            return

        if action == 'coverage_check':
            _topic = (params.get('topic') or msg_text_resolved or '').strip()
            _course = (params.get('course') or '').strip() or None
            if not _topic:
                await safe_reply(update, 'איזה נושא לבדוק? למשל: "האם למדנו משפט קיילי-המילטון בלינארית?"')
                return
            await update.message.reply_text(f'🔎 בודק אם "{_topic[:50]}" נלמד בפועל...')
            import asyncio as _aio_cov
            _cov = await _aio_cov.to_thread(handlers.check_coverage, _topic, _course)
            _hist_add(chat_id, 'bot', _cov)
            await safe_reply(update, _cov)
            return

        if action == 'greeting':
            await safe_reply(update, 'היי! אני כאן לעזור עם החומר. שאל אותי שאלה, בקש קובץ, או שלח הקלטה קולית.')
            return

        # Default → RAG. Use the resolved standalone question (already merged
        # with conversation context by brain.resolve_standalone).
        text = handlers.handle_ask(msg_text_resolved)
        # Remember the top cited source file so "שלח לי אותה" works after a question
        try:
            _srcs = getattr(handlers, 'LAST_ASK_SOURCES', []) or []
            _found = []
            for _s in _srcs[:2]:
                _stem = (_s.get('stem') or '').strip()
                if not _stem:
                    continue
                for _ext in ('.pdf', '.md'):
                    _hits = list(SUMMARIES_ROOT.rglob(_stem + _ext))
                    if _hits:
                        _found.append(str(_hits[0]))
                        break
            if _found:
                _LAST_FILES[chat_id] = _found[:1]
        except Exception:
            pass
        _hist_add(chat_id, 'bot', text)
        await safe_reply(update, text)

    async def on_voice(update: Update, ctx) -> None:
        """Voice message → Whisper → route through full message pipeline (brain.py).
        Supports ANY action (ask, file_send, email_send, ical, costs, etc.)."""
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        if not update.message or not update.message.voice:
            return
        try:
            await update.message.reply_text('🎙️ מתמלל...')
            voice_file = await update.message.voice.get_file()
            tmp_path = Path(__file__).resolve().parent.parent / 'data' / 'tmp_voice.ogg'
            tmp_path.parent.mkdir(exist_ok=True)
            await voice_file.download_to_drive(custom_path=str(tmp_path))
            # Audio duration for usage tracking (Telegram voice messages carry duration)
            voice_secs = float(getattr(update.message.voice, 'duration', 0) or 0)
            from openai import OpenAI
            client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
            with open(tmp_path, 'rb') as f:
                transcript = client.audio.transcriptions.create(
                    model='whisper-1',
                    file=f,
                    language='he',
                )
            try:
                from utils.usage_tracker import log_whisper
                log_whisper(audio_seconds=voice_secs, op='bot.voice_message')
            except Exception: pass
            question = (transcript.text or '').strip()
            if not question:
                await update.message.reply_text('⚠️ לא הצלחתי להבין את ההקלטה.')
                return
            await update.message.reply_text(f'📝 שמעתי: _"{question}"_',
                                            parse_mode='Markdown')

            # Voice-to-Voice: if it's a question-style message, after handling
            # we'll also synthesize the bot's reply and send as an audio note.
            q_lower = question.lower()
            wants_audio_reply = (
                q_lower.startswith(('מה', 'איך', 'למה', 'מי', 'מתי', 'איפה', 'תסביר'))
                and len(question) > 8
            )

            # Inject the transcribed text into the same path as a typed message
            update.message.text = question
            await on_message(update, ctx)

            # Voice-to-Voice reply
            if wants_audio_reply:
                try:
                    history = _chat_history.get(update.effective_chat.id, [])
                    last_bot = next((t['text'] for t in reversed(history)
                                     if t.get('role') == 'bot'), None)
                    if last_bot and len(last_bot) > 30:
                        # Strip markdown formatting before TTS
                        import re as _re_md
                        clean = _re_md.sub(r'\*([^*]+)\*', r'\1', last_bot)
                        clean = _re_md.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', clean)
                        clean = _re_md.sub(r'`([^`]+)`', r'\1', clean)
                        clean = _re_md.sub(r'#+\s*', '', clean)
                        clean = _re_md.sub(r'[🎓📚📖💡✅🚨📋⚡🔢]+\s*', '', clean)
                        # Drop "מקורות" block — too noisy in audio
                        clean = clean.split('מקורות:')[0].split('—')[0].strip()
                        if len(clean) > 100:
                            from generators.tts_gemini import synthesize
                            import asyncio as _aio
                            audio_path = await _aio.to_thread(synthesize, clean)
                            with open(audio_path, 'rb') as fp:
                                await update.message.reply_voice(voice=fp)
                except Exception as _tts_err:
                    log.warning('voice-to-voice TTS failed: %r', _tts_err)
        except Exception as e:
            log.exception('voice processing failed')
            await update.message.reply_text(friendly_error(e, 'עיבוד הקלטה קולית'))

    app.add_handler(CommandHandler('start',    cmd_start))
    app.add_handler(CommandHandler('cancel',   cmd_cancel))
    app.add_handler(CommandHandler('menu',     cmd_menu))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(CommandHandler('help',     make_cmd(handlers.handle_help)))
    app.add_handler(CommandHandler('ask',      cmd_ask))
    app.add_handler(CommandHandler('today',    make_cmd(handlers.handle_today)))
    app.add_handler(CommandHandler('grades',   make_cmd(handlers.handle_grades)))
    app.add_handler(CommandHandler('deadline', make_cmd(handlers.handle_deadline)))
    app.add_handler(CommandHandler('sync',     make_cmd(handlers.handle_sync)))
    app.add_handler(CommandHandler('preview',  cmd_preview))
    app.add_handler(CommandHandler('remind',   cmd_remind))
    app.add_handler(CommandHandler('image',    cmd_image))
    app.add_handler(CommandHandler('video',      cmd_video))
    app.add_handler(CommandHandler('podcast',    cmd_podcast))
    app.add_handler(CommandHandler('quiz',       cmd_quiz))
    app.add_handler(CommandHandler('flashcards', cmd_flashcards))
    app.add_handler(CommandHandler('studyguide', cmd_studyguide))
    # Detect caption that requests homework grading (vs generic image analysis)
    _GRADE_TRIGGERS = (
        'תבדוק', 'בדוק', 'תעבור על', 'תוודא', 'נכון', 'תגיד אם',
        'תן ציון', 'שיעורי בית', 'שיעורי בית הפתור', 'הפתרון', 'פתרון שלי',
        'התרגיל הזה', 'תבחן', 'תאמת', 'check', 'grade',
    )

    async def _send_agent_outs(message, outs) -> None:
        """Send agent.core Out(...) items to a Telegram chat."""
        from telegram import InlineKeyboardButton as _IKB, InlineKeyboardMarkup as _IKM
        for o in outs:
            try:
                if o.kind == 'document' and o.path:
                    with open(o.path, 'rb') as _fp:
                        _m = await message.reply_document(document=_fp, filename=Path(o.path).name,
                                                          caption=(o.text or '')[:1000])
                    if o.register_practice:
                        from generators.practice_grader import register_sent
                        register_sent(o.register_practice[0], o.register_practice[1], message.chat_id,
                                      getattr(_m, 'message_id', None))
                elif o.kind == 'buttons':
                    await message.reply_text(o.text, reply_markup=_IKM(
                        [[_IKB(lbl, callback_data=p) for lbl, p in o.buttons]]))
                else:
                    await message.reply_text(o.text)
            except Exception as e:
                log.warning('agent out failed: %r', e)

    async def _grade_practice(update: Update, entry: dict) -> None:
        import asyncio as _aio_pg
        from generators.practice_grader import grade as _grade_pg
        try:
            await update.message.reply_text(
                f"📝 בודק את הפתרון ({len(entry['paths'])} תמונות)..." if len(entry['paths']) > 1
                else '📝 בודק את הפתרון...')
            r = await _aio_pg.to_thread(_grade_pg, entry['set']['md'], entry['set']['course'],
                                        entry['paths'], entry['n'])
            await safe_reply(update, r['text'])
        except Exception as e:
            log.exception('practice grading failed')
            await safe_reply(update, friendly_error(e, 'בדיקת פתרון לסט תרגול'))
        finally:
            for _p in entry['paths']:
                try: _p.unlink()
                except OSError: pass

    async def _grade_album_later(update: Update, ctx, gid: str) -> None:
        import asyncio as _aio_al
        await _aio_al.sleep(3)          # the rest of the album arrives as separate updates meanwhile
        entry = ctx.chat_data.get('practice_albums', {}).pop(gid, None)
        if entry:
            await _grade_practice(update, entry)

    async def _maybe_practice_photo(update: Update, ctx) -> bool:
        """True when the photo was taken by the practice grader (reply to a practice set,
        or a "תרגיل N" caption while a set is active)."""
        import re as _re_pp
        import asyncio as _aio_pp
        msg = update.message
        chat_id = update.effective_chat.id
        gid = msg.media_group_id
        albums = ctx.chat_data.setdefault('practice_albums', {})
        caption = (msg.caption or '').strip()
        if gid and gid in albums:                                   # later photo of an album
            p = Path(__file__).resolve().parent.parent / 'data' / f'tmp_photo_{msg.message_id}.jpg'
            await (await msg.photo[-1].get_file()).download_to_drive(custom_path=str(p))
            albums[gid]['paths'].append(p)
            m = _re_pp.search(r'תרגיל\s*(\d+)', caption)
            if m and not albums[gid]['n']:
                albums[gid]['n'] = int(m.group(1))
            return True
        from generators.practice_grader import resolve_set as _resolve
        m = _re_pp.search(r'תרגיל\s*(\d+)', caption)
        reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
        course_hint = next((ck for w, ck in _COURSE_ALIASES.items() if w in caption.split()), None)
        found = _resolve(chat_id, reply_id, course_hint)
        if not found or not (m or found['via_reply']):
            return False
        p = Path(__file__).resolve().parent.parent / 'data' / f'tmp_photo_{msg.message_id}.jpg'
        p.parent.mkdir(exist_ok=True)
        await (await msg.photo[-1].get_file()).download_to_drive(custom_path=str(p))
        entry = {'paths': [p], 'n': int(m.group(1)) if m else None, 'set': found}
        if gid:
            albums[gid] = entry
            _aio_pp.get_running_loop().create_task(_grade_album_later(update, ctx, gid))
        else:
            await _grade_practice(update, entry)
        return True

    async def on_photo(update: Update, ctx) -> None:
        """Photo → practice-set grading (reply to a set / "תרגיל N"), homework grading
        (caption says "תבדוק"/"נכון"/...) or generic Vision analysis."""
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        if not update.message or not update.message.photo:
            return
        try:
            if await _maybe_practice_photo(update, ctx):
                return
        except Exception as e:
            log.exception('practice photo routing failed')
            await safe_reply(update, friendly_error(e, 'בדיקת פתרון לסט תרגול'))
            return
        tmp_path = None
        try:
            caption = (update.message.caption or '').strip()
            caption_lower = caption.lower()
            is_grading = any(t in caption or t in caption_lower for t in _GRADE_TRIGGERS)

            mode_label = '📝 בודק שיעורי בית' if is_grading else '🖼️ מנתח את התמונה'
            await update.message.reply_text(f'{mode_label}...')

            # Pick highest-resolution variant
            best = update.message.photo[-1]
            photo_file = await best.get_file()
            tmp_path = Path(__file__).resolve().parent.parent / 'data' / f'tmp_photo_{update.message.message_id}.jpg'
            tmp_path.parent.mkdir(exist_ok=True)
            await photo_file.download_to_drive(custom_path=str(tmp_path))

            if is_grading:
                # Homework grading path — use the dedicated grader
                from generators.homework_grader import grade_homework
                want_full = any(p in caption for p in
                                ('פתרון מלא', 'תן פתרון', 'תפתור', 'הראה פתרון'))
                # Detect course hint
                course_hint = ''
                for ck_word, ck_he in [
                    ('לינארית', 'לינארית 2'), ('חדואא', 'חדו"א 2'), ('חדו"א', 'חדו"א 2'),
                    ('בדידה', 'בדידה 2'), ('מבוא', 'מבוא מורחב'),
                    ('כלכלה', 'מיקרו א\''), ('מיקרו', 'מיקרו א\''),
                ]:
                    if ck_word in caption:
                        course_hint = ck_he; break
                import asyncio as _aio
                feedback = await _aio.to_thread(
                    grade_homework,
                    [tmp_path], course_hint, caption, want_full,
                )
                await safe_reply(update, feedback)
                return

            # Generic image-analysis path (board, diagram, etc)
            instruction = caption or (
                'נתח את התמונה. אם זו שאלת מבחן/תרגיל — פתור צעד-צעד עם הסבר. '
                'אם זה לוח/שקף — חלץ את הטקסט והסבר את המושג העיקרי. '
                'אם זה גרף/דיאגרמה — תאר ופרש. תענה בעברית.'
            )
            from google import genai
            from google.genai import types as genai_types
            key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
            client = genai.Client(api_key=key)
            img_bytes = tmp_path.read_bytes()
            resp = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[
                    genai_types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg'),
                    instruction,
                ],
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=2500, temperature=0.2,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            try:
                from utils.usage_tracker import log_gemini
                log_gemini(resp, 'gemini-2.5-flash', op='bot.photo_analyze')
            except Exception: pass
            txt = (resp.text or '').strip()
            if not txt:
                await safe_reply(update, '⚠️ לא הצלחתי לנתח את התמונה.')
                return
            await safe_reply(update, txt)
        except Exception as e:
            log.exception('photo processing failed')
            await safe_reply(update, friendly_error(e, 'ניתוח תמונה / בדיקת שיעורי בית'))
        finally:
            try:
                if tmp_path: tmp_path.unlink()
            except Exception: pass

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    async def on_document(update: Update, ctx) -> None:
        """PDF upload → homework grading (if caption suggests it) else generic analysis."""
        if not update.effective_chat or not _allowed(update.effective_chat.id):
            return
        if not update.message or not update.message.document:
            return
        doc = update.message.document
        # Only handle PDFs for now
        mime = (doc.mime_type or '').lower()
        if 'pdf' not in mime and not (doc.file_name or '').lower().endswith('.pdf'):
            return  # ignore other doc types silently
        tmp_path = None
        try:
            caption = (update.message.caption or '').strip()
            is_grading = any(t in caption or t in caption.lower() for t in _GRADE_TRIGGERS)
            if not is_grading:
                # Nudge for clarity — we don't auto-analyze random PDF uploads
                await safe_reply(update,
                    '📎 קיבלתי PDF. כדי שאבדוק כשיעורי בית — שלח שוב עם caption "תבדוק".')
                return
            await update.message.reply_text('📝 בודק את ה-PDF...')
            tmp_path = Path(__file__).resolve().parent.parent / 'data' / f'tmp_hw_{update.message.message_id}.pdf'
            tmp_path.parent.mkdir(exist_ok=True)
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(custom_path=str(tmp_path))

            from generators.homework_grader import grade_homework
            want_full = any(p in caption for p in
                            ('פתרון מלא', 'תן פתרון', 'תפתור', 'הראה פתרון'))
            course_hint = ''
            for ck_word, ck_he in [
                ('לינארית', 'לינארית 2'), ('חדואא', 'חדו"א 2'), ('חדו"א', 'חדו"א 2'),
                ('בדידה', 'בדידה 2'), ('מבוא', 'מבוא מורחב'),
                ('כלכלה', 'מיקרו א\''), ('מיקרו', 'מיקרו א\''),
            ]:
                if ck_word in caption:
                    course_hint = ck_he; break
            import asyncio as _aio
            feedback = await _aio.to_thread(
                grade_homework, [tmp_path], course_hint, caption, want_full,
            )
            await safe_reply(update, feedback)
        except Exception as e:
            log.exception('document processing failed')
            await safe_reply(update, friendly_error(e, 'בדיקת PDF'))
        finally:
            try:
                if tmp_path: tmp_path.unlink()
            except Exception: pass

    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    # ─── Reminder watcher (background asyncio task) ───
    # Use asyncio.create_task (not application.create_task) so the task runs
    # immediately in the current event loop without needing the app to be "started".
    async def _post_init(application):
        import asyncio
        from bot import reminders as rem
        asyncio.get_event_loop().create_task(
            rem.watcher(application.bot, ALLOWED_IDS)
        )
        # Heartbeat: every 60s touch /tmp/bot_heartbeat — memory_watchdog reads this
        async def _heartbeat():
            from pathlib import Path as _P
            hb = _P('/tmp/bot_heartbeat')
            while True:
                try: hb.touch()
                except Exception: pass
                await asyncio.sleep(60)
        asyncio.get_event_loop().create_task(_heartbeat())
    app.post_init = _post_init

    log.info(
        'Telegram bot starting. Allowlist: %s',
        ALLOWED_IDS or '(empty — bot is LOCKED; only /start replies with your chat_id)',
    )
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    # callback_query is REQUIRED for every inline button (menu, flashcard grades,
    # summary 👍/👎) — with ['message'] only, Telegram never delivered button taps.
    app.run_polling(allowed_updates=['message', 'callback_query'])


def main() -> None:
    """Outer loop — keeps the bot alive forever with exponential backoff."""
    import time as _time
    from telegram.error import NetworkError, TimedOut
    attempt = 0
    while True:
        attempt += 1
        try:
            _run_bot()
            break  # clean shutdown (SIGINT / SIGTERM)
        except SystemExit:
            raise  # pass through explicit exits (bad config, etc.)
        except KeyboardInterrupt:
            log.info('Bot stopped by user.')
            break
        except (NetworkError, TimedOut) as e:
            # Transient network hiccup — exponential backoff up to 5 min
            wait = min(300, 5 * (2 ** min(attempt - 1, 6)))
            log.warning('Network error (attempt %d): %s — retry in %ds', attempt, e, wait)
            _time.sleep(wait)
            # Fresh event loop for next attempt
            try:
                import asyncio
                asyncio.set_event_loop(asyncio.new_event_loop())
            except Exception:
                pass
        except Exception as e:
            # Any other crash — wait up to 5 min, then try again
            wait = min(300, 30 * min(attempt, 10))
            log.error('Unexpected crash (attempt %d): %s — retry in %ds', attempt, e, wait,
                      exc_info=True)
            _time.sleep(wait)
            try:
                import asyncio
                asyncio.set_event_loop(asyncio.new_event_loop())
            except Exception:
                pass


if __name__ == '__main__':
    main()
