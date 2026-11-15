"""
scripts/bot_eval.py — the WhatsApp bot's regression eval: score every bot change before deploy.

Born from the 03/11/2026 QA run (37 turns through the real router, weighted 80%). Each case in
tests/bot_eval_cases.yaml is sent through core.handle() exactly like the owner's chat — sender 'qa-probe', a throwaway
state file — and checked with wording-independent checks whose ground truth is read from the data at eval time
(tau_calendar, the Moodle cache, the lecturer's weekly page…), so the eval doesn't rot when the data changes.

    cd ~/smart_study_agent && venv312/bin/python scripts/bot_eval.py [--only id1,id2] [--compare old.json]
    … --resolve        resolve the checks against today's data, no bot / no LLM (cheap sanity check)
    … --list           the case ids

Verdicts: pass (1) · partial (0.5: every HARD check passed, a SOFT one — latency, route, should_* — did not) ·
fail (0) · skip (the data a check needs isn't there today, or valid_until passed; not counted).
Results → data/bot_eval_results/<timestamp>.json (the only place this script writes in the project).

Sandbox (fail closed) — nothing leaves the machine, nothing in the project / vault changes:
  • every write under the project tree and the Obsidian vault goes to a throwaway copy-on-write overlay in /tmp
    (open / os.open / replace / rename / remove / mkdir / stat are redirected; an audit hook rejects anything that
    slips past). The bot reads its own writes back within the run, like production.
  • outward actions raise and FAIL the case: WhatsApp outbox / Cloud API sends, Google Calendar writes (and any
    non-GET Google API request), Gmail send, Claude jobs, the browser agent, NotebookLM source uploads, any
    subprocess except the Gemini-subscription runner, Telegram / Meta / SMTP hosts, localhost ports except Ollama.
  • life.remember / feedback are stubbed (as in the QA probe); the run's LLM spend is reported in the JSON and goes to
    data/usage.jsonl (extra.eval), the one project file besides the results it really writes: it's real money, and the
    budget lock reads that ledger. The overlay isn't used for it, so each call is counted once.
A self-test proves the blocks before the first case; if any block is missing the run aborts.
"""
from __future__ import annotations

import argparse
import builtins
import collections
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _find_root() -> Path:
    for c in (os.environ.get('BOT_EVAL_ROOT'), Path.cwd(), HERE.parent):
        if c and (Path(c) / 'agent' / 'core.py').exists():
            return Path(c).resolve()
    sys.exit('bot_eval: run it from the project root (or set BOT_EVAL_ROOT)')


ROOT = _find_root()
RESULTS = ROOT / 'data' / 'bot_eval_results'
REAL_WRITES = [RESULTS, ROOT / 'data' / 'usage.jsonl']
SUB_RUNNER = '/usr/local/bin/ssa-run-agy'

PLACEHOLDERS = ('staff', 'session', 'lessons_on', 'lessons_left_today', 'lessons_done_today', 'due', 'open_due_week',
                'open_due_soonest', 'exam_or_unknown', 'exam_any', 'exam_nearest', 'site_weeks_latest',
                'moodle_file', 'site_url', 'date')
ROUTE_TAGS = ('classify', 'decide', 'assistant', 'lookup', 'web', 'rag', 'deadline', 'one_deadline', 'calendar',
              'exams', 'direct', 'latest_summary', 'nlm', 'recall', 'planner', 'life', 'day', 'mind', 'tool')
CASE_KEYS = {'id', 'q', 'prior', 'must_contain', 'must_contain_each', 'must_not_contain', 'should_contain',
             'should_not_contain', 'submitted_not_pending', 'route_any', 'route_not', 'max_seconds', 'valid_until',
             'note', 'fixture_mail'}
WEIGHT = {'pass': 1.0, 'partial': 0.5, 'fail': 0.0}
ICON = {'pass': '✅', 'partial': '⚠️', 'fail': '❌', 'skip': '⏭️'}
PH_RX = re.compile(r'^\{([a-z_]+)(?::([^{}]*))?\}$')


# ── the cases file ──────────────────────────────────────────────────────────

def default_cases_path() -> Path:
    for p in (HERE.parent / 'tests' / 'bot_eval_cases.yaml', HERE / 'bot_eval_cases.yaml',
              ROOT / 'tests' / 'bot_eval_cases.yaml'):
        if p.exists():
            return p
    return ROOT / 'tests' / 'bot_eval_cases.yaml'


def load_cases(path: Path | None = None) -> dict:
    import yaml
    with open(path or default_cases_path(), encoding='utf-8') as f:
        return yaml.safe_load(f)


def _groups(v) -> list[list[str]]:
    return [[g] if isinstance(g, str) else list(g) for g in (v or [])]


def _strings(case: dict) -> list[str]:
    out = [s for k in ('must_contain', 'should_contain') for g in _groups(case.get(k)) for s in g]
    out += list(case.get('must_not_contain') or []) + list(case.get('should_not_contain') or [])
    if case.get('must_contain_each'):
        out.append(case['must_contain_each'])
    return out


