"""scripts/local_asr_worker.py — one lecture → Hebrew transcript on the VM's own CPU ($0).

Runs inside ~/venv_asr (faster-whisper + CTranslate2, separate from the main venv), always started by
collectors/local_asr.py under `nice -n 19 ionice -c3` with a hard timeout. Never call it directly from the bot.

    venv_asr/bin/python scripts/local_asr_worker.py <media> <out.txt> --model <ct2 dir>

Writes '[HH:MM:SS] text' lines (real segment timestamps) to <out.txt> only when done (tmp + rename).
Exit codes: 0 ok · 2 decode failed · 3 not Hebrew audio · 4 transcription error.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

EXIT_DECODE, EXIT_NOT_HEBREW, EXIT_ASR = 2, 3, 4
SR = 16000


def _ts(sec: float) -> str:
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f'{h:02d}:{m:02d}:{s:02d}'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('media')
    ap.add_argument('out')
    ap.add_argument('--model', required=True)
    ap.add_argument('--threads', type=int, default=4)
    ap.add_argument('--beam', type=int, default=5)
    a = ap.parse_args()
    try:   # if memory ever runs out, the kernel kills this job — never the WhatsApp bot
        Path('/proc/self/oom_score_adj').write_text('800')
    except OSError:
        pass

    # decode first (cheap, fails fast on a bad file) — ffmpeg → 16 kHz mono PCM; PyAV in faster-whisper is skipped
    t0 = time.time()
    try:
        raw = subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-i', a.media,
                              '-f', 's16le', '-ac', '1', '-ar', str(SR), '-'],
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        print(f'decode failed: {str(e)[:200]}', flush=True)
        return EXIT_DECODE
    import numpy as np
    audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    del raw
    dur = len(audio) / SR
    if dur < 30:
        print(f'audio too short: {dur:.0f}s', flush=True)
        return EXIT_DECODE

    from faster_whisper import WhisperModel
    model = WhisperModel(a.model, device='cpu', compute_type='int8', cpu_threads=a.threads)
    lang, prob, _ = model.detect_language(audio[: SR * 60])
    print(f'audio {dur / 60:.1f} min · language {lang} ({prob:.2f})', flush=True)
    if lang != 'he' and prob >= 0.6:
        return EXIT_NOT_HEBREW            # an English lecture → the caller falls back to the API (auto language)

    out, tmp = Path(a.out), Path(a.out + '.part')
    n, last = 0, time.time()
    try:
        segs, _info = model.transcribe(audio, language='he', beam_size=a.beam, vad_filter=True,
                                       condition_on_previous_text=False)
        with open(tmp, 'w', encoding='utf-8') as f:
            for s in segs:
                text = s.text.strip()
                if text:
                    f.write(f'[{_ts(s.start)}] {text}\n')
                    n += 1
                if time.time() - last > 300:
                    f.flush()
                    last = time.time()
                    print(f'  {_ts(s.end)} / {_ts(dur)} · {n} lines · {(time.time() - t0) / 60:.0f} min', flush=True)
    except Exception as e:
        print(f'transcription error: {e!r}'[:300], flush=True)
        return EXIT_ASR
    os.replace(tmp, out)
    took = time.time() - t0
    print(f'done: {n} lines · {took / 60:.1f} min · RTF {took / dur:.2f}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
