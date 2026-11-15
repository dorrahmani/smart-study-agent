"""
rag/vault_indexer.py — Index the entire vault for retrieval.

Two backends:
  - openai (preferred): text-embedding-3-small (1536-dim, $0.02/1M tokens).
    Used when OPENAI_API_KEY is set. Cost for full vault is fractions of a cent.
  - tfidf (fallback, zero deps): TF-IDF over word stems. Used when no API key
    or any error in the embedding pipeline. Works fully offline.

Index files (under DATA_DIR):
    vault_index.embed.json — OpenAI vectors {meta, chunks: [{stem, vec, ...}]}
    vault_index.tfidf.json — TF-IDF state (idf + sparse vectors)

Public API (stable; dashboard imports these):
    build_index(force=False) -> dict (stats: chunks, vocab|dim, model, cost_usd)
    search(query, k=5, course_filter=None) -> list[dict]
        course_filter: if set, only return chunks where chunk['course_key'] == course_filter
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from config.paths import DATA_DIR
from dashboard import vault_reader as vr

# ───────────────────────── shared helpers ─────────────────────────

EMBED_PATH = DATA_DIR / 'vault_index.embed.json'
TFIDF_PATH = DATA_DIR / 'vault_index.tfidf.json'

# Primary embedding backend: Gemini (per project policy: Google only for AI calls).
# Fallback: OpenAI text-embedding-3-small (if GEMINI_API_KEY missing AND OPENAI_API_KEY set).
# Final fallback: TF-IDF (zero-dep, offline).
GEMINI_EMBED_MODEL = 'gemini-embedding-001'   # 768-d, free tier, Hebrew-capable
GEMINI_EMBED_DIM   = 768
GEMINI_COST_PER_1M_TOKENS = 0.15  # USD (paid tier; free tier is generous)

EMBED_MODEL = 'text-embedding-3-small'        # legacy OpenAI fallback
EMBED_DIM   = 1536
EMBED_COST_PER_1M_TOKENS = 0.02  # USD

_WORD_RE = re.compile(r"[\u0590-\u05FF\w']+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text or '') if len(t) >= 2]


def _chunks_for_note(note: dict, chunk_chars: int = 1500) -> list[dict]:
    """Split a note body into ≤chunk_chars segments on paragraph boundaries."""
    body = (note.get('body') or '').strip()
    if not body:
        return []

    # paragraph-aware: glue paragraphs until ~chunk_chars, then break
    paras = [p.strip() for p in re.split(r'\n\s*\n', body) if p.strip()]
    chunks_text: list[str] = []
    buf = ''
    for p in paras:
        if not buf:
            buf = p
        elif len(buf) + 2 + len(p) <= chunk_chars:
            buf = buf + '\n\n' + p
        else:
            chunks_text.append(buf)
            buf = p
    if buf:
        chunks_text.append(buf)

    out = []
    for i, txt in enumerate(chunks_text):
        out.append({
            'text':       txt,
            'path':       note['path'],
            'rel_path':   note.get('rel_path', ''),
            'title':      note.get('title', ''),
            'course_key': note.get('course_key', ''),
            'type':       note.get('type', ''),
            'date':       note.get('date', ''),
            'stem':       note.get('stem', ''),
            'chunk_idx':  i,
        })
    return out


def _all_chunks() -> list[dict]:
    vr.invalidate_cache()
    notes = (vr.list_summaries() + vr.list_assignments() + vr.list_announcements()
             + getattr(vr, 'list_exams', lambda: [])())
    chunks: list[dict] = []
    for n in notes:
        chunks.extend(_chunks_for_note(n))
    return chunks


# ───────────────────────── Gemini embeddings backend (primary) ─────────────────────────

def _have_gemini() -> bool:
    return bool(os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))


def _gemini_client():
    """Lazy-import google-genai. Raises ImportError if unavailable."""
    from google import genai  # type: ignore
    key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
    return genai.Client(api_key=key)


def _embed_batch_gemini(texts: list[str], task: str = 'RETRIEVAL_DOCUMENT') -> list[list[float]]:
    """Embed a batch of texts with Gemini. Splits into sub-batches to honour
    per-request token limits and free-tier RPM.
    task: 'RETRIEVAL_DOCUMENT' for stored chunks, 'RETRIEVAL_QUERY' for queries.
    """
    from google.genai import types as genai_types  # type: ignore
    client = _gemini_client()
    out: list[list[float]] = []
    # Gemini embed batch limit is 100 per request; keep small to respect 100 RPM free.
    SUB = 50
    for i in range(0, len(texts), SUB):
        sub = texts[i:i + SUB]
        resp = client.models.embed_content(
            model=GEMINI_EMBED_MODEL,
            contents=sub,
            config=genai_types.EmbedContentConfig(
                task_type=task,
                output_dimensionality=GEMINI_EMBED_DIM,
            ),
        )
        for e in resp.embeddings:
            out.append(list(e.values))
    return out


def _build_embed_gemini(force: bool = False) -> dict[str, Any]:
    """Embed the vault with Gemini. Returns stats dict."""
    if not _have_gemini():
        raise RuntimeError('GEMINI_API_KEY / GOOGLE_API_KEY missing')
    # Staleness check
    if not force and EMBED_PATH.exists():
        try:
            idx_mtime = EMBED_PATH.stat().st_mtime
            if idx_mtime >= _vault_max_mtime():
                meta = json.loads(EMBED_PATH.read_text(encoding='utf-8')).get('meta', {})
                if meta.get('model','').startswith('gemini'):
                    return {
                        'chunks':    meta.get('n_chunks', 0),
                        'dim':       meta.get('dim', GEMINI_EMBED_DIM),
                        'model':     meta.get('model', GEMINI_EMBED_MODEL),
                        'cost_usd':  meta.get('cost_usd', 0.0),
                        'cached':    True,
                        'index_path': str(EMBED_PATH),
                    }
        except Exception:
            pass

    chunks = _all_chunks()
    if not chunks:
        raise RuntimeError('vault is empty — nothing to embed')

    # ── Incremental embedding (cost saver) ───────────────────────────────
    # Hash each chunk's text. Reuse existing vector when hash already exists
    # in the prior index. Only embed truly NEW or CHANGED chunks.
    def _chunk_hash(text: str) -> str:
        import hashlib
        return hashlib.sha1(text.encode('utf-8')).hexdigest()[:20]

    existing_by_hash: dict[str, list[float]] = {}
    if not force and EMBED_PATH.exists():
        try:
            prev = json.loads(EMBED_PATH.read_text(encoding='utf-8'))
            prev_model = (prev.get('meta') or {}).get('model', '')
            # Only reuse if same model (different models produce incompatible vectors)
            if prev_model == GEMINI_EMBED_MODEL:
                for c in prev.get('chunks', []) or []:
                    h = c.get('hash') or _chunk_hash(c.get('text', ''))
                    if h and c.get('vec'):
                        existing_by_hash[h] = c['vec']
        except Exception:
            pass

    # Decide which chunks need embedding now
    chunk_hashes = [_chunk_hash(c['text']) for c in chunks]
    to_embed_idxs: list[int] = []
    to_embed_texts: list[str] = []
    for i, (c, h) in enumerate(zip(chunks, chunk_hashes)):
        if h not in existing_by_hash:
            to_embed_idxs.append(i)
            to_embed_texts.append(c['text'])

    reused = len(chunks) - len(to_embed_idxs)
    print(f'  Incremental: {reused} cached, {len(to_embed_idxs)} new to embed '
          f'({GEMINI_EMBED_MODEL})...',
          file=sys.stderr)

    new_vectors: list[list[float]] = []
    est_tokens = 0
    if to_embed_texts:
        new_vectors = _embed_batch_gemini(to_embed_texts, task='RETRIEVAL_DOCUMENT')
        est_tokens = sum(len(t) for t in to_embed_texts) // 4
        cost_usd_new = est_tokens / 1_000_000 * GEMINI_COST_PER_1M_TOKENS
        try:
            from utils.usage_tracker import log_embed
            log_embed(n_tokens=est_tokens, model=GEMINI_EMBED_MODEL,
                      op='rag.build_index')
        except Exception: pass
    cost_usd = est_tokens / 1_000_000 * GEMINI_COST_PER_1M_TOKENS

    # Assemble final vectors list aligned with `chunks` order
    new_iter = iter(new_vectors)
    vectors: list[list[float]] = []
    for i, (c, h) in enumerate(zip(chunks, chunk_hashes)):
        if h in existing_by_hash:
            vectors.append(existing_by_hash[h])
        else:
            vectors.append(next(new_iter))

    stored = []
    for c, h, vec in zip(chunks, chunk_hashes, vectors):
        # Already-normalised vectors from cache stay as-is; new ones need norming
        norm = math.sqrt(sum(x*x for x in vec)) or 1.0
        nvec = vec if abs(norm - 1.0) < 1e-3 else [x / norm for x in vec]
        stored.append({**c, 'hash': h, 'vec': nvec})

    payload = {
        'meta': {
            'model':     GEMINI_EMBED_MODEL,
            'dim':       GEMINI_EMBED_DIM,
            'n_chunks':  len(stored),
            'tokens':    est_tokens,
            'cost_usd':  round(cost_usd, 6),
        },
        'chunks': stored,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EMBED_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return {
        'chunks':     len(stored),
        'dim':        GEMINI_EMBED_DIM,
        'model':      GEMINI_EMBED_MODEL,
        'cost_usd':   round(cost_usd, 6),
        'cached':     False,
        'index_path': str(EMBED_PATH),
    }


def _embed_query_gemini(query: str) -> list[float] | None:
    if not _have_gemini():
        return None
    try:
        vecs = _embed_batch_gemini([query], task='RETRIEVAL_QUERY')
        v = vecs[0]
        norm = math.sqrt(sum(x*x for x in v)) or 1.0
        return [x / norm for x in v]
    except Exception as e:
        print(f'  ⚠️ Gemini query embed failed: {e}', file=sys.stderr)
        return None


# ───────────────────────── OpenAI embeddings backend (fallback) ─────────────────────────

def _have_openai() -> bool:
    return bool(os.getenv('OPENAI_API_KEY'))


def _vault_max_mtime() -> float:
    """Latest mtime across all .md files we'd index. Used for staleness check."""
    from config.paths import VAULT_DIR
    try:
        return max(
            (p.stat().st_mtime for p in Path(VAULT_DIR).rglob('*.md')),
            default=0.0,
        )
    except Exception:
        return 0.0