def validate(data) -> list[str]:
    """Schema errors of a parsed cases file ([] = fine). Used by the safety-test gate — no network, no bot."""
    errs: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get('cases'), list) or not data['cases']:
        return ['top level must be a mapping with a non-empty "cases" list']
    courses = data.get('courses') or {}
    if not isinstance(courses, dict) or not all(isinstance(v, list) and v and all(isinstance(w, str) for w in v)
                                                for v in courses.values()):
        errs.append('"courses" must map a course key to a non-empty list of words')
    d = data.get('defaults') or {}
    if not isinstance(d, dict) or set(d) - {'max_seconds', 'must_not_contain'}:
        errs.append('"defaults" may hold only max_seconds / must_not_contain')
    seen = set()
    for i, c in enumerate(data['cases']):
        where = f'case #{i + 1} ({c.get("id") if isinstance(c, dict) else c!r})'
        if not isinstance(c, dict):
            errs.append(f'{where}: not a mapping')
            continue
        if extra := set(c) - CASE_KEYS:
            errs.append(f'{where}: unknown keys {sorted(extra)}')
        cid = c.get('id')
        if not isinstance(cid, str) or not re.fullmatch(r'[a-z0-9_]+', cid or ''):
            errs.append(f'{where}: id must be a lower-case slug')
        elif cid in seen:
            errs.append(f'{where}: duplicate id')
        seen.add(cid)
        if not isinstance(c.get('q'), str) or not c['q'].strip():
            errs.append(f'{where}: q must be a non-empty string')
        if 'prior' in c and not (isinstance(c['prior'], list) and all(isinstance(p, str) and p.strip() for p in c['prior'])):
            errs.append(f'{where}: prior must be a list of strings')
        for k in ('must_contain', 'should_contain'):
            v = c.get(k)
            if v is not None and not (isinstance(v, list) and v and all(
                    isinstance(g, str) or (isinstance(g, list) and g and all(isinstance(s, str) for s in g)) for g in v)):
                errs.append(f'{where}: {k} must be a list of strings / any-of lists')
        for k in ('must_not_contain', 'should_not_contain', 'route_any', 'route_not'):
            v = c.get(k)
            if v is not None and not (isinstance(v, list) and all(isinstance(s, str) and s for s in v)):
                errs.append(f'{where}: {k} must be a list of strings')
        for k in ('route_any', 'route_not'):
            for t in c.get(k) or []:
                if t.split(':')[0] not in ROUTE_TAGS:
                    errs.append(f'{where}: unknown route tag {t!r}')
        if 'must_contain_each' in c and not (isinstance(c['must_contain_each'], str) and PH_RX.match(c['must_contain_each'])):
            errs.append(f'{where}: must_contain_each must be one placeholder')
        if not any(c.get(k) for k in ('must_contain', 'must_contain_each', 'should_contain')):
            errs.append(f'{where}: needs at least one positive check')
        if 'max_seconds' in c and not (isinstance(c['max_seconds'], (int, float)) and c['max_seconds'] > 0):
            errs.append(f'{where}: max_seconds must be a positive number')
        if 'fixture_mail' in c and not (isinstance(c['fixture_mail'], list) and c['fixture_mail'] and all(
                isinstance(m, dict) and isinstance(m.get('text'), str) for m in c['fixture_mail'])):
            errs.append(f'{where}: fixture_mail must be a list of mails with a text')
        if 'submitted_not_pending' in c and not isinstance(c['submitted_not_pending'], bool):
            errs.append(f'{where}: submitted_not_pending must be true/false')
        if 'valid_until' in c:
            try:
                date.fromisoformat(str(c['valid_until']))
            except ValueError:
                errs.append(f'{where}: valid_until must be YYYY-MM-DD')
        for s in _strings(c):
            if not isinstance(s, str):
                continue
            if '{' in s or '}' in s:
                m = PH_RX.match(s)
                if not m or m.group(1) not in PLACEHOLDERS:
                    errs.append(f'{where}: bad placeholder {s!r}')
                elif m.group(1) in ('staff', 'session', 'due', 'exam_or_unknown', 'site_weeks_latest', 'moodle_file') \
                        and (m.group(2) or '').split(':')[0] not in courses:
                    errs.append(f'{where}: {s!r} names a course missing from "courses"')
    return errs


# ── ground truth from the data ──────────────────────────────────────────────

HE_MONTHS = ['ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני', 'יולי', 'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר']
WEEKDAYS = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
TITLE_RX = re.compile(r'^(?:ד"ר|ד״ר|דר\'|פרופ\'|פרופ׳|פרופסור|גב\'|גב׳|מר|Dr\.?|Prof\.?)\s+')
UNKNOWN = ['לא מצאתי', 'עוד לא', 'לא פורסם', 'לא ידוע', 'אין לי', 'אין עדיין', 'טרם', 'לא מופיע', 'לא יודע']


def date_forms(d: date) -> list[str]:
    return list(dict.fromkeys([f'{d.day:02d}/{d.month:02d}', f'{d.day}/{d.month}', f'{d.day:02d}.{d.month:02d}',
                               f'{d.day}.{d.month}', f'{d.day} ב{HE_MONTHS[d.month - 1]}',
                               f'{d.day} {HE_MONTHS[d.month - 1]}']))


