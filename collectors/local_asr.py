"""collectors/local_asr.py — lecture transcription on the VM's own CPU, $0 (19/11).

The paid path (OpenAI gpt-4o-transcribe → whisper-1) cost ~$16/month. The Oracle VM (4 ARM cores, 24 GB, no GPU) sits
idle at night, so a lecture is transcribed there first with faster-whisper + ivrit-ai's Hebrew fine-tune
(ivrit-ai/whisper-large-v3-turbo-ct2, int8), which ran at RTF ~0.46 on the bench: a 2-hour lecture in ~1 hour.

Rules:
- the job runs in its own venv (~/venv_asr) as a subprocess under `nice -n 19 ionice -c3`: the WhatsApp bot always gets
  the CPU first, and the job's oom_score_adj is raised so an OOM kills it, never the bot;
- one job at a time (a file lock = the queue; a second lecture waits its turn);
- a hard time budget per lecture; a timeout / crash / transcript that fails the checks → None, and the caller falls
  back to the paid API (which still obeys the budget lock).
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASR_PYTHON = Path(os.getenv('SSA_ASR_PYTHON', '/home/ubuntu/venv_asr/bin/python'))
ASR_MODEL = Path(os.getenv('SSA_ASR_MODEL', '/home/ubuntu/asr_models/ivrit-turbo-ct2'))
WORKER = ROOT / 'scripts' / 'local_asr_worker.py'
LOCK_PATH = ROOT / 'data' / 'local_asr.lock'
NICE = ['nice', '-n', '19', 'ionice', '-c', '3']
MODEL_NAME = 'local-ivrit-whisper'

MAX_BUDGET_S = 4 * 3600          # never more than 4 h on one lecture
MIN_BUDGET_S = 30 * 60
BUDGET_PER_AUDIO_S = 1.2         # bench RTF ~0.46 → 1.2x the audio length is a generous ceiling

MIN_CHARS = 1000                 # same floor as a reusable paid transcript
MIN_HEBREW = 0.5
MIN_WORDS_PER_MIN = 12           # the worst real tutorial (bad mic) still had ~35 w/min
MAX_LOOP_SHARE = 0.15            # hallucination loops ("בלודי, בלודי, …") above this share of the words → reject

_LINE = re.compile(r'^\[(\d{2}):(\d{2}):(\d{2})\] (.*)$')


def enabled() -> bool:
    """Local transcription is set up on this machine (VM) and not switched off (SSA_LOCAL_ASR=0)."""
    if os.getenv('SSA_LOCAL_ASR', '1') == '0' or os.name == 'nt':
        return False
    return ASR_PYTHON.exists() and (ASR_MODEL / 'model.bin').exists() and WORKER.exists()


def time_budget(duration_s: float | None) -> float:
    if not duration_s or duration_s <= 0:
        return MAX_BUDGET_S
    return max(MIN_BUDGET_S, min(MAX_BUDGET_S, duration_s * BUDGET_PER_AUDIO_S + 600))


def _collapse_loops(text: str) -> tuple[str, float]:
    """Cut a word repeated 4+ times in a row down to one; returns (text, share of the words that were loop)."""
    total = len(text.split()) or 1
    removed = 0

    def _cut(m):
        nonlocal removed
        removed += len(m.group(0).split()) - 1
        return m.group(1)
    text = re.sub(r'(?<!\S)(\S+?)(?:[,.]?[ \t]+\1(?=[\s,.]|$)){3,}[,.]?', _cut, text)
    return text, removed / total


def check_transcript(text: str, duration_s: float | None = None) -> tuple[str | None, str]:
    """Same bar as srt_is_good_hebrew (length + Hebrew share) plus coverage and loop checks.
    Returns (cleaned text, '') when usable, else (None, reason)."""
    lines = [ln for ln in (text or '').splitlines() if _LINE.match(ln)]
    if not lines:
        return None, 'no timestamped lines'
    cleaned, loop_share = _collapse_loops('\n'.join(lines))
    if loop_share > MAX_LOOP_SHARE:
        return None, f'hallucination loops ({loop_share:.0%} of words)'
    if len(cleaned) < MIN_CHARS:
        return None, f'too short ({len(cleaned)} chars)'
    body = ''.join(_LINE.match(ln).group(4) for ln in cleaned.splitlines() if _LINE.match(ln))
    heb = sum('א' <= c <= 'ת' for c in body)
    alpha = sum(c.isalpha() for c in body) or 1
    if heb / alpha < MIN_HEBREW:
        return None, f'not Hebrew ({heb / alpha:.0%})'
    if duration_s and duration_s > 600:
        wpm = len(body.split()) / (duration_s / 60)
        if wpm < MIN_WORDS_PER_MIN:
            return None, f'too little speech ({wpm:.0f} words/min)'
    return cleaned, ''


def _acquire(lock_file, deadline: float) -> bool:
    try:
        import fcntl
    except ImportError:          # Windows dev box: local ASR never runs there (enabled() is False)
        return True
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.time() > deadline:
                return False
            time.sleep(15)


def _duration(media_path: str) -> float:
    try:
        from collectors.whisper_transcriber import get_duration
        return get_duration(media_path)
    except Exception:
        return 0.0


def transcribe(media_path: str, _runner=subprocess.run) -> str | None:
    """Transcribe on the VM CPU. Returns the '[HH:MM:SS] text' transcript, or None → the caller falls back to the API."""
    if not enabled():
        return None
    duration = _duration(media_path)
    budget = time_budget(duration)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = LOCK_PATH.parent / 'tmp_audio' / f'local_asr_{os.getpid()}.txt'
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, 'a+') as lock_file:
        if not _acquire(lock_file, time.time() + budget):           # the queue: one lecture at a time
            print('  ⚠️ local ASR: another lecture still running — falling back', flush=True)
            return None
        t0 = time.time()
        print(f'🖥️  תמלול מקומי (ivrit-ai, nice 19) — {duration / 60:.0f} דק\', תקציב {budget / 60:.0f} דק\'', flush=True)
        cmd = [*NICE, str(ASR_PYTHON), str(WORKER), str(media_path), str(out), '--model', str(ASR_MODEL)]
        try:
            r = _runner(cmd, timeout=budget, cwd=str(ROOT))
        except subprocess.TimeoutExpired:
            print(f'  ⚠️ local ASR: over the {budget / 60:.0f}-min budget — falling back', flush=True)
            return None
        except Exception as e:
            print(f'  ⚠️ local ASR failed to start: {e!r}'[:200], flush=True)
            return None
        took = time.time() - t0
    try:
        if getattr(r, 'returncode', 1) != 0 or not out.exists():
            print(f'  ⚠️ local ASR exit {getattr(r, "returncode", "?")} — falling back', flush=True)
            return None
        text, why = check_transcript(out.read_text(encoding='utf-8'), duration)
        if not text:
            print(f'  ⚠️ local ASR transcript rejected: {why} — falling back', flush=True)
            return None
    finally:
        try:
            out.unlink()
        except OSError:
            pass
    try:
        from utils.usage_tracker import log_usage
        log_usage(service='local', model=MODEL_NAME, audio_seconds=duration, op='transcribe.local',
                  extra={'wall_s': round(took), 'rtf': round(took / duration, 3) if duration else None})
    except Exception:
        pass
    print(f'✅ תמלול מקומי: {len(text)} תווים, {took / 60:.0f} דק\' ($0)', flush=True)
    return text
