"""
utils/gemini_sub.py — Gemini calls on the owner's Google AI Pro subscription instead of the paid API.

03/10/2026: The owner's explicit choice, aware of the risk that automated use of a personal account could get it flagged.
The bot's text calls went through the Gemini CLI.
03/11/2026: Google closed the Gemini CLI to personal accounts ("migrate to the Antigravity suite"), so every call had
quietly fallen back to the API. The owner: "אני רוצה להשתמש בחשבון פרו שיש לי". The backend is now the Antigravity CLI
(`agy`, signed in once with the dedicated Google account). It runs as the isolated `geminiagent` user, which has no sudo, can't read
the project or .env, and can't reach localhost or the tailnet (deploy/claude_agent/claudeagent-firewall.sh). It runs
in plan mode in an empty folder, through /usr/local/bin/ssa-run-agy (deploy/gemini_agent/run_agy.sh). Plan mode is agy's
own promise and nobody has proven it holds, so agy counts as an agent that can act: what contains it is that user and
that firewall, and text from anyone but the owner stays off it (api_only).

    ask(prompt) → the answer, or None (not on the server / resting / quota / error); the caller then uses the API

Two ways in:
  • install(): with SSA_BOT_VIA_GEMINI_SUB=1, wraps generate_content in a chat process (the WhatsApp worker). Only
    text prompts go to the subscription. Images, files and Parts stay on the API. So does everything under api_only():
    groups, friends, the partner, contacts, mail, documents, web pages, and the owner's assistant call while mail is in its context.
    A new call that reads anyone else's text belongs under it too: this wrapper can't tell whose text it was given.
  • a direct ask() from background jobs (site pages, summaries of the Claude sessions).
Lecture summaries and board reading stay on the API.
After a failure it rests REST_SECONDS, so a spent quota doesn't slow every call. DAILY_CAP leaves some of the
subscription for the owner's own use. Each call is counted in data/gemini_sub_usage.json.
11/11: 2-3 site-build calls a night hung to the 330 s kill, and each one sent the rest of the build to the paid API for
an hour (the API answers the same prompts in ~20 s, so it was a stuck agy, not the prompt). A hung call isn't a spent
quota: a background caller retries it once (retries=1), and a timeout rests TIMEOUT_REST, not an hour. At most SLOTS agy
runs at once in a process (the site build asked six in parallel). The last calls' seconds are kept under 'recent'.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USAGE = ROOT / 'data' / 'gemini_sub_usage.json'
RUNNER = Path('/usr/local/bin/ssa-run-agy')            # root-owned; runs agy as geminiagent
MODEL = 'gemini-3.8-flash-low'
TIMEOUT = int(os.environ.get('SSA_GEMINI_SUB_TIMEOUT', '60'))    # chat calls; background jobs pass their own
DAILY_CAP = 1000
REST_SECONDS = 3600                      # quota / login / agy errors
TIMEOUT_REST = 600                       # a call that hung: agy is stuck, not out of quota
SLOTS = threading.BoundedSemaphore(int(os.environ.get('SSA_GEMINI_SUB_SLOTS', '2')))
_stats = {'sub': 0, 'fallback': 0}
_NOTE_LOCK = threading.Lock()
API_ONLY: ContextVar[bool] = ContextVar('ssa_api_only', default=False)   # 05/11 text that must not reach agy


@contextmanager
def api_only():
    """Inside it nothing goes to the subscription, and nothing that fails here can rest it. agy is an agent, so a
    WhatsApp group's words never go to it (agent/group_mode.py)."""
    t = API_ONLY.set(True)
    try:
        yield
    finally:
        API_ONLY.reset(t)


class _Resp:
    """Just enough of a google-genai response for our callers (.text; no usage → logged as free)."""
    def __init__(self, text: str):
        self.text = text
        self.usage_metadata = None
        self.candidates = []


def _text_only(contents) -> str | None:
    if isinstance(contents, str):
        return contents
    if isinstance(contents, list) and all(isinstance(c, str) for c in contents):
        return '\n\n'.join(contents)
    return None                                  # images / files / Parts → API