class Truth:
    """Placeholder → acceptable strings, read from the project's data now. None = the data isn't there (→ skip)."""

    def __init__(self, courses: dict, root: Path = ROOT, now: datetime | None = None):
        self.courses, self.root, self.now = courses, root, now or datetime.now()
        self._events = self._items = None

    # data
    def events(self) -> list[dict]:
        if self._events is None:
            try:
                self._events = json.loads((self.root / 'data' / 'tau_calendar.json').read_text(encoding='utf-8')).get('events') or []
            except (OSError, ValueError):
                self._events = []
        return self._events

    def items(self) -> list[dict]:
        if self._items is None:
            try:                                     # what the bot itself sees: Moodle + the lecturer-site exercises
                from utils.deadlines import load_items
                self._items = load_items()
            except Exception:
                try:
                    self._items = json.loads((self.root / 'data' / 'assignments' / '_cache.json')
                                             .read_text(encoding='utf-8')).get('assignments') or []
                except (OSError, ValueError):
                    self._items = []
        return [a for a in self._items if isinstance(a, dict) and a.get('due_date')]

    def ck_of(self, course: str) -> str | None:
        for ck, words in self.courses.items():
            if words and words[0] in (course or ''):
                return ck
        return None

    @staticmethod
    def _due(a: dict) -> datetime | None:
        try:
            return datetime.fromisoformat(str(a['due_date'])).replace(tzinfo=None)
        except (KeyError, ValueError):
            return None

    def _day(self, when: str) -> date | None:
        today = self.now.date()
        if when == 'today':
            return today
        if when == 'tomorrow':
            return today + timedelta(days=1)
        if when in WEEKDAYS:
            return today + timedelta(days=(WEEKDAYS[when] - today.weekday()) % 7 or 7)
        return None

    def _lessons(self, ck: str | None = None, kind: str | None = None) -> list[dict]:
        out = []
        for e in self.events():
            if e.get('is_exam') or not e.get('time'):
                continue
            k = e.get('kind') or ''
            if kind == 'lecture' and 'שיעור' not in k or kind == 'tutorial' and 'תרגיל' not in k:
                continue
            if not kind and not any(w in k for w in ('שיעור', 'תרגיל', 'מעבדה', 'סדנה', 'סמינר')):
                continue
            if ck and self.ck_of(e.get('course', '')) != ck:
                continue
            out.append(e)
        return sorted(out, key=lambda e: (e['date'], e['time']))

    @staticmethod
    def _start(e: dict) -> datetime:
        return datetime.fromisoformat(f"{e['date']}T{e['time']}")

    def _end(self, e: dict) -> datetime:
        return datetime.fromisoformat(f"{e['date']}T{e['end_time']}") if e.get('end_time') else self._start(e) + timedelta(hours=2)

    # resolvers
    def staff(self, ck: str, role: str):
        from agent import timetable
        word = self.courses[ck][0]
        for line in timetable.staff(self.events()):
            head, _, rest = line.partition(' — ')
            if word not in head:
                continue
            for part in rest.split(' · '):
                r, _, name = part.partition(': ')
                if (role == 'lecturer') == (r == 'מרצה'):
                    name = TITLE_RX.sub('', re.sub(r"\s*\(קב['׳] \d+\)\s*$", '', name).strip())
                    sur = name.split()[-1] if name.split() else ''
                    return [name] + ([sur] if len(sur) >= 3 and sur != name else [])
        return None

    def session(self, ck: str, kind: str, when: str, field: str):
        ls = self._lessons(ck, kind)
        if when == 'next':
            ls = [e for e in ls if self._start(e) > self.now]
        else:
            d = self._day(when)
            ls = [e for e in ls if d and e['date'] == d.isoformat()]
        if not ls:
            return None
        e = ls[0]
        if field == 'room':
            return [e['room']] if e.get('room') else None
        if field == 'building':
            return [w for w in re.split(r'\s*-\s*', e.get('building') or '') if len(w) >= 3] or None
        if field == 'time':
            return [e['time'], e['time'].lstrip('0')]
        if field == 'date':
            return date_forms(date.fromisoformat(e['date']))
        return None

    def lessons_on(self, when: str):
        d = self._day(when)
        if not d:
            return None
        return list(dict.fromkeys(self.courses[ck][0] for e in self._lessons() if e['date'] == d.isoformat()
                                  for ck in [self.ck_of(e['course'])] if ck))

    def lessons_left_today(self, field: str = 'time'):
        t = self.now.date().isoformat()
        return list(dict.fromkeys(e['time'] for e in self._lessons() if e['date'] == t and self._end(e) > self.now))

    def lessons_done_today(self, field: str = 'time'):
        t = self.now.date().isoformat()
        left = set(self.lessons_left_today())
        return [x for x in dict.fromkeys(e['time'] for e in self._lessons() if e['date'] == t and self._end(e) <= self.now)
                if x not in left]

    def due(self, ck: str, title: str):
        for a in self.items():
            if a.get('course_key') == ck and str(a.get('title', '')).strip() == title.strip() and self._due(a):
                return date_forms(self._due(a).date())
        return None

    def _open_week(self) -> list[dict]:
        return sorted([a for a in self.items() if not a.get('submitted') and self._due(a)
                       and self.now <= self._due(a) <= self.now + timedelta(days=7)], key=self._due)

    def open_due_week(self):
        return [str(a['title']).strip() for a in self._open_week()]

    def open_due_soonest(self):
        w = self._open_week()
        return [str(w[0]['title']).strip()] if w else None

    def submitted_recent(self) -> list[str]:
        return [str(a['title']).strip() for a in self.items() if a.get('submitted') and self._due(a)
                and self.now - timedelta(days=1) <= self._due(a) <= self.now + timedelta(days=8)]

    def _exams(self, ck: str | None = None) -> list[dict]:
        t = self.now.date().isoformat()
        return sorted([e for e in self.events() if e.get('is_exam') and e.get('date', '') >= t
                       and (ck is None or self.ck_of(e.get('course', '')) == ck)], key=lambda e: e['date'])

    def exam_or_unknown(self, ck: str):
        ex = self._exams(ck)
        return [f for e in ex for f in date_forms(date.fromisoformat(e['date']))] if ex else list(UNKNOWN)

    def exam_any(self, field: str = 'date'):
        ex = self._exams()
        return [f for e in ex for f in date_forms(date.fromisoformat(e['date']))] or None

    def exam_nearest(self, field: str = 'date'):
        ex = self._exams()
        return date_forms(date.fromisoformat(ex[0]['date'])) if ex else None

    def site_weeks_latest(self, ck: str):
        try:
            txt = (self.root / 'data' / 'moodle' / ck / 'site_weeks.txt').read_text(encoding='utf-8')
        except OSError:
            return None
        seg = txt.split('דברים שעשינו', 1)[-1]
        seg = re.split(r'(?:\s\d{1,2}){3,}\s', seg, maxsplit=1)[0]     # the run of empty weeks ("5 6 7 8 …") ends it
        parts = re.split(r'(?:^|\s)(\d{1,2})\s+(?=\D)', ' ' + seg)
        latest = None
        for i in range(1, len(parts) - 1, 2):
            nums = re.findall(r'(?<!\d)\d{4}(?!\d)', parts[i + 1])
            if nums:
                latest = nums
        return latest

    def moodle_file(self, ck: str, pattern: str):
        d = self.root / 'data' / 'moodle' / ck / 'files'
        try:
            hits = sorted(p for p in d.iterdir() if fnmatch.fnmatch(p.name, pattern))
        except OSError:
            return None
        return [re.sub(r'^\d+_', '', hits[0].stem)] if hits else None

    def site_url(self, *_):
        try:
            m = re.search(r"^URL\s*=\s*'([^']+)'", (self.root / 'scripts' / 'build_course_site.py').read_text(encoding='utf-8'), re.M)
        except OSError:
            return None
        return [m.group(1).rstrip('/').split('://', 1)[-1]] if m else None

    def date(self, iso: str):
        return date_forms(date.fromisoformat(iso))

    def resolve(self, s: str):
        """A literal → [s]; a placeholder → its strings, or None when the data isn't there."""
        m = PH_RX.match(s) if isinstance(s, str) else None
        if not m:
            return [s]
        name, args = m.group(1), [a for a in (m.group(2) or '').split(':') if a != ''] if m.group(2) else []
        if name == 'moodle_file':                       # the glob may hold ':' — keep it whole
            args = [m.group(2).split(':', 1)[0], m.group(2).split(':', 1)[1]]
        try:
            r = getattr(self, name)(*args)
        except Exception as e:
            print(f'  ⚠️ resolving {s}: {e!r}')
            return None
        return list(r) if r is not None else None


