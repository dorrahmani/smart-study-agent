"""
agent/nlm_ask.py — course questions go to the course's NotebookLM notebook first (The owner 13/10: "שהשאלות ילכו
ל-NotebookLM ואז נחסוך כסף").

NotebookLM runs on the notebooks account's subscription — no API cost — and answers only from the course's own
sources, with references. Follow-ups within 30 minutes continue the same conversation (bot/nlm_conversations).
Only courses that already HAVE a notebook (config/notebooklm_notebooks.json, scope 'all') — an empty new one
would answer from nothing. Any failure / timeout → None, and the caller falls back to the RAG answer.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / 'config' / 'notebooklm_notebooks.json'
TIMEOUT = 75


_TEX = {r'\varepsilon': 'ε', r'\epsilon': 'ε', r'\delta': 'δ', r'\alpha': 'α', r'\beta': 'β', r'\lambda': 'λ',
        r'\mu': 'μ', r'\sigma': 'σ', r'\pi': 'π', r'\theta': 'θ', r'\infty': '∞', r'\in': '∈', r'\notin': '∉',
        r'\subseteq': '⊆', r'\subset': '⊂', r'\cup': '∪', r'\cap': '∩', r'\le': '≤', r'\leq': '≤', r'\ge': '≥',
        r'\geq': '≥', r'\neq': '≠', r'\ne': '≠', r'\approx': '≈', r'\cdot': '·', r'\times': '×', r'\to': '→',
        r'\rightarrow': '→', r'\Rightarrow': '⇒', r'\iff': '⇔', r'\sum': '∑', r'\int': '∫', r'\iint': '∬',
        r'\partial': '∂', r'\nabla': '∇', r'\forall': '∀', r'\exists': '∃', r'\sqrt': '√', r'\lim': 'lim',
        r'\sup': 'sup', r'\inf': 'inf', r'\max': 'max', r'\min': 'min', r'\ldots': '…', r'\dots': '…', r'\,': ' ',
        r'\;': ' ', r'\quad': ' ', r'\left': '', r'\right': '', r'\mathbb{R}': 'ℝ', r'\mathbb{N}': 'ℕ'}


def whatsapp_text(s: str) -> str:
    """NotebookLM's markdown + LaTeX → plain WhatsApp text (14/10: '\\(f_n(x)\\)', '[2, 3]', '###' came through raw)."""
    import re
    s = s.replace('\\\\', '\\')
    s = re.sub(r'(?m)^(\s*)[*\-+]\s+', r'\1• ', s)                      # markdown bullets → • (before ** → *)
    s = re.sub(r'\\(?:bar|overline|hat|tilde|vec)\{([^{}]*)\}', r'\1̄', s)
    s = re.sub(r'\\[()\[\]]', '', s)                                    # \( \) \[ \] delimiters
    s = re.sub(r'\$\$?', '', s)
    s = re.sub(r'\\(?:text|mathrm|mathbf|operatorname)\{([^{}]*)\}', r'\1', s)
    s = re.sub(r'\^\{([^{}]*)\}', r'^(\1)', s)                          # powers/indices first, so \frac sees no braces
    s = re.sub(r'_\{([^{}]*)\}', r'_(\1)', s)
    s = re.sub(r'\\binom\{([^{}]*)\}\{([^{}]*)\}', r'C(\1,\2)', s)        # 18/10: \binom{5}{3} stayed raw
    # 20/10 (micro's exam solutions): "\rightarrow" came out "arrow" (\right was stripped first) and a \frac with a
    # root inside stayed raw — arrows and roots first, then \frac from the innermost out
    for k in (r'\Longrightarrow', r'\Rightarrow', r'\rightarrow', r'\longrightarrow', r'\Leftrightarrow', r'\iff', r'\to'):
        s = s.replace(k, _TEX.get(k, '⇒' if 'R' in k[:3] or 'Long' in k else '→'))
    s = re.sub(r'\\sqrt\{([^{}]*)\}', lambda m: '√' + (m.group(1) if re.fullmatch(r'[\w.]+', m.group(1)) else f'({m.group(1)})'), s)
    for _ in range(4):
        t = re.sub(r'\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}',              # simple parts need no parentheses: 2/3
                   lambda m: '/'.join(x if re.fullmatch(r'[\w.]+|√[\w.]+', x) else f'({x})' for x in m.groups()), s)
        if t == s:
            break
        s = t
    s = re.sub(r'\\left|\\right(?!arrow)', '', s)
    for k in sorted(_TEX, key=len, reverse=True):
        s = s.replace(k, _TEX[k])
    s = re.sub(r'\s*\[\d+(?:\s*[,–-]\s*\d+)*\]', '', s)                 # citation markers [1] / [2, 3]
    s = re.sub(r'(?m)^\s*#{1,6}\s*(.+?)\s*$', r'*\1*', s)               # headings → bold
    s = re.sub(r'(?m)^\s*-{3,}\s*$', '', s)                             # --- separators
    s = s.replace('**', '*').replace('**', '*')
    s = re.sub(r'\*\s*\*', '', s)
    return re.sub(r'\n{3,}', '\n\n', s).strip()


