"""
scripts/weekly_bot_report.py — once a week, a short WhatsApp to the owner about the bot itself (10/11).

What it says, for the last 7 days, all from files already on the VM (no LLM, no network but the send):
  * API spend — data/usage.jsonl (the ledger every Gemini/Whisper call writes; the budget lock reads the same file):
    the week's total, the bot's share (agent.* / bot.* / gcal_parse / learn.*), the top items, and the month against the
    $50 lock. Calls on the Gemini subscription (bot.subscription) cost $0 and are counted apart.
  * turns — data/agent_conversations.jsonl, by who wrote (The owner / the partner / others) and how many went through the mind loop
    (data/mind_log.jsonl).
  * the safety gate — mind_log's gate {'asked': [...], 'ok': [...]}: a turn where something asked wasn't approved.
  * failures — "⚠️ משהו נכשל אצלי" / "לא זמין כרגע" answers, and mind turns that ended in error / timeout / late.
  * Claude jobs — data/claude_jobs.json, the ones started this week, by status (the nightly review counted apart), and
    the deploy gate's rejections (logs/deploy.log*, "REJECTED").
  * 🧠 self-improvement — agent/improve.py: scouts run, findings, fixed by itself, waiting for ✅, backlog, next run.
  * fixes deployed — every "deploy A -> B" in logs/deploy.log* this week, as `git log A..B` subjects from the bare repo
    the push lands in (~/smart_study_agent.git), or SSA_GIT_DIR.
No study content, ever: this is about the bot.

    venv/bin/python3 scripts/weekly_bot_report.py --dry     # print it, send nothing, mark nothing
    venv/bin/python3 scripts/weekly_bot_report.py           # send to the owner chat, once per ISO week
    venv/bin/python3 scripts/weekly_bot_report.py --force   # send again this week
Cron: docs/RUNBOOK.md ("📊 דוח שבועי על הבוט").
"""
from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SENT = ROOT / 'data' / 'weekly_bot_report_sent.json'
BOT_OPS = ('agent.', 'bot.', 'gcal_parse', 'learn.')
FREE_OPS = ('bot.subscription',)
HARD_LIMIT_USD = 50.0                          # scripts/budget_check.py: the lock
FAILED_RX = re.compile(r'משהו נכשל אצלי|לא זמין כרגע')
MIND_FAILS = ('error', 'timeout', 'late')
NIGHTLY_RX = re.compile(r'^סקירה יומית')
DEPLOY_RX = re.compile(r'^(\S+)\s+deploy\s+([0-9a-f]{7,40})\s+->\s+([0-9a-f]{7,40})')
REJECT_RX = re.compile(r'^(\S+)\s+REJECTED\s+([0-9a-f]{7,40})')
MAX_FIXES = 10
WIP_RX = re.compile(r'\s*wip(?![a-z])', re.I)

try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo('Asia/Jerusalem')         # the worker and the hooks write naive local times
except Exception:                              # no tzdata (Windows without the package)
    LOCAL = None


def default_paths(root: Path = ROOT) -> dict:
    return {'ledger': root / 'data' / 'usage.jsonl', 'convo': root / 'data' / 'agent_conversations.jsonl',
            'mind': root / 'data' / 'mind_log.jsonl', 'jobs': root / 'data' / 'claude_jobs.json',
            'undelivered': root / 'data' / 'undelivered_alerts.jsonl',
            'security': root / 'data' / 'security_status.json',
            'improve_state': root / 'data' / 'improve_state.json', 'backlog': root / 'data' / 'improvement_backlog.json',
            'filtered': root / 'data' / 'univ_filtered.jsonl',
            'deploy_logs': sorted((root / 'logs').glob('deploy.log*')),
            'git_dir': Path(os.environ.get('SSA_GIT_DIR') or (Path.home() / 'smart_study_agent.git'))}


def _ts(s, naive_tz=None) -> datetime | None:
    """Any timestamp these files hold → aware UTC. 'Z' and offsets are kept; a naive one is local (Israel) unless said."""
    try:
        if isinstance(s, (int, float)) or re.fullmatch(r'\d+(?:\.\d+)?', str(s)):
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except (TypeError, ValueError, OSError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=naive_tz or LOCAL) if (naive_tz or LOCAL) else d.astimezone()
    return d.astimezone(timezone.utc)


def _jsonl(path: Path):
    try:
        with open(path, encoding='utf-8') as f:
            for ln in f:
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue
    except OSError:
        return


