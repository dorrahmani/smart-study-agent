"""
agent/claude_job.py — "קלוד: <task>" from WhatsApp → Claude Code works on the project, on the owner's
Claude subscription, as the isolated `claudeagent` user (no sudo; can't read /home/ubuntu, .env, keys).

  start()  : hands the agent a fresh bundle of production `main` + the task; runs
             /home/claudeagent/agent_job.sh in the background (one job at a time)
  poll()   : finished jobs → their result (summary, diffstat, commits, test line)
  deploy() : only after the owner's "1": push job-<id> to the real repo → the normal pre-receive test gate
             decides; nothing else can reach production
  discard(), diff() : the other two options

05/11 the owner: "better control" over his Claude account:
  advance()  : the worker's tick, day and night: a run stopped by a usage limit is parked with its session id
               (it survives restarts) and goes on by itself in the same session when the limit resets; whatever
               arrives meanwhile waits in a queue and runs once, in order. Bounded, and it never deploys.
  control()  : "מה קלוד עושה" / "תעצור את קלוד" / "תמשיך עכשיו" / "תבטל מהתור 2"
  notices()  : the few messages about all that (held during quiet hours; only the latest per job)

Owner-only (agent/core.py routes partners elsewhere).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:                      # Windows (the tests); production is Linux
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'claude_jobs.json'
QUEUE = ROOT / 'data' / 'claude_queue.json'     # {'items': [...], 'limit_until': ts, 'notices': [...]}
LOCK = ROOT / 'data' / 'claude_jobs.lock'       # the worker and the nightly review's cron both write the state
AGENT_STATE = ROOT / 'data' / 'agent_state.json'  # agent/core.py's: "עצור סוכן" lives there
BARE = Path('/home/ubuntu/smart_study_agent.git')
AGENT_HOME = Path('/home/claudeagent')
SCRIPT = AGENT_HOME / 'agent_job.sh'
TRIGGER = re.compile(r'^\s*(?:קלוד|claude)\s*[:,]\s*(.+)$', re.I | re.S)
IL = ZoneInfo('Asia/Jerusalem')

RESET_MARGIN_S = 120                     # the limit is really open a moment after the time it names
RETRY_S = 30 * 60                        # the reset time is unknown → try again every half hour…
UNKNOWN_BOUND_S = 12 * 3600              # …for this long
MAX_WAKES = 6                            # a job may go on after a known reset this many times (each is a 5-hour window)
MAX_PARK_S = 8 * 86400                   # the weekly limit fits; nothing waits longer
QUIET_MAX_WAIT_S = 12 * 3600             # the nightly review is stale by then: it just ends, tomorrow's will come
MAX_QUEUE = 10
MAX_QUEUE_TRIES = 3
MAX_LAUNCH_FAILS = 6                     # the job can't even be started (half-hourly): that's 3 hours
MAX_REVIVES = 2                          # a run killed from outside (a reboot) goes on this many times
DEAD_AFTER_S = 120                       # no process and no result this long after a launch = it died
LOST_AFTER_S = 40 * 60
STALE_REPORT_S = 48 * 3600               # a bare digit deploys nothing older than this: the id is written out
RESULT_CAP = 256 * 1024                  # what the job's own files may cost us
STREAM_CAP = 8 * 1024 * 1024             # from the end: that's where the result line is
READ_TIMEOUT_S = 10
GIT_TIMEOUT_S = 60
REPO_CAP = 512 * 1024 * 1024             # the raw copy of the job's repo: past this it's cut, and a cut copy is refused

CONTINUE_PROMPT = ('The usage limit that stopped you has reset. Continue the same task from where you stopped. Your '
                   'partial work is still in this working copy (see git status and git diff): do not start over and do '
                   'not redo finished steps. Then run the safety tests, commit on the current branch (no push), and '
                   'finish with the same SHORT summary in Hebrew for WhatsApp.')
INTERRUPTED_PROMPT = ('Your run was cut off from outside (the server restarted), not by anything you did. Continue the '
                      'same task from where you stopped. Your partial work is still in this working copy (see git status '
                      'and git diff): do not start over. Then run the safety tests, commit on the current branch (no '
                      'push), and finish with the same SHORT summary in Hebrew for WhatsApp.')
RESTART_NOTE = ('An earlier attempt at this task was stopped by a usage limit and its session could not be resumed. Its '
                'partial work is still in this working copy (see git status, git diff base and git log base..HEAD). '
                'Keep what is good and finish the task; do not start over blindly.')


_held = threading.RLock()
_depth = 0


@contextmanager
def _lock():
    """One writer at a time across the worker and the review's cron. Reentrant."""
    global _depth
    with _held:
        if _depth or fcntl is None:
            _depth += 1
            try:
                yield
            finally:
                _depth -= 1
            return
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        with open(LOCK, 'a', encoding='utf-8') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            _depth += 1
            try:
                yield
            finally:
                _depth -= 1
                fcntl.flock(f, fcntl.LOCK_UN)