def _wants_json(config) -> bool:
    try:
        return 'json' in str(getattr(config, 'response_mime_type', '') or '')
    except Exception:
        return False


def _usage(path: Path | None = None) -> dict:
    try:
        return json.loads((path or USAGE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _timed_out(why: str) -> bool:
    return 'timeout' in (why or '').lower()


def _note(ok: bool, tokens: int = 0, why: str = '', path: Path | None = None, now: float | None = None,
          secs: float | None = None, rest: bool = True) -> None:
    """rest=False: a failed try that will be tried again — counted, but it doesn't rest anyone."""
    p = path or USAGE
    with _NOTE_LOCK:                                         # the worker calls ask() from two threads (the prefetch)
        u = _usage(p)
        day = u.setdefault(date.today().isoformat(), {'calls': 0, 'failed': 0, 'tokens': 0})
        day['calls' if ok else 'failed'] += 1
        day['tokens'] += tokens
        if not ok:
            if rest:
                u['rest_until'] = (now or time.time()) + (TIMEOUT_REST if _timed_out(why) else REST_SECONDS)
            u['last_error'] = why[:300]
        if secs is not None:                                 # how long agy takes: the evidence for TIMEOUT / SLOTS
            u['recent'] = (u.get('recent') or [])[-49:] + [[int(now or time.time()), round(secs, 1), ok, tokens]]
        for k in sorted(k for k in u if re.fullmatch(r'\d{4}-\d{2}-\d{2}', k))[:-30]:
            u.pop(k)                                         # a month of history is enough
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f'{p.name}.{os.getpid()}.tmp')
            tmp.write_text(json.dumps(u, ensure_ascii=False, indent=1), encoding='utf-8')
            tmp.replace(p)
        except OSError:
            pass


def _no_new_privs() -> bool:
    """A hardened service (NoNewPrivileges=yes) can't sudo to geminiagent. It mustn't try, fail and rest everyone."""
    try:
        return 'NoNewPrivs:\t1' in Path('/proc/self/status').read_text()
    except OSError:
        return False


def available(path: Path | None = None, now: float | None = None) -> bool:
    if os.environ.get('PYTEST_CURRENT_TEST') or os.environ.get('SSA_NO_SUBSCRIPTION'):
        return False
    u = _usage(path)
    if (now or time.time()) < u.get('rest_until', 0):
        return False
    return ((u.get(date.today().isoformat()) or {}).get('calls', 0) < DAILY_CAP and RUNNER.exists()
            and not _no_new_privs())


def parse(out: str) -> tuple[str | None, int, str]:
    """agy's stream-json output → (the answer or None, tokens, error)."""
    for ln in reversed((out or '').splitlines()):
        if '"event":"result"' not in ln.replace(' ', ''):
            continue
        try:
            r = json.loads(ln)['result']
        except (ValueError, KeyError, TypeError):
            continue
        tokens = int((r.get('usage') or {}).get('total_tokens') or 0)
        if r.get('status') == 'SUCCESS' and (r.get('response') or '').strip():
            return r['response'].strip(), tokens, ''
        return None, tokens, str(r.get('error') or r.get('status') or 'empty')
    return None, 0, (out or '')[-200:] or 'no result'


def json_of(text: str | None) -> str | None:
    """A JSON answer without the ```json fence an agent likes to add.
    14/11: the fence is the OUTER one. Software 1's site topics hold Java code blocks (```java … ```) inside the JSON
    strings; the first-``` pair cut the answer down to a code snippet, so "תכנות מונחה-חוזים וחבילות ב-Java" failed
    as 'bad json' every night. A bare JSON answer is returned whole; a fenced one up to its LAST fence."""
    if not text:
        return None
    t = text.strip()
    if t[:1] in '{[':
        return t
    m = re.search(r'```(?:json)?[ \t]*\n?\s*([{\[].*)```', t, re.S)      # greedy: up to the last ```
    if m:
        return m.group(1).strip()
    a = min((i for i in (t.find('{'), t.find('[')) if i >= 0), default=-1)
    b = max(t.rfind('}'), t.rfind(']'))
    return t[a:b + 1].strip() if 0 <= a < b else t


def ask(prompt: str, json_mode: bool = False, model: str = MODEL, timeout: int | None = None, run=None,
        path: Path | None = None, retries: int = 0) -> str | None:
    """One subscription call (a hung one tried again up to `retries` times). None on any problem (the caller falls
    back to the API)."""
    if API_ONLY.get() or run is None and not available(path):
        return None
    t = timeout or TIMEOUT
    if json_mode:
        prompt += '\n\nחשוב: החזר JSON תקין בלבד, בלי טקסט נוסף ובלי ```.'
    msg = json.dumps({'event': 'user', 'message': {'content': prompt}}, ensure_ascii=False) + '\n'
    for attempt in range(retries + 1):
        last = attempt == retries
        t0 = time.monotonic()
        try:
            with SLOTS:
                if run:
                    out = run(msg, model, t)
                else:                                # the runner unsets GEMINI_API_KEY / GOOGLE_API_KEY: login only
                    out = subprocess.run(['sudo', '-n', '-u', 'geminiagent', '-H', str(RUNNER), model, str(t)],
                                         input=msg.encode('utf-8'), capture_output=True,
                                         timeout=t + 30).stdout.decode('utf-8', 'replace')
        except (OSError, subprocess.SubprocessError) as e:
            hung = isinstance(e, subprocess.TimeoutExpired)
            _note(False, why=repr(e), path=path, secs=time.monotonic() - t0, rest=last or not hung)
            if hung and not last:
                print(f'  ⚠️ gemini-sub: no answer in {t}s — trying the subscription once more')
                continue
            print(f'  ⚠️ gemini-sub: {e!r} — the API for the next {(TIMEOUT_REST if hung else REST_SECONDS) // 60} min')
            return None
        text, tokens, err = parse(out)
        hung = text is None and _timed_out(err)
        _note(text is not None, tokens, err, path=path, secs=time.monotonic() - t0, rest=last or not hung)
        if text is None:
            if hung and not last:
                print(f'  ⚠️ gemini-sub: no answer in {t}s — trying the subscription once more')
                continue
            print(f'  ⚠️ gemini-sub failed ({err[:120]}) — the API for the next '
                  f'{(TIMEOUT_REST if hung else REST_SECONDS) // 60} min')
            return None
        return json_of(text) if json_mode else text
    return None


def _wrap(original):
    def generate_content(self, *, model, contents, config=None, **kw):
        prompt = None if API_ONLY.get() else _text_only(contents)
        if prompt is not None and len(prompt) < 200_000 and not getattr(config, 'tools', None):   # Google Search → API
            text = ask(prompt, json_mode=_wants_json(config))
            if text:
                _stats['sub'] += 1
                try:                             # visible in the ledger, at $0 (subscription, not API)
                    from utils.usage_tracker import log_usage
                    log_usage(service='gemini-subscription', model=f'agy-{MODEL}',
                              op='bot.subscription', extra={'chars_in': len(prompt), 'chars_out': len(text)})
                except Exception:
                    pass
                return _Resp(text)
        _stats['fallback'] += 1
        return original(self, model=model, contents=contents, config=config, **kw)

    generate_content._ssa_sub = True
    return generate_content


def install() -> bool:
    """Wrap generate_content in this process: text prompts → subscription, everything else → API."""
    if os.environ.get('SSA_BOT_VIA_GEMINI_SUB') != '1' or not available():
        return False
    from google.genai import models as _models
    original = _models.Models.generate_content             # the budget guard, when installed: only the API pays
    if getattr(original, '_ssa_sub', False):
        return True
    _models.Models.generate_content = _wrap(original)
    print('gemini-sub: bot conversations go through the Gemini Pro subscription (Antigravity; API fallback)')
    return True