HEALTH = ROOT / 'data' / 'nlm_health.json'
COOLDOWN_H = 6


def _broken_recently(path: Path | None = None) -> bool:
    """14/10: Google drops the NotebookLM login on the server within ~a day (the session is copied from the PC). After a
    failure, skip NotebookLM for 6h — answer right away from the RAG instead of a slow failed attempt each time."""
    from datetime import datetime, timedelta
    try:
        ts = json.loads((path or HEALTH).read_text(encoding='utf-8')).get('failed_at')
        return bool(ts) and datetime.now() - datetime.fromisoformat(ts) < timedelta(hours=COOLDOWN_H)
    except (OSError, ValueError):
        return False


def _mark(failed: bool, path: Path | None = None) -> None:
    from datetime import datetime
    try:
        (path or HEALTH).write_text(json.dumps({'failed_at': datetime.now().isoformat(timespec='seconds')} if failed else {}),
                                    encoding='utf-8')
    except OSError:
        pass


def has_notebook(course: str, path: Path | None = None) -> bool:
    if _broken_recently():
        return False
    try:
        idx = json.loads((path or INDEX).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    nb = (idx.get(course) or {}).get('all') or {}
    return bool(nb.get('notebook_id')) and bool(nb.get('sources_synced'))   # an empty notebook can't answer


def _bounded(fn, seconds: float):
    """fn() in a daemon thread. `with ThreadPoolExecutor` didn't protect the bot: .result(timeout) gave up on time, but
    leaving the block waited for the stuck worker, so a NotebookLM hang held the reply. A hang is now left behind."""
    box: dict = {}

    def run():
        try:
            box['value'] = fn()
        except BaseException as e:                      # handed to the caller
            box['error'] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f'no answer within {seconds}s')
    if 'error' in box:
        raise box['error']
    return box.get('value')


import re as _re

NOT_FOUND_RX = _re.compile(
    r'(?:המקורות|במקורות|בחומרים?|בחומרי הקורס|במסמכים)\s+(?:שסופקו\s+|שבידי\s+|שלפניי\s+)?(?:לא|אינם|אין)|'
    r'לא\s+(?:מצאתי|נמצא(?:ה|ו)?|מופיע(?:ה|ים)?|מוזכר(?:ת|ים)?|מצוין|מצוינ(?:ת|ים)|ציינו)|אינו\s+מופיע|אינה\s+מופיעה|'
    r'אין\s+(?:בהם|בהן|במקורות|מידע|לי מידע|התייחסות|אזכור)|'
    r"not\s+(?:mentioned|found|specified|covered)|(?:do|does)\s*(?:n't|\s+not)\s+(?:contain|mention|include|specify)|"
    r'no\s+information', _re.I)
GROUNDED_SECONDS = 30
_TITLES: dict = {}
TITLES_SECONDS = 3600


def source_titles(course: str, agent) -> dict:
    """{source id: title} of the course's 'all' notebook, kept an hour (an answer cites ids, not names)."""
    import time as _t
    at, names = _TITLES.get(course, (0, None))
    if names is None or _t.time() - at > TITLES_SECONDS:
        from generators import notebooklm_agent as nlm
        nb = agent.get_or_create_notebook(course, 'all')
        names = {s.id: s.title for s in nlm._run(agent._list_sources(nb))}
        _TITLES[course] = (_t.time(), names)
    return names