def _write(path: Path, obj) -> None:
    """Whole or not at all: a parked job in a half-written file would be a lost job."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load() -> dict:
    try:
        st = json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) else {}


def _save(st: dict) -> None:
    _write(STATE, st)


def _load_q() -> dict:
    try:
        q = json.loads(QUEUE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        q = {}
    q = q if isinstance(q, dict) else {}
    return {'items': q.get('items') or [], 'limit_until': q.get('limit_until') or 0, 'notices': q.get('notices') or []}


def _save_q(q: dict) -> None:
    _write(QUEUE, q)


def _sh(*args, check=True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, encoding='utf-8', errors='replace', check=check, **kw)


_AGENT = ('sudo', '-n', '-u', 'claudeagent', '-H')


def _as_agent(*args, check=True, **kw) -> subprocess.CompletedProcess:
    return _sh(*_AGENT, *args, check=check, **kw)


def _read_job(path: Path, cap: int = RESULT_CAP) -> str | None:
    """A file the job could have written: as the agent (a link to a secret is just denied), capped, with a timeout (a
    FIFO would hang the worker, a link to /dev/zero never ends). Anything but a plain read → None."""
    try:
        r = _as_agent('timeout', str(READ_TIMEOUT_S), 'tail', '-c', str(cap), str(path), check=False,
                      timeout=READ_TIMEOUT_S + 5)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout if r.returncode == 0 else None


def task_of(text: str) -> str | None:
    m = TRIGGER.match(text or '')
    return m.group(1).strip() if m else None


def running() -> dict | None:
    return next((j for j in _load().values() if j.get('status') == 'running'), None)


def parked(st: dict | None = None) -> tuple[str, dict] | None:
    """The job waiting for the limit to reset → (id, job)."""
    return next(((k, j) for k, j in (_load() if st is None else st).items() if j.get('status') == 'parked'), None)


def _busy(st: dict) -> bool:
    return any(j.get('status') in ('running', 'parked') for j in st.values())


# ── what the CLI says when a usage limit stops it (read from the installed 2.1.289 + two real jobs of 20/10) ────────
#   text:   "You've hit your weekly limit · resets 6pm (Asia/Jerusalem)"; "session limit" is the 5-hour one; the time is
#           "6pm" / "6:30pm" within 24 hours, "Nov 6, 6pm" further on, "Jan 2, 2027, 6pm" in another year; exit code 1
#   stream: {"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":<unix>,"rateLimitType":"five_hour"}}
#           then a {"type":"result","is_error":true,"result":<that text>}; the assistant line carries "error":"rate_limit"
LIMIT_TEXT_RX = re.compile(r"^\s*(?:You['’]ve (?:hit|reached) your\b|You['’]re out of (?:usage credits|extra usage)\b|"
                           r'Your org is out of usage\b)')
RESET_RX = re.compile(r'\bresets?\s+(?:at\s+|on\s+)?(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})'
                      r'(?:,\s*(?P<year>\d{4}))?(?:,|\s+at)?\s+(?:at\s+)?)?(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*'
                      r'(?P<ap>[ap]m)\b(?:\s*\((?P<tz>[^)]+)\))?', re.I)
RESET_IN_RX = re.compile(r'\bresets?\s+in\s+(?:(?P<d>\d+)\s*d\w*\s*)?(?:(?P<h>\d+)\s*h\w*\s*)?(?:(?P<m>\d+)\s*m\w*)?', re.I)
MONTHS = dict(zip('jan feb mar apr may jun jul aug sep oct nov dec'.split(), range(1, 13)))
RESUME_FAILED_RX = re.compile(r'No conversation found with session ID|Failed to resume session')


def parse_reset(text: str, now: float | None = None) -> float | None:
    """The time inside the CLI's limit message → epoch seconds, or None when it doesn't say."""
    now = time.time() if now is None else now
    m = RESET_IN_RX.search(text or '')
    if m and any(m.groups()):
        return now + 86400 * int(m['d'] or 0) + 3600 * int(m['h'] or 0) + 60 * int(m['m'] or 0)
    m = RESET_RX.search(text or '')
    if not m:
        return None
    try:
        tz = ZoneInfo((m['tz'] or '').strip()) if m['tz'] else IL
    except Exception:                    # an unknown zone name (or a short one like "IST")
        tz = IL
    hour = int(m['h']) % 12 + (12 if m['ap'].lower() == 'pm' else 0)
    minute = int(m['mi'] or 0)
    cur = datetime.fromtimestamp(now, tz)
    try:
        if m['mon']:
            month = MONTHS.get(m['mon'].lower())
            if not month:
                return None
            at = datetime(int(m['year'] or cur.year), month, int(m['day']), hour, minute, tzinfo=tz)
            if not m['year'] and at.timestamp() < now - 86400:   # "Jan 2" said in December
                at = at.replace(year=cur.year + 1)
        else:
            at = cur.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if at.timestamp() <= now:
                at += timedelta(days=1)
    except ValueError:
        return None
    return at.timestamp()


def digest(stream: str) -> dict:
    """The stream-json of one run → its final text and what it said about limits. The file is the job's: nothing in it
    is assumed to have the right type, and a line that isn't what it should be is skipped."""
    d = {'text': '', 'result': '', 'events': 0, 'is_error': False, 'rejected': None, 'rate_error': False, 'api_status': None}
    last = ''
    for line in (stream or '').splitlines():
        try:
            ev = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(ev, dict):
            continue
        d['events'] += 1
        kind = ev.get('type')
        if kind == 'rate_limit_event':
            info = ev.get('rate_limit_info')
            d['rejected'] = info if isinstance(info, dict) and info.get('status') == 'rejected' else None
        elif kind == 'assistant' and not ev.get('parent_tool_use_id'):
            msg = ev.get('message')
            parts = msg.get('content') if isinstance(msg, dict) else None
            said = ' '.join(c['text'] for c in parts if isinstance(c, dict) and isinstance(c.get('text'), str)) \
                if isinstance(parts, list) else ''
            last = said.strip() or last
            d['rate_error'] = ev.get('error') == 'rate_limit'
        elif kind == 'result':
            r, code = ev.get('result'), ev.get('api_error_status')
            d['result'] = r.strip() if isinstance(r, str) else ''
            d['is_error'] = ev.get('is_error') is True
            d['api_status'] = code if isinstance(code, int) and not isinstance(code, bool) else None
    d['text'] = d['result'] or last          # a timeout has no result line
    return d


def _sane_reset(at, now: float) -> float | None:
    """A reset time we'd really wait for: a number, in the future, inside the longest wait; milliseconds are read as such."""
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    at = float(at) / 1000 if at > 1e11 else float(at)
    return at if now < at <= now + MAX_PARK_S else None


def detect_limit(status: str, summary: str, d: dict, now: float | None = None) -> dict | None:
    """Only a run that FAILED, and the text only from the CLI's own `result` line: what the model last said is the
    model's, and a task about limits would make it say exactly these words. No stream at all = the old text mode."""
    now = time.time() if now is None else now
    if status == 'timeout' or (status == 'done' and not d.get('is_error')):
        return None
    msg = (d.get('result') or ('' if d.get('events') else summary) or '').strip()
    said = len(msg) <= 400 and bool(LIMIT_TEXT_RX.match(msg))
    rej = d.get('rejected')
    if not (said or rej or d.get('rate_error') or d.get('api_status') == 429):
        return None
    resets = _sane_reset((rej or {}).get('resetsAt'), now)
    if resets is None and said:
        resets = _sane_reset(parse_reset(msg, now), now)
    return {'resets_at': resets, 'text': msg[:200]}


def _when(ts: float, now: float | None = None) -> str:
    """HH:MM Israel time, with the date when it isn't today."""
    at, cur = datetime.fromtimestamp(ts, IL), datetime.fromtimestamp(time.time() if now is None else now, IL)
    return at.strftime('%H:%M') if at.date() == cur.date() else at.strftime('%d/%m %H:%M')


def _label(j: dict) -> str:
    """A job or a queue item in a few words."""
    if j.get('kind') == 'scout':
        return 'סריקת שיפור עצמי'
    if j.get('kind') == 'improve':
        return ('שיפור עצמי: ' + (j.get('title') or ''))[:70]
    if j.get('quiet'):
        return 'סקירה יומית'
    task = (j.get('task') or '').replace(REDO_NOTE, '')
    return re.sub(r'^בקשת שיפור מהמשתמש \([^)]*\):\s*', '', task).strip()[:70]


def _note(q: dict, jid: str, chat: str, text: str) -> None:
    """One slot per job: a parked night that ended by morning leaves only what's still true."""
    q['notices'] = [n for n in q['notices'] if n.get('id') != jid]
    if text:
        q['notices'].append({'id': jid, 'chat': chat, 'text': text})


def notices() -> list[tuple[str, str]]:
    """(chat, text) to send now. The worker calls this outside quiet hours only."""
    with _lock():
        q = _load_q()
        if not q['notices']:
            return []
        out = [(n['chat'], n['text']) for n in q['notices'] if n.get('chat')]
        q['notices'] = []
        _save_q(q)
        return out


def _enqueue(q: dict, chat: str, task: str, quiet: bool, kind: str | None = None) -> str:
    if any(i['task'] == task for i in q['items']):
        return '📥 זה כבר בתור, ירוץ פעם אחת.'
    if len(q['items']) >= MAX_QUEUE and not quiet:
        return f'⚠️ התור מלא ({MAX_QUEUE}). אפשר לפנות עם "תבטל מהתור".'
    item = {'id': secrets.token_hex(3), 'task': task, 'chat': chat, 'quiet': quiet, 'added': time.time(), 'tries': 0}
    if kind:
        item['kind'] = kind
    if quiet:                            # only tonight's review counts, and the owner's own tasks go first; 14/11 the
        q['items'] = [i for i in q['items'] if not (i.get('quiet') and i.get('kind') == kind)]  # loop's fixes go last
        at = next((n for n, i in enumerate(q['items']) if i.get('kind') == 'improve'), len(q['items']))
        q['items'].insert(at, item)
    else:
        at = next((n for n, i in enumerate(q['items']) if i.get('quiet')), len(q['items']))
        q['items'].insert(at, item)
    _save_q(q)
    if quiet:
        return 'queued'
    place = q['items'].index(item) + 1
    if q['limit_until'] > time.time():
        return f'📥 המכסה נגמרה, אז זה בתור (מקום {place}). ירוץ לבד אחרי {_when(q["limit_until"])}.'
    return f'📥 קלוד עסוק, אז זה בתור (מקום {place}). ירוץ לבד כשיתפנה.'


def _put(path: Path, data: str | bytes) -> None:
    """A file into the agent's tree, written AS the agent: root following a link planted there could write anywhere."""
    body = data.encode('utf-8') if isinstance(data, str) else data
    subprocess.run([*_AGENT, 'sh', '-c', 'umask 077; cat > "$0"', str(path)], input=body, capture_output=True,
                   check=True, timeout=120)