def resolve_case(case: dict, truth: Truth, defaults: dict) -> tuple[dict | None, str]:
    """→ (checks with every placeholder replaced, '') or (None, why it is skipped today)."""
    if case.get('valid_until') and truth.now.date() > date.fromisoformat(str(case['valid_until'])):
        return None, f"valid_until {case['valid_until']} passed"

    def groups(key):
        out = []
        for g in _groups(case.get(key)):
            opts = []
            for s in g:
                r = truth.resolve(s)
                if r is None:
                    if len(g) == 1:
                        return None, f'{key}: no data for {s}'
                    continue
                opts += r
            if not opts:
                return None, f'{key}: no data for {g}'
            out.append(list(dict.fromkeys(opts)))
        return out, ''

    def flat(key, extra=()):
        out = list(extra)
        for s in case.get(key) or []:
            out += truth.resolve(s) or []
        return list(dict.fromkeys(out))

    chk = {}
    for key in ('must_contain', 'should_contain'):
        g, why = groups(key)
        if g is None:
            return None, why
        chk[key] = g
    if case.get('must_contain_each'):
        r = truth.resolve(case['must_contain_each'])
        if r is None:
            return None, f"must_contain_each: no data for {case['must_contain_each']}"
        chk['must_contain'] = chk['must_contain'] + [[x] for x in r]
    chk['must_not_contain'] = flat('must_not_contain', defaults.get('must_not_contain') or [])
    chk['should_not_contain'] = flat('should_not_contain')
    chk['submitted'] = truth.submitted_recent() if case.get('submitted_not_pending') else []
    chk['route_any'] = list(case.get('route_any') or [])
    chk['route_not'] = list(case.get('route_not') or [])
    chk['max_seconds'] = float(case.get('max_seconds') or defaults.get('max_seconds') or 25)
    return chk, ''


# ── scoring ─────────────────────────────────────────────────────────────────

def norm(s: str) -> str:
    s = (s or '').replace('׳', "'").replace('״', '"').replace('`', "'").replace('–', '-').replace('—', '-')
    s = re.sub(r'[*_~]', '', s)
    return re.sub(r'\s+', ' ', s).lower()


def has(text: str, token: str) -> bool:
    """Substring match on normalised text; a token that starts / ends with a digit must not touch more digits
    ('5/11' is not in '15/11', room '118' is not in '1180')."""
    t, n = norm(token), norm(text)
    if not t:
        return False
    pre = r'(?<!\d)' if t[0].isdigit() else ''
    post = r'(?!\d)' if t[-1].isdigit() else ''
    return re.search(pre + re.escape(t) + post, n) is not None


def _route_match(pattern: str, tags: list[str]) -> bool:
    return any(t == pattern or t.startswith(pattern + ':') for t in tags)


def score(answer: str, tags: list[str], seconds: float, chk: dict, violations: list[str], error: str = '') -> dict:
    hard, soft = [], []
    if error:
        hard.append(f'exception: {error[:200]}')
    if violations:
        hard.append('side effect attempted (blocked): ' + '; '.join(violations)[:300])
    for g in chk['must_contain']:
        if not any(has(answer, s) for s in g):
            hard.append(f'missing {g[:4]}')
    for s in chk['must_not_contain']:
        if has(answer, s):
            hard.append(f'contains {s!r}')
    for g in chk['should_contain']:
        if not any(has(answer, s) for s in g):
            soft.append(f'should have {g[:4]}')
    for s in chk['should_not_contain']:
        if has(answer, s):
            soft.append(f'should not have {s!r}')
    for title in chk['submitted']:
        for line in answer.splitlines():
            if has(line, title) and not re.search(r'הוגש|הגשת|✅|submitted|כבר הגש', line):
                soft.append(f'submitted {title!r} shown as pending')
                break
    if chk['route_any'] and not any(_route_match(p, tags) for p in chk['route_any']):
        soft.append(f'route not any of {chk["route_any"]}')
    for p in chk['route_not']:
        if _route_match(p, tags):
            soft.append(f'route went through {p!r}')
    if seconds > chk['max_seconds']:
        soft.append(f'slow {seconds:.1f}s > {chk["max_seconds"]:.0f}s')
    verdict = 'fail' if hard else 'partial' if soft else 'pass'
    return {'verdict': verdict, 'score': WEIGHT[verdict], 'hard': hard, 'soft': soft}


# ── the sandbox ─────────────────────────────────────────────────────────────

SPEND = {'calls': 0, 'cost_usd': 0.0, 'by_service': collections.Counter()}


class Blocked(RuntimeError):
    """An outward side effect the eval refused."""