def grounded(answer: str, references: list) -> bool:
    """A notebook answer the bot can stand on: it cites the course's sources and doesn't open with "the sources don't say".
    (A long answer that says, at its end, that one detail is missing is still an answer.)"""
    a = (answer or '').strip()
    if not a or not references:
        return False
    head = a[:240]
    return not (NOT_FOUND_RX.search(head) or (len(a) < 400 and NOT_FOUND_RX.search(a)))


def ask_grounded(course: str, question: str, chat: str = 'course_lookup', agent=None,
                 timeout: float = GROUNDED_SECONDS) -> dict | None:
    """17/11 the course-question path asks the notebook first (free, from the course's own transcripts and summaries).
    → {'text': WhatsApp answer with its sources, 'sources': [titles], 'sec'} only when grounded; None = no notebook /
    too slow / failed / "not in the sources", and the caller goes on to the paid lookup. Too slow is not "down": only
    a real failure switches NotebookLM off for 6h."""
    import time as _t
    if not has_notebook(course):
        return None
    start = _t.monotonic()
    try:
        from bot import nlm_conversations as conv
        from generators.notebooklm_agent import NotebookLMAgent
        agent = agent or NotebookLMAgent()
        prev = conv.get(chat, course, 'all')

        def run():
            r = agent.ask(course, 'all', question, prev)
            if any(x.get('source_id') and not x.get('title') for x in r.get('references') or []):
                names = source_titles(course, agent)
                for x in r['references']:
                    x['title'] = x.get('title') or names.get(x.get('source_id'), '')
            return r
        result = _bounded(run, timeout)
    except TimeoutError as e:
        print(f'  ⚠️ NotebookLM too slow ({course}): {e}'[:200])
        return None
    except Exception as e:
        print(f'  ⚠️ NotebookLM ask failed ({course}): {e!r}'[:300])
        _mark(True)
        return None
    _mark(False)
    answer, refs = (result.get('answer') or '').strip(), result.get('references') or []
    if result.get('conversation_id'):
        try:
            conv.update(chat, course, 'all', result['conversation_id'])
        except Exception:
            pass
    if not grounded(answer, refs):
        return None
    text = whatsapp_text(answer)
    if len(text) > 3200:
        text = text[:3200] + '…'
    titles = list(dict.fromkeys((r.get('title') or '').strip() for r in refs if r.get('title')))[:3]
    return {'text': '🎓 ' + text + (f"\n\n📚 מקורות: {', '.join(titles)}" if titles else ''), 'sources': titles,
            'sec': round(_t.monotonic() - start, 1)}


def ask(course: str, question: str, chat: str, agent=None, timeout: int = TIMEOUT, mark_failed: bool = True) -> str | None:
    """→ a WhatsApp-ready answer, or None (no notebook / failed / too slow). A caller with a short timeout passes
    mark_failed=False: its "too slow" isn't "NotebookLM is down", which would switch it off for everyone for 6h."""
    if not has_notebook(course):
        return None
    try:
        from bot import nlm_conversations as conv
        from generators.notebooklm_agent import NotebookLMAgent
        agent = agent or NotebookLMAgent()
        prev = conv.get(chat, course, 'all')
        result = _bounded(lambda: agent.ask(course, 'all', question, prev), timeout)
    except Exception as e:
        print(f'  ⚠️ NotebookLM ask failed ({course}): {e!r}'[:300])
        if mark_failed:
            _mark(True)
        return None
    _mark(False)
    answer = (result.get('answer') or '').strip()
    if not answer:
        return None
    if result.get('conversation_id'):
        try:
            conv.update(chat, course, 'all', result['conversation_id'])
        except Exception:
            pass
    answer = whatsapp_text(answer)
    if len(answer) > 3200:
        answer = answer[:3200] + '…'
    titles = list(dict.fromkeys((r.get('title') or '').strip() for r in result.get('references') or [] if r.get('title')))[:3]
    return '🎓 ' + answer + (f"\n\n📚 מקורות: {', '.join(titles)}" if titles else '')