def _launch(jid: str, mode: str, session: str) -> None:
    """Its own transient unit: inside the worker's cgroup (KillMode=control-group, and sudo here opens no systemd
    session) every worker restart, each ✅ deploy included, killed the run. The same user, so the same firewall."""
    r = _sh('sudo', '-n', 'systemd-run', f'--unit=claude-job-{jid}-{secrets.token_hex(2)}', '--collect', '--quiet',
            '--uid=claudeagent', '--gid=claudeagent', '--setenv=TZ=Asia/Jerusalem', f'--working-directory={AGENT_HOME}',
            str(SCRIPT), jid, mode, session, check=False)
    if r.returncode == 0:
        return
    print(f'  ⚠️ systemd-run failed ({r.stderr[-120:]!r}): the job runs under the worker')
    subprocess.Popen([*_AGENT, str(SCRIPT), jid, mode, session],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _can_resume() -> bool:
    """agent_job.sh is installed by hand: until version 2 is there, a job can only start over. Not being able to read
    it raises (the caller tries later): starting over wipes the working copy, so that takes really seeing version 1."""
    r = _sh('sudo', '-n', 'cat', str(SCRIPT), check=False)
    if r.returncode != 0:
        raise OSError(f'cannot read {SCRIPT}')
    return 'AGENT_JOB_VERSION=2' in r.stdout


def _snapshot(jid: str, wip: bool = False) -> bool:
    """Everything the job did so far as one diff in its folder. wip: it won't go on, so uncommitted work becomes a commit
    on its branch, or "saved in the branch" wouldn't be true. Never blocks the caller for long. → really saved."""
    ws, out = AGENT_HOME / 'ws', AGENT_HOME / 'jobs' / jid / 'partial.diff'
    try:
        _as_agent('rm', '-f', str(ws / '.git' / 'index.lock'), check=False, timeout=GIT_TIMEOUT_S)    # a killed git's
        _as_agent('sh', '-c', f'cd {ws} && git add -N . && git diff base > {out}; git reset -q', check=False,
                  timeout=GIT_TIMEOUT_S)
        if not wip:
            return True
        return _as_agent('sh', '-c', f'cd {ws} && [ "$(git rev-parse --abbrev-ref HEAD)" = "job-{jid}" ] && git add -A && '
                         '{ git diff --cached --quiet || git commit -q --no-verify -m "WIP: stopped before it finished"; }',
                         check=False, timeout=GIT_TIMEOUT_S).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _saved(jid: str, ok: bool) -> str:
    return f'מה שנעשה עד כאן שמור בענף job-{jid}.' if ok else f'⚠️ לא הצלחתי לשמור בענף. יש עותק ב-jobs/{jid}/partial.diff.'


def _alive() -> bool:
    """Anything running as the agent (only job processes do). Not knowing counts as yes."""
    return _sh('sudo', '-n', 'pgrep', '-u', 'claudeagent', check=False).returncode != 1


def _paused(chat: str) -> bool:
    """"עצור סוכן": nothing starts by itself. A file that can't be read says wait a tick, not go."""
    try:
        st = json.loads(AGENT_STATE.read_text(encoding='utf-8'))
    except OSError:
        return False
    except ValueError:
        return True
    return bool(isinstance(st, dict) and isinstance(st.get(chat), dict) and st[chat].get('paused'))


# ── reading what a job did, without the job's repo ──────────────────────────────────────────────────────────────────
# 07/11 audit: the job owns ~/ws, .git/config included: diff.external, textconv, aliases and fsmonitor all run or lie when
# git runs there. So no git runs there for anything the owner reads or anything that deploys. As the agent only `tar` runs,
# on the object store and the refs by name; config, hooks and info never leave. Those bytes become a repo of ours, with
# our config, fetched into a clone of production. result.json's diffstat and commits are not read at all.
_RAW_FILE_RX = re.compile(r'(?:packed-refs|refs/heads/job-[\w-]+|objects/[0-9a-f]{2}/[0-9a-f]{38}|'
                          r'objects/pack/pack-[0-9a-f]{40,64}\.(?:pack|idx))')


def _take_job_repo(jid: str, dest: Path) -> None:
    """The job's objects and its branch into `dest`, a repo we made. Anything but a plain file with an expected name
    stays behind (a link planted there would be read by us, not by the agent). A cut or missing copy fails later, in git."""
    _sh('git', 'init', '-q', '--bare', str(dest), timeout=GIT_TIMEOUT_S)
    raw = dest.parent / 'raw.tar'
    with open(raw, 'wb') as out:
        subprocess.run([*_AGENT, 'sh', '-c', 'cd "$0" && tar -cf - objects "refs/heads/job-$1" $(ls packed-refs 2>/dev/null) '
                        '| head -c "$2"', str(AGENT_HOME / 'ws' / '.git'), jid, str(REPO_CAP)],
                       stdout=out, stderr=subprocess.DEVNULL, timeout=120, check=False)
    with tarfile.open(raw, 'r:') as tar:
        for m in tar:
            name = m.name.removeprefix('./')
            if not m.isreg() or not _RAW_FILE_RX.fullmatch(name):
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(target, 'wb') as f:
                shutil.copyfileobj(src, f)


@contextmanager
def _clean_clone(jid: str):
    """A clone of production with the job's branch as `job`: only we ever wrote here. Raises when that can't be made."""
    tmp = Path(tempfile.mkdtemp(prefix='cjd_'))
    try:
        _take_job_repo(jid, tmp / 'job.git')
        work = tmp / 'w'
        _sh('git', 'clone', '-q', str(BARE), str(work), timeout=120)
        _sh('git', '-C', str(work), 'fetch', '-q', str(tmp / 'job.git'), f'job-{jid}:job', timeout=120)
        yield work
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _git(work: Path, *args) -> subprocess.CompletedProcess:
    return _sh('git', '-C', str(work), '-c', 'core.quotepath=off', *args, check=False, timeout=GIT_TIMEOUT_S)


def _review(jid: str) -> dict | None:
    """What the job really changed, read in the clean clone: {'sha', 'files', 'diffstat', 'commits', 'diff'}.
    None = it can't be said (no branch, no shared history, an unreadable copy), so nothing is offered for deploy."""
    try:
        with _clean_clone(jid) as work:
            sha, fork = _git(work, 'rev-parse', '--verify', 'job^{commit}'), _git(work, 'merge-base', 'origin/main', 'job')
            base = fork.stdout.strip()
            if sha.returncode or fork.returncode or not re.fullmatch(r'[0-9a-f]{40,64}', base):
                return None
            view = ('--no-ext-diff', '--no-textconv', '--no-color', '--no-renames')
            names = _git(work, 'diff', *view, '--name-only', '-z', base, 'job')
            stat = _git(work, 'diff', *view, '--stat=100', base, 'job')
            full = _git(work, 'diff', *view, base, 'job')
            log = _git(work, 'log', '--oneline', '--no-decorate', f'{base}..job')
            if names.returncode or stat.returncode or full.returncode or log.returncode:
                return None
            return {'sha': sha.stdout.strip(), 'files': [f for f in names.stdout.split('\0') if f],
                    'diffstat': stat.stdout[-1500:], 'commits': log.stdout[-1500:], 'diff': full.stdout}
    except (OSError, EOFError, tarfile.TarError, subprocess.SubprocessError) as e:
        print(f'  ⚠️ claude job {jid}: no clean view: {e!r}'[:200])
        return None


def start(chat: str, task: str, quiet: bool = False, kind: str | None = None) -> tuple[str | None, str]:
    """→ (job id, message for the owner); no id = it waits in the queue (or couldn't start). kind: 'scout' = the
    self-improvement loop's analysis run (agent/improve.py)."""
    with _lock():
        q = _load_q()
        if _busy(_load()) or q['limit_until'] > time.time():
            return None, _enqueue(q, chat, task, quiet, kind)
        return _begin(chat, task, quiet, **({'extra': {'kind': kind}} if kind else {}))


def _begin(chat: str, task: str, quiet: bool = False, extra: dict | None = None) -> tuple[str | None, str]:
    jid = datetime.now().strftime('%m%d%H%M') + secrets.token_hex(2)
    tmp = Path(tempfile.mkdtemp(prefix='cj_'))
    try:
        _sh('git', '--git-dir', str(BARE), 'bundle', 'create', str(tmp / 'main.bundle'), 'main')
        _as_agent('mkdir', '-p', str(AGENT_HOME / 'inbox'), str(AGENT_HOME / 'jobs' / jid))
        _put(AGENT_HOME / 'inbox' / 'main.bundle', (tmp / 'main.bundle').read_bytes())
        _put(AGENT_HOME / 'jobs' / jid / 'task.txt', task)
        # 05/11: HEAD left on `base` made git refuse the next fetch and every job die with "fetch failed" (03–05/11).
        # Here as well as in agent_job.sh, so it's fixed before the script is reinstalled by hand.
        _as_agent('git', '-C', str(AGENT_HOME / 'ws'), 'checkout', '-q', '--detach', check=False)
    except subprocess.SubprocessError as e:
        said = getattr(e, 'stderr', '') or ''
        said = said.decode('utf-8', 'replace') if isinstance(said, bytes) else said
        return None, f'⚠️ לא הצלחתי להכין את המשימה: {said[-200:]}'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    session = str(uuid.uuid4())          # ours, not the CLI's: known even if the run dies before it prints a thing
    st = _load()
    st[jid] = {'task': task, 'chat': chat, 'status': 'running', 'started': time.time(), 'quiet': quiet,
               'mode': 'start', 'session': session, **(extra or {})}
    _save(st)                            # before the launch: a run nobody knows about can't be stopped or reported
    try:
        _launch(jid, 'start', session)
    except (OSError, subprocess.SubprocessError) as e:
        st = _load()
        st[jid]['status'] = 'failed'
        _save(st)
        return None, f'⚠️ לא הצלחתי להפעיל את המשימה: {e!r}'[:300]
    return jid, (f'🛠️ קלוד התחיל לעבוד (משימה {jid}):\n"{task[:200]}"\n'
                 f'זה יכול לקחת כמה דקות. אני ממשיך לענות בינתיים, ואעדכן כשזה מוכן.')


def _give_up(j: dict, jid: str, why: str, q: dict) -> None:
    saved = _saved(jid, _snapshot(jid, wip=True))
    j['status'] = 'finished'
    j['report'] = True
    j['result'] = {'status': 'limit', 'summary': f'{why} {saved}\nאפשר לבקש שוב מקלוד.', 'diffstat': '', 'commits': ''}
    for k in ('wake_at', 'limit'):
        j.pop(k, None)
    _note(q, jid, '', '')


def _park(j: dict, jid: str, limit: dict, q: dict, now: float) -> None:
    """Stopped by a usage limit: it waits with its session, or gives up when the wait would be too long."""
    resets = limit.get('resets_at')
    resets = resets if resets and resets > now else None
    since = j.setdefault('parked_since', now)
    manual, before = j.pop('manual', False), (j.get('limit') or {}).get('resets_at')
    _snapshot(jid)
    q['limit_until'] = (resets + RESET_MARGIN_S) if resets else now + RETRY_S
    if j.get('quiet') and (not resets or resets - now > QUIET_MAX_WAIT_S):
        return _give_up(j, jid, 'הסקירה לא חיכתה למכסה.', q)
    if resets and (j.get('wakes', 0) >= MAX_WAKES or resets - since > MAX_PARK_S):
        return _give_up(j, jid, f'⏸️ הפסקתי לחכות למכסה (היא חוזרת ב-{_when(resets, now)}).', q)
    if not resets and now - j.setdefault('unknown_since', now) >= UNKNOWN_BOUND_S:
        return _give_up(j, jid, f'⏸️ ניסיתי {UNKNOWN_BOUND_S // 3600} שעות והמכסה לא חזרה.', q)
    if resets:
        j.pop('unknown_since', None)
    first = not j.get('limit')
    j.update(status='parked', wake_at=q['limit_until'], limit=limit)
    if j.get('quiet'):
        return
    if manual:
        text = f'⏸️ עדיין במכסה. ממשיך לבד ב-{_when(j["wake_at"], now)}.'
    elif not resets:                         # the half-hourly tries say it once
        text = '⏸️ נגמרה המכסה, ולא כתוב מתי היא חוזרת. אנסה שוב כל חצי שעה.' if first or before else ''
    elif first or not before or abs(resets - before) > 300:              # not again for the same wait
        text = f'⏸️ נגמרה המכסה, ממשיך לבד ב-{_when(j["wake_at"], now)}.'
    else:
        text = ''
    if text:
        _note(q, jid, j.get('chat', ''), text)


def _relaunch(st: dict, j: dict, jid: str, mode: str, now: float, prompt: str = CONTINUE_PROMPT) -> None:
    """The same job goes on: the old result moves aside (or poll would read it as this run's), the work stays."""
    folder = AGENT_HOME / 'jobs' / jid
    if mode == 'restart':
        j['session'] = str(uuid.uuid4())
    if mode != 'start':
        _put(folder / 'continue.txt', prompt)
    for name in ('result.json', 'stream.jsonl'):
        _as_agent('mv', '-f', str(folder / name), str(folder / f'{name}.prev'), check=False)
    before = {k: j.get(k) for k in ('status', 'started', 'mode')}
    wake = j.pop('wake_at', None)
    j.update(status='running', started=now, mode=mode)
    _save(st)                            # before the launch
    try:
        _launch(jid, mode, j.get('session', ''))
    except (OSError, subprocess.SubprocessError):
        j.update(before)
        if wake:
            j['wake_at'] = wake
        _save(st)
        raise


def _wake(st: dict, j: dict, jid: str, q: dict, now: float, manual: bool = False) -> bool:
    known = bool((j.get('limit') or {}).get('resets_at'))
    try:
        _relaunch(st, j, jid, 'resume' if _can_resume() and j.get('session') else 'start', now)
    except (OSError, subprocess.SubprocessError) as e:
        print(f'  ⚠️ claude job {jid} did not go on: {e!r}'[:200])
        j['launch_fails'] = j.get('launch_fails', 0) + 1
        if j['launch_fails'] >= MAX_LAUNCH_FAILS:
            _give_up(j, jid, '⚠️ לא הצלחתי להמשיך את המשימה (התקלה בשרת, לא במכסה).', q)
        else:
            j['wake_at'] = q['limit_until'] = now + RETRY_S
        return False
    q['limit_until'] = 0
    if manual:
        j['manual'] = True
    else:
        j['wakes'] = j.get('wakes', 0) + (1 if known else 0)
    if known and not manual and not j.get('quiet'):      # a half-hourly try isn't news
        _note(q, jid, j.get('chat', ''), f'▶️ המכסה חזרה, קלוד ממשיך ({_label(j)}).')
    return True


def _no_result(st: dict, j: dict, jid: str, now: float, revive: bool) -> bool:
    """A run with nothing to show: still working, cut off from outside (it goes on), or lost. → the state changed."""
    age = now - j.get('started', 0)
    dead = age > DEAD_AFTER_S and not _alive()
    if not dead and age <= LOST_AFTER_S:
        return False
    if not revive:
        return False
    if dead and j.get('session') and j.get('revived', 0) < MAX_REVIVES:
        try:
            if _can_resume():
                j['revived'] = j.get('revived', 0) + 1
                _relaunch(st, j, jid, 'resume', now, INTERRUPTED_PROMPT)
                return True
        except (OSError, subprocess.SubprocessError) as e:
            print(f'  ⚠️ claude job {jid} did not revive: {e!r}'[:200])
            return False
    j['status'] = 'lost'
    j['report'] = True
    j['result'] = {'status': 'lost', 'summary': '', 'diffstat': ''}
    return True


def _collect_one(st: dict, q: dict, jid: str, j: dict, now: float, revive: bool) -> bool:
    folder = AGENT_HOME / 'jobs' / jid
    raw = _read_job(folder / 'result.json')
    if raw is None:
        return _no_result(st, j, jid, now, revive)
    try:
        res = json.loads(raw)
        if not isinstance(res, dict):
            raise ValueError('not an object')
    except ValueError:
        if now - j.get('started', 0) <= LOST_AFTER_S and _alive():   # it's still writing it
            return False
        raise
    res = {k: str(res.get(k) or '') for k in ('status', 'summary', 'tests')}
    res.update(diffstat='', commits='')      # the job's own word on these isn't read: see _review
    d = digest(_read_job(folder / 'stream.jsonl', STREAM_CAP) or '')
    res['summary'] = (d['text'] or res['summary'])[-3500:]
    limit = detect_limit(res['status'], res['summary'], d, now)
    if limit:
        _park(j, jid, limit, q, now)
        return True
    if j.get('mode') == 'resume' and res['status'] != 'done' and RESUME_FAILED_RX.search(res['summary']):
        try:                                                         # the session is gone; the work isn't
            _relaunch(st, j, jid, 'restart', now, RESTART_NOTE)
            return True
        except (OSError, subprocess.SubprocessError) as e:
            print(f'  ⚠️ claude job {jid} did not restart: {e!r}'[:200])
    j['status'] = 'finished'
    if j.get('kind') == 'scout':             # 14/11 the loop's scout: analysis only, its findings read before the next
        from agent.improve import FINDINGS_FILE   # job's `git clean` wipes them; nothing of it is ever deployed
        res['findings'] = (_read_job(AGENT_HOME / 'ws' / FINDINGS_FILE, 64 * 1024) or '')[:64 * 1024]
    elif res['status'] == 'done':
        seen = _review(jid)
        if seen is None:                     # no fingerprint, no ✅: deploy() would refuse it anyway
            res['summary'] += '\n\n⚠️ לא הצלחתי לבדוק מה השתנה בענף, אז אין מה לפרוס מהמשימה הזו.'
        elif seen['commits'] and seen['files']:
            j.update(status='ready', sha=seen['sha'], ready_at=now)
            res.update(diffstat=seen['diffstat'], commits=seen['commits'], files=seen['files'])
            if j.get('kind') == 'improve':   # its run's one message asks for the ✅, not proactive's daily nudge
                j['ready_ts'] = j.pop('ready_at')
    j['result'] = res
    j['report'] = True
    for k in ('wake_at', 'limit'):
        j.pop(k, None)
    return True


def _collect(now: float | None = None, revive: bool = True) -> None:
    """Runs that ended: parked when a limit stopped them, otherwise marked for poll() to report. One job that can't be
    read ends alone: it must not freeze the rest, "תעצור את קלוד" included."""
    now = time.time() if now is None else now
    st, q, changed = _load(), _load_q(), False
    for jid, j in st.items():
        if j.get('status') != 'running':
            continue
        try:
            changed = _collect_one(st, q, jid, j, now, revive) or changed
        except Exception as e:
            print(f'  ⚠️ claude job {jid} unreadable: {e!r}'[:200])
            j.update(status='finished', report=True, result={
                'status': 'error', 'summary': 'התוצאה של המשימה לא קריאה.', 'diffstat': '', 'commits': ''})
            changed = True
    if changed:
        _save(st)
        _save_q(q)


def poll() -> list[dict]:
    """Jobs that just finished, with their result (each reported once)."""
    with _lock():
        _collect()
        st, q, out = _load(), _load_q(), []
        for jid, j in st.items():
            if j.pop('report', False):
                out.append({**j, 'id': jid})
                _note(q, jid, '', '')
        if out:
            _save(st)
            _save_q(q)
        return out


def advance(now: float | None = None) -> None:
    """The tick, also at night: end what ended, wake what's due, run the next one in the queue. Starts Claude work
    only; nothing here deploys: that stays the owner's ✅ on a finished job (deploy())."""
    now = time.time() if now is None else now
    with _lock():
        _collect(now)
        st, q = _load(), _load_q()
        try:                                 # 14/11 a finished scout → its fixes into the queue, the rest to the backlog
            from agent import improve
            if improve.process(st, q, now):
                _save(st)
                _save_q(q)
        except Exception as e:
            print(f'  ⚠️ improve tick failed: {e!r}'[:200])
        waiting = parked(st)
        if waiting and waiting[1].get('wake_at', 0) <= now:
            if not _paused(waiting[1].get('chat', '')):
                _wake(st, waiting[1], waiting[0], q, now)
                _save(st)
                _save_q(q)
            return
        if _busy(st) or q['limit_until'] > now or not q['items'] or _paused(q['items'][0].get('chat', '')):
            return
        item = q['items'][0]
        if item.get('kind') == 'improve':     # the loop's own fixes: at night only, so the owner's "קלוד:" never waits on them
            from agent import improve
            if not improve.may_launch(item, now):
                return
        extra = {k: item[k] for k in ('kind', 'run', 'title', 'forced') if item.get(k)}
        try:
            new, msg = _begin(item['chat'], item['task'], item.get('quiet', False), **({'extra': extra} if extra else {}))
        except (OSError, subprocess.SubprocessError) as e:
            new, msg = None, repr(e)[:200]
        q = _load_q()
        q['items'] = [i for i in q['items'] if i.get('id') != item.get('id')]
        if new:
            q['limit_until'] = 0
            if not item.get('quiet'):
                _note(q, new, item['chat'], f'▶️ קלוד התחיל את המשימה מהתור ({_label(item)}).')
        else:                                # the task runs once, but "once" means it really ran
            item['tries'] = item.get('tries', 0) + 1
            if item['tries'] < MAX_QUEUE_TRIES:
                q['items'].insert(0, item)
                q['limit_until'] = now + 300
            else:
                _note(q, item['id'], item['chat'], f'⚠️ לא הצלחתי להריץ מהתור ({_label(item)}): {msg}')
        _save_q(q)


# ── control from WhatsApp ───────────────────────────────────────────────────────────────────────────────────────────
_ME = r'קלוד'
_NOW = r'(?:\s+(?:עכשיו|כרגע))?'
STATUS_RX = re.compile(rf'מה\s+(?:קורה\s+עם\s+|המצב\s+(?:של|עם)\s+|הסטטוס\s+(?:של|עם)\s+|עם\s+)?{_ME}(?:\s+עושה)?{_NOW}|'
                       rf'{_ME}\s+עובד{_NOW}|מה\s+(?:יש\s+)?בתור\s+של\s+{_ME}|'
                       rf'{_ME}\s*[:,]?\s*מה\s+(?:אתה\s+עושה|המצב|הסטטוס){_NOW}')
STOP_RX = re.compile(rf'(?:ת?עצור|תפסיק)\s+את\s+(?:ה?משימה\s+של\s+)?{_ME}{_NOW}|{_ME}\s*[:,]?\s*(?:ת?עצור|תפסיק|די){_NOW}')
RESUME_RX = re.compile(rf'(?:{_ME}\s*[:,]?\s*)תמשיך{_NOW}|תמשיך\s+את\s+{_ME}{_NOW}')
RESUME_BARE_RX = re.compile(r'(?:תמשיך|המשך)\s+עכשיו')      # only when something waits; otherwise it's not for us
CANCEL_RX = re.compile(r'(?:ת?בטל|תוריד|ת?מחק)\s+(?:את\s+)?(?:משימה\s+)?(?:(?P<n>\d{1,2})\s+)?מהתור'
                       rf'(?:\s+של\s+{_ME})?(?:\s+(?P<n2>\d{{1,2}}))?')
CLEAR_RX = re.compile(rf'(?:ת?בטל|תנקה|נקה|תרוקן)\s+את\s+התור\s+של\s+{_ME}')
_JOB = r'(?:את\s+)?(?:משימה\s+)?(?P<id>[0-9a-f]{8,16})'
DEPLOY_RX = re.compile(rf'(?:ת?פרוס|לפרוס)\s+{_JOB}')
# 14/11 the self-improvement loop (agent/improve.py)
SELF_CHECK_RX = re.compile(rf'(?:{_ME}\s*[:,]?\s*)?(?:ת?בדוק|תשפר)\s+את\s+עצמך(?:\s+(?:עכשיו|כרגע|היום))?')
CADENCE_RX = re.compile(rf'(?:{_ME}\s*[:,]?\s*)?(?:כל\s+כמה\s+זמן|מתי|באיזה\s+קצב)\s+(?:אתה\s+)?'
                        r'(?:משתפר|בודק\s+את\s+עצמך)')
AUTO_ON_RX = re.compile(rf'{_ME}\s*[:,]?\s*(?:ת?פרוס|לפרוס)\s+לבד\s+(?:שינויי|תיקוני)\s+תיעוד')
AUTO_OFF_RX = re.compile(rf'{_ME}\s*[:,]?\s*(?:בלי\s+פריסה\s+לבד|אל\s+תפרוס\s+לבד)')
DISCARD_RX = re.compile(rf'(?:תזרוק|לזרוק)\s+{_JOB}')


def _places(q: dict) -> str:
    """No "1." lines: this chat has numbered questions, and a bare digit answers the open one, a deploy included."""
    return '\n'.join(f'מקום {n}: {_label(i)}' for n, i in enumerate(q['items'], 1))


def status_text(now: float | None = None) -> str:
    now = time.time() if now is None else now
    st, q, lines = _load(), _load_q(), []
    for j in st.values():
        if j.get('status') == 'running':
            lines.append(f'🛠️ עובד כבר {max(1, int((now - j.get("started", now)) / 60))} דק׳: {_label(j)}')
    waiting = parked(st)
    if waiting and _paused(waiting[1].get('chat', '')):
        lines.append(f'⏸️ מחכה (הסוכן עצור, ימשיך אחרי "הפעל סוכן"): {_label(waiting[1])}')
    elif waiting:
        lines.append(f'⏸️ מחכה למכסה, ממשיך לבד ב-{_when(waiting[1].get("wake_at", now), now)}: {_label(waiting[1])}')
    elif q['limit_until'] > now and q['items']:
        lines.append(f'⏸️ המכסה נגמרה עד {_when(q["limit_until"], now)}.')
    if q['items']:
        lines.append('📥 בתור:\n' + _places(q))
    return '\n'.join(lines) or '😴 קלוד פנוי, אין משימות.'


def stop() -> str:
    """The job in hand ends here, its work saved. What's queued stays and goes on by itself. Killing comes first and
    needs no reading at all: a job that can't be read is exactly the one to stop."""
    with _lock():
        if any(j.get('status') == 'running' for j in _load().values()):  # only job processes run as this user
            _sh('sudo', '-n', 'pkill', '-KILL', '-u', 'claudeagent', check=False)
        try:
            _collect(revive=False)
        except Exception as e:
            print(f'  ⚠️ claude stop: {e!r}'[:200])
        st, q = _load(), _load_q()
        jid, j = next(((k, v) for k, v in st.items() if v.get('status') in ('running', 'parked')), (None, None))
        if not jid:
            return 'קלוד לא עובד על כלום כרגע.' + (f' בתור יש {len(q["items"])}.' if q['items'] else '')
        saved = _saved(jid, _snapshot(jid, wip=True))
        j['status'] = 'stopped'
        j.pop('wake_at', None)
        _note(q, jid, '', '')
        _save(st)
        _save_q(q)
        rest = f'\nבתור נשארו {len(q["items"])} ("תבטל מהתור" מוריד).' if q['items'] else ''
        return f'🛑 עצרתי ({_label(j)}). {saved}{rest}'


def waiting_now(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    q = _load_q()
    return bool(parked()) or (q['limit_until'] > now and bool(q['items']))


def resume_now(now: float | None = None) -> str:
    """The owner's "now": one try before the time, which doesn't count against the bound; still limited → it waits again."""
    now = time.time() if now is None else now
    with _lock():
        _collect(now)
        st, q = _load(), _load_q()
        waiting = parked(st)
        chat = waiting[1].get('chat', '') if waiting else (q['items'][0].get('chat', '') if q['items'] else '')
        if chat and _paused(chat):
            return '⏸️ הסוכן עצור. קודם "הפעל סוכן".'
        if waiting:
            ok = _wake(st, waiting[1], waiting[0], q, now, manual=True)
            _save(st)
            _save_q(q)
            return f'▶️ מנסה עכשיו ({_label(waiting[1])}).' if ok else '⚠️ לא הצלחתי להמשיך. אנסה שוב עוד חצי שעה.'
        if q['limit_until'] > now and q['items']:
            q['limit_until'] = 0
            _save_q(q)
            advance(now)
            return '▶️ מריץ מהתור עכשיו.'
        return 'אין כרגע משהו שמחכה.'


def cancel_queued(n: int | None = None, everything: bool = False) -> str:
    with _lock():
        q = _load_q()
        if not q['items']:
            return 'התור ריק.'
        if everything:
            count, q['items'] = len(q['items']), []
            _save_q(q)
            return f'🗑️ ביטלתי את כל התור ({count}).'
        if n is None and len(q['items']) > 1:
            return 'איזו? כתוב "תבטל מהתור" ואת המקום:\n' + _places(q)
        k = (n or 1) - 1
        if not 0 <= k < len(q['items']):
            return f'אין מקום {n} בתור (יש {len(q["items"])}).'
        item = q['items'].pop(k)
        _save_q(q)
        return f'🗑️ הורדתי מהתור: {_label(item)}'


def replace_recent(chat: str, prefix: str, within_s: float, now: float | None = None) -> list[str]:
    """15/11 one request became two jobs six minutes apart (1115130461e9, 1115131072ea), the second one the better
    worded: the newer request takes the place of his own same-kind job that started (or was queued) within within_s.
    Queued → off the queue; running → stopped with its work saved, as "עצור את קלוד" does. → the replaced tasks."""
    now = time.time() if now is None else now
    gone: list[str] = []
    with _lock():
        q = _load_q()
        keep = []
        for i in q['items']:
            if i.get('chat') == chat and not i.get('quiet') and str(i.get('task', '')).startswith(prefix) \
                    and now - float(i.get('added') or 0) <= within_s:
                gone.append(str(i['task']).split('\n', 1)[0])
            else:
                keep.append(i)
        if len(keep) != len(q['items']):
            q['items'] = keep
            _save_q(q)
        st = _load()
        for jid, j in st.items():
            if j.get('status') == 'running' and j.get('chat') == chat and not j.get('quiet') \
                    and str(j.get('task', '')).startswith(prefix) and now - float(j.get('started') or 0) <= within_s:
                _sh('sudo', '-n', 'pkill', '-KILL', '-u', 'claudeagent', check=False)
                _snapshot(jid, wip=True)
                j['status'] = 'stopped'
                j['replaced'] = True
                _note(q, jid, '', '')
                _save(st)
                _save_q(q)
                gone.append(str(j['task']).split('\n', 1)[0])
                break
    return gone


def control(text: str, bare: bool = True) -> tuple[str, str] | None:
    """The owner's words about Claude's work → (action for the audit, reply); None = it wasn't about that.
    bare=False: a lesson or a quiz is going on, so a plain "תמשיך עכשיו" is about that."""
    t = (text or '').strip().rstrip(' .!?,')
    if not t or not re.search(_ME + '|תור|תמשיך|המשך|פרוס|זרוק|משתפר|את עצמך', t):
        return None
    if SELF_CHECK_RX.fullmatch(t) and re.search(_ME + '|עכשיו', t):
        from agent import improve
        return 'self_check', improve.check_now(_owner_chat())
    if CADENCE_RX.fullmatch(t):
        from agent import improve
        return 'status', improve.cadence_text()
    if AUTO_ON_RX.fullmatch(t) or AUTO_OFF_RX.fullmatch(t):
        from agent import improve
        return 'auto_docs', improve.set_auto_docs(bool(AUTO_ON_RX.fullmatch(t)))
    if STATUS_RX.fullmatch(t):
        return 'status', status_text()
    if STOP_RX.fullmatch(t):
        return 'stop', stop()
    if RESUME_RX.fullmatch(t) or (bare and RESUME_BARE_RX.fullmatch(t) and waiting_now()):
        return 'resume_now', resume_now()
    if CLEAR_RX.fullmatch(t):
        return 'cancel', cancel_queued(everything=True)
    m = CANCEL_RX.fullmatch(t)
    if m:
        n = m['n'] or m['n2']
        return 'cancel', cancel_queued(int(n) if n else None)
    m = DEPLOY_RX.fullmatch(t)
    if m:
        return 'deploy', deploy(m['id'])
    m = DISCARD_RX.fullmatch(t)
    if m:
        return 'discard', discard(m['id'])
    return None


def _owner_chat() -> str:
    from whatsapp import outbox
    return outbox.owner_chat() or ''


def digit_guard(chat: str, jid: str, op: str, now: float | None = None) -> str | None:
    """A bare digit reaches here as the button of the LATEST open question, which may not be the report the owner is looking
    at: with another job's report open, or a report days old, nothing happens until the job is named. → why not."""
    from whatsapp import outbox
    now = time.time() if now is None else now
    st = _load()
    open_ids = {pl.split(':', 2)[2] for pl in outbox.open_payloads(chat, 'cj:deploy:')}
    others = sorted(k for k in open_ids - {jid} if (st.get(k) or {}).get('status') == 'ready')
    ready_at = (st.get(jid) or {}).get('ready_at')
    old = op == 'deploy' and ready_at and now - ready_at > STALE_REPORT_S
    if not others and not old:
        return None
    word = 'פרוס' if op == 'deploy' else 'תזרוק'
    names = '\n'.join(f'{word} {k}: {_label(st.get(k) or {})}' for k in [jid] + others)
    why = 'יש כמה משימות שמחכות לתשובה' if others else 'הדוח הזה ישן'
    return f'✋ {why}, אז המספר לא ברור ולא עשיתי כלום. כתוב במילים:\n{names}'


def _files_line(r: dict) -> str:
    """The files a fix touches (read in the clean clone), the PC-only ones flagged — so a ✅ is never blind."""
    files = r.get('files') or []
    bad = set(sensitive(files))
    return ', '.join(('🔒' if f in bad else '') + f for f in files[:8]) or '(אין)'


def result_message(job: dict) -> tuple[str, list]:
    """('', []) = nothing to tell the owner. 23/10: the daily review (quiet) said "התחלתי…" + a long report every morning
    at 07:00 even when it changed nothing — now it speaks only when it prepared a fix to deploy, in a few lines."""
    r, jid = job.get('result') or {}, job['id']
    if job.get('kind') == 'scout':           # 14/11 the loop: the scout says nothing, its fixes say it once per run
        return '', []
    if job.get('kind') == 'improve':
        from agent import improve
        return improve.batch_message(job)
    if job.get('quiet'):
        if not (r.get('status') == 'done' and r.get('commits')):
            return '', []
        first = next((ln.strip(' -*') for ln in (r.get('summary') or '').splitlines()
                      if re.search(r'תיקון|תיקנתי|הוספתי|באג', ln)), '')[:400]
        body = first or (r.get('summary') or '').strip()[:400]
        files = _files_line(r)
        return (f"🔧 *הסקירה היומית הכינה תיקון* (משימה {jid})\n{body}\n\n📁 {files}\n🧪 {(r.get('tests') or '').strip()}",
                [('🚀 לפרוס', f'cj:deploy:{jid}'), ('🗑️ לזרוק', f'cj:discard:{jid}'), ('📄 להראות שינויים', f'cj:diff:{jid}')])
    if r.get('status') == 'limit':
        return f"{r.get('summary', '')} (משימה {jid})", []
    head = {'done': '✅ קלוד סיים', 'timeout': '⌛ קלוד לא הספיק (30 דקות)', 'error': '⚠️ קלוד נתקע',
            'lost': '⚠️ המשימה אבדה'}.get(r.get('status'), '🛠️ קלוד סיים')
    text = f"{head} — משימה {jid}\n\n{(r.get('summary') or '').strip()[-1800:]}"
    if r.get('tests'):
        text += f"\n\n🧪 בדיקות: {r['tests'].strip()}"
    if not r.get('commits'):
        return text + '\n\n(אין שינויים לפרוס.)', []
    text += f"\n\n📦 שינויים:\n{(r.get('diffstat') or '').strip()[-700:]}"
    if sensitive(r.get('files') or []):
        text += f"\n📁 {_files_line(r)}\n🔒 = רק מהמחשב."
    return text, [('🚀 לפרוס', f'cj:deploy:{jid}'), ('🗑️ לזרוק', f'cj:discard:{jid}'), ('📄 להראות שינויים', f'cj:diff:{jid}')]


# 03/11 red team: a message from someone else in the nightly review could steer Claude into a "fix" that widens what
# the bot may do. 07/11 audit: a list of what's forbidden always misses something (a cron script, a test the gate runs,
# core.py itself), so this is what MAY deploy from WhatsApp; everything else is PC-only. Docs and the course site's
# static files, plus a few modules that format study data and hold no power of their own: they import no subprocess,
# send nothing and decide no permission. They are still code that runs as ubuntu once deployed, so the real diff below
# is what guards them. PC-only by name of the audit: agent/core.py, whatsapp/, tests/ (the gate runs them), cron-run
# scripts/, deploy/. Links and submodules never pass, whatever their path.
WHATSAPP_DEPLOY_RX = re.compile(r'(?:docs/.+\.(?:md|txt)|README\.md|PROJECT\.md|INSTALLATION\.md|'
                                r'scripts/site_assets/[\w.-]+\.(?:js|css|svg|png)|scripts/build_course_site\.py|'
                                r'agent/(?:timetable|exam_weight|office_hours|study_focus|weekly|tutor)\.py|'
                                r'tests/improve_cases\.json)')   # 14/11 data only, add-only (checked in _deploy_from)


def sensitive(files: list[str]) -> list[str]:
    """The files that can't deploy from WhatsApp."""
    return [f for f in files if not WHATSAPP_DEPLOY_RX.fullmatch(f)]


def _not_plain(work: Path, a: str, b: str) -> list[str] | None:
    """Paths that become a link or a submodule between a and b. None = the check itself failed."""
    r = _git(work, 'diff', '--no-ext-diff', '--raw', '--no-renames', '-z', a, b)
    if r.returncode != 0:
        return None
    parts = r.stdout.split('\0')
    return [path for meta, path in zip(parts[::2], parts[1::2]) if meta.split(' ')[1:2] in (['120000'], ['160000'])]


def diff(jid: str) -> str:
    """The real change, from the clean clone: the file list first, so cutting the text can't hide a file."""
    if (_load().get(jid) or {}).get('kind') == 'scout':     # 🔍 פרטים of a self-improvement run
        from agent import improve
        return improve.details(jid)
    seen, sha = _review(jid), (_load().get(jid) or {}).get('sha')
    if seen is None:
        return '⚠️ לא הצלחתי להציג את השינויים (זה לא אומר שאין כאלה).'
    if sha and seen['sha'] != sha:
        return f'🔒 הענף של משימה {jid} השתנה אחרי הדוח, אז זה לא מה שנפרס.'
    d = f"{seen['diffstat'].rstrip()}\n\n{seen['diff']}".strip() or '(אין שינויים)'
    return d if len(d) < 3500 else d[:3500] + '\n…(קוצר)'


def discard(jid: str) -> str:
    if (_load().get(jid) or {}).get('kind') == 'scout':
        from agent import improve
        return improve.discard_run(jid)
    with _lock():
        st = _load()
        if jid in st:
            st[jid]['status'] = 'discarded'
            _save(st)
    _as_agent('git', '-C', str(AGENT_HOME / 'ws'), 'branch', '-D', 'job-' + jid, check=False)
    return '🗑️ נזרק. שום דבר לא השתנה בשרת.'


REDO_NOTE = ('\n\n(הערה: זו הרצה חוזרת — הניסיון הקודם נכתב על קוד ישן והתנגש בשינויים שעלו מאז. בדוק קודם אם '
             'הבעיה עוד קיימת בקוד הנוכחי; אם כבר תוקנה — אל תשנה כלום ודווח שזה כבר מטופל.)')


def _redo(st: dict, jid: str, start_fn=None) -> str:
    """03/11: a fix that no longer applies on today's code (it changed while Claude worked) was a dead end ("צריך
    להריץ מחדש"). It's run again by itself, once, on the current code — often finding it was already fixed."""
    job = st.get(jid) or {}
    st[jid]['status'] = 'superseded'
    with _lock():
        fresh = _load()
        fresh.setdefault(jid, job)['status'] = 'superseded'
        _save(fresh)
    if REDO_NOTE in (job.get('task') or ''):
        return '⚠️ גם ההרצה החוזרת התנגשה בקוד החדש — לא פרסתי כלום. כדאי לבקש מקלוד שוב במילים שלך.'
    new, msg = (start_fn or start)(job.get('chat', ''), (job.get('task') or '') + REDO_NOTE,
                                   quiet=str(job.get('quiet')) == 'True')
    if not new:
        return '⚠️ הקוד בשרת השתנה מאז שקלוד התחיל, והשינוי שלו מתנגש — ' + msg
    return ('🔁 הקוד בשרת השתנה מאז שקלוד התחיל, אז השינוי שלו כבר לא מתאים. הפעלתי אותו מחדש על הקוד '
            'הנוכחי — אעדכן כשיהיה מוכן (ושוב רק באישור שלך).')


def deploy(jid: str, restart: bool = True) -> str:
    """Push job-<id> to production main through the pre-receive test gate (fast-forward only).
    The sensitive-files check runs here, in a clone only we write to, on exactly what the push will send. The job's own
    repo can lie: it can move `base`, drop the common history, hide a commit behind a merge. Any doubt refuses."""
    job = _load().get(jid) or {}
    if job.get('kind') == 'scout':           # 14/11 "🚀 לפרוס הכול": every ready fix of that run, one by one, same gate
        from agent import improve
        return improve.deploy_run(jid)
    if job.get('status') != 'ready':
        return 'המשימה הזו לא מוכנה לפריסה (או שכבר טופלה).'
    sha = job.get('sha')
    if not sha:
        return '🔒 אין למשימה הזו טביעת גרסה, אז לא פורס. אפשר לבקש שוב מקלוד.'
    try:
        said = _deploy_from(jid, sha, job.get('kind') == 'improve')
    except (OSError, EOFError, tarfile.TarError, subprocess.SubprocessError) as e:
        print(f'  ⚠️ claude job {jid}: {e!r}'[:200])
        return '⚠️ לא הצלחתי לקחת את השינויים מהעותק של קלוד.'
    if said is not None:
        return said
    with _lock():                            # the gate took a minute: the state may have moved on
        st = _load()
        st[jid]['status'] = 'deployed'
        _save(st)
    if restart:
        _restart_worker()
    return '🚀 נפרס! עבר את כל הבדיקות האוטומטיות ועלה לשרת.'


def _cases_ok(work: Path) -> list[str]:
    """tests/improve_cases.json may deploy from WhatsApp because it's data that only ever adds: what was there stays."""
    from agent import improve

    def at(rev):
        r = _git(work, 'show', f'{rev}:tests/improve_cases.json')
        if r.returncode:
            return {'cases': []}
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None
    return improve.cases_add_only(at('origin/main'), at('job'))


def _deploy_from(jid: str, sha: str, loop: bool = False) -> str | None:
    """The push itself, in the clean clone. None = it went through; otherwise why not."""
    with _clean_clone(jid) as work:
        got = _sh('git', '-C', str(work), 'rev-parse', '--verify', 'job^{commit}', check=False)
        if got.returncode != 0 or got.stdout.strip() != sha:
            return f'🔒 הענף של משימה {jid} השתנה אחרי הדוח שקראת, אז לא פורס.'
        ff = _sh('git', '-C', str(work), 'merge-base', '--is-ancestor', 'origin/main', 'job', check=False)
        if ff.returncode != 0:
            # 08/10: the daily review's fix was branched off yesterday's code — replaying it on today's main
            # keeps both (a plain push would have dropped everything deployed since; the gate refused it)
            rb = _sh('git', '-C', str(work), '-c', 'user.name=Claude (WhatsApp agent)',
                     '-c', 'user.email=claude-agent@smart-study.local', 'rebase', '-q', 'origin/main', 'job', check=False)
            if rb.returncode != 0:
                _sh('git', '-C', str(work), 'rebase', '--abort', check=False)
                return _redo(_load(), jid)
        changed = _sh('git', '-C', str(work), 'diff', '--no-ext-diff', '--name-only', '--no-renames', '-z', 'origin/main', 'job', check=False)
        plain = _not_plain(work, 'origin/main', 'job')
        if changed.returncode != 0 or plain is None:
            return '🔒 לא הצלחתי לבדוק אילו קבצים השתנו, אז לא פורס.'
        files = [f for f in changed.stdout.split('\0') if f]
        if not files:
            return 'אין מה לפרוס: זה כבר בשרת.'
        bad = list(dict.fromkeys(sensitive(files) + plain))
        if loop:                             # 14/11 what the self-improvement loop may never change, allowlist or not
            from agent.improve import DENY_RX
            bad = list(dict.fromkeys(bad + [f for f in files if DENY_RX.fullmatch(f)]))
        if bad:
            return _to_pc(jid, bad)
        if 'tests/improve_cases.json' in files and (errs := _cases_ok(work)):
            return '🔒 לא פורס: מקרי הבדיקה יכולים רק להתווסף.\n' + '\n'.join(f'• {e}' for e in errs[:5])
        push = _sh('git', '-C', str(work), 'push', 'origin', 'job:main', check=False)
        out = (push.stdout or '') + (push.stderr or '')
        if push.returncode != 0:
            fails = [l.strip() for l in out.splitlines() if '❌' in l][:5]
            return '❌ הבדיקות האוטומטיות חסמו את הפריסה — השרת לא השתנה.\n' + '\n'.join(fails)
    return None


def _to_pc(jid: str, bad: list[str]) -> str:
    """15/11 "🔒 לא פורס מוואטסאפ … את זה רק מהמחשב" read as a failure he had to do something about. The deploy stays
    blocked exactly as before; the job is written to data/improvement_backlog.json, which the PC round goes through, and
    he's told that it's queued there and he needs to do nothing."""
    job = _load().get(jid) or {}
    title = _label(job) or f'משימה {jid}'
    try:
        from agent import improve
        improve.add_backlog([{'title': f'פריסה מהמחשב: {title}', 'evidence': f'משימת קלוד {jid} (ענף job-{jid}), מוכנה',
                              'change': (job.get('summary') or '')[:600] or f'השינוי שבענף job-{jid}',
                              'files': bad[:12], 'why': 'נוגע בקבצים שנפרסים רק מהמחשב, אחרי קריאת השינוי'}],
                            datetime.now(), STATE.parent / improve.BACKLOG.name)   # = data/improvement_backlog.json
        queued = True
    except Exception as e:
        print(f'  ⚠️ backlog {jid}: {e!r}'[:200])
        queued = False
    shown = '\n'.join(f'• {f}' for f in bad[:6]) + (f'\n• ועוד {len(bad) - 6}' if len(bad) > 6 else '')
    where = ('נכנס לרשימה של הסבב הבא במחשב, ושם הוא ייקרא וייפרס. אין צורך לעשות כלום.' if queued else
             f'הוא נשמר בענף job-{jid} ויחכה לסבב במחשב. אין צורך לעשות כלום.')
    return f'🔒📥 השינוי מוכן, אבל הוא נוגע בקבצים שנפרסים רק מהמחשב:\n{shown}\nלכן הוא לא עלה מהטלפון. {where}'


def _restart_worker() -> None:
    # the post-receive hook restarts the dashboard (+ until 10/11 the Telegram bot); restart this worker shortly after replying
    subprocess.run(['sudo', '-n', 'systemd-run', '--on-active=20s', 'systemctl', 'restart', 'smart-study-wa-worker'],
                   capture_output=True)