def _lines(path: Path) -> list[str]:
    try:
        if path.suffix == '.gz':
            with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
                return f.read().splitlines()
        return path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []


def spend(paths: dict, since: datetime, now: datetime) -> dict:
    week = bot = 0.0
    month = 0.0
    by_op: Counter = Counter()
    free_calls = 0
    m0 = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for r in _jsonl(paths['ledger']):
        t = _ts(r.get('ts'), naive_tz=timezone.utc)          # the ledger writes UTC
        if not t or t > now:
            continue
        c = float(r.get('cost_usd') or 0)
        op = r.get('op') or '(כללי)'
        if t >= m0:
            month += c
        if t < since:
            continue
        if op.startswith(FREE_OPS):
            free_calls += 1
            continue
        week += c
        by_op[op] += c
        if op.startswith(BOT_OPS):
            bot += c
    return {'week': week, 'bot': bot, 'month': month, 'top': by_op.most_common(3), 'free_calls': free_calls}


def turns(paths: dict, since: datetime, now: datetime) -> dict:
    who: Counter = Counter()
    failed = 0
    for r in _jsonl(paths['convo']):
        t = _ts(r.get('ts'))
        if not t or not since <= t <= now:
            continue
        who[r.get('role') or '?'] += 1
        if any(isinstance(o, (list, tuple)) and len(o) > 1 and isinstance(o[1], str) and FAILED_RX.search(o[1])
               for o in r.get('out') or []):
            failed += 1
    mind = refused = 0
    refused_kinds: Counter = Counter()
    mind_fail: Counter = Counter()
    cost = 0.0
    for r in _jsonl(paths['mind']):
        t = _ts(r.get('ts'))
        if not t or not since <= t <= now:
            continue
        mind += 1
        cost += float(r.get('cost') or 0)
        if r.get('kind') in MIND_FAILS:
            mind_fail[r['kind']] += 1
        g = r.get('gate') or {}
        lost = [k for k in g.get('asked') or [] if k not in (g.get('ok') or [])]
        if lost:
            refused += 1
            refused_kinds.update(lost)
    return {'who': who, 'total': sum(who.values()), 'failed': failed, 'mind': mind, 'mind_cost': cost,
            'refused': refused, 'refused_kinds': refused_kinds, 'mind_fail': mind_fail}


