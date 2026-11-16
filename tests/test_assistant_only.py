"""
tests/test_assistant_only.py — setup path 1: the personal secretary with the student domain OFF.

Runs the code the way a new "assistant only" install has it: SSA_STUDY=0, no university login (TAU_*), no Moodle /
Panopto / NotebookLM session, no Gemini key, an empty HOME. Nothing here may crash, and nothing study-related may be
offered, ticked or promised.

    PYTHONIOENCODING=utf-8 python -m tests.test_assistant_only

The checks run in a child process so every module is imported fresh under that environment.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHILD = '--child' in sys.argv

STRIP = ('TAU_USERNAME', 'TAU_ID', 'TAU_PASSWORD', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_API_KEY',
         'SSA_ACTIVE_COURSES', 'GMAIL_CREDENTIALS_JSON', 'TELEGRAM_BOT_TOKEN', 'SSA_PROACTIVE')


def _child_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP}
    env.update(SSA_STUDY='0', SSA_TEST_MODE='assistant-only', PYTHONIOENCODING='utf-8',
               HOME=str(home), USERPROFILE=str(home), SMART_STUDY_VAULT=str(home / 'vault'))
    return env


# ─── the checks (child process) ──────────────────────────────────────────────

def t_flags_and_config():
    from config import features, paths
    assert features.study_enabled() is False
    assert features.courses_config() == {}                         # even though tests/ may have copied an example
    assert paths.ACTIVE_COURSES == []


def t_semester_and_day_have_no_lectures():
    from utils import semester
    from agent import daytools
    assert semester.load_config() == {}
    ok, _why = semester.semester_active({}, date.today())
    assert not ok
    assert daytools.lectures_on(date.today()) == [] and daytools.lectures_on(date.today() + timedelta(days=1)) == []


def t_mind_offers_only_the_secretary_tools():
    from agent import mind
    names = set(mind.active_tools())
    assert not names & mind.STUDY_TOOLS, names & mind.STUDY_TOOLS
    assert {'calendar', 'tasks', 'waiting', 'memory', 'web_search'} <= names
    assert mind.run_tool('obligations', {}, mind.Turn('מה חסר לי', 'owner@test'))['error'].startswith('אין כלי')
    assert mind.ASSISTANT_ONLY_NOTE in mind.system_prompt()


def t_menu_and_courses_text():
    from agent import core
    assert core.menu() == core.MENU_ASSISTANT
    for word in ('מודל', 'בחן אותי', 'המבחן', 'הקורסים'):
        assert word not in core.MENU_ASSISTANT, word
    assert 'SSA_STUDY' in core.my_courses_text()


def t_worker_ticks_skip_the_student_domain():
    from agent import core
    from whatsapp import worker
    ticks = worker.tick_list(core)
    study = {core.exam_sync_prompts, core.exam_day_messages, core.timetable_tick, core.mail_tick, core.review_offer_tick,
             core.lesson_sync_tick, core.review_resume_tick, core.homework_site_tick, core.loops_tick,
             core.office_hours_tick}
    assert not set(ticks) & study
    assert core.event_reminder_tick in ticks and core.tasks_tick in ticks and worker.proactive_outs in ticks
    buf = io.StringIO()
    with redirect_stdout(buf):
        for tick in ticks:                                         # each one, for real, with no credentials at all
            assert isinstance(worker._one_tick(tick), list)
    assert 'failed' not in buf.getvalue(), buf.getvalue()[:500]


def t_secretary_handles_messages_without_study_config():
    from agent import core
    import bot.brain as brain
    from integrations import gcal
    saved = gcal.configured, gcal.parse_events, gcal.week_text, brain.classify, core.audit
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / 'state.json'
        try:
            gcal.configured = lambda token=None: True
            gcal.parse_events = lambda text, now=None, client=None: (calls.append('parse'), (
                [gcal.validate({'title': 'ארוחת ערב עם ההורים', 'start': (date.today() + timedelta(days=2)).isoformat()
                                + 'T20:00', 'end': (date.today() + timedelta(days=2)).isoformat() + 'T22:00'})], ''))[1]
            gcal.week_text = lambda d, days=7, service=None, hide_private=False: (calls.append('week'), 'השבוע ביומן')[1]
            brain.classify = lambda m, history='': {'action': 'calendar_add' if 'ליומן' in m else
                                                    'calendar_list' if 'השבוע' in m else 'ask', 'params': {}}
            core.audit = lambda *a, **k: None
            r = core.handle(core.In('owner', 'text', text='תפריט'), state_path=sp)
            assert r and core.MENU_ASSISTANT[:20] in r[0].text and 'בסיכומים' not in r[0].text, r[0].text[:80]
            r = core.handle(core.In('owner', 'text', text='תוסיף ליומן ארוחת ערב עם ההורים ביום חמישי ב-20:00'),
                            state_path=sp)
            assert r[0].kind == 'buttons' and 'parse' in calls                       # an offer behind a ✅, not a write
            r = core.handle(core.In('owner', 'text', text='מה יש לי השבוע ביומן'), state_path=sp)
            assert 'week' in calls and r[0].text == 'השבוע ביומן'
            for text in ('מה הקורסים שלי', 'מה יש לי מחר', 'מה מחכה לי', 'מה המשימות שלי', 'היי'):
                r = core.handle(core.In('owner', 'text', text=text), state_path=sp)   # no crash, always an answer
                assert r and all(o.text or o.kind != 'text' for o in r), text
        finally:
            gcal.configured, gcal.parse_events, gcal.week_text, brain.classify, core.audit = saved


def t_reminder_and_task_stores_work():
    """A reminder comes due and is sent with its buttons; an open task is listed — no course config anywhere."""
    from agent import life, tasks
    with tempfile.TemporaryDirectory() as tmp:
        lp, tp = Path(tmp) / 'life.json', Path(tmp) / 'tasks.json'
        now = datetime.now().replace(second=0, microsecond=0)
        life.add_reminder('owner@test', {'text': 'לקנות חלב', 'when': (now - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M')},
                          path=lp)
        due = life.due_now(now, path=lp)
        assert [r['text'] for r, _ in due] == ['לקנות חלב'] and due[0][1], due
        tasks.add('owner@test', 'להחזיר ספרים לספרייה', (now + timedelta(days=2)).date().isoformat(), path=tp, now=now)
        assert [x['text'] for x in tasks.open_tasks('owner@test', path=tp)] == ['להחזיר ספרים לספרייה']


CHECKS = [t_flags_and_config, t_semester_and_day_have_no_lectures, t_mind_offers_only_the_secretary_tools,
          t_menu_and_courses_text, t_worker_ticks_skip_the_student_domain,
          t_secretary_handles_messages_without_study_config, t_reminder_and_task_stores_work]


def child() -> int:
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    import tests  # noqa: F401  — pins the student domain OFF (SSA_TEST_MODE=assistant-only)
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print(f'  ✅ {fn.__name__}')
        except Exception:
            failed += 1
            print(f'  ❌ {fn.__name__}\n' + ''.join('     ' + l for l in traceback.format_exc().splitlines(True)[-8:]))
    print(f'\nassistant-only: {len(CHECKS) - failed}/{len(CHECKS)} passed')
    return 1 if failed else 0


def test_assistant_only_mode():
    """pytest entry: the whole child run must pass."""
    assert main() == 0


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='ssa_home_') as home:
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child'], cwd=ROOT,
                           env=_child_env(Path(home)), capture_output=True, text=True, encoding='utf-8', timeout=600)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr[-3000:])
    return r.returncode


if __name__ == '__main__':
    sys.exit(child() if CHILD else main())