class Sandbox:
    WFLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    PATH_EVENTS = {'os.remove': (0,), 'os.rmdir': (0,), 'os.mkdir': (0,), 'os.rename': (0, 1), 'os.truncate': (0,),
                   'os.chmod': (0,), 'os.chown': (0,), 'os.link': (0, 1), 'os.symlink': (1,), 'os.utime': (0,),
                   'os.setxattr': (0,), 'os.removexattr': (0,), 'shutil.rmtree': (0,), 'shutil.move': (0, 1),
                   'shutil.copyfile': (1,), 'shutil.copytree': (1,), 'shutil.copymode': (1,), 'shutil.copystat': (1,)}
    PROC_EVENTS = ('os.system', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.fork', 'os.forkpty', 'pty.spawn')
    DENY_HOSTS = ('graph.facebook.com', 'api.telegram.org', 'smtp.gmail.com', 'smtp.')
    LOCAL_OK_PORTS = {11434}                                   # Ollama

    def __init__(self, roots: list[Path], allow: list[Path]):
        self.roots = [os.path.abspath(str(r)) for r in roots]
        self.allow = [os.path.abspath(str(a)) for a in allow]
        self.overlay = tempfile.mkdtemp(prefix='bot_eval_overlay_')
        self.violations: list[dict] = []
        self.absorbed = collections.Counter()
        self.case = ''
        self._o = {'open': io.open, 'os_open': os.open, 'stat': os.stat, 'replace': os.replace, 'rename': os.rename,
                   'remove': os.remove, 'unlink': os.unlink, 'mkdir': os.mkdir, 'rmdir': os.rmdir}

    # paths
    @staticmethod
    def _abs(p) -> str | None:
        if isinstance(p, int) or p is None:
            return None
        try:
            p = os.fspath(p)
        except TypeError:
            return None
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        return os.path.abspath(p)

    def guarded(self, a: str | None) -> bool:
        if not a or a.startswith(self.overlay):
            return False
        if any(a == x or a.startswith(x + os.sep) for x in self.allow):
            return False
        return any(a == r or a.startswith(r + os.sep) for r in self.roots)

    def shadow(self, a: str) -> str:
        for i, r in enumerate(self.roots):
            if a == r or a.startswith(r + os.sep):
                return os.path.join(self.overlay, str(i), os.path.relpath(a, r))
        raise ValueError(a)

    def _exists(self, p: str) -> bool:
        try:
            self._o['stat'](p)
            return True
        except OSError:
            return False

    def _cow(self, real: str, sh: str) -> None:
        """Copy-on-write: the overlay starts from the real file."""
        os.makedirs(os.path.dirname(sh), exist_ok=True)
        if not self._exists(sh) and os.path.isfile(real):
            with self._o['open'](real, 'rb') as s, self._o['open'](sh, 'wb') as d:
                shutil.copyfileobj(s, d)

    def _rel(self, a: str) -> str:
        for r in self.roots:
            if a.startswith(r):
                return os.path.relpath(a, os.path.dirname(r))
        return a

    def violation(self, what: str, detail: str = '') -> Blocked:
        self.violations.append({'case': self.case, 'what': what, 'detail': str(detail)[:300]})
        return Blocked(f'bot_eval blocked {what} {str(detail)[:120]}')

    # redirected filesystem calls
    def install(self) -> None:
        o, sb = self._o, self

        def s_open(file, mode='r', *a, **k):
            p = sb._abs(file)
            if sb.guarded(p):
                sh = sb.shadow(p)
                if any(c in mode for c in 'wax+'):
                    if 'w' not in mode:
                        sb._cow(p, sh)
                    os.makedirs(os.path.dirname(sh), exist_ok=True)
                    sb.absorbed[sb._rel(p)] += 1
                    return o['open'](sh, mode, *a, **k)
                if sb._exists(sh):
                    return o['open'](sh, mode, *a, **k)
            return o['open'](file, mode, *a, **k)

        def s_os_open(path, flags, *a, **k):
            p = sb._abs(path) if 'dir_fd' not in k else None
            if sb.guarded(p):
                sh = sb.shadow(p)
                if flags & sb.WFLAGS:
                    if not flags & os.O_TRUNC:
                        sb._cow(p, sh)
                    os.makedirs(os.path.dirname(sh), exist_ok=True)
                    sb.absorbed[sb._rel(p)] += 1
                    return o['os_open'](sh, flags, *a, **k)
                if sb._exists(sh):
                    return o['os_open'](sh, flags, *a, **k)
            return o['os_open'](path, flags, *a, **k)

        def s_stat(path, *a, **k):
            p = sb._abs(path)
            if sb.guarded(p) and not k.get('dir_fd'):
                sh = sb.shadow(p)
                if sb._exists(sh):
                    return o['stat'](sh, *a, **k)
            return o['stat'](path, *a, **k)

        def _mv(name):
            def mv(src, dst, *a, **k):
                s, d = sb._abs(src), sb._abs(dst)
                if sb.guarded(s):
                    sh = sb.shadow(s)
                    sb._cow(s, sh)
                    src = sh
                if sb.guarded(d):
                    sh = sb.shadow(d)
                    os.makedirs(os.path.dirname(sh), exist_ok=True)
                    sb.absorbed[sb._rel(d)] += 1
                    dst = sh
                return o[name](src, dst, *a, **k)
            return mv

        def _rm(name):
            def rm(path, *a, **k):
                p = sb._abs(path)
                if sb.guarded(p):
                    sh = sb.shadow(p)
                    sb.absorbed[f'(delete) {sb._rel(p)}'] += 1
                    if sb._exists(sh):
                        return o[name](sh, *a, **k)
                    return None                                    # the real one stays
                return o[name](path, *a, **k)
            return rm

        def s_chmod(path, *a, **k):
            p = sb._abs(path)                                  # a refreshed Google token lands in the overlay
            if sb.guarded(p):
                sh = sb.shadow(p)
                return real_chmod(sh, *a, **k) if sb._exists(sh) else None
            return real_chmod(path, *a, **k)

        def s_mkdir(path, *a, **k):
            p = sb._abs(path)
            if sb.guarded(p):
                if sb._exists(p):
                    raise FileExistsError(17, 'File exists', str(path))
                sh = sb.shadow(p)
                if sb._exists(sh):
                    raise FileExistsError(17, 'File exists', str(path))
                os.makedirs(os.path.dirname(sh), exist_ok=True)
                return o['mkdir'](sh, *a, **k)
            return o['mkdir'](path, *a, **k)

        io.open = builtins.open = s_open
        os.open, os.stat = s_os_open, s_stat
        os.replace, os.rename = _mv('replace'), _mv('rename')
        os.remove, os.unlink, os.rmdir = _rm('remove'), _rm('unlink'), _rm('rmdir')
        os.mkdir = s_mkdir
        real_chmod, os.chmod = os.chmod, s_chmod
        sys.addaudithook(self._hook)

    # the backstop: anything that bypassed the redirects
    def _hook(self, event: str, args: tuple) -> None:
        if event == 'open':
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or \
                      (isinstance(flags, int) and bool(flags & self.WFLAGS))
            if writing and self.guarded(self._abs(path)):
                if '__pycache__' in str(path):                     # bytecode caches: refused, not a bot side effect
                    raise PermissionError(f'bot_eval: read-only project ({path})')
                self.violations.append({'case': self.case, 'what': 'unredirected write', 'detail': str(path)[:300]})
                raise PermissionError(f'bot_eval: read-only project ({path})')
        elif event in self.PATH_EVENTS:
            for i in self.PATH_EVENTS[event]:
                if i < len(args) and self.guarded(self._abs(args[i])):
                    raise self.violation(f'fs {event}', args[i])
        elif event == 'subprocess.Popen':
            argv = [str(x) for x in (args[1] if len(args) > 1 and isinstance(args[1], (list, tuple)) else [args[1] if len(args) > 1 else args[0]])]
            if not (argv[:4] == ['sudo', '-n', '-u', 'geminiagent'] and SUB_RUNNER in argv):
                raise self.violation('subprocess', ' '.join(argv)[:200])
        elif event in self.PROC_EVENTS:
            raise self.violation(event, args[0] if args else '')
        elif event == 'socket.getaddrinfo':
            host = str(args[0] or '').lower() if args else ''
            if any(host == h or host.startswith(h) if h.endswith('.') else host == h for h in self.DENY_HOSTS):
                raise self.violation('network', host)
        elif event == 'socket.connect':
            addr = args[1] if len(args) > 1 else None
            if isinstance(addr, tuple) and len(addr) >= 2 and str(addr[0]) in ('127.0.0.1', '::1', 'localhost') \
                    and addr[1] not in self.LOCAL_OK_PORTS:
                raise self.violation('localhost connect', f'{addr[0]}:{addr[1]}')
        elif event == 'urllib.Request':
            url, data, _h, method = (tuple(args) + (None,) * 4)[:4]
            method = (method or ('POST' if data is not None else 'GET')).upper()
            if method not in ('GET', 'HEAD') and not str(url).startswith('http://127.0.0.1:11434/'):
                raise self.violation('http ' + method, url)


def _block(sb: Sandbox, mod, name: str, label: str) -> None:
    if not callable(getattr(mod, name, None)):
        return

    def blocked(*a, **k):
        raise sb.violation(label, f'{name}({", ".join(repr(x)[:40] for x in a[:2])})')
    blocked._bot_eval_blocked = True
    setattr(mod, name, blocked)


def install_blocks(sb: Sandbox) -> dict:
    """Outward actions → Blocked (+ a recorded violation). A module that can't be imported aborts the run."""
    import importlib
    stubs = collections.Counter()
    req = {m: importlib.import_module(m) for m in ('whatsapp.outbox', 'integrations.gcal', 'agent.claude_job',
                                                   'agent.web_agent', 'agent.life', 'agent.feedback', 'agent.brain',
                                                   'agent.nlm_docs')}
    ob = req['whatsapp.outbox']
    for n in dir(ob):
        if n.startswith('send') or n in ('_job', 'reask_open', 'release_deferred'):
            _block(sb, ob, n, 'whatsapp send')
    try:
        wc = importlib.import_module('whatsapp.client')
        for n in ('send_text', 'send_buttons', 'send_document', 'upload_media', '_post_message', 'mark_read'):
            _block(sb, wc, n, 'whatsapp cloud api')
    except Exception as e:                                           # the hosts are blocked by the hook too
        print(f'  (whatsapp.client not importable: {e!r})')
    gc = req['integrations.gcal']
    for n in dir(gc):
        if re.search(r'insert|create|delete|update|patch|move|add_|set_|sync_|import|clear', n) and callable(getattr(gc, n)) \
                and not n.startswith('_') and n not in ('configured',):
            _block(sb, gc, n, 'gcal write')
    for n in ('start', 'deploy', 'discard', '_redo', '_sh', '_as_agent', 'stop', 'resume_now', 'cancel_queued', 'advance'):
        _block(sb, req['agent.claude_job'], n, 'claude job')
    for n in ('start', 'answer', '_spawn', 'run'):
        _block(sb, req['agent.web_agent'], n, 'browser agent')
    _block(sb, req['agent.nlm_docs'], 'add', 'notebooklm upload')
    try:
        gs = importlib.import_module('generators.gmail_sender')
        for cls in vars(gs).values():
            if isinstance(cls, type) and callable(getattr(cls, 'send', None)):
                def _no_send(self, *a, **k):
                    raise sb.violation('gmail send', '')
                cls.send = _no_send
    except Exception as e:
        print(f'  (generators.gmail_sender not importable: {e!r})')
    try:                                                              # every Google API write, however it's built
        from googleapiclient import http as gh
        orig_exec = gh.HttpRequest.execute

        def g_execute(self, *a, **k):
            if (self.method or 'GET').upper() != 'GET' and 'freeBusy' not in (self.uri or ''):
                raise sb.violation(f'google api {self.method}', (self.uri or '').split('?')[0])
            return orig_exec(self, *a, **k)
        gh.HttpRequest.execute = g_execute

        def b_execute(self, *a, **k):
            raise sb.violation('google api batch', '')
        gh.BatchHttpRequest.execute = b_execute
    except ImportError:
        pass
    life, fb, brain = req['agent.life'], req['agent.feedback'], req['agent.brain']
    life.remember = lambda *a, **k: (stubs.update(['life.remember']), '')[1]
    fb.implicit = lambda *a, **k: (stubs.update(['feedback.implicit']), '')[1]
    fb.record = lambda *a, **k: (stubs.update(['feedback.record']), '[blocked feedback]')[1]
    brain_tmp = Path(sb.overlay) / 'brain_notes'
    brain.brain_dir = lambda: brain_tmp
    return stubs


def self_test(sb: Sandbox) -> list[str]:
    """Prove the blocks before spending a single LLM call. → problems ([] = airtight)."""
    import subprocess
    from whatsapp import outbox
    from integrations import gcal
    from agent import claude_job
    bad = []
    probe = ROOT / 'data' / '_bot_eval_selftest.txt'
    try:
        probe.write_text('x', encoding='utf-8')
        with open(probe, 'a', encoding='utf-8') as f:
            f.write('y')
        if probe.read_text(encoding='utf-8') != 'xy':
            bad.append('overlay read-back failed')
        if os.path.exists(probe) is False:
            bad.append('overlay stat failed')
        os.replace(probe, probe.with_suffix('.moved'))
        if sb._exists(str(probe)) or sb._exists(str(probe.with_suffix('.moved'))):
            bad.append('a write reached the real project tree')
    except Exception as e:
        bad.append(f'overlay error {e!r}')
    for what, fn in (('outbox.send_text', lambda: outbox.send_text('qa-probe', 'x')),
                     ('gcal.insert_event', lambda: gcal.insert_event({})),
                     ('claude_job.start', lambda: claude_job.start('qa-probe', 'x')),
                     ('subprocess', lambda: subprocess.run(['true'])),
                     ('os.system', lambda: os.system('true'))):
        try:
            fn()
            bad.append(f'{what} was NOT blocked')
        except Blocked:
            pass
        except Exception as e:
            bad.append(f'{what} raised {e!r} instead of Blocked')
    sb.violations.clear()
    sb.absorbed.clear()
    return bad


# ── tracing the route ───────────────────────────────────────────────────────

def install_trace(tags: list) -> None:
    from agent import core, assistant, course_lookup, nlm_ask, nlm_docs, daytools
    from bot import brain, handlers
    from integrations import websearch

    def wrap(mod, name, tag):
        f = getattr(mod, name, None)
        if not callable(f):
            return

        def w(*a, **k):
            entry = [None, 0.0]                      # in call order (nested calls finish first)
            tags.append(entry)
            t = time.time()
            r = f(*a, **k)
            entry[0], entry[1] = (tag(r) if callable(tag) else tag), round(time.time() - t, 1)
            return r
        setattr(mod, name, w)

    wrap(brain, 'classify', lambda r: f"classify:{(r or {}).get('action')}")
    wrap(assistant, 'decide', lambda r: f"decide:{(r or {}).get('kind')}")
    wrap(course_lookup, 'find', lambda r: 'lookup:found' if r else 'lookup:none')
    wrap(websearch, 'ask_web', 'web')
    wrap(handlers, 'handle_ask', 'rag')
    wrap(handlers, 'handle_deadline', 'deadline')
    wrap(nlm_ask, 'ask', 'nlm')
    wrap(nlm_docs, 'ask', 'recall')
    wrap(daytools, 'day_overview', 'day')
    try:                                             # the agent loop: its verdict and each tool it called
        from agent import mind
        wrap(mind, 'think', lambda r: f"mind:{(r or {}).get('kind', 'none')}")
        for name in list(mind.TOOLS):
            mind.TOOLS[name]['fn'] = _traced(tags, mind.TOOLS[name]['fn'], f'tool:{name}')
    except ImportError:
        pass                                         # an older tree: the same cases, for the before numbers
    for name, tag in (('_calendar', 'calendar'), ('_exams_text', 'exams'), ('_latest_summary', 'latest_summary'),
                      ('_assistant', 'assistant')):
        wrap(core, name, tag)
    for name, tag in (('_one_deadline', 'one_deadline'), ('_direct', 'direct'), ('_plan_and_learn', 'planner'),
                      ('_life', 'life')):
        wrap(core, name, lambda r, _t=tag: _t if r is not None else None)


# ── running ─────────────────────────────────────────────────────────────────

def _traced(tags: list, fn, tag: str):
    def w(*a, **k):
        tags.append([tag, 0.0])
        return fn(*a, **k)
    return w


def _with_mail(fixture: list[dict], fetch=None):
    """A planted mailbox for a case: what integrations.tau_mail.fetch returns, whatever the query."""
    mails = [{'id': f'fixture{i}', 'thread': f'fixture{i}', 'from': m.get('from', ''), 'subject': m.get('subject', ''),
              'date': datetime.now().strftime('%a, %d %b %Y %H:%M:%S +0200'), 'labels': ['INBOX'],
              'snippet': m['text'][:120], 'text': m['text'], 'attachments': []} for i, m in enumerate(fixture)]
    return lambda query='', limit=30, service=None: list(mails)[:limit]


def _answer_text(outs) -> str:
    parts = []
    for o in outs or []:
        s = o.text or ''
        if getattr(o, 'path', None):
            s += f'\n[{o.kind} {Path(str(o.path)).name}]'
        if getattr(o, 'buttons', None):
            s += '\n' + ' | '.join(str(b[0]) for b in o.buttons)
        parts.append(s)
    return '\n---\n'.join(parts)


def run_case(case: dict, chk: dict, sb: Sandbox, tags: list, state_dir: Path) -> dict:
    from agent import core
    sb.case = case['id']
    from integrations import tau_mail
    real_fetch, real_conf = tau_mail.fetch, tau_mail.configured
    if case.get('fixture_mail'):
        tau_mail.fetch, tau_mail.configured = _with_mail(case['fixture_mail']), lambda: True
    sp = state_dir / f"state_{case['id']}.json"
    sp.unlink(missing_ok=True)
    turns, err, outs, dt = [], '', [], 0.0
    nviol = len(sb.violations)
    for i, q in enumerate(list(case.get('prior') or []) + [case['q']]):
        tags.clear()
        t, paid = time.time(), SPEND['cost_usd']
        try:
            outs = core.handle(core.In('qa-probe', 'text', text=q, channel='whatsapp', role='owner'), state_path=sp)
            err = ''
        except Exception:
            outs, err = [], traceback.format_exc()[-800:]
        dt = time.time() - t
        done = [e for e in tags if e[0]]
        turns.append({'q': q, 'a': _answer_text(outs), 'sec': round(dt, 1), 'route': [e[0] for e in done],
                      'route_sec': [e[1] for e in done], 'cost': round(SPEND['cost_usd'] - paid, 5)})
    tau_mail.fetch, tau_mail.configured = real_fetch, real_conf
    final = turns[-1]
    viol = [f"{v['what']}: {v['detail']}" for v in sb.violations[nviol:]]
    sc = score(final['a'], final['route'], dt, chk, viol, err)
    return {'id': case['id'], 'q': case['q'], **sc, 'sec': final['sec'], 'cost': final['cost'], 'route': final['route'],
            'answer': final['a'], 'turns': turns, 'violations': viol, 'error': err, 'checks': chk}


def summarize(results: list[dict]) -> dict:
    counted = [r for r in results if r['verdict'] != 'skip']
    secs = [r['sec'] for r in counted]
    n = len(counted)
    return {'cases': len(results), 'counted': n, 'skipped': len(results) - n,
            **{v: sum(r['verdict'] == v for r in counted) for v in ('pass', 'partial', 'fail')},
            'weighted_pct': round(100 * sum(r['score'] for r in counted) / n, 1) if n else 0.0,
            'strict_pct': round(100 * sum(r['verdict'] == 'pass' for r in counted) / n, 1) if n else 0.0,
            'median_sec': round(statistics.median(secs), 1) if secs else 0.0,
            'p90_sec': round(sorted(secs)[int(0.9 * (n - 1))], 1) if secs else 0.0,
            'max_sec': max(secs) if secs else 0.0,
            'cost_per_case': round(sum(r.get('cost') or 0 for r in counted) / n, 4) if n else 0.0,
            'slow': sum(any(s.startswith('slow') for s in r.get('soft', [])) for r in counted),
            'violations': sum(len(r.get('violations') or []) for r in results)}


def print_scorecard(results: list[dict], summ: dict) -> None:
    print('\n' + '═' * 100)
    for r in results:
        why = '' if r['verdict'] in ('pass',) else ' — ' + '; '.join((r.get('hard') or []) + (r.get('soft') or []) or [r.get('skip', '')])
        route = '→'.join(r.get('route') or [])
        print(f"{ICON[r['verdict']]} {r['id']:<34} {r.get('sec', 0):>5.1f}s  {route[:46]:<46}{why[:160]}")
    print('═' * 100)
    print(f"score {summ['weighted_pct']}% weighted · {summ['strict_pct']}% strict  |  ✅ {summ['pass']}  ⚠️ {summ['partial']}  "
          f"❌ {summ['fail']}  ⏭️ {summ['skipped']}  |  median {summ['median_sec']}s · max {summ['max_sec']}s · "
          f"{summ['slow']} slow  |  side-effect attempts: {summ['violations']}")


def compare(new: dict, old_path: Path) -> list[str]:
    old = json.loads(Path(old_path).read_text(encoding='utf-8'))
    o = {r['id']: r for r in old.get('cases') or []}
    lines = [f"\nvs {Path(old_path).name}: {old['summary']['weighted_pct']}% → {new['summary']['weighted_pct']}% weighted, "
             f"median {old['summary']['median_sec']}s → {new['summary']['median_sec']}s"]
    reg, imp = [], []
    for r in new['cases']:
        p = o.get(r['id'])
        if not p or 'skip' in (p['verdict'], r['verdict']):
            continue
        d = r['score'] - p['score']
        row = f"  {r['id']:<34} {ICON[p['verdict']]}→{ICON[r['verdict']]}  {p['sec']}s→{r['sec']}s"
        if d < 0:
            reg.append(row + '  ' + '; '.join(r['hard'] + r['soft'])[:120])
        elif d > 0:
            imp.append(row)
    lines += ['REGRESSIONS:'] + (reg or ['  none']) + ['IMPROVEMENTS:'] + (imp or ['  none'])
    new_ids, old_ids = {r['id'] for r in new['cases']}, set(o)
    if new_ids - old_ids:
        lines.append(f"new cases: {sorted(new_ids - old_ids)}")
    if old_ids - new_ids:
        lines.append(f"dropped cases: {sorted(old_ids - new_ids)}")
    return lines


def _code_version() -> str:
    h = hashlib.sha1()
    for p in ('agent/core.py', 'agent/assistant.py', 'bot/brain.py', 'bot/handlers.py', 'agent/course_lookup.py'):
        try:
            h.update((ROOT / p).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--only', help='comma-separated case ids (or id prefixes)')
    ap.add_argument('--compare', help='an earlier results JSON')
    ap.add_argument('--cases', help='cases YAML (default tests/bot_eval_cases.yaml)')
    ap.add_argument('--resolve', action='store_true', help='resolve the checks only — no bot, no LLM')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--out', help='results JSON path (default data/bot_eval_results/<timestamp>.json)')
    a = ap.parse_args(argv)

    sys.dont_write_bytecode = True                                   # no .pyc into the project's __pycache__
    os.environ.setdefault('TZ', 'Asia/Jerusalem')                    # like the worker's systemd unit
    os.environ.setdefault('SSA_ROUTER', 'agent')                     # …and its router
    os.environ.setdefault('SSA_BRAIN', '1')                          # SSA_BRAIN=0: the old path, for the before numbers
    if hasattr(time, 'tzset'):
        time.tzset()
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    data = load_cases(Path(a.cases) if a.cases else None)
    if errs := validate(data):
        print('cases file invalid:\n  ' + '\n  '.join(errs))
        return 2
    cases = data['cases']
    if a.only:
        want = [w.strip() for w in a.only.split(',') if w.strip()]
        cases = [c for c in cases if any(c['id'] == w or c['id'].startswith(w) for w in want)]
    if a.list:
        print('\n'.join(f"{c['id']:<34} {c['q']}" for c in cases))
        return 0
    defaults = data.get('defaults') or {}
    truth = Truth(data.get('courses') or {})
    resolved = [(c, *resolve_case(c, truth, defaults)) for c in cases]
    if a.resolve:
        for c, chk, why in resolved:
            print(f"{c['id']:<34} " + (f'SKIP {why}' if chk is None else
                  json.dumps({k: v for k, v in chk.items() if v}, ensure_ascii=False)[:300]))
        return 0

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        pass
    vault = []
    try:
        from config.paths import VAULT_ROOT
        vault = [Path(VAULT_ROOT)]
    except Exception:
        pass
    sb = Sandbox([ROOT] + vault, allow=REAL_WRITES)
    sb.install()
    stubs = install_blocks(sb)
    if problems := self_test(sb):
        print('❌ sandbox self-test failed — not running:\n  ' + '\n  '.join(problems))
        return 3
    print(f'🔒 sandbox ok (overlay {sb.overlay}) · {len(cases)} cases · code {_code_version()}')
    try:
        from utils import gemini_sub
        sub = gemini_sub.install()
    except Exception as e:
        print(f'  (gemini_sub: {e!r})')
        sub = False
    from utils.budget import budget_lock_reason
    if budget_lock_reason():                                         # paid fallbacks must not run past the lock
        print(f'budget lock is on — no eval: {budget_lock_reason()}')
        return 2
    spend = SPEND
    try:
        from utils import usage_tracker
        orig_log = usage_tracker.log_usage

        def log_usage(**kw):
            rec = orig_log(**{**kw, 'extra': {**(kw.get('extra') or {}), 'eval': True}})
            spend['calls'] += 1
            spend['cost_usd'] += float(rec.get('cost_usd') or 0)
            spend['by_service'][kw.get('service', '?')] += 1
            return rec
        usage_tracker.log_usage = log_usage
    except Exception:
        pass
    tags: list = []
    install_trace(tags)
    state_dir = Path(tempfile.mkdtemp(prefix='bot_eval_state_'))

    results, t0 = [], time.time()
    for i, (c, chk, why) in enumerate(resolved, 1):
        if chk is None:
            r = {'id': c['id'], 'q': c['q'], 'verdict': 'skip', 'score': None, 'skip': why, 'sec': 0.0, 'route': [],
                 'hard': [], 'soft': [], 'violations': []}
        else:
            r = run_case(c, chk, sb, tags, state_dir)
        results.append(r)
        print(f"[{i}/{len(resolved)}] {ICON[r['verdict']]} {c['id']} {r['sec']}s {'→'.join(r['route'])}", flush=True)

    summ = summarize(results)
    out = {'meta': {'ts': datetime.now().isoformat(timespec='seconds'), 'host': os.uname().nodename if hasattr(os, 'uname') else '',
                    'code_version': _code_version(), 'cases_file': str(a.cases or default_cases_path()),
                    'gemini_subscription': bool(sub), 'wall_sec': round(time.time() - t0, 1),
                    'llm_usage': {'calls': spend['calls'], 'cost_usd': round(spend['cost_usd'], 4),
                                  'by_service': dict(spend['by_service'])},
                    'absorbed_writes': dict(sb.absorbed.most_common(40)), 'stubs': dict(stubs),
                    'violations': sb.violations},
           'summary': summ, 'cases': results}
    print_scorecard(results, summ)
    if a.compare:
        print('\n'.join(compare(out, Path(a.compare))))
    path = Path(a.out) if a.out else RESULTS / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    print(f'\n→ {path}  (LLM: {spend["calls"]} logged calls, ${spend["cost_usd"]:.3f}; subscription={bool(sub)})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
