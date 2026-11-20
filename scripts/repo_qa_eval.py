"""
scripts/repo_qa_eval.py — how well the bot answers the owner's friends about the public repo (17/11).

Runs every question in tests/repo_qa_eval.json through the real answer path (agent/group_helper.reply_for: the
screen, then NotebookLM or the local index + Gemini, then guard), and scores it without a model:
  answer   → status ok, every `must` group matched (any of its alternatives), nothing of `must_not`, ≤10 lines,
             a file named (SETUP.md, agent/mind.py, .env.example …)
  none     → "אין לזה תשובה בפרויקט" (or an ok answer that matches `none_ok`)
  refuse   → the fixed refusal, before any model
  offtopic → nothing sent, or the one-line reply

    python scripts/repo_qa_eval.py                        # NotebookLM first, the local index when it fails
    python scripts/repo_qa_eval.py --path local           # only the local index + Gemini (costs a few cents)
    python scripts/repo_qa_eval.py --path nlm --sync      # re-mirror the repo into the notebook first
    python scripts/repo_qa_eval.py --dir ../smart-study-release   # a local checkout instead of GitHub (unpushed docs)
    python scripts/repo_qa_eval.py --only wa-01,key-06 -v # some questions, with the answers
Exit 0 when the pass rate is at least --min (default 0.9).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
QUESTIONS = ROOT / 'tests' / 'repo_qa_eval.json'
FILE_RX = re.compile(r'[\w./-]*\w\.(?:md|py|example|ya?ml|txt|sh|json|service|conf)\b|\.env\b|deploy/|config/|scripts/')


def _norm(s: str) -> str:
    return re.sub(r'[*_`]', '', s or '').replace('״', '"').replace('׳', "'").lower()


def score(item: dict, status: str, text: str) -> tuple[bool, str]:
    """→ (passed, why not)."""
    kind, t = item.get('kind', 'answer'), _norm(text)
    if kind == 'refuse':
        return (status == 'refused', '' if status == 'refused' else f'not refused ({status})')
    if kind == 'offtopic':
        return (status in ('offtopic', 'refused'), '' if status in ('offtopic', 'refused') else f'answered ({status})')
    if kind == 'none':
        if status == 'none' or (status == 'ok' and any(_norm(a) in t for a in item.get('none_ok') or [])):
            return True, ''
        return False, f'expected none ({status})'
    if status != 'ok':
        return False, f'status {status}'
    missing = [g[0] for g in item.get('must') or [] if not any(_norm(a) in t for a in (g if isinstance(g, list) else [g]))]
    if missing:
        return False, 'missing ' + ', '.join(missing)
    bad = [x for x in item.get('must_not') or [] if _norm(x) in t]
    if bad:
        return False, 'said ' + ', '.join(bad)
    if len([r for r in text.splitlines() if r.strip()]) > 11:            # the intro line is not counted
        return False, 'too long'
    if not FILE_RX.search(text):
        return False, 'no file named'
    return True, ''


def _spent(since: float) -> float:
    from utils.usage_tracker import LEDGER_PATH
    total = 0.0
    try:
        for ln in LEDGER_PATH.read_text(encoding='utf-8').splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if str(r.get('op', '')).startswith('starter_help') and r.get('ts', '') >= time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(since)):
                total += float(r.get('cost_usd') or 0)
    except OSError:
        pass
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--path', choices=('auto', 'nlm', 'local'), default='auto')
    ap.add_argument('--dir', help='a local checkout of the repo instead of GitHub')
    ap.add_argument('--only', default='')
    ap.add_argument('--sync', action='store_true', help='mirror the repo into the notebook first (force)')
    ap.add_argument('--min', type=float, default=0.9)
    ap.add_argument('--out', help='write the results as JSON here')
    ap.add_argument('--env', help='the .env to load (default: the one of this checkout)')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(a.env) if a.env else ROOT / '.env', override=False)
    except ImportError:
        pass
    if a.dir:
        os.environ['SSA_STARTER_DIR'] = str(Path(a.dir).resolve())
    from agent import group_helper, starter_help
    if a.dir:                                              # its own index file: never the live one
        starter_help.INDEX = Path(os.environ.get('TEMP', '/tmp')) / 'starter_index_eval.json'
        if starter_help.INDEX.exists():
            starter_help.INDEX.unlink()
    if a.sync:
        print('sync:', starter_help.sync(force=True))
    items = json.loads(QUESTIONS.read_text(encoding='utf-8'))['questions']
    if a.only:
        want = set(a.only.split(','))
        items = [i for i in items if i['id'] in want]
    t0, rows, passed = time.time(), [], 0
    for it in items:
        st, text, why = group_helper.reply_for(it['q'], '' if a.path == 'auto' else a.path)
        ok, miss = score(it, st, text)
        passed += ok
        rows.append({'id': it['id'], 'q': it['q'], 'status': st, 'via': why, 'ok': ok, 'why': miss, 'answer': text})
        print(f"{'✅' if ok else '❌'} {it['id']:<9} {st:<8} {why:<6} {miss}")
        if a.verbose or not ok:
            print('   ' + text.replace('\n', '\n   ')[:900])
            if st == 'blocked':
                print(f'   [held back: {why}] ' + group_helper.LAST_BLOCKED.replace('\n', '\n   ')[:900])
    rate = passed / max(1, len(items))
    cost = _spent(t0)
    print(f'\n{passed}/{len(items)} passed ({rate:.0%}) · path={a.path} · ${cost:.3f} · {time.time() - t0:.0f}s')
    if a.out:
        Path(a.out).write_text(json.dumps({'passed': passed, 'total': len(items), 'cost': cost, 'rows': rows},
                                          ensure_ascii=False, indent=1), encoding='utf-8')
    return 0 if rate >= a.min else 1


if __name__ == '__main__':
    sys.exit(main())