def _build_embed(force: bool = False) -> dict[str, Any]:
    """Embed the vault with OpenAI. Returns stats dict. Raises on API errors —
    caller (build_index) catches and falls back to TF-IDF."""
    if not _have_openai():
        raise RuntimeError('OPENAI_API_KEY missing')

    # Skip if fresh
    if not force and EMBED_PATH.exists():
        try:
            idx_mtime = EMBED_PATH.stat().st_mtime
            if idx_mtime >= _vault_max_mtime():
                meta = json.loads(EMBED_PATH.read_text(encoding='utf-8'))['meta']
                return {
                    'chunks':    meta['n_chunks'],
                    'dim':       meta['dim'],
                    'model':     meta['model'],
                    'cost_usd':  meta.get('cost_usd', 0.0),
                    'cached':    True,
                    'index_path': str(EMBED_PATH),
                }
        except Exception:
            pass  # treat as stale, rebuild

    chunks = _all_chunks()
    if not chunks:
        raise RuntimeError('vault is empty — nothing to embed')

    from openai import OpenAI  # type: ignore
    client = OpenAI()

    # Batch embed (API supports up to 2048 inputs per call; 100 is safe)
    BATCH = 100
    vectors: list[list[float]] = []
    total_tokens = 0
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i + BATCH]
        resp = client.embeddings.create(
            model=EMBED_MODEL,
            input=[c['text'] for c in batch],
        )
        for d in resp.data:
            vectors.append(list(d.embedding))
        total_tokens += getattr(resp.usage, 'total_tokens', 0)
        print(f'  embedded {i + len(batch)}/{len(chunks)} chunks',
              file=sys.stderr)

    cost_usd = total_tokens / 1_000_000 * EMBED_COST_PER_1M_TOKENS

    # Strip 'text' from chunks in stored payload (keeps file smaller; we still
    # need a snippet for context, so keep first 1500 chars max — they're small)
    stored_chunks = []
    for c, vec in zip(chunks, vectors):
        stored_chunks.append({**c, 'vec': vec})

    payload = {
        'meta': {
            'model':     EMBED_MODEL,
            'dim':       EMBED_DIM,
            'n_chunks':  len(stored_chunks),
            'tokens':    total_tokens,
            'cost_usd':  round(cost_usd, 6),
        },
        'chunks': stored_chunks,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EMBED_PATH.write_text(json.dumps(payload, ensure_ascii=False),
                          encoding='utf-8')
    return {
        'chunks':     len(stored_chunks),
        'dim':        EMBED_DIM,
        'model':      EMBED_MODEL,
        'cost_usd':   round(cost_usd, 6),
        'cached':     False,
        'index_path': str(EMBED_PATH),
    }


def _load_embed_state() -> dict | None:
    if not EMBED_PATH.exists():
        return None
    try:
        return json.loads(EMBED_PATH.read_text(encoding='utf-8'))
    except Exception:
        return None


def _embed_query(query: str) -> list[float] | None:
    """Embed a single query. Returns None on error."""
    if not _have_openai():
        return None
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI()
        resp = client.embeddings.create(model=EMBED_MODEL, input=[query])
        return list(resp.data[0].embedding)
    except Exception as e:
        print(f'  ⚠️ query embed failed: {e}', file=sys.stderr)
        return None


def _cos_dense(a: list[float], b: list[float]) -> float:
    s = 0.0
    for x, y in zip(a, b):
        s += x * y
    return s  # both are L2-normalized by OpenAI already


def _search_embed(query: str, k: int = 5,
                  course_filter: str | None = None) -> list[dict] | None:
    """Search using embeddings. Returns None if backend unavailable.

    course_filter: if set, only chunks whose course_key matches are returned.
    Embeds query with the SAME backend that built the index (otherwise vectors
    aren't comparable). Detects this via meta.model in the index file.
    """
    state = _load_embed_state()
    if not state:
        return None
    idx_model = (state.get('meta') or {}).get('model', '').lower()
    if idx_model.startswith('gemini'):
        qv = _embed_query_gemini(query)
    else:
        qv = _embed_query(query)
    if qv is None:
        return None

    chunks = state['chunks']
    scored: list[tuple[float, int]] = []
    for idx, c in enumerate(chunks):
        if course_filter and c.get('course_key') != course_filter:
            continue
        s = _cos_dense(qv, c['vec'])
        scored.append((s, idx))
    scored.sort(reverse=True)

    out, seen = [], set()
    for s, idx in scored:
        c = chunks[idx]
        if c['path'] in seen:
            continue
        seen.add(c['path'])
        out.append({
            'text':       c['text'],
            'path':       c['path'],
            'rel_path':   c.get('rel_path', ''),
            'title':      c.get('title', ''),
            'course_key': c.get('course_key', ''),
            'type':       c.get('type', ''),
            'date':       c.get('date', ''),
            'stem':       c.get('stem', ''),
            'chunk_idx':  c.get('chunk_idx', 0),
            'score':      round(s, 4),
        })
        if len(out) >= k:
            break
    return out


# ───────────────────────── TF-IDF fallback backend ─────────────────────────

def _build_tfidf() -> dict[str, Any]:
    chunks = _all_chunks()
    df: Counter[str] = Counter()
    tokenized = []
    for c in chunks:
        toks = _tokenize(c['text'])
        tokenized.append(toks)
        df.update(set(toks))
    N = max(1, len(chunks))
    idf = {w: math.log((N + 1) / (cnt + 1)) + 1.0 for w, cnt in df.items()}

    vectors = []
    for toks in tokenized:
        if not toks:
            vectors.append({})
            continue
        tf = Counter(toks)
        L = len(toks)
        v = {w: (cnt / L) * idf.get(w, 0.0) for w, cnt in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        v = {w: x / norm for w, x in v.items()}
        vectors.append(v)

    state = {'idf': idf, 'chunks': chunks, 'vectors': vectors}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TFIDF_PATH.write_text(json.dumps(state, ensure_ascii=False),
                          encoding='utf-8')
    return {
        'chunks':     len(chunks),
        'vocab':      len(idf),
        'model':      'tfidf',
        'cost_usd':   0.0,
        'index_path': str(TFIDF_PATH),
    }


def _load_tfidf_state() -> dict | None:
    if not TFIDF_PATH.exists():
        return None
    try:
        return json.loads(TFIDF_PATH.read_text(encoding='utf-8'))
    except Exception:
        return None


def _query_vec(query: str, idf: dict[str, float]) -> dict[str, float]:
    toks = _tokenize(query)
    if not toks:
        return {}
    tf = Counter(toks)
    L = len(toks)
    v = {w: (cnt / L) * idf.get(w, 0.0) for w, cnt in tf.items() if w in idf}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {w: x / norm for w, x in v.items()}


def _cosine_sparse(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(va * b.get(k, 0.0) for k, va in a.items())


def _search_tfidf(query: str, k: int = 5,
                  course_filter: str | None = None) -> list[dict]:
    state = _load_tfidf_state()
    if not state:
        _build_tfidf()
        state = _load_tfidf_state()
    if not state:
        return []
    qv = _query_vec(query, state['idf'])
    if not qv:
        return []
    scores = []
    for idx, dv in enumerate(state['vectors']):
        if course_filter:
            c = state['chunks'][idx]
            if c.get('course_key') != course_filter:
                continue
        s = _cosine_sparse(qv, dv)
        if s > 0:
            scores.append((s, idx))
    scores.sort(reverse=True)
    out, seen = [], set()
    for s, idx in scores:
        c = state['chunks'][idx]
        if c['path'] in seen:
            continue
        seen.add(c['path'])
        out.append({**c, 'score': round(s, 4)})
        if len(out) >= k:
            break
    return out


# ───────────────────────── public API ─────────────────────────

def build_index(force: bool = False) -> dict[str, Any]:
    """Build the index. Try Gemini first (project policy), OpenAI second,
    TF-IDF as final fallback. Each backend handles its own staleness check."""
    if _have_gemini():
        try:
            return _build_embed_gemini(force=force)
        except Exception as e:
            print(f'  ⚠️ Gemini embedding failed, falling back: {e}',
                  file=sys.stderr)
    if _have_openai():
        try:
            return _build_embed(force=force)
        except Exception as e:
            print(f'  ⚠️ OpenAI embedding failed, falling back to TF-IDF: {e}',
                  file=sys.stderr)
    return _build_tfidf()


def _hybrid_merge(embed_hits: list[dict], tfidf_hits: list[dict],
                  k: int = 5) -> list[dict]:
    """Reciprocal Rank Fusion (RRF): merges two ranked lists.
    A chunk that appears in both lists scores higher than one in only one.
    Formula: score = sum( 1 / (rank + 60) ) across ranking lists.
    This is the standard hybrid-search fusion used at Pinecone, Vespa, Elastic.
    """
    K_RRF = 60
    scores: dict[str, float] = {}
    by_id: dict[str, dict] = {}
    for hits in (embed_hits, tfidf_hits):
        for rank, h in enumerate(hits or []):
            # Identity = path + chunk_idx (chunks are unique per (path, idx))
            cid = f"{h.get('path','')}#{h.get('chunk_idx',0)}"
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (K_RRF + rank + 1)
            if cid not in by_id:
                by_id[cid] = h
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    out: list[dict] = []
    seen_paths: set[str] = set()
    for cid, s in ranked:
        h = dict(by_id[cid])
        if h.get('path') in seen_paths:
            continue
        seen_paths.add(h['path'])
        h['rrf_score'] = round(s, 5)
        out.append(h)
        if len(out) >= k:
            break
    return out


def search(query: str, k: int = 5,
           course_filter: str | None = None,
           hybrid: bool = True) -> list[dict[str, Any]]:
    """Retrieve top-k chunks. Hybrid by default (embed + TF-IDF via RRF).

    Args:
        query: user question.
        k: number of results to return.
        course_filter: optional course_key. If set, only matching chunks returned.
        hybrid: if True (default), merges embed + tfidf via reciprocal rank fusion.
                If False, falls back to embed-only (then tfidf) — old behaviour.
    """
    have_embed_api = _have_gemini() or _have_openai()
    if hybrid:
        # Get more from each side, then RRF-merge to top-k
        K_INNER = max(k * 2, 8)
        embed_hits: list[dict] = []
        if have_embed_api and EMBED_PATH.exists():
            embed_hits = _search_embed(query, k=K_INNER,
                                       course_filter=course_filter) or []
        tfidf_hits = _search_tfidf(query, k=K_INNER,
                                   course_filter=course_filter) or []
        if embed_hits and tfidf_hits:
            return _hybrid_merge(embed_hits, tfidf_hits, k=k)
        # If only one side has hits, return it as-is (truncated to k)
        return (embed_hits or tfidf_hits)[:k]

    # Legacy path (no hybrid)
    if have_embed_api and EMBED_PATH.exists():
        hits = _search_embed(query, k=k, course_filter=course_filter)
        if hits:
            return hits
    return _search_tfidf(query, k=k, course_filter=course_filter)


if __name__ == '__main__':
    if '--build' in sys.argv:
        print(json.dumps(build_index(force='--force' in sys.argv),
                         ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1:
        for r in search(' '.join(sys.argv[1:]), k=5):
            print(f"{r['score']}  [{r['course_key']}] {r['title']}  -> {r['stem']}")
