"""utils/usage_tracker.py — Single-file usage + cost ledger.

Append-only JSONL at data/usage.jsonl. One line per API call.

Schema:
    {ts, service, model, input_tokens, output_tokens, audio_seconds, cost_usd, op[, cached_tokens][, extra]}
    cached_tokens (18/11): the part of input_tokens Gemini served from its implicit context cache (usage_metadata
    .cached_content_token_count). They are billed at CACHED_SHARE of the input price; extra.thoughts = thinking tokens
    (already inside output_tokens).

Public API:
    log_usage(service, model, **kw)  — record one call
    log_gemini(resp, model, op='')   — extract usage_metadata from a Gemini resp
    log_whisper(audio_seconds, op='', model='whisper-1') — per-minute transcription pricing
    log_embed(n_tokens, model, op='')— embeddings pricing
    summary(since='30d')             — dict: total, by_service, by_model
    summary_text(since='30d')        — Hebrew markdown formatted summary
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / 'data' / 'usage.jsonl'

# Pricing (USD per 1M tokens unless noted) — keep in sync with vendor pages
PRICING: dict[str, dict[str, float]] = {
    'gemini-2.5-flash':         {'in':  0.30, 'out':  2.50},
    'gemini-2.5-flash-lite':    {'in':  0.10, 'out':  0.40},  # 5-6x cheaper than 2.5-flash
    'gemini-2.5-pro':           {'in':  1.25, 'out': 10.00},
    # Calibrated 10/2026 against the real Google bill (the ledger was 3-5x low):
    # erring HIGH on purpose — this feeds the budget lock.
    'gemini-3.1-pro-preview':   {'in':  2.00, 'out': 12.00},
    'gemini-3-pro-preview':     {'in':  2.00, 'out': 12.00},
    'gemini-3-flash-preview':   {'in':  1.50, 'out':  9.00},   # billed as "3.5 flash" on the invoice
    'gemini-3.5-flash':         {'in':  1.50, 'out':  9.00},
    'gemini-3.8-flash':         {'in':  1.50, 'out':  7.50},   # list price; promo 0.75/3.75 until 31.12.2026 (err high)
    'gemini-3.1-flash-lite':    {'in':  0.25, 'out':  1.50},
    'gemini-3.1-flash-lite-preview': {'in': 0.25, 'out': 1.50},
    'gemini-2.5-flash-image-preview': {'in': 0.30, 'out': 30.00},  # output very expensive
    # Embeddings (only input counted)
    'gemini-embedding-001':     {'in':  0.15, 'out':  0.00},
    'text-embedding-3-small':   {'in':  0.02, 'out':  0.00},
    'text-embedding-004':       {'in':  0.13, 'out':  0.00},
    # Transcription, charged per minute of audio
    'whisper-1':                {'audio_per_minute': 0.006},
    'gpt-4o-transcribe':        {'audio_per_minute': 0.006},
    'gpt-4o-mini-transcribe':   {'audio_per_minute': 0.003},
    # 19/11 lectures transcribed on the VM's own CPU (faster-whisper + ivrit-ai) — logged for the minutes, costs nothing
    'local-ivrit-whisper':      {'audio_per_minute': 0.0},
}


# 18/11 implicit context caching: Google bills a cached input token at 10% of the input price on Gemini 3 (25% on 2.5).
# The ledger feeds the budget lock and errs high on purpose, so it charges 25% everywhere.
CACHED_SHARE = 0.25


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec='seconds') + 'Z'


def _estimate_cost(model: str, input_tokens: int = 0,
                   output_tokens: int = 0,
                   audio_seconds: float = 0,
                   cached_tokens: int = 0) -> float:
    p = PRICING.get(model)
    if not p:
        # Unknown model: never log $0 (that silently starved the budget lock).
        # Charge it like the most expensive text model we use.
        p = PRICING['gemini-3.1-pro-preview'] if 'whisper' not in model and 'transcribe' not in model             else {'audio_per_minute': 0.006}
    cost = 0.0
    if input_tokens and 'in' in p:
        cached = max(0, min(int(cached_tokens or 0), int(input_tokens)))
        cost += ((input_tokens - cached) + cached * CACHED_SHARE) / 1_000_000 * p['in']
    if output_tokens and 'out' in p:
        cost += (output_tokens / 1_000_000) * p['out']
    if audio_seconds and 'audio_per_minute' in p:
        cost += (audio_seconds / 60) * p['audio_per_minute']
    return round(cost, 6)


def log_usage(*, service: str, model: str,
              input_tokens: int = 0,
              output_tokens: int = 0,
              audio_seconds: float = 0.0,
              op: str = '',
              extra: dict | None = None,
              cached_tokens: int = 0) -> dict:
    """Append one usage record. Returns the recorded dict."""
    rec = {
        'ts':            _now_iso(),
        'service':       service,
        'model':         model,
        'op':            op,
        'input_tokens':  int(input_tokens or 0),
        'output_tokens': int(output_tokens or 0),
        'audio_seconds': round(float(audio_seconds or 0.0), 1),
        'cost_usd':      _estimate_cost(model, input_tokens, output_tokens, audio_seconds, cached_tokens),
    }
    if cached_tokens:
        rec['cached_tokens'] = int(cached_tokens)
    if extra:
        rec['extra'] = extra
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass  # never block the caller on logging failure
    return rec


def log_gemini(resp: Any, model: str, op: str = '') -> dict | None:
    """Extract usage from a google-genai response object and log it.
    Returns the record or None if usage metadata is unavailable."""
    try:
        um = getattr(resp, 'usage_metadata', None)
        if um is None:
            return None
        in_t = getattr(um, 'prompt_token_count', 0) or 0
        # thinking models bill their "thoughts" as output — ignoring them was a
        # big part of why the ledger read 3-5x lower than the Google invoice
        thoughts = getattr(um, 'thoughts_token_count', 0) or 0
        out_t = (getattr(um, 'candidates_token_count', 0) or 0) + thoughts
        cached = getattr(um, 'cached_content_token_count', 0) or 0
        return log_usage(service='gemini', model=model, input_tokens=in_t, output_tokens=out_t, op=op,
                         cached_tokens=cached if isinstance(cached, int) else 0,
                         extra={'thoughts': thoughts} if isinstance(thoughts, int) and thoughts else None)
    except Exception:
        return None


def log_whisper(audio_seconds: float, op: str = '', model: str = 'whisper-1') -> dict:
    """13/11: the model that actually transcribed. Every chunk was logged as 'whisper-1' — gpt-4o-transcribe (the
    primary) read as 13 fallbacks in the ledger, though none had happened."""
    return log_usage(service='openai', model=model or 'whisper-1',
                     audio_seconds=audio_seconds, op=op)


def log_embed(n_tokens: int, model: str = 'gemini-embedding-001',
              op: str = '') -> dict:
    return log_usage(service='gemini' if model.startswith('gemini') else 'openai',
                     model=model, input_tokens=n_tokens, op=op)


# ─── Summary / reporting ────────────────────────────────────────────────

def _parse_since(since: str) -> datetime:
    """Accept '7d', '30d', '24h', 'month', or ISO date."""
    now = datetime.utcnow()
    s = since.strip().lower()
    if s == 'month':
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if s == 'today':
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if s.endswith('d'):
        try: return now - timedelta(days=int(s[:-1]))
        except ValueError: pass
    if s.endswith('h'):
        try: return now - timedelta(hours=int(s[:-1]))
        except ValueError: pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return now - timedelta(days=30)


def _iter_records(since: datetime):
    if not LEDGER_PATH.exists():
        return
    with open(LEDGER_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            try:
                ts = datetime.fromisoformat(r['ts'].replace('Z', ''))
                if ts < since: continue
            except Exception:
                pass
            yield r


def summary(since: str = '30d') -> dict:
    """Aggregate usage since the given window."""
    since_dt = _parse_since(since)
    by_model: dict[str, dict] = {}
    by_service: dict[str, float] = {}
    by_op: dict[str, float] = {}
    total = 0.0
    n_calls = 0
    audio_total = 0.0
    for r in _iter_records(since_dt):
        n_calls += 1
        c = float(r.get('cost_usd', 0))
        total += c
        m = r.get('model', '?')
        s = r.get('service', '?')
        o = r.get('op', '') or '(general)'
        if m not in by_model:
            by_model[m] = {'n_calls': 0, 'cost_usd': 0.0,
                           'input_tokens': 0, 'output_tokens': 0,
                           'audio_seconds': 0.0}
        d = by_model[m]
        d['n_calls'] += 1
        d['cost_usd'] += c
        d['input_tokens']  += int(r.get('input_tokens', 0))
        d['output_tokens'] += int(r.get('output_tokens', 0))
        d['audio_seconds'] += float(r.get('audio_seconds', 0))
        by_service[s] = by_service.get(s, 0) + c
        by_op[o] = by_op.get(o, 0) + c
        audio_total += float(r.get('audio_seconds', 0))
    # round
    for m in by_model:
        by_model[m]['cost_usd'] = round(by_model[m]['cost_usd'], 4)
        by_model[m]['audio_seconds'] = round(by_model[m]['audio_seconds'], 1)
    return {
        'since': since,
        'since_iso': since_dt.isoformat(),
        'n_calls': n_calls,
        'total_usd': round(total, 4),
        'audio_minutes_total': round(audio_total / 60, 1),
        'by_model':   dict(sorted(by_model.items(), key=lambda x: -x[1]['cost_usd'])),
        'by_service': {k: round(v, 4) for k, v in
                       sorted(by_service.items(), key=lambda x: -x[1])},
        'by_op':      {k: round(v, 4) for k, v in
                       sorted(by_op.items(), key=lambda x: -x[1])[:10]},
    }


def summary_text(since: str = '30d') -> str:
    """Hebrew markdown summary for Telegram."""
    s = summary(since)
    label = {'30d':'30 ימים','7d':'שבוע','24h':'יממה','month':'חודש',
             'today':'היום'}.get(since, since)
    lines = [
        f'💰 *סיכום הוצאות — {label}*',
        '',
        f'סה"כ: *${s["total_usd"]}*  |  קריאות: {s["n_calls"]}',
    ]
    if s['audio_minutes_total'] > 0:
        lines.append(f'דקות תמלול: {s["audio_minutes_total"]}')
    lines.append('')
    if s['by_model']:
        lines.append('*לפי מודל:*')
        for m, d in list(s['by_model'].items())[:6]:
            lines.append(f'  • `{m}`  ${d["cost_usd"]}  ({d["n_calls"]} קריאות)')
    if s['by_op']:
        lines.append('')
        lines.append('*לפי פעולה:*')
        for op, c in list(s['by_op'].items())[:6]:
            lines.append(f'  • {op}  ${c}')
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    period = sys.argv[1] if len(sys.argv) > 1 else '30d'
    print(summary_text(period))