def jobs(paths: dict, since: datetime, now: datetime) -> dict:
    try:
        st = json.loads(Path(paths['jobs']).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        st = {}
    status: Counter = Counter()
    nightly = 0
    for j in st.values() if isinstance(st, dict) else []:
        t = _ts(j.get('started'))
        if not t or not since <= t <= now:
            continue
        if NIGHTLY_RX.match(j.get('task') or ''):
            nightly += 1
        status[j.get('status') or '?'] += 1
    deploys, rejected = [], set()
    for p in paths['deploy_logs']:
        for ln in _lines(Path(p)):
            if m := DEPLOY_RX.match(ln):
                t = _ts(m.group(1))
                if t and since <= t <= now:
                    deploys.append((t, m.group(2), m.group(3)))
            elif m := REJECT_RX.match(ln):
                t = _ts(m.group(1))
                if t and since <= t <= now:
                    rejected.add((t.isoformat(), m.group(2)))
    deploys = sorted(set(deploys))
    return {'status': status, 'total': sum(status.values()), 'nightly': nightly, 'deploys': deploys,
            'rejected': len(rejected), 'rejected_shas': sorted({s for _, s in rejected})}


def fixes(git_dir: Path, deploys: list) -> list[str]:
    """The commit subjects the week's deploys brought, newest first; merges and WIP commits left out."""
    out: list[str] = []
    for _, old, new in reversed(deploys):
        try:
            r = subprocess.run(['git', f'--git-dir={git_dir}', 'log', '--no-merges', '--format=%s',
                                f'{old}..{new}'], capture_output=True, text=True, encoding='utf-8', timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            out += [s.strip() for s in r.stdout.splitlines()
                    if s.strip() and not WIP_RX.match(s) and s.strip() not in out]
    return out


def collect(paths: dict | None = None, now: datetime | None = None, git_log=fixes) -> dict:
    paths = paths or default_paths()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = now - timedelta(days=7)
    j = jobs(paths, since, now)
    return {'since': since, 'now': now, 'spend': spend(paths, since, now), 'turns': turns(paths, since, now), 'jobs': j,
            'fixes': git_log(paths['git_dir'], j['deploys']) if j['deploys'] else [],
            'undelivered': _undelivered(paths.get('undelivered')), 'security': _security(paths.get('security'), now),
            'improve': _improve(paths, since), 'filtered': _filtered(paths.get('filtered'), since)}


def _filtered(path, since: datetime) -> int:
    """15/11: university updates not sent because they were another group's (integrations/univ_relevance)."""
    if not path:
        return 0
    n = 0
    try:
        for ln in Path(path).read_text(encoding='utf-8').splitlines():
            try:
                t = _ts(json.loads(ln).get('ts'))
            except (ValueError, AttributeError):
                continue
            n += bool(t and t >= since)
    except OSError:
        return 0
    return n


def _improve(paths: dict, since: datetime) -> list[str]:
    """14/11 "🧠 שיפור עצמי" (agent/improve.py): found, fixed by itself, waiting for ✅, backlog, next run."""
    try:
        from agent import improve
        try:
            jobs = json.loads(Path(paths['jobs']).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            jobs = {}
        local = since.astimezone(LOCAL).replace(tzinfo=None) if LOCAL else since.replace(tzinfo=None)
        return improve.weekly_lines(local, jobs, paths.get('improve_state'), paths.get('backlog'))
    except Exception as e:
        return ['', f'🧠 שיפור עצמי: לא הצלחתי לקרוא ({e!r})'[:120]]


def _security(path, now: datetime) -> dict | None:
    """14/11: the nightly scripts/security_check.py result — counts and the top warnings, never a secret (it has none)."""
    if not path:
        return None
    try:
        d = json.loads(Path(path).read_text(encoding='utf-8'))
        ts = datetime.fromisoformat(d['ts'])
    except (OSError, ValueError, KeyError, TypeError):
        return {'missing': True}
    ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    checks = d.get('checks') or []
    pick = lambda st: [c for c in checks if c.get('status') == st]
    return {'ts': ts, 'stale': now - ts > timedelta(days=2), 'pass': len(pick('PASS')),
            'warn': pick('WARN'), 'fail': pick('FAIL')}


def security_lines(sec: dict | None) -> list[str]:
    if not sec:
        return []
    if sec.get('missing'):
        return ['🔒 אבטחה: אין עדיין תוצאה מהבדיקה הלילית (scripts/security_check.py)']
    head = f"🔒 אבטחה: {sec['pass']} בדיקות עברו, {len(sec['warn'])} אזהרות"
    if sec['fail']:
        head += f", {len(sec['fail'])} כשלים"
    if sec['stale']:
        head += f" (הבדיקה האחרונה ב-{sec['ts'].strftime('%d/%m')} — הלילית לא רצה)"
    top = (sec['fail'] + sec['warn'])[:3]
    return [head] + [f"   • {_short(c.get('reason') or c.get('id') or '', 80)}" for c in top]


def _undelivered(path) -> list[dict]:
    """10/11: alerts WhatsApp couldn't take (utils.notify keeps them; Telegram, the old fallback, is gone)."""
    if not path:
        return []
    try:
        from utils.notify import undelivered
        return undelivered(7, path=Path(path))
    except Exception:
        return []


ROLE = {'owner': 'אתה', 'partner': 'בת הזוג', 'friend': 'חברים', 'contact': 'אנשי קשר'}
STATUS = {'deployed': 'נפרסו', 'ready': 'מחכות לאישור', 'discarded': 'נזרקו', 'finished': 'הסתיימו בלי שינוי לפריסה',
          'failed': 'נכשלו', 'superseded': 'הוחלפו בהרצה חוזרת', 'lost': 'אבדו', 'stopped': 'נעצרו', 'running': 'רצות',
          'parked': 'ממתינות למגבלה'}
GATE = {'calendar_add': 'יומן', 'reminder': 'תזכורת', 'message_partner': 'הודעה לבת הזוג', 'feature': 'משימה לקלוד',
        'exam': 'מבחן', 'exam_place': 'מקום מבחן', 'open': 'שאלה פתוחה', 'memory': 'זיכרון'}


def _short(s: str, n: int = 90) -> str:
    s = re.sub(r'\s+', ' ', s).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + '…'


def build_text(r: dict) -> str:
    sp, tu, jb = r['spend'], r['turns'], r['jobs']
    loc = (lambda d: d.astimezone(LOCAL)) if LOCAL else (lambda d: d.astimezone())
    lines = [f"📊 *הבוט השבוע* ({loc(r['since']).strftime('%d/%m')}–{loc(r['now']).strftime('%d/%m')})", '']
    lines.append(f"💸 API: ${sp['week']:.2f} השבוע, מתוכם הבוט ${sp['bot']:.2f}"
                 + (f" (לולאת המוח ${tu['mind_cost']:.2f})" if tu['mind'] else '') + '.')
    if sp['top']:
        lines.append('   הכי יקר: ' + ' · '.join(f'{op} ${c:.2f}' for op, c in sp['top']))
    lines.append(f"   החודש: ${sp['month']:.2f} מתוך ${HARD_LIMIT_USD:.0f} (נעילה)"
                 + (f" · {sp['free_calls']} קריאות על המנוי ($0)" if sp['free_calls'] else ''))
    who = ' · '.join(f'{ROLE.get(k, k)} {v}' for k, v in tu['who'].most_common())
    lines.append(f"💬 {tu['total']} פניות" + (f' ({who})' if who else '') + (f", {tu['mind']} דרך המוח" if tu['mind'] else ''))
    gate = f"🛡️ השער עצר {tu['refused']} פעולות שלא ביקשת"
    if tu['refused_kinds']:
        gate += ' (' + ', '.join(f'{GATE.get(k, k)} {v}' for k, v in tu['refused_kinds'].most_common()) + ')'
    lines.append(gate)
    if r.get('filtered'):
        lines.append(f"🔇 לא שלחתי {r['filtered']} עדכונים מהאוניברסיטה שהיו של קבוצה אחרת (\"מה סיננת?\")")
    fails =tu['failed'] + sum(tu['mind_fail'].values())
    fl = f'⚠️ תקלות: {fails}'
    parts = ([f"{tu['failed']} תשובות \"משהו נכשל\""] if tu['failed'] else []) + \
            [f'{v} {k}' for k, v in tu['mind_fail'].most_common()]
    lines.append(fl + (f" ({', '.join(parts)})" if parts else ''))
    js = f"🛠️ קלוד: {jb['total']} משימות"
    if jb['status']:
        js += ' — ' + ', '.join(f'{v} {STATUS.get(k, k)}' for k, v in jb['status'].most_common())
    if jb['nightly']:
        js += f" (מתוכן {jb['nightly']} סקירות לילה)"
    lines.append(js)
    lines.append(f"🚀 {len(jb['deploys'])} פריסות, {jb['rejected']} נדחו בבדיקות")
    lines += security_lines(r.get('security'))
    if r.get('undelivered'):
        u = r['undelivered']
        lines.append(f'📭 {len(u)} התראות לא נמסרו בוואטסאפ (data/undelivered_alerts.jsonl)')
        lines += [f"   • {_short(x.get('text') or '', 70)}" for x in u[:3]]
    if r['fixes']:
        lines += ['', '*מה עלה (מהחדש):*'] + [f'• {_short(s)}' for s in r['fixes'][:MAX_FIXES]]
        if len(r['fixes']) > MAX_FIXES:
            lines.append(f"• ועוד {len(r['fixes']) - MAX_FIXES}")
    lines += r.get('improve') or []
    return '\n'.join(lines)


def _week_key(d: date | None = None) -> str:
    y, w, _ = (d or date.today()).isocalendar()
    return f'{y}-W{w:02d}'


def main(argv: list[str] | None = None, paths: dict | None = None, sent_path: Path | None = None, send=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry, force = '--dry' in argv or '--dry-run' in argv, '--force' in argv
    text = build_text(collect(paths))
    if dry:
        print(text)
        return 0
    sent_path = sent_path or SENT
    try:
        sent = json.loads(sent_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        sent = {}
    week = _week_key()
    if sent.get(week) and not force:
        print(f'already sent for {week}')
        return 0
    if send is None:
        from whatsapp import outbox
        chat = outbox.owner_chat()
        if not chat:
            print('no owner chat')
            return 1
        send = lambda t: outbox.send_text(chat, t)
    if not send(text):
        print('send failed')
        return 1
    sent[week] = datetime.now().isoformat(timespec='seconds')
    sent_path.parent.mkdir(parents=True, exist_ok=True)
    sent_path.write_text(json.dumps(dict(sorted(sent.items())[-30:]), ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'sent for {week}')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    os.chdir(str(ROOT))
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=True)
    except ImportError:
        pass
    sys.exit(main())
