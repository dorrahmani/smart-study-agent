"""
utils/latency.py — how long the owner waits: per-turn latency, and a small per-day summary for the dashboard (18/11).

    with latency.turn('owner', 'whatsapp') as t:     # whatsapp/worker.py, around core.handle
        outs = handle(msg)
    → one row in data/latency.jsonl: {ts, role, channel, total, llm, tool, calls, path}
      total = handle() wall time; llm = wall time inside generate_content (API or subscription), summed over the
      turn's calls; tool = the agent loop's tool time (agent/mind._run_all), 0 when the loop didn't run; path = 'mind'
      when the loop ran, else 'router'.
    → data/latency_summary.json, rewritten after each row (last DAYS days):
      {"updated": iso, "days": {"YYYY-MM-DD": {"n": int, "total": {"p50": s, "p95": s}, "llm": {...}, "tool": {...}}},
       "all": {same shape, over the whole window}}

Never raises into a turn: timing is a side line. No LLM, no network.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / 'data' / 'latency.jsonl'
SUMMARY = ROOT / 'data' / 'latency_summary.json'
DAYS = 14
ACC: ContextVar[dict | None] = ContextVar('ssa_latency', default=None)
_LOCK = threading.Lock()


def add(key: str, seconds: float) -> None:
    """Into the current turn's sums (a thread started with copy_context() shares the turn's dict)."""
    acc = ACC.get()
    if acc is not None:
        with _LOCK:
            acc[key] = acc.get(key, 0.0) + max(0.0, float(seconds))
            if key == 'llm':
                acc['calls'] = acc.get('calls', 0) + 1


def mark(key: str, value=True) -> None:
    acc = ACC.get()
    if acc is not None:
        acc[key] = value


def install_timer() -> None:
    """Once per process, OUTERMOST (after budget.install_guard and gemini_sub.install): every generate_content call
    in a turn, subscription or API, counts as LLM time."""
    from google.genai import models as _models
    original = _models.Models.generate_content
    if getattr(original, '_ssa_timed', False):
        return

    def timed(self, *a, **kw):
        t0 = time.monotonic()
        try:
            return original(self, *a, **kw)
        finally:
            add('llm', time.monotonic() - t0)
    timed._ssa_timed = True
    for k in ('_ssa_budget', '_ssa_sub'):            # the wrappers below still answer for themselves
        if getattr(original, k, False):
            setattr(timed, k, True)
    _models.Models.generate_content = timed


@contextmanager
def turn(role: str, channel: str = 'whatsapp', log: Path | None = None, summary: Path | None = None):
    acc: dict = {}
    tok = ACC.set(acc)
    t0 = time.monotonic()
    try:
        yield acc
    finally:
        ACC.reset(tok)
        try:
            record({'ts': datetime.now().isoformat(timespec='seconds'), 'role': role or '', 'channel': channel or '',
                    'total': round(time.monotonic() - t0, 2), 'llm': round(acc.get('llm', 0.0), 2),
                    'tool': round(acc.get('tool', 0.0), 2), 'calls': int(acc.get('calls', 0)),
                    'path': acc.get('path') or ('mind' if acc.get('mind') else 'router')}, log, summary)
        except Exception as e:
            print(f'  ⚠️ latency: {e!r}'[:160])


def record(row: dict, log: Path | None = None, summary: Path | None = None) -> None:
    p = log or LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')
    write_summary(p, summary or SUMMARY)


def _pct(xs: list[float], q: float) -> float:
    """Nearest-rank percentile."""
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = max(0, min(len(xs) - 1, int(-(-q * len(xs) // 1)) - 1))
    return round(xs[k], 2)


def _stats(rows: list[dict]) -> dict:
    out = {'n': len(rows)}
    for key in ('total', 'llm', 'tool'):
        xs = [float(r.get(key) or 0) for r in rows]
        out[key] = {'p50': _pct(xs, 0.5), 'p95': _pct(xs, 0.95)}
    out['mind'] = sum(1 for r in rows if r.get('path') == 'mind')
    return out


def write_summary(log: Path | None = None, out: Path | None = None, now: datetime | None = None) -> dict:
    """Owner turns of the last DAYS days → p50/p95 per day. Read from the end of the log only."""
    p, now = log or LOG, now or datetime.now()
    since = (now - timedelta(days=DAYS)).isoformat(timespec='seconds')
    rows = []
    try:
        with open(p, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2_000_000))
            for ln in f.read().decode('utf-8', 'ignore').splitlines():
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and str(r.get('ts', '')) >= since and r.get('role') == 'owner':
                    rows.append(r)
    except OSError:
        pass
    days: dict[str, list] = {}
    for r in rows:
        days.setdefault(str(r['ts'])[:10], []).append(r)
    d = {'updated': now.isoformat(timespec='seconds'), 'window_days': DAYS, 'unit': 'seconds',
         'days': {k: _stats(v) for k, v in sorted(days.items())}, 'all': _stats(rows)}
    q = out or SUMMARY
    q.parent.mkdir(parents=True, exist_ok=True)
    tmp = q.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, q)
    return d
